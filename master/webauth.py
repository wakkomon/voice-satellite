"""Benutzerverwaltung und Sessions fuer die Master-WebUI.

Zwei Rollen:
  admin  – darf die Konfiguration lesen UND aendern, Geraete steuern/
           umbenennen und Benutzer verwalten
  user   – darf alles lesen (Dashboard, Chat, Config lesend) und chatten,
           aber nichts aendern

Die Satelliten-/App-Zugriffe (Heartbeat, Config-Pull, Proxy, MCP) laufen
unveraendert ueber MASTER_TOKEN (Bearer) und werden von diesem Modul nicht
beruehrt — es geht ausschliesslich um die menschlichen WebUI-Zugriffe.

Speicher: users.json im VS_CONFIG_DIR (persistiertes Config-Volume).
Passwoerter: PBKDF2-HMAC-SHA256 (stdlib), randomer Salt, konstanter
Vergleich (hmac.compare_digest).

Ersteinrichtung: ist der Store leer, erlaubt POST /api/setup das erste
Admin-Konto (nur solange leer). Optional seedet das Master-ENV
WEB_ADMIN_USER + WEB_ADMIN_PASSWORD beim Start dasselbe.

Sessions: memory-inter Token -> (Benutzer, Ablauf); Cookie 'vs_sid'
(HttpOnly, SameSite=Lax). Ein Master-Neustart meldet alle Browser ab —
bewusst einfach gehalten, der Store selbst (Benutzer) bleibt erhalten.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time

log = logging.getLogger("master.webauth")

ROLES = ("admin", "user")
SESSION_TTL_SEC = 14 * 86400          # 14 Tage gueltig, sliding bei Nutzung
_MAX_FAILS = 5                        # Login-Versuche pro Benutzer vor Pause
_FAIL_COOLDOWN = 30.0                 # Sekunden Pause nach _MAX_FAILS
_PBKDF2_ITERS = 240_000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")

_USER_CACHE: dict | None = None
_lock = threading.Lock()
_sessions: dict[str, tuple[str, float]] = {}   # token -> (username, exp)
_fail_counts: dict[str, tuple[int, float]] = {}  # username -> (fails, until)


def _users_path() -> str:
    d = os.environ.get("VS_CONFIG_DIR", "").strip()
    if not d:
        from .satconfig import CONFIG_DIR
        d = CONFIG_DIR
    return os.path.join(d, "users.json")


def _load() -> dict:
    """{'users': {name: {pass, role, created}}} — cached, bei Aenderung neu."""
    global _USER_CACHE
    if _USER_CACHE is not None:
        return _USER_CACHE
    path = _users_path()
    data: dict = {"users": {}}
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict) and isinstance(loaded.get("users"), dict):
            data = loaded
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as e:
        log.warning("users.json unlesbar (%s) — leer gestartet", e)
    _USER_CACHE = data
    return data


def _save(data: dict) -> None:
    global _USER_CACHE
    path = _users_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    _USER_CACHE = data


def _hash_pw(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), _PBKDF2_ITERS)
    return f"pbkdf2:{_PBKDF2_ITERS}:{salt}:{dk.hex()}"


def _verify_pw(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt, hexhash = str(stored).split(":")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), hexhash)
    except (ValueError, TypeError):
        return False


# ── Benutzer-Store ──────────────────────────────────────────────

def needs_setup() -> bool:
    """True, solange es kein einziges Benutzerkonto gibt (Ersteinrichtung)."""
    with _lock:
        return not _load()["users"]


def seed_admin_from_env() -> None:
    """Beim Start: leerer Store + WEB_ADMIN_USER/WEB_ADMIN_PASSWORD -> Admin."""
    user = os.environ.get("WEB_ADMIN_USER", "").strip()
    pw = os.environ.get("WEB_ADMIN_PASSWORD", "")
    if not user or not pw:
        return
    with _lock:
        data = _load()
        if user.lower() in data["users"]:
            return
        data["users"][user.lower()] = {
            "pass": _hash_pw(pw), "role": "admin",
            "display": user, "created": time.time()}
        _save(data)
    log.info("Admin-Konto '%s' aus ENV angelegt (WEB_ADMIN_USER)", user)


def create_user(username: str, password: str, role: str,
                display: str = "") -> str | None:
    """Neues Konto anlegen. Gibt None bei Erfolg zurueck, sonst Fehlerstring."""
    name = (username or "").strip().lower()
    if not _NAME_RE.match(name):
        return ("Benutzername: 3-32 Zeichen, nur Buchstaben, Zahlen, "
                "Punkt, Bindestrich, Unterstrich (Start alphanumerisch)")
    if len(password or "") < 6:
        return "Passwort muss mindestens 6 Zeichen haben"
    if role not in ROLES:
        return f"Rolle muss {' oder '.join(ROLES)} sein"
    with _lock:
        data = _load()
        if name in data["users"]:
            return "Benutzername existiert bereits"
        data["users"][name] = {
            "pass": _hash_pw(password), "role": role,
            "display": (display or username or name).strip()[:48],
            "created": time.time()}
        _save(data)
    return None


def delete_user(username: str) -> str | None:
    name = (username or "").strip().lower()
    with _lock:
        data = _load()
        if name not in data["users"]:
            return "Benutzer nicht bekannt"
        if data["users"][name]["role"] == "admin" and \
                sum(1 for u in data["users"].values()
                    if u.get("role") == "admin") <= 1:
            return "den letzten Admin kann man nicht loeschen"
        del data["users"][name]
        _save(data)
    # aktive Sessions des Benutzers entwerten
    with _lock:
        for tok, (u, _exp) in list(_sessions.items()):
            if u == name:
                _sessions.pop(tok, None)
    return None


def update_user(username: str, *, password: str = "", role: str = "",
                display: str = "") -> str | None:
    """Passwort und/oder Rolle und/oder Anzeigename eines Kontos aendern."""
    name = (username or "").strip().lower()
    if role and role not in ROLES:
        return f"Rolle muss {' oder '.join(ROLES)} sein"
    if password and len(password) < 6:
        return "Passwort muss mindestens 6 Zeichen haben"
    with _lock:
        data = _load()
        u = data["users"].get(name)
        if not u:
            return "Benutzer nicht bekannt"
        if role:
            if u["role"] == "admin" and role != "admin" and \
                    sum(1 for x in data["users"].values()
                        if x.get("role") == "admin") <= 1:
                return "den letzten Admin darf man nicht herabstufen"
            u["role"] = role
        if password:
            u["pass"] = _hash_pw(password)
        if display is not None and display != "":
            u["display"] = display.strip()[:48]
        _save(data)
    return None


def list_users() -> list[dict]:
    """Alle Konten ohne Passwort-Hashes (fuer die Verwaltungs-UI)."""
    with _lock:
        return [
            {"username": name,
             "display": u.get("display") or name,
             "role": u.get("role", "user"),
             "created": u.get("created", 0)}
            for name, u in sorted(_load()["users"].items())
        ]


def verify_login(username: str, password: str) -> str | None:
    """Login pruefen; gibt Rolle zurueck oder None (mit Login-Brute-Guard)."""
    name = (username or "").strip().lower()
    now = time.time()
    fails, until = _fail_counts.get(name, (0, 0.0))
    if until and now < until:
        log.warning("Login '%s': zu viele Fehlversuche, %d s Pause",
                    name, int(until - now))
        return None
    with _lock:
        u = _load()["users"].get(name)
    if not u or not _verify_pw(password or "", u.get("pass", "")):
        fails += 1
        wait = _FAIL_COOLDOWN if fails >= _MAX_FAILS else 0.0
        _fail_counts[name] = (fails if wait == 0 else 0, now + wait)
        if wait:
            log.warning("Login '%s': %d Fehlversuche — %d s Sperre",
                        name, _MAX_FAILS, int(wait))
        else:
            log.warning("Login fehlgeschlagen: '%s'", name)
        return None
    _fail_counts.pop(name, None)
    return u.get("role", "user")


# ── Sessions ────────────────────────────────────────────────────

def start_session(username: str) -> str:
    tok = secrets.token_urlsafe(32)
    with _lock:
        _sessions[tok] = (username, time.time() + SESSION_TTL_SEC)
    return tok


def session_user(token: str) -> dict | None:
    """{username, role, display} zu einem Session-Token, sonst None.

    Slide: gueltige Sessions werden bei Nutzung bis TTL verlaengert."""
    if not token:
        return None
    now = time.time()
    with _lock:
        ent = _sessions.get(token)
        if not ent:
            return None
        name, exp = ent
        if exp < now:
            _sessions.pop(token, None)
            return None
        _sessions[token] = (name, now + SESSION_TTL_SEC)
        u = _load()["users"].get(name)
        if not u:                      # Geloeschter Benutzer -> Session raus
            _sessions.pop(token, None)
            return None
        return {"username": name, "role": u.get("role", "user"),
                "display": u.get("display") or name}


def end_session(token: str) -> None:
    with _lock:
        _sessions.pop(token, None)
