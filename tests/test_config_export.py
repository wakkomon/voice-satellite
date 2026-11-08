#!/usr/bin/env python3
"""Getrennte Config-Exporte: Satelliten-Config (inkl. INTENTS) vs. Master.

Laeuft mit Stdlib + Projekmodulen (Store-Schicht); der HTTP-Stack wird nicht
benoetigt. Pruft:
  * Satelliten-Export traegt immer die Intent-Regeln (INTENTS), auch wenn
    der Key im Store gar nicht gesetzt ist (leere Zeilenliste).
  * Satelliten-Export enthaelt alle Satelliten-Keys + alle Overrides,
    aber NIEMALS Master-eigene Ziele (*_URL/*_BACKEND_URL) oder
    master_only-Keys (MUSIC_ROOT, MEMORY_DB, ...).
  * Master-Export enthaelt Backend-Ziele + Secrets, keine Satelliten-Keys.
  * secrets=0 streicht Secrets (Satellit: auch MASTER_TOKEN).
  * import_payload merget beide Datei-Typen ohne unknown_keys.

Aufruf:  python3 tests/test_config_export.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["VS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="vs-cfg-export-")

from master import satconfig as sc

fails = []

def ck(cond, msg):
    print(("  ok  " if cond else "  FAIL") + "  " + msg)
    if not cond:
        fails.append(msg)


store = sc.store  # Singleton wie in der App
store.set_global({
    "INTENTS": "flur an|Flurlampe|ON\nflur aus|Flurlampe|OFF",
    "LLM_MODEL": "hermes-agent",
    "TTS_URL": "http://192.168.82.31:7811/v1",
    "TTS_TOKEN": "geheim",
    "OPENHAB_TOKEN": "oh-token",
    "MASTER_TOKEN": "master-token",
    "MUSIC_ROOT": "/m",
    "MEMORY_DB": "/data/chat/memory.db",
    "DEFAULT_IMAGE_URL": "http://192.168.82.33/weather/snap.jpg",
})
store.set_satellite("AA:BB:CC:DD:EE:FF", {"MIC_DEVICE": "plughw:2,0"})

sat = store.export_satellites()
mas = store.export_master()

print("== Satelliten-Export traegt die Intent-Regeln ==")
ck(sat["kind"] == "satellite", "kind=satellite")
ck(sat["global"].get("INTENTS") == "flur an|Flurlampe|ON\nflur aus|Flurlampe|OFF",
   "INTENTS vollstaendig enthalten")
ck(sat["satellites"] == {"aa:bb:cc:dd:ee:ff": {"MIC_DEVICE": "plughw:2,0"}},
   "alle Satelliten-Overrides enthalten")
ck("LLM_MODEL" in sat["global"] and "DEFAULT_IMAGE_URL" in sat["global"],
   "Satelliten-Keys global enthalten")

print("== Satelliten-Export lasst Master-Kram weg ==")
ck("TTS_URL" not in sat["global"], "kein Master-Backend-Ziel (TTS_URL)")
ck(not (sat["global"].keys() & set(sc.BACKEND_KEYS)), "keine *_BACKEND_URL")
ck("MUSIC_ROOT" not in sat["global"] and "MEMORY_DB" not in sat["global"],
   "keine master_only-Keys")
ck("TTS_TOKEN" not in sat["global"] and "OPENHAB_TOKEN" not in sat["global"],
   "Upstream-Secrets nicht in der Satellitendatei")
ck(sat["global"].get("MASTER_TOKEN") == "master-token",
   "MASTER_TOKEN wohlbehalten dabei")

print("== Master-Export = nur Master-Konfiguration ==")
ck(mas["kind"] == "master", "kind=master")
ck(mas["satellites"] == {}, "keine Satelliten-Overrides")
ck(mas["global"].get("TTS_URL", "").startswith("http://192.168.82.31"),
   "Backend-Ziel enthalten")
ck("MUSIC_ROOT" in mas["global"] and "MEMORY_DB" in mas["global"],
   "master_only-Keys enthalten")
ck(mas["global"].get("TTS_TOKEN") == "geheim"
   and mas["global"].get("OPENHAB_TOKEN") == "oh-token", "Secrets dabei")
ck("INTENTS" not in mas["global"] and "LLM_MODEL" not in mas["global"],
   "keine Satelliten-Keys")

print("== secrets=0 und Leer-Store ==")
s2 = store.export_satellites(include_secrets=False)
ck("MASTER_TOKEN" not in s2["global"] and s2["global"].get("INTENTS"),
   "secrets=0: MASTER_TOKEN raus, INTENTS bleiben")
m2 = store.export_master(include_secrets=False)
ck(not (m2["global"].keys() & set(sc.SECRET_KEYS)), "secrets=0: keine Secrets")
fresh = sc.ConfigStore(os.path.join(os.environ["VS_CONFIG_DIR"], "fresh.json"))
ck(fresh.export_satellites(include_secrets=False)["global"].get("INTENTS") == "",
   "leerer Store: INTENTS als leerer Key dabei (fehlt nie)")

print("== Import merget beide Datei-Typen ==")
r1 = store.import_payload(mas)
r2 = store.import_payload(sat)
ck(r1["unknown_keys"] == [] and r2["unknown_keys"] == [],
   f"keine unknown_keys ({r1['unknown_keys'] + r2['unknown_keys']})")
again = sc.ConfigStore(sc.CONFIG_PATH)
ck(again.get_secret("MASTER_TOKEN") == "master-token", "MASTER_TOKENzurueck")
ck(again.get_effective("AA:BB:CC:DD:EE:FF").get("MIC_DEVICE") == "plughw:2,0",
   "Satelliten-Override zurueck")

sys.exit(1 if fails else 0)
