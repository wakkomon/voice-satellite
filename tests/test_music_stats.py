#!/usr/bin/env python3
"""Regressionstest /api/music: Stats-Modus + vorserialisiertes Payload-Cache.

Hintergrund: Der Dashboard-Refresh lud den kompletten Medien-Index (>40 MB),
json.dumps/gzip pro Request blockierten die Event-Loop — Satellitenliste und
Live-Log luden ewig oder garnicht.
"""
import gzip
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CFGDIR = tempfile.mkdtemp(prefix="vsmusic")
os.environ["VS_CONFIG_DIR"] = CFGDIR
os.environ["CHAT_DIR"] = os.path.join(CFGDIR, "chat")
os.environ.pop("MASTER_TOKEN", None)

from starlette.testclient import TestClient

from master import api
from master import satconfig

TOK = "test-master-token-123"
satconfig.store.set_global({"MASTER_TOKEN": TOK})
H = {"Authorization": f"Bearer {TOK}"}
client = TestClient(api.app)

fails = []


def ok(name, cond):
    print(("  ok    " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


PAYLOAD = {
    "m3u_idx": {"playlist": ["mp3/playlist.m3u"]},
    "m3u_all": ["mp3/playlist.m3u"],
    "mp3_idx": {"song": ["mp3/a/song.mp3"]},
    "mp3_all": ["mp3/a/song.mp3", "mp3/b/song2.mp3"],
    "dir_idx": {},
}
api._index_data = PAYLOAD
api._index_body = None

# Stats-Modus: nur Zähler, winziger Body
r = client.get("/api/music?stats=1", headers=H)
j = r.json()
ok("stats=1 -> 200", r.status_code == 200)
ok("stats: m3u_count", j.get("m3u_count") == 1)
ok("stats: mp3_count", j.get("mp3_count") == 2)
ok("stats: kein Vollpayload", "mp3_all" not in j)

# Vollpayload identity + gzip
r = client.get("/api/music", headers=H)
ok("full -> 200", r.status_code == 200)
ok("full: json rund", json.loads(r.content) == PAYLOAD)
ok("full: Cache gesetzt", api._index_body is not None and api._index_body[0] is PAYLOAD)

r = client.get("/api/music", headers={**H, "Accept-Encoding": "gzip"})
ok("gzip: Header", r.headers.get("content-encoding") == "gzip")
# TestClient/httpx dekodiert Content-Encoding transparent -> r.json() prueft
# sowohl gzip-Rundlauf als auch Payload-Identitaet.
ok("gzip: dekodt rund", r.json() == PAYLOAD)
raw = client.get("/api/music", headers={**H, "Accept-Encoding": "identity"})
ok("identity: unguzippte Bytes identisch",
   raw.content == api._index_body[1])
body1 = api._index_body

# Index-Version wechselt (neues dict-Objekt) -> Cache muss neu gebaut werden
PAYLOAD2 = dict(PAYLOAD, mp3_all=PAYLOAD["mp3_all"] + ["mp3/c/x.mp3"])
api._index_data = PAYLOAD2
j = client.get("/api/music?stats=1", headers=H).json()
ok("stats nach Indexwechsel", j.get("mp3_count") == 3)
r = client.get("/api/music", headers=H)
ok("full nach Indexwechsel rund", json.loads(r.content) == PAYLOAD2)
ok("Cache neu gebaut", api._index_body is not body1 and api._index_body[0] is PAYLOAD2)
ok("Cache wird wiederverwendet", client.get("/api/music", headers=H).content == r.content)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
