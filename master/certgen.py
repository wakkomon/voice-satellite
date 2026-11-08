"""Selbstsigniertes TLS-Zertifikat fuer den Master (HTTPS).

Wird beim Container-Start von ``master.main`` aufgerufen. ``ensure_cert()``
ergibt genau dann ein neues Zertifikat, wenn im persistierten ``VS_CERT_DIR``
(Defaults: ``VS_CONFIG_DIR/certs``)
  * noch keins liegt ODER
  * das vorhandene in <= ``RENEW_DAYS`` Tagen ablaeuft.

Reine Verschlueesselung fuer den internen LAN-Verkehr zwischen Master und
Satelliten (Pi + Android). Im 1. Schritt pruefen die Satelliten NICHT, ob das
Zertifikat zu "ihrem" Master passt; die SANs sind fuer spaetere Pinning/PKI
vorbereitet.

Persistenz: Zertifikat + Key liegen unter ``VS_CONFIG_DIR/certs`` und
ueberleben damit Container- und Image-Neustarts.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import os

log = logging.getLogger("master.certgen")

RENEW_DAYS = 30          # erneuern, wenn in <= 30 Tagen ablaeuft
VALIDITY_DAYS = 825      # ~2+ Jahre Gueltigkeit

# Standard-SANs (internes LAN). Erweitern via VS_CERT_SANS (kommagetrennt),
# z. B. "IP:192.168.82.24,DNS:dev,IP:10.0.0.5".
_DEFAULT_SANS = [
    "IP:192.168.82.24",      # dev-Server (Master-Host)
    "IP:127.0.0.1",
    "DNS:localhost",
    "DNS:voice-satellite-master",
    "DNS:dev",
]


def _cert_dir() -> str:
    d = os.environ.get("VS_CERT_DIR", "").strip()
    if not d:
        try:
            from .satconfig import CONFIG_DIR
        except Exception:
            CONFIG_DIR = "/data/config"
        d = os.path.join(CONFIG_DIR, "certs")
    os.makedirs(d, exist_ok=True)
    return d


def _paths():
    d = _cert_dir()
    return os.path.join(d, "fullchain.crt"), os.path.join(d, "privkey.key")


def _sans():
    raw = os.environ.get("VS_CERT_SANS", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return list(_DEFAULT_SANS)


def _expiry(cert):
    """not_valid_after (UTC-aware). cryptography>=42: *_utc, sonst naive UTC."""
    attr = getattr(cert, "not_valid_after_utc", None)
    if attr is not None:
        return attr
    return cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)


def _needs_regen(cert_path: str, key_path: str) -> bool:
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        return True
    try:
        from cryptography import x509
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        left = (_expiry(cert) - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        if left < RENEW_DAYS * 86400:
            log.info("Zertifikat laeuft bald ab (< %d Tage) — neu", RENEW_DAYS)
            return True
        return False
    except Exception as e:
        log.warning("Bestehendes Zertifikat unlesbar (%s) — neu", e)
        return True


def _atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _generate(cert_path: str, key_path: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "voice-satellite-master")])
    now = _dt.datetime.now(_dt.timezone.utc)
    san = []
    for s in _sans():
        s = s.strip()
        if s.upper().startswith("IP:"):
            san.append(x509.IPAddress(ipaddress.ip_address(s[3:].strip())))
        else:
            dns = s[4:].strip() if s.upper().startswith("DNS:") else s
            if dns:
                san.append(x509.DNSName(dns))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    _atomic_write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _atomic_write(key_path, priv)
    os.chmod(key_path, 0o600)
    log.info("Selbstsigniertes Zertifikat erzeugt: %s (Gueltig %d Tage, SAN: %s)",
             cert_path, VALIDITY_DAYS, _sans())


def ensure_cert() -> tuple[str, str]:
    """Zertifikat sicherstellen; liefert (cert_path, key_path)."""
    cert_path, key_path = _paths()
    if _needs_regen(cert_path, key_path):
        _generate(cert_path, key_path)
    return cert_path, key_path
