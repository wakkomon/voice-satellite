#!/usr/bin/env python3
"""Satelliten-seitige URL-Regeln: alle API-Endpunkte stammen aus MASTER_URL.

Der Satellit darf Backends und deren Tokens weder kennen noch ziehen:
  * TTS_URL/STT_URL/LLM_URL/OPENHAB_URL werden ausschliesslich aus
    MASTER_URL abgeleitet ({MASTER_URL}/proxy/<api>); ein ENV TTS_URL
    o. dgl. hat keine Wirkung mehr.
  * Alte Master-Konfigs, die *_URL/*_TOKEN/FORCE_MASTER_PROXY noch
    mitschicken, still ignoriert (kein applied, kein ignored-Eintrag).
  * _OVERWRITABLE enthaelt keine Backend-URLs/Tokens mehr.
  * MASTER_MUSIC_AUTH haengt an MASTER_TOKEN.

Steht kein Master konfiguriert da, sind die Endpunkte leer — Aufrufe
brechen sichtbar ab, statt lokal ein Backend zu raten.

Aufruf:  python3 tests/test_satellite_proxy_config.py
"""
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def ck(cond, msg):
    print(("  ok  " if cond else "  FAIL") + "  " + msg)
    if not cond:
        fails.append(msg)


def load_config(env: dict):
    """app/config.py eigenstaendig importieren (sys.modules-Eintrag vor
    exec_module noetig: apply_overrides nutzt sys.modules[__name__])."""
    for k in ("TTS_URL", "STT_URL", "LLM_URL", "OPENHAB_URL", "MASTER_URL",
              "MASTER_TOKEN", "TTS_TOKEN", "LLM_TOKEN", "OPENHAB_TOKEN",
              "STT_TOKEN", "FORCE_MASTER_PROXY", "MASTER_MUSIC_PROXY_URL",
              "MASTER_MUSIC_AUTH"):
        os.environ.pop(k, None)
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(
        "vs_config_under_test", os.path.join(ROOT, "app", "config.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vs_config_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


tmp = tempfile.mkdtemp(prefix="vs-satcfg-")

print("== Master konfiguriert: Endpunkte = Proxy-Pfade ==")
c = load_config({
    "MASTER_URL": "http://192.168.82.24:8791",
    "MASTER_TOKEN": "m-token",
    "SATELLITE_CONFIG_DIR": tmp,
    # Altlasten: ein alter Start-ENV darf keine Wirkung mehr haben.
    "TTS_URL": "http://192.168.82.31:7811/v1/audio/speech",
    "LLM_URL": "http://192.168.82.33:8642/v1/chat/completions",
    "OPENHAB_URL": "http://192.168.82.33:8080/rest/items",
    "OPENHAB_TOKEN": "should-be-ignored",
})
ck(c.TTS_URL == "http://192.168.82.24:8791/proxy/tts", "TTS_URL -> /proxy/tts")
ck(c.STT_URL == "http://192.168.82.24:8791/proxy/stt", "STT_URL -> /proxy/stt")
ck(c.LLM_URL == "http://192.168.82.24:8791/proxy/llm", "LLM_URL -> /proxy/llm")
ck(c.OPENHAB_URL == "http://192.168.82.24:8791/proxy/openhab",
   "OPENHAB_URL -> /proxy/openhab")
ck(c.OPENHAB_URL != "http://192.168.82.33:8080/rest/items",
   "ENV-Direkt-URL hat keine Wirkung")
ck(c.MASTER_MUSIC_PROXY_URL == "http://192.168.82.24:8791",
   "Musik-Proxy haengt an MASTER_URL")
ck(c.MASTER_MUSIC_AUTH == "Bearer m-token", "Musik-Auth aus MASTER_TOKEN")
for probe in ("OPENHAB_TOKEN", "TTS_TOKEN", "STT_TOKEN", "LLM_TOKEN",
              "FORCE_MASTER_PROXY", "PROXY_PATHS"):
    ck(not hasattr(c, probe) or probe in c._LEGACY_API_KEYS,
       f"{probe} ist kein Satelliten-Key mehr")

print("== Config-Pull: Altlasten still ignoriert, neue Keys angewendet ==")
applied, ignored = c.apply_overrides({
    "TTS_URL": "http://192.168.82.31:7811/v1/audio/speech",
    "OPENHAB_TOKEN": "x",
    "FORCE_MASTER_PROXY": "0",
    "TTS_MODEL": "qwen3",
    "totally_unknown": "1",
})
ck("TTS_URL" not in applied and "OPENHAB_TOKEN" not in applied,
   "Legacy-Keys werden nicht angewendet")
ck("TTS_URL" not in ignored and "FORCE_MASTER_PROXY" not in ignored,
   "Legacy-Keys tauchen nicht als 'ignoriert' im Log auf")
ck(applied == ["TTS_MODEL"] and ignored == ["totally_unknown"],
   "nur echte ueberschreibbare Keys kommen durch")
ck(c.TTS_URL == "http://192.168.82.24:8791/proxy/tts",
   "gezogene TTS_URL kann den Proxy-Pfad nicht kippen")

print("== Kein Master: Endpunkte leer (sichtbarer Abbruch, kein Lokalrat) ==")
c2 = load_config({"MASTER_URL": "", "SATELLITE_CONFIG_DIR": tmp,
                  "TTS_URL": "http://192.168.82.31:7811"})
ck(c2.TTS_URL == "" and c2.STT_URL == "" and c2.LLM_URL == ""
   and c2.OPENHAB_URL == "", "ohne Master sind alle Endpunkte leer")
ck(c2.MASTER_MUSIC_AUTH == "", "ohne Master kein Musik-Auth")

print()
print("FAILS:", fails or "keine")
sys.exit(1 if fails else 0)
