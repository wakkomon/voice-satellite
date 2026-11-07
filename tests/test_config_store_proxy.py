#!/usr/bin/env python3
"""Master-seitige Konfig/Secret-Regeln (Concept 1/2), ohne HTTP-Stack.

Laeuft mit Stdlib + Projekmodulen. Der HTTP-Stack des Proxys (fastapi) wird
nicht benoetigt, weil die Store-Schicht von master/satconfig.py getrennt von
den Request-Handlern testbar ist:

  * Tokens wohnen nur am Master (SECRET_KEYS, global) und werden im Snapshot
    maskiert — nie im Klartext.
  * Der Satellit erhaelt per get_effective() NIEMALS einen Token.
  * Proxy-Loop-Guard: Backend-Ziele duerfen nicht auf /proxy/ zurueckzeigen.
  * URL-Modell (Master-Backend vs. Satellit-Proxy-URL) ist dokumentiert.
  * Der Proxy huelt Schluesselwort-Namen (TTS_URL im Store = Proxy-Adresse,
    TTS_BACKEND_URL im ENV = echtes Backend).

Aufruf:  python3 tests/test_config_store_proxy.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []

def ck(cond, msg):
    print(("  ok  " if cond else "  FAIL") + "  " + msg)
    if not cond:
        fails.append(msg)

tmp = tempfile.mkdtemp(prefix="vs-store-")
os.environ["VS_CONFIG_DIR"] = tmp
from master import satconfig as sc

# Wichtig: das Singleton sc.store verwenden — proxy.py liest genau dieses
# (eine Zweit-Instanz waere am Proxy vorbei geschrieben, Alias-Fehler).
store = sc.store

print("== Secret-Heimat: nur Master, nur global ==")
store.set_global({
    "OPENHAB_TOKEN": "oh-supersecret",
    "LLM_TOKEN": "llm-supersecret",
    "TTS_TOKEN": "piper",
    "TTS_MODEL": "de-DEXT2-medium",
})
ck(store.get_secret("OPENHAB_TOKEN") == "oh-supersecret",
   "get_secret('OPENHAB_TOKEN') liefert das echte Token an den Proxy")
ck(store.get_secret("STT_TOKEN") == "", "leeres Secret -> ''")

snap = store.snapshot()
ck(snap["global"]["OPENHAB_TOKEN"] == "gesetzt",
   "Snapshot maskiert gesetztes Secret ('gesetzt', nicht '***')")
raw = str(snap) + str(store._global)
ck("oh-supersecret" not in str(snap),
   "Snapshot enthaelt NIEMALS das Klartext-Token")
store.set_global({"TTS_TOKEN": ""})
ck(store.snapshot()["global"]["TTS_TOKEN"] == "",
   "geleertes Secret -> '' im Snapshot")

print("== Satellit sieht nie ein Token ==")
store.set_satellite("B8:C7:5D:10:E8:DC", {"TTS_MODEL": "other"})
eff = store.get_effective("B8:C7:5D:10:E8:DC")
ck(all(k not in eff for k in sc.SECRET_KEYS),
   "get_effective() enthaelt keine SECRET_KEYS -> Satellit sendet nie ein Token")
ck(eff["TTS_MODEL"] == "other" and "LLM_MODEL" not in eff,
   "Satelliten-Override ueberschreibt global")
ck(store.get_effective()["TTS_MODEL"] == "de-DEXT2-medium",
   "globaler Wert gilt ohne Override")
ck(store.get_effective("unknown:mac")["TTS_MODEL"] == "de-DEXT2-medium",
   "unbekannter Satellit erhaelt die globalen Defaults")

print("== Backend-Ziele: editierbar, aber loop-guard-behandelt ==")
before = dict(store._global)
store.set_global({"TTS_BACKEND_URL": "http://127.0.0.1:8791/proxy/tts",
                  "OPENHAB_URL": "http://192.168.82.33:8080/rest"})
ck("TTS_BACKEND_URL" not in store._global and "OPENHAB_URL" not in store._global,
   "Proxy-Pfad als Backend-Ziel abgelehnt; *_URL bleiben nicht editierbar")
ck(store._global == before, "ablehnen laesst den Bestand unveraendert")
store.set_global({"LLM_BACKEND_URL": "http://litellm:4000/v1/chat/completions"})
ck(store.get_backend("LLM_BACKEND_URL") == "http://litellm:4000/v1/chat/completions",
   "*_BACKEND_URL ist Master-seitig speicherbar (UI-Domain Backends)")
ck("LLM_BACKEND_URL" not in store.get_effective() and
   "LLM_BACKEND_URL" not in store.get_effective("B8:C7:5D:10:E8:DC"),
   "Backend-Ziele werden nie an Satelliten ausgeliefert")

print("== Proxy-Loop-Guard ==")
g = sc.backend_loop_reject
ck(g("TTS_BACKEND_URL", "http://192.168.82.24:8791/proxy/tts"),
   "Backend-Ziel auf /proxy/ -> abgelehnt")
ck(not g("TTS_BACKEND_URL", "http://192.168.82.24:4000/api/tts/openai"),
   "echtes Backend-Ziel -> erlaubt")
ck(g("LLM_BACKEND_URL", "http://localhost:8791/v1/chat/completions"),
   "localhost + Master-Port -> abgelehnt")
ck(not g("LLM_URL", "http://192.168.82.24:8791/proxy/llm"),
   "Satelliten-Wert = Master-Proxy ist erlaubt (der Proxy selbst huelt ihn nicht)")
ck(g("TTS_URL", "http://127.0.0.1:8791/proxy/tts"),
   "Satelliten-Override zurueck auf localhost-Master -> abgelehnt")
try:
    import master.proxy as px
except Exception as e:                          # httpx/starlette fehlen lokal
    px = None
    print("  --  master.proxy nicht importierbar (%s) "
          "— Proxy-Checks prueft der Lauf auf dem Master" % e)
if px:
    ck(px._is_self_proxy("http://x/proxy/stt") is True,
       "proxy._is_self_proxy erkennt /proxy/-Ziele")
    ck(px._is_self_proxy("http://x:4000/api/stt/openai") is False,
       "echtes Backend ist kein Self-Proxy")

print("== Proxy-ENV: Store-Token vor ENV-Token, Backend-ENV vor URL-ENV ==")
if px:
    store.set_global({"TTS_TOKEN": "piper"})  # nach dem Leeren fuer Snapshot-Check
    os.environ["TTS_BACKEND_TOKEN"] = "env-token"
    os.environ["LLM_BACKEND_URL"] = "http://from-env:4000/v1/chat/completions"
    ck(px._upstream_token("TTS_TOKEN", px.ENV_TTS_TOKEN) == "piper",
       "Store-Secret gewinnt gegen ENV-Fallback")
    ck(px._upstream_token("STT_TOKEN", px.ENV_STT_TOKEN) == "",
       "kein Secret nirgends -> '' (Request ohne Authorization)")
    os.environ.pop("TTS_BACKEND_TOKEN", None)
    ck(px._upstream_url("LLM_URL", "http://from-env:4000/v1/chat/completions") ==
       "http://litellm:4000/v1/chat/completions",
       "Upstream-Ziel: Store (*_BACKEND_URL) gewinnt vor ENV-Fallback")
    ck(px._upstream_url("TTS_URL", "http://from-env:7811/v1/audio/speech") ==
       "http://from-env:7811/v1/audio/speech",
       "ohne Store-Wert gewinnt das ENV (alt: .env.master)")
    ck(px._upstream_url("TTS_URL", "http://127.0.0.1:8791/proxy/tts") == "",
       "Self-Proxy-Ziel -> '' -> Proxy antwortet 502 statt Schleife")

print("== URL-Modell: Master-Backend-Schluessel == vs. Satellit-Proxy-Keys ==")
ck(sc.URL_MODEL["satellite_value"] == "<MASTER_URL>/proxy/<llm|stt|tts|openhab>",
   "Satelliten-URLs sind ausschliesslich Master-Proxy-Adressen")
ck(sc.URL_MODEL["satellite_sends_token"] is False,
   "Dokumentiert: Satellit sendet kein Token")
ck(sorted(sc.BACKEND_KEYS) == sorted(sc.URL_MODEL["master_keys"]),
   "BACKEND_KEYS == master_keys (ein Schluesselpaar pro API)")

print()
print("FAILS:", fails or "keine")
sys.exit(1 if fails else 0)
