#!/usr/bin/env python3
"""MASTER_TOKEN-Auth: Satelliten-Zugaenge am Master (Heartbeat, Config-Pull, Proxy).

Ohne Server/Netzwerk: die Helfer werden direkt mit Request-Attrappen geprueft.
Master und Satellit verwenden denselben Token (Master: UI-Secret/ENV,
Satellit: ENV MASTER_TOKEN wie MASTER_URL).
"""
import os
import tempfile

CFG = os.path.join(tempfile.mkdtemp(prefix="vsauth"), "satellites.json")
os.environ["VS_CONFIG_DIR"] = CFG
os.environ.pop("AUTH_TOKEN", None)
os.environ.pop("MASTER_TOKEN", None)

from app import config as appcfg
from app import http
from master import api, proxy, satconfig

store = satconfig.ConfigStore(CFG)
api.config_store = store
proxy.config_store = store

TOKEN = "a-b-c-d-e-f-1-2-3-4"


class Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


def checks():
    fails = []

    def ok(name, cond):
        print(("  ok    " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    ok("MASTER_TOKEN ist UI-Secret (SECRET_KEYS)", "MASTER_TOKEN" in satconfig.SECRET_KEYS)

    # Kein Token gesetzt -> offen (abwaertskompatibel zu aelteren Satelliten)
    ok("Ohne Token bleibt der Satelliten-Zugang offen",
       api._sat_auth_ok(Req(), "aa:bb:cc:dd:ee:01"))

    store.set_global({"MASTER_TOKEN": TOKEN})
    ok("UI-Secret MASTER_TOKEN wird gelesen", api._sat_token() == TOKEN)
    ok("Request ohne Header -> abgelehnt", not api._sat_auth_ok(Req(), "x"))
    ok("Bearer-Header passt", api._sat_auth_ok(
        Req({"authorization": f"Bearer {TOKEN}"}), "x"))
    ok("Header ohne Bearer-Praefix passt",
       api._sat_auth_ok(Req({"authorization": TOKEN}), "x"))
    ok("x-master-token wird akzeptiert",
       api._sat_auth_ok(Req({"x-master-token": TOKEN}), "x"))
    ok("Falscher Token -> abgelehnt",
       not api._sat_auth_ok(Req({"authorization": "Bearer falsch"}), "x"))

    # ENV-Fallback, wenn die UI leer ist; UI-Secret gewinnt
    store.set_global({"MASTER_TOKEN": ""})
    os.environ["MASTER_TOKEN"] = "env-token"
    ok("ENV-Fallback, wenn UI-Secret leer", api._sat_auth_ok(
        Req({"authorization": "Bearer env-token"}), "y"))
    store.set_global({"MASTER_TOKEN": TOKEN})
    ok("UI-Secret gewinnt gegen ENV-Fallback", api._sat_token() == TOKEN)
    os.environ.pop("MASTER_TOKEN", None)

    # Proxy-Endpunkte: AUTH_TOKEN oder MASTER_TOKEN
    proxy.AUTH_TOKEN = "auth-token"
    ok("Proxy akzeptiert AUTH_TOKEN",
       proxy._authorized(Req({"authorization": "Bearer auth-token"})))
    ok("Proxy akzeptiert MASTER_TOKEN",
       proxy._authorized(Req({"authorization": f"Bearer {TOKEN}"})))
    ok("Proxy lehnt unbekannten Token ab",
       not proxy._authorized(Req({"authorization": "Bearer nope"})))
    ok("Token-Liste: beide Tokens, keine Leeren",
       sorted(proxy.auth_tokens()) == sorted(["auth-token", TOKEN]))
    proxy.AUTH_TOKEN = ""

    # Satellit: Authorization nur mit gesetztem MASTER_TOKEN
    ok("Satellit ohne MASTER_TOKEN: kein Bearer",
       appcfg.MASTER_TOKEN == "" and appcfg.MASTER_MUSIC_AUTH == "")
    appcfg.MASTER_TOKEN = TOKEN
    appcfg.MASTER_MUSIC_AUTH = f"Bearer {TOKEN}"
    ok("Satellit mit MASTER_TOKEN: Bearer fuer Master-Requests",
       appcfg.MASTER_MUSIC_AUTH == f"Bearer {TOKEN}")
    ok("http-Helfer unterstuetzen token-Argument",
       all("token" in f.__code__.co_varnames
           for f in (http.get_json, http.get_text, http.post_json)))
    return fails


if __name__ == "__main__":
    f = checks()
    print("\nFAILS:", f if f else "none")
    raise SystemExit(1 if f else 0)
