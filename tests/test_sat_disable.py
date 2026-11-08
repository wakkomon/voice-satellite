#!/usr/bin/env python3
"""Satelliten de-/aktivieren + Bild-Push: Manager-Logik + Master-API.

Ohne echten Server: starlette TestClient gegen die Master-App.
VS_CONFIG_DIR zeigt auf ein Temp-Verzeichnis (nie den produktiven Store).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CFGDIR = tempfile.mkdtemp(prefix="vsdisable")
os.environ["VS_CONFIG_DIR"] = CFGDIR
os.environ.pop("AUTH_TOKEN", None)
os.environ.pop("MASTER_TOKEN", None)
os.environ.pop("SATELLITES", None)
CHAT_DIR = os.path.join(CFGDIR, "chat")
os.environ["CHAT_DIR"] = CHAT_DIR
os.makedirs(CHAT_DIR, exist_ok=True)

from starlette.testclient import TestClient

from master import api
from master import satconfig
from master.manager import SatManager

TOK = "test-master-token-123"
satconfig.store.set_global({"MASTER_TOKEN": TOK})
H = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}
client = TestClient(api.app)

fails = []


def ok(name, cond):
    print(("  ok    " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


def hb(mac, extra=None):
    payload = {"hostname": "h-" + mac[:4], "kind": "pi",
               "pipeline_state": "idle"}
    if extra:
        payload.update(extra)
    return client.post(f"/api/satellite/{mac}/heartbeat", json=payload,
                       headers=H)


# ── Manager: De-/Aktivierung (persistiert) ─────────────────────
api.manager.record_heartbeat("aa:bb:cc:00:01:01",
                             {"hostname": "pi1", "ip": "192.168.82.51"})
ok("set_disabled: unbekannter Satellit -> Fehler",
   api.manager.set_disabled("keiner", True) is not None)

err = api.manager.set_disabled("aa:bb:cc:00:01:01", True)
ok("set_disabled: bekannter Satellit -> ok", err is None)
ok("is_disabled(mac) true", api.manager.is_disabled("aa:bb:cc:00:01:01"))
ok("is_disabled_for(mac) true", api.manager.is_disabled_for("aa:bb:cc:00:01:01"))
ok("is_disabled_for(ip) true", api.manager.is_disabled_for("xx", "192.168.82.51"))
ok("is_disabled_for(fremd) false",
   not api.manager.is_disabled_for("zz", "10.0.0.9"))
m2 = SatManager()
ok("Persistenz: neue Instanz kennt Deaktivierung",
   "aa:bb:cc:00:01:01" in m2._disabled)
with open(os.path.join(CFGDIR, "sat_disabled.json"), encoding="utf-8") as f:
    ok("sat_disabled.json geschrieben",
       json.load(f)["disabled"] == ["aa:bb:cc:00:01:01"])

# ── API: Gatter fuer deaktivierte Satelliten ────────────────────
r = client.post("/api/satellite/aa:bb:cc:00:01:01/heartbeat",
                json={"hostname": "pi1"}, headers=H)
ok("Heartbeat deaktiviert -> 403", r.status_code == 403
   and "deaktiviert" in r.json()["error"])
r = client.post("/api/satellite/aa:bb:cc:00:01:01/chat",
                json={"entries": [{"eid": 1, "role": "user", "text": "x"}]},
                headers=H)
ok("Chat-Push deaktiviert -> 403", r.status_code == 403)
r = client.post("/api/satellite/aa:bb:cc:00:01:01/dm",
                json={"to": "all", "text": "hallo"}, headers=H)
ok("DM-Sender deaktiviert -> 403", r.status_code == 403)

# Zweites (aktives) Gerät: App + Pi
hb("androidabcdef12345", {"kind": "android", "hostname": "sat-android-1"})
api.manager.record_heartbeat("cc:dd:ee:00:02:02",
                             {"hostname": "pi2", "ip": "192.168.82.52"})
api.manager.satellites["cc:dd:ee:00:02:02"].status = "online"
api.manager.satellites["androidabcdef12345"].status = "online"

r = client.post("/api/broadcast", json={"text": "test", "sat": "aa:bb:cc:00:01:01"},
                headers=H)
ok("Broadcast an deaktiviertes Ziel -> 403", r.status_code == 403)

r = client.post("/api/satellite/aa:bb:cc:00:01:01/volume",
                json={"volume": 40}, headers=H)
ok("Steuerung (volume) deaktiviert -> 403", r.status_code == 403)

# DM 'all' ueberspringt deaktiviertes Ziel
r = client.post("/api/satellite/cc:dd:ee:00:02:02/dm",
                json={"to": "all", "text": "alle"}, headers=H)
targets = r.json().get("targets") or []
ok("DM all: deaktiviertes Ziel fehlt",
   r.status_code == 200 and "aa:bb:cc:00:01:01" not in targets)

# ── Bild-Push ───────────────────────────────────────────────────
r = client.post("/api/image",
                json={"url": "http://192.168.82.33/x.png",
                      "sat": "androidabcdef12345", "ttl": 45,
                      "text": "Blick in die Kuche"},
                headers=H)
ok("Bild-Push an App -> 200", r.status_code == 200 and r.json()["ok"] is True)
r = client.post("/api/image",
                json={"url": "http://192.168.82.33/x.png",
                      "sat": "aa:bb:cc:00:01:01"},
                headers=H)
ok("Bild-Push an deaktiviert -> 403", r.status_code == 403)
r = client.post("/api/image",
                json={"url": "ftp://x/y.png", "sat": "androidabcdef12345"},
                headers=H)
ok("Bild-Push ohne http(s) -> 400", r.status_code == 400)
r = client.post("/api/image",
                json={"url": "http://192.168.82.33/x.png", "sat": "pi2"},
                headers=H)
ok("Bild-Push an Pi -> gemeldet (noch nicht verfuegbar)",
   r.status_code == 501 and r.json()["targets"][0]["ok"] is False)

# App holt das Bild per Heartbeat ab
r = hb("androidabcdef12345", {"kind": "android"})
msgs = r.json().get("messages") or []
img = [m for m in msgs if m.get("image")]
ok("App-Heartbeat liefert Bild-Nachricht",
   len(img) == 1 and img[0]["image"] == "http://192.168.82.33/x.png"
   and img[0]["ttl"] == 45 and img[0]["text"] == "Blick in die Kuche")

# ── Wiederaktivierung ───────────────────────────────────────────
r = client.post("/api/satellite/aa:bb:cc:00:01:01/enable", headers=H)
ok("enable -> ok", r.status_code == 200 and r.json()["disabled"] is False)
r = hb("aa:bb:cc:00:01:01")
ok("Herbeat nach Aktivierung -> 200", r.status_code == 200)

# ── MCP: Master-Tools ───────────────────────────────────────────
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                              "method": "list_satellites", "params": {}})
d = r.json()
ok("MCP list_satellites", r.status_code == 200
   and any(x["mac"] == "androidabcdef12345"
           for x in d["result"]["satellites"]))

r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                              "method": "disable_satellite",
                              "params": {"sat": "cc:dd:ee:00:02:02"}})
ok("MCP disable_satellite", r.json()["result"]["ok"] is True)
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3,
                              "method": "push_image",
                              "params": {"url": "http://x/y.jpg"}})
# deaktivierter pi2 + aktives android + (aa:bb jetzt online) -> ok (App trifft)
ok("MCP push_image (Ziel-Report)", r.json()["result"]["ok"] is True)

# Satellite-Tool (Passthrough) an deaktiviertes Ziel -> -32003
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4,
                              "method": "play_tone",
                              "target": "cc:dd:ee:00:02:02"})
ok("MCP Passthrough: deaktivierter Satellite -> -32003",
   r.json().get("error", {}).get("code") == -32003)

r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5,
                              "method": "enable_satellite",
                              "params": {"sat": "cc:dd:ee:00:02:02"}})
ok("MCP enable_satellite", r.json()["result"]["ok"] is True)

r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 6,
                              "method": "send_dm",
                              "params": {"to": "androidabcdef12345",
                                         "text": "hi"}})
ok("MCP send_dm", r.json()["result"]["ok"] is True
   and r.json()["result"]["queued"] == 1)

r = client.get("/mcp/tools")
names = [t["name"] for t in r.json()["tools"]]
ok("MCP tools enthalten Master-Tools",
   {"broadcast", "chat_send", "push_image", "disable_satellite",
    "list_satellites"} <= set(names))

# ── Web-Gate: ohne Token/Login keine Deaktivierung ─────────────
r = client.post("/api/satellite/cc:dd:ee:00:02:02/disable")
ok("disable ohne Auth -> 401", r.status_code == 401)

print()
if fails:
    print(f"FAILS: {len(fails)}")
    sys.exit(1)
print("FAILS: none")
