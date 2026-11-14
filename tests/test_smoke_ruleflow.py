"""Smoke: router.try_intents mit Regel-Intents, standalone + master(Mock-HTTP)."""
import json, os, sys, tempfile, threading
os.environ["VS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="vsmoke_")
os.environ["MASTER_URL"] = ""
os.environ["SATELLITE_MAC"] = "aa:bb:cc:dd:ee:01"
os.environ["CHAT_DIR"] = tempfile.mkdtemp(prefix="vschat_")
ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(ROOT))

from app import router, schedule as sched, config as cfg  # noqa: E402
from app.ruleintents import SatCtx  # noqa: E402

fails = []
def ok(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (" | " + str(extra)[:120] if extra else ""))
    if not cond:
        fails.append(name)

# ── standalone ──
cfg.MASTER_URL = ""
cfg.DATA_DIR = tempfile.mkdtemp(prefix="vsdata_")
sched._store = None
r = router.try_intents("Stelle einen Timer auf 10 Minuten")
ok("standalone timer", r and "10 Minute" in r, r)
r = router.try_intents("Stelle Wecker auf 07:15")
ok("standalone alarm", r and "07:15" in r, r)
r = router.try_intents("Erinnere mich morgen um 7:30 an die Abrechnung")
ok("standalone reminder", r and "Erinnerung" in r, r)
items = sched.get_store().list()
kinds = sorted(i["kind"] for i in items)
ok("standalone store (3 items)", kinds == ["alarm", "reminder", "timer"], kinds)
r = router.try_intents("Sende eine Nachricht an antje: Essen ist fertig")
ok("standalone message abgelehnt", r and "Master-Modus" in r, r)
r = router.try_intents("Rufe antje")
ok("standalone call abgelehnt", r and "Master-Modus" in r, r)
r = router.try_intents("Zeige Kamera garten")
ok("standalone camera unbekannt -> bestehender Kamera-Pfad",
   r is not None and "amera" in r, r)

# ── master-Modus gegen lokalem Mock ──
hits = []
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(ln) or b"{}")
        hits.append((self.path, body))
        out = {"ok": True, "answer": None}
        if "/schedule" in self.path and not self.path.endswith("/delete"):
            out["answer"] = "am Master gespeichert"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())
    def log_message(self, *a): pass

srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
cfg.MASTER_URL = f"http://127.0.0.1:{port}"
ctx = SatCtx()
ok("master mode erkannt", ctx.mode == "master", ctx.mode)

r = router.try_intents("Stelle einen Timer auf 30 Sekunden")
ok("master timer", r and "Master" in (r or ""), r)
r = router.try_intents("Sende eine Nachricht an antje: Essen ist fertig")
ok("master message", r and "antje" in r, r)
r = router.try_intents("Sende Sprachnachricht an flur: Das Essen wartet")
ok("master voice message", r and "antw" in r or (r and "Spra" in r), r)
r = router.try_intents("Rufe antje")
ok("master call", r and "10" in r, r)
# Konfigurierte Kamera: Snapshot vom Stub-Server (GET /snap.jpg)
from PIL import Image as _PILImage  # noqa: E402
import io as _io  # noqa: E402
_buf = _io.BytesIO(); _PILImage.new("RGB", (640, 480), (10, 60, 120)).save(_buf, "JPEG")
JPEG = _buf.getvalue()
class _Snap(H):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.end_headers()
        self.wfile.write(JPEG)
srv2 = http.server.HTTPServer(("127.0.0.1", 0), _Snap)
threading.Thread(target=srv2.serve_forever, daemon=True).start()
cfg.CAMERAS = f"garten=http://127.0.0.1:{srv2.server_address[1]}/snap.jpg"
cfg.MASTER_URL = ""  # Kamera ist gerate-lokal, unabhangig vom Modus
r = router.try_intents("Zeige Kamera garten")
ok("camera snapshot geladen (ohne Display definiert)",
   r and "Snapshot geladen" in r and "B)" in r, r)
srv2.shutdown()

paths = [p for p, _ in hits]
ok("schedule-route (mac im Pfad)", any(p == "/api/satellite/aa:bb:cc:dd:ee:01/schedule" for p in paths), paths)
ok("dm-route", any(p.endswith("/dm") for p in paths), paths)
ok("dm-voice-route", any(p.endswith("/dm-voice") for p in paths), paths)
ok("call-route", any(p.endswith("/call") for p in paths), paths)
bysched = [b for p, b in hits if "/schedule" in p and not p.endswith("/delete")]
ok("payload kinds", sorted(b["kind"] for b in bysched) == ["timer"], bysched)

# Master nicht erreichbar -> klare Fehleransage
cfg.MASTER_URL = "http://127.0.0.1:1"
r = router.try_intents("Stelle Wecker auf 8 Uhr")
ok("master down -> klare Absage", r and ("Master" in r or "Wecker ging nicht" in r), r)

srv.shutdown()
print()
if fails:
    print("SMOKE FEHLGESCHLAGEN:", fails); sys.exit(1)
print("ALL SMOKE TESTS PASSED")
