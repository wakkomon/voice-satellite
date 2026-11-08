#!/usr/bin/env python3
"""App-Update-Verteilung: Versionsvergleich, Hinweis-Logik, Pfad-Schutz.

Reine Stdlib — laeuft lokal wie im Master-Image. APPS_DIR zeigt auf ein
Temp-Verzeichnis (nie die produktive Ablage).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

APPS = tempfile.mkdtemp(prefix="vsapps")
os.environ["APPS_DIR"] = APPS

from master import appupdate

fails = []


def ok(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails.append(label)


def put(name, content=b"x"):
    with open(os.path.join(APPS, name), "wb") as f:
        f.write(content)


# Ablage leer -> keine Infos
ok("leere Ablage: latest None", appupdate.latest() is None)
ok("leere Ablage: list []", appupdate.list_apks() == [])
ok("leere Ablage: kein Hinweis", appupdate.update_hint("2.4.20", "android") is None)

put("voice-satellite-app-2.4.9.apk")
put("voice-satellite-app-2.4.10.apk")
put("voice-satellite-app-2.4.21.apk")
put("stoesstimmen.apk")            # passt nicht ins Namensschema
put("voice-satellite-app-x.apk")   # keine Version im Namen

top = appupdate.latest()
ok("latest nach Version (2.4.21, nicht lexikaliisch 2.4.9)",
   top and top["version"] == "2.4.21")
ok("Stoesnamen ignoriert", len(appupdate.list_apks()) == 3)

# Hinweis nur an Android, nur bei strikt aelterer gemeldeter Version
h = appupdate.update_hint("2.4.20", "android")
ok("Hinweis an 2.4.20-App", h and h["version"] == "2.4.21"
   and h["file"] == "voice-satellite-app-2.4.21.apk"
   and h["url"] == "/api/app/download?file=voice-satellite-app-2.4.21.apk"
   and h["size"] == 1)
ok("kein Hinweis fuer aktuelle Version", appupdate.update_hint("2.4.21", "android") is None)
ok("kein Hinweis fuer neuere App", appupdate.update_hint("2.5.0", "android") is None)
ok("kein Pi-Hinweis", appupdate.update_hint("2.0.15", "pi") is None)
ok("kein Hinweis ohne Version", appupdate.update_hint("", "android") is None)
ok("Pi-artige gemeldete Version ignoriert (kind fehlt)",
   appupdate.update_hint("1.0", "") is None)

# Versionsvergleich numerisch, nicht zeichenketten-basiert
ok("2.4.9 < 2.4.10 numerisch", appupdate.parse_version("2.4.9") < appupdate.parse_version("2.4.10"))

# Download-Aufloesung: kanonischer Name ok, Traversal/Fremdnamen abgelehnt
ok("resolve: kanonischer Name", appupdate.resolve_apk("voice-satellite-app-2.4.10.apk")
   == os.path.join(APPS, "voice-satellite-app-2.4.10.apk"))
ok("resolve: Traversal abgelehnt", appupdate.resolve_apk("../../etc/passwd") is None)
ok("resolve: Pfadanteil normalisiert auf basename",
   appupdate.resolve_apk("a/b/voice-satellite-app-2.4.10.apk")
   == os.path.join(APPS, "voice-satellite-app-2.4.10.apk"))  # basename -> kanonisch ok
ok("resolve: fremder Name abgelehnt", appupdate.resolve_apk("stoesnamen.apk") is None)
ok("resolve: fehlende Datei None", appupdate.resolve_apk("voice-satellite-app-9.9.9.apk") is None)

print()
if fails:
    print(f"FAILS: {len(fails)}")
    sys.exit(1)
print("FAILS: none")
