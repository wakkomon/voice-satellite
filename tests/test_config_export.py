#!/usr/bin/env python3
"""Getrennte Config-Exporte: Master (komplett) vs. Satellit (Minimum).

Laeuft mit Stdlib + Projekmodulen (Store-Schicht); der HTTP-Stack wird nicht
benoetigt. Semantik:
  * Master-Export  = KOMPLETTE Konfiguration (alle globalen Werte inkl.
    Secrets plus alle Satelliten-Overrides) — die Datei, die der
    Import-Button braucht, um einen neuen Master vollstaendig aufzusetzen.
  * Satelliten-Export = nur das Noetigste fuer ein neues Geraet: MASTER_URL +
    MASTER_TOKEN. Keine Modell-/Sprach-/Intent-Keys, keine Backend-Ziele,
    keine Upstream-Secrets, keine Overrides.
  * Import mergt beide Datei-Typen ohne unknown_keys (Master-Export stellt
    alles wieder her, Satelliten-Export setzt nur den Master-Token).

Aufruf:  python3 tests/test_config_export.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["VS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="vs-cfg-export-")
os.environ.pop("MASTER_PORT", None)

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

mas = store.export_master()
sat = store.export_satellites(master_url="http://192.168.82.24:8791")

print("== Master-Export = komplette Konfiguration (was der Import braucht) ==")
ck(mas["kind"] == "master", "kind=master")
ck(mas["satellites"] == {"aa:bb:cc:dd:ee:ff": {"MIC_DEVICE": "plughw:2,0"}},
   "alle Satelliten-Overrides enthalten")
ck(mas["global"].get("LLM_MODEL") == "hermes-agent"
   and mas["global"].get("INTENTS", "").startswith("flur an"),
   "Modell-/Intent-Keys (global) enthalten")
ck(mas["global"].get("TTS_URL", "").startswith("http://192.168.82.31"),
   "Master-Backend-Ziel (TTS_URL) enthalten")
ck("MUSIC_ROOT" in mas["global"] and "MEMORY_DB" in mas["global"],
   "master_only-Keys enthalten")
ck(mas["global"].get("TTS_TOKEN") == "geheim"
   and mas["global"].get("OPENHAB_TOKEN") == "oh-token"
   and mas["global"].get("MASTER_TOKEN") == "master-token", "Secrets dabei")

print("== Satelliten-Export = nur Master-URL + Token (Minimum) ==")
ck(sat["kind"] == "satellite", "kind=satellite")
ck(sat.get("master_url") == "http://192.168.82.24:8791", "master_url enthalten")
ck(sat.get("master_token") == "master-token", "master_token enthalten")
ck(sat["global"].get("MASTER_TOKEN") == "master-token",
   "global.MASTER_TOKEN (damit der Import ihn anwenden kann)")
ck(sat["satellites"] == {}, "keine Satelliten-Overrides")
ck(set(sat["global"].keys()) == {"MASTER_TOKEN"}, "global: NUR MASTER_TOKEN")
ck("LLM_MODEL" not in sat["global"] and "INTENTS" not in sat["global"],
   "keine Modell-/Intent-Keys")
ck("TTS_URL" not in sat["global"]
   and not (set(sat["global"].keys()) & set(sc.BACKEND_KEYS)),
   "keine Master-Backend-Ziele (*_URL/*_BACKEND_URL)")
ck("TTS_TOKEN" not in sat["global"] and "OPENHAB_TOKEN" not in sat["global"],
   "keine Upstream-Secrets")
ck("MUSIC_ROOT" not in sat["global"] and "MEMORY_DB" not in sat["global"],
   "keine master_only-Keys")

print("== secrets=0 ==")
m2 = store.export_master(include_secrets=False)
ck(not (set(m2["global"].keys()) & set(sc.SECRET_KEYS)),
   "Master-Export: keine Secrets")
ck(set(m2["satellites"].keys()) == {"aa:bb:cc:dd:ee:ff"},
   "Master-Export: Satelliten-Overrides bleiben")
s2 = store.export_satellites(include_secrets=False,
                             master_url="http://192.168.82.24:8791")
ck("master_token" not in s2 and "MASTER_TOKEN" not in s2["global"],
   "Satelliten-Export: Token raus")
ck(s2.get("master_url") == "http://192.168.82.24:8791",
   "Satelliten-Export: master_url bleibt")

print("== Import: Master-Export stellt neuen Master vollstaendig wieder her ==")
fresh = sc.ConfigStore(os.path.join(os.environ["VS_CONFIG_DIR"], "fresh.json"))
r1 = fresh.import_payload(mas)
ck(r1["unknown_keys"] == [], f"keine unknown_keys ({r1['unknown_keys']})")
ck(fresh.get_secret("MASTER_TOKEN") == "master-token", "MASTER_TOKEN zurueck")
ck(fresh.get_secret("TTS_TOKEN") == "geheim", "TTS_TOKEN zurueck")
ck(fresh.get_backend("TTS_URL") == "http://192.168.82.31:7811/v1", "TTS_URL zurueck")
ck(fresh._global.get("LLM_MODEL") == "hermes-agent", "LLM_MODEL zurueck")
ck(fresh._global.get("MUSIC_ROOT") == "/m", "MUSIC_ROOT zurueck")
ck(fresh.get_effective("AA:BB:CC:DD:EE:FF").get("MIC_DEVICE") == "plughw:2,0",
   "Satelliten-Override zurueck")

print("== Import: Satelliten-Export setzt nur den Master-Token ==")
fresh2 = sc.ConfigStore(os.path.join(os.environ["VS_CONFIG_DIR"], "fresh2.json"))
r2 = fresh2.import_payload(sat)
ck(r2["unknown_keys"] == [], f"keine unknown_keys ({r2['unknown_keys']})")
ck(fresh2.get_secret("MASTER_TOKEN") == "master-token", "MASTER_TOKEN gesetzt")
ck(fresh2._global.get("LLM_MODEL") in (None, ""), "keine Modell-Keys durchgezogen")

print("== Satelliten-Export: ENV-Fallback, wenn der Store keinen Token haelt ==")
# Deployment-Fall: MASTER_TOKEN nur per ENV gesetzt (Store selbst leer).
# Ohne Fallback waere die Satelliten-Export-Datei fuer ein neues Geraet
# unbrauchbar (kein Zugang) — genau der Bug, den der Fix schliesst.
os.environ["MASTER_TOKEN"] = "env-deploy-token"
envstore = sc.ConfigStore(os.path.join(os.environ["VS_CONFIG_DIR"], "env.json"))
ck(not envstore._global.get("MASTER_TOKEN"), "Store selbst haelt keinen MASTER_TOKEN")
es = envstore.export_satellites(master_url="http://192.168.82.24:8791")
ck(es["global"].get("MASTER_TOKEN") == "env-deploy-token"
   and es.get("master_token") == "env-deploy-token",
   "effektiver Token (ENV) wird exportiert")
es0 = envstore.export_satellites(include_secrets=False,
                                 master_url="http://192.168.82.24:8791")
ck("MASTER_TOKEN" not in es0["global"] and "master_token" not in es0,
   "secrets=0: auch ENV-Token raus")
del os.environ["MASTER_TOKEN"]

sys.exit(1 if fails else 0)
