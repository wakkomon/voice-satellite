"""WebUI-Benutzerverwaltung + Sessions + Geräte-Namen (Stdlib, lokal).

VS_CONFIG_DIR wird auf ein Scratch-Verzeichnis gelegt — der produktive
Store bleibt unberührt. Aufruf: python3 tests/test_webauth_users.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="vs-webauth-test-")
os.environ["VS_CONFIG_DIR"] = _tmp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from master import webauth  # noqa: E402


def test_setup_and_create():
    assert webauth.needs_setup() is True
    assert webauth.create_user("Antje", "geheim123", "admin") is None
    assert webauth.needs_setup() is False
    # Duplikat / ungültige Namen / kurzes Passwort / falsche Rolle
    assert webauth.create_user("antje", "geheim123", "user") is not None
    assert webauth.create_user("x", "geheim123", "user") is not None
    assert webauth.create_user("neu", "kurz", "user") is not None
    assert webauth.create_user("neu", "geheim123", "root") is not None
    assert webauth.create_user("antje2", "geheim123", "user",
                               display="Antje Z.") is None
    names = {u["username"] for u in webauth.list_users()}
    assert names == {"antje", "antje2"}, names
    # kein Hash in der Liste
    for u in webauth.list_users():
        assert "pass" not in u
        assert u["role"] in ("admin", "user")


def test_login_and_sessions():
    assert webauth.verify_login("Antje", "falsch") is None
    assert webauth.verify_login("antje", "geheim123") == "admin"
    assert webauth.verify_login("ANTJE2", "geheim123") == "user"
    tok = webauth.start_session("antje")
    u = webauth.session_user(tok)
    assert u and u["role"] == "admin" and u["display"] == "Antje"
    assert webauth.session_user("unbekannt") is None
    webauth.end_session(tok)
    assert webauth.session_user(tok) is None
    # unbekannter/gelöschter Benutzer: Session ungültig
    tok2 = webauth.start_session("antje2")
    assert webauth.session_user(tok2) is not None
    assert webauth.delete_user("antje2") is None
    assert webauth.session_user(tok2) is None


def test_roles_and_protection():
    # letzter Admin: keine Herabstufung, kein Löschen
    assert webauth.update_user("antje", role="user") is not None
    assert webauth.delete_user("antje") is not None
    webauth.create_user("boss", "geheim123", "admin")
    assert webauth.update_user("antje", role="user") is None
    assert webauth.update_user("boss", role="user") is not None  # letzter Admin
    # Passwortwechsel wirkt
    assert webauth.update_user("antje", password="neuespw9") is None
    assert webauth.verify_login("antje", "geheim123") is None
    assert webauth.verify_login("antje", "neuespw9") == "user"
    # Rolle zurück + Anzeigename
    assert webauth.update_user("antje", role="admin", display="Antje R.") is None
    assert [u for u in webauth.list_users() if u["username"] == "antje"] \
        [0]["display"] == "Antje R."


def test_env_seed():
    # Zweiter, leerer Store: ENV-Seed legt Admin an
    import importlib
    path = os.path.join(_tmp, "u2", "users.json")
    os.environ["VS_CONFIG_DIR"] = os.path.join(_tmp, "u2")
    os.environ["WEB_ADMIN_USER"] = "admin"
    os.environ["WEB_ADMIN_PASSWORD"] = "seedsafe9"
    importlib.reload(webauth)
    assert webauth.needs_setup() is True
    webauth.seed_admin_from_env()
    assert webauth.needs_setup() is False
    assert webauth.verify_login("admin", "seedsafe9") == "admin"
    # doppeltes Seed-ENR-Setup ist no-op (existiert bereits)
    webauth.seed_admin_from_env()
    assert len([u for u in webauth.list_users() if u["username"] == "admin"]) == 1
    os.environ.pop("WEB_ADMIN_USER", None)
    os.environ.pop("WEB_ADMIN_PASSWORD", None)


def test_rate_limit():
    import importlib
    os.environ["VS_CONFIG_DIR"] = os.path.join(_tmp, "rl")
    importlib.reload(webauth)
    webauth.create_user("lim", "richtigpw1", "user")
    for _ in range(webauth._MAX_FAILS):
        assert webauth.verify_login("lim", "falsch") is None
    # jetzt gesperrt — auch mit richtigem Passwort
    assert webauth.verify_login("lim", "richtigpw1") is None


def test_device_names():
    """manager: persistenter Anzeigename + Herzschlag-Verhalten."""
    os.environ["VS_CONFIG_DIR"] = os.path.join(_tmp, "names")
    import master.manager as mgr_mod
    importlib = __import__("importlib")
    importlib.reload(mgr_mod)
    m = mgr_mod.SatManager()
    m.record_heartbeat("02:42:ac:11:00:03",
                       {"hostname": "pi-hole", "ip": "192.168.82.32"})
    assert m.display_name("192.168.82.32") == "pi-hole"
    assert m.set_name("192.168.82.32", "Wohnzimmer") is None
    assert m.display_name("192.168.82.32") == "Wohnzimmer"
    # per custom-Name auflösbar (sat_target + display_name)
    assert m.display_name("wohnzimmer") == "Wohnzimmer"
    hit = m.sat_target("wohnzimmer")
    # Registry-Key = MAC (hier ohne SATELLITES-ENV; live ist es die Host-IP)
    assert hit and hit[0] == "02:42:ac:11:00:03"
    # Herzschlag ersetzt den Namen NICHT (bleibt gespeichert)
    m.record_heartbeat("02:42:ac:11:00:03",
                       {"hostname": "pi-hole", "ip": "192.168.82.32"})
    assert m.display_name("192.168.82.32") == "Wohnzimmer"
    # persistiert: neue Instanz liest den Namen
    m2 = mgr_mod.SatManager()
    m2.record_heartbeat("02:42:ac:11:00:03",
                        {"hostname": "pi-hole", "ip": "192.168.82.32"})
    assert m2.display_name("192.168.82.32") == "Wohnzimmer"
    # leeren = Hostname-Fallback
    assert m2.set_name("192.168.82.32", "") is None
    m3 = mgr_mod.SatManager()
    m3.record_heartbeat("02:42:ac:11:00:03",
                        {"hostname": "pi-hole", "ip": "192.168.82.32"})
    assert m3.display_name("192.168.82.32") == "pi-hole"
    # unbekanntes Ziel
    assert m.set_name("keiner-dieser-keys-existiert", "X") == "Satellit nicht bekannt"


def test_setup_ui_flow():
    # frischer Store: vorherige Tests haben Konten angelegt
    os.environ["VS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="vs-setup-flow-")
    webauth._USER_CACHE = None  # Store-Cache auf neues Verzeichnis umhaengen
    # Regression: WebUI zeigt bei leerem Store das Setup-Formular und ruft
    # /api/setup auf (nicht /login). Der Test haelt den API-Vertrag fest,
    # auf dem diese UI-Entscheidung beruht.
    assert webauth.needs_setup() is True
    # UI-Pfad A versehentlich /login: -> Ablehnung (das war der Bug-Symptom)
    assert webauth.verify_login("root", "geheim123") is None
    # UI-Pfad B (korrekt): /api/setup legt erstes Admin-Konto an
    assert webauth.create_user("root", "geheim123", role="admin") is None
    assert webauth.needs_setup() is False
    me = webauth.session_user(webauth.start_session("root"))
    assert me and me["role"] == "admin"
    # Zweiter Setup-Versuch: /api/setup prueft needs_setup() vor create_user
    # und antwortet dann 403 "Ersteinrichtung bereits abgeschlossen"
    # (UI biegt daraufhin auf den Login-Schirm um) — Store ist ab jetzt nicht mehr leer.
    assert webauth.needs_setup() is False
    # Login funktioniert jetzt
    assert webauth.verify_login("root", "geheim123") is not None


def main():
    for fn in (test_setup_and_create, test_login_and_sessions,
               test_roles_and_protection, test_env_seed, test_rate_limit,
               test_device_names, test_setup_ui_flow):
        fn()
        print(f"OK  {fn.__name__}")
    print("alle webauth/user-Tests bestanden")


if __name__ == "__main__":
    main()
