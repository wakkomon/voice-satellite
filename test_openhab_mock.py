#!/usr/bin/env python3
"""P6-Mocktest: openHAB-Fehlerpfade ohne echtes Schalten.
Faked http-Layer + Fake-Item-Cache gegen router.try_openhab()."""
import sys
sys.path.insert(0, ".")

import app.http as http
import app.router as r

class FakeCache:
    def __init__(self):
        self.items = []
    def get(self):
        return self.items
    def refresh(self):
        pass

r._cache = FakeCache()
r._cache.items = [{"name": "FlurLampe", "label": "FlurLampe", "metadata": {}}]

# 1) Status-Pfad: GET wirft 'URI is null'
http.get_text = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 400: URI is null"))
got = r.try_openhab("ist die flurlampe da")
assert got == "Bei der Verbindung zum Smarthome ist ein fehler aufgetreten.", repr(got)
print("OK  Status-Fehlerpfad ->", repr(got))

# 2) Schalt-Pfad: POST wirft denselben Rohfehler
def post_boom(url, value, token=None, timeout=10):
    assert value == "ON", value
    raise RuntimeError("HTTP 400: URI is null")
http.post_raw = post_boom
got = r.try_openhab("flur lampe an")
assert got == "Bei der Verbindung zum Smarthome ist ein fehler aufgetreten.", repr(got)
print("OK  Schalt-Fehlerpfad ->", repr(got))

# 3) URL-Encodierung: '/' im Item-Namen muss escaped werden
from urllib.parse import quote
assert quote("Flur/Lampe", safe="") == "Flur%2FLampe"
url = f"http://x/{quote('Flur/Lampe', safe='')}"
assert url.endswith("/Flur%2FLampe"), url
print("OK  URL-Encodierung   ->", url)

# 4) Erfolgspfad (sanity): POST ok -> Bestaetigung statt Rohfehler
http.post_raw = lambda *a, **k: True
got = r.try_openhab("flur lampe an")
assert got == "FlurLampe ist an.", repr(got)
print("OK  Erfolgspfad      ->", repr(got))

print("ALL P6 MOCK TESTS PASSED")
