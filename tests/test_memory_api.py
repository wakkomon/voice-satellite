#!/usr/bin/env python3
"""Memory-API am Master: Gate + Satelliten-Routen + Admin-Routen.

Ohne echten Server: starlette TestClient. VS_CONFIG_DIR/CHAT_DIR auf
Temp-Verzeichnisse (nie den produktiven Store beruehren).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CFGDIR = tempfile.mkdtemp(prefix="vsmemory")
os.environ["VS_CONFIG_DIR"] = CFGDIR
os.environ.pop("AUTH_TOKEN", None)
os.environ.pop("MASTER_TOKEN", None)
os.environ.pop("SATELLITES", None)
CHAT_DIR = os.path.join(CFGDIR, "chat")
os.environ["CHAT_DIR"] = CHAT_DIR
os.makedirs(CHAT_DIR, exist_ok=True)

from starlette.testclient import TestClient

from master import api, satconfig

TOK = "test-master-token-mem"
satconfig.store.set_global({"MASTER_TOKEN": TOK})
H = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}
BAD = {"Authorization": "Bearer falsch", "Content-Type": "application/json"}
client = TestClient(api.app)

fails = []


def ok(name, cond):
    print(("  ok    " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


MAC = "aa:bb:cc:dd:ee:99"

# ── Gate: ohne Login 401 ─────────────────────────────────────────────
r = client.get("/api/memory")
ok("GET /api/memory ohne Login -> 401", r.status_code == 401)
r = client.put("/api/memory/facts", json={"key": "web", "facts": ["x"]})
ok("PUT /api/memory/facts ohne Login -> 401", r.status_code == 401)
r = client.post("/api/memory/history/clear", json={"key": "web"})
ok("POST /api/memory/history/clear ohne Login -> 401", r.status_code == 401)

# ── Sat-Routen: Token-Pflicht ────────────────────────────────────────
r = client.get(f"/api/memory/sat/facts?mac={MAC}")
ok("Sat /facts ohne Token -> 401", r.status_code == 401)
r = client.post("/api/memory/sat/exchange",
                json={"mac": MAC, "user": "a", "assistant": "b"},
                headers=BAD)
ok("Sat /exchange falscher Token -> 401", r.status_code == 401)

# ── Sat-Exchange + History ───────────────────────────────────────────
r = client.post("/api/memory/sat/exchange",
                json={"mac": MAC, "channel": "voice",
                      "user": "Wo wohnt der Nutzer?", "assistant": "Rosenstrasse"},
                headers=H)
ok("Sat /exchange -> ok", r.status_code == 200 and r.json().get("ok"))
r = client.get(f"/api/memory/sat/history?mac={MAC}&channel=voice", headers=H)
ok("Sat /history liefert role/content",
   r.status_code == 200 and
   [m["content"] for m in r.json()["messages"]] ==
   ["Wo wohnt der Nutzer?", "Rosenstrasse"])

# ── Fakten schreiben + lesen ─────────────────────────────────────────
r = client.put("/api/memory/facts",
               json={"key": MAC, "facts": ["Wohnt in Rosenstrasse", "x"]},
               headers=H)
ok("PUT /api/memory/facts -> ok", r.status_code == 200)
r = client.get(f"/api/memory/sat/facts?mac={MAC}", headers=H)
ok("Sat /facts liest neue Fakten",
   r.status_code == 200 and "Wohnt in Rosenstrasse" in r.json()["facts"])

# ── Snapshot (Admin, Token-Fallback reicht) ──────────────────────────
r = client.get("/api/memory", headers=H)
ok("GET /api/memory -> ok", r.status_code == 200)
devices = {d["key"]: d for d in r.json().get("devices", [])}
ok("Snapshot enthaelt 'web' und Satellit",
   "web" in devices and MAC in devices)
ok("Snapshot-Fakten + Historie vorhanden",
   devices[MAC]["facts"] and
   any(h.get("channel") == "voice" for h in devices[MAC]["history"]))

# ── Verlauf loeschen (Fakten bleiben) ────────────────────────────────
r = client.post("/api/memory/history/clear", json={"key": MAC}, headers=H)
ok("POST history/clear -> ok", r.status_code == 200)
r = client.get("/api/memory", headers=H)
devices = {d["key"]: d for d in r.json().get("devices", [])}
ok("Verlauf geleert, Fakten erhalten",
   devices[MAC]["history"] == [] and devices[MAC]["facts"])

# ── Registry-Key: Heartbeat-MAC -> kanonischer Key ───────────────────
r = client.post(f"/api/satellite/{MAC}/heartbeat",
                json={"hostname": "pi-test", "ip": "192.168.82.77",
                      "kind": "pi", "pipeline_state": "idle"},
                headers=H)
ok("Heartbeat registriert", r.status_code == 200)
r = client.post("/api/memory/sat/exchange",
                json={"mac": MAC, "user": "u2", "assistant": "a2"},
                headers=H)
ok("Exchange (2) -> ok", r.status_code == 200)
r = client.get("/api/memory", headers=H)
devs = r.json().get("devices", [])
ok("Snapshot zeigt das Geraet (Registry-Key MAC) mit Historie",
   any(d["key"] == MAC and d["history"] for d in devs))

print()
if fails:
    print("GESCHEITERT:", fails)
    sys.exit(1)
print("ALLE PRUEFUNGEN BESTANDEN")
