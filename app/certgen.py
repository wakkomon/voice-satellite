"""Selbstsigniertes TLS-Zertifikat fuer die Web-UI (HTTPS).

Wird beim Container-Start (web.start) aufgerufen, wenn config.WEB_TLS aktiv
ist. ensure_cert() liefert (cert_path, key_path) und erzeugt genau dann ein
neues Zertifikat, wenn unter dem Zert-Verzeichnis (Default: DATA_DIR/certs =
/data/certs, via VS_CERT_DIR ueberschreibbar)
  * noch keins liegt ODER
  * das vorhandene in <= RENEW_DAYS Tagen ablaeuft.

Reine Verdichtung fuer den internen LAN-Zugriff auf die Web-UI
(https://<pi-ip>:8770). Persists im Daten-Volume und ueberlebt damit
Container-/Image-Neustarts; das Zertifikat bleibt stabil und die
Browser-Zert-Warnung ("Trotzdem fortfahren") bleibt gueltig.

Erzeugung per `openssl` (liegt im Image) statt cryptography — kein
armv7l-Quellbau-/Wheel-Risiko. Der private Key wird mit 0600 geschrieben.
"""
from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger("satellite.certgen")

RENEW_DAYS = 30          # erneuern, wenn in <= 30 Tagen ablaeuft
VALIDITY_DAYS = 825      # ~2+ Jahre Gueltigkeit


def _cfg():
    try:
        from . import config
        return config
    except Exception:
        return None


def _cert_dir() -> str:
    c = _cfg()
    d = ""
    if c is not None:
        d = (getattr(c, "VS_CERT_DIR", "") or "").strip()
    if not d:
        d = (os.environ.get("VS_CERT_DIR") or "").strip()
    if not d:
        base = getattr(c, "DATA_DIR", "/data") if c is not None else "/data"
        d = os.path.join(base, "certs")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        log.warning("Zert-Verzeichnis %s nicht anlegbar: %s", d, e)
    return d


def _paths():
    d = _cert_dir()
    return os.path.join(d, "fullchain.crt"), os.path.join(d, "privkey.key")


def _guess_source_ip():
    """Best effort: lokale Quell-IP (im Container oft die veth-IP). Die echte
    Pi-LAN-IP, gegen die der BROWSER zulaerft, kommt via VS_CERT_SANS — die
    veth-IP ist nur ein Bonus-SAN, schadet nie."""
    import socket
    for target in ("1.1.1.1", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect((target, 53))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            continue
    return None


def _sans():
    """Subject-Alternative-Names. VS_CERT_SANS (kommagetrennt) hat Vorrang,
    z. B. "IP:192.168.82.32,IP:127.0.0.1,DNS:localhost,DNS:voice-satellite"."""
    c = _cfg()
    raw = ""
    if c is not None:
        raw = (getattr(c, "VS_CERT_SANS", "") or "").strip()
    if not raw:
        raw = (os.environ.get("VS_CERT_SANS") or "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    sans = []
    ip = _guess_source_ip()
    if ip:
        sans.append(f"IP:{ip}")
    sans += ["IP:127.0.0.1", "DNS:localhost", "DNS:voice-satellite"]
    return sans


def _openssl_ok() -> bool:
    try:
        subprocess.run(["openssl", "version"], capture_output=True, timeout=10,
                       check=False)
        return True
    except Exception as e:
        log.warning("openssl nicht verfuegbar (%s) — TLS wird deaktiviert", e)
        return False


def _needs_regen(cert_path: str, key_path: str) -> bool:
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        return True
    # openssl x509 -checkend <Sekunden> -> rc 0, solange das Zert noch
    # mindestens <Sekunden> gueltig ist.
    try:
        r = subprocess.run(
            ["openssl", "x509", "-checkend", str(RENEW_DAYS * 86400),
             "-noout", "-in", cert_path],
            capture_output=True, timeout=15)
        return r.returncode != 0
    except Exception as e:
        log.warning("Bestehendes Zertifikat unpruefbar (%s) — neu", e)
        return True


def _generate(cert_path: str, key_path: str) -> None:
    sans = _sans()
    san_ext = "subjectAltName=" + ",".join(sans)
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
        "-nodes", "-days", str(VALIDITY_DAYS),
        "-keyout", key_path, "-out", cert_path,
        "-subj", "/CN=voice-satellite",
        "-addext", san_ext,
    ]
    subprocess.run(cmd, capture_output=True, timeout=90, check=True)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    log.info("Selbstsigniertes Web-Zertifikat erzeugt: %s (Gueltig %d Tage, SAN: %s)",
             cert_path, VALIDITY_DAYS, san_ext)


def ensure_cert():
    """Zertifikat sicherstellen; liefert (cert_path, key_path) oder None
    (falls openssl fehlt oder die Erzeugung fehlschlug)."""
    if not _openssl_ok():
        return None
    cert_path, key_path = _paths()
    if _needs_regen(cert_path, key_path):
        try:
            _generate(cert_path, key_path)
        except Exception as e:
            log.error("Zertifikat-Erzeugung fehlgeschlagen: %s", e)
            return None
    return cert_path, key_path
