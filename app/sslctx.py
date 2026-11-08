"""TLS-Optionen fuer ausgehende HTTP-Requests des Satelliten.

Seit der Master-HTTPS-Haertung spricht der Satellit den Master ueber
selfsigned-TLS an. Im 1. Schritt prueft der Satellit NICHT, ob das
Zertifikat zu "seinem" Master passt (reine Verschlueesselung); dafuer wird
global ein nicht-validierender SSL-Context installiert. plain-http URLs
bleiben davon unbeeinflusst (der Context greift nur bei https://).
"""
from __future__ import annotations

import ssl
import urllib.request

_installed = False


def _install() -> None:
    global _installed
    if _installed:
        return
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    )
    _installed = True


# Beim Import aktivieren, damit JEDER urllib.request.urlopen-Call des
# Satelliten (http.py, heartbeat.py, playback.py, master_index.py, …)
# automatisch den Context nutzt.
_install()
