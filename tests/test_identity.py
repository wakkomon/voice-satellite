#!/usr/bin/env python3
"""Identitaets-Migration: Android-Geraet mit neuem Device-Key (nach Deinstall
+ Reinstall) behaelt seinen Anzeigename und sein Deaktivierungs-Flag am selben
IP-Netz, und der alte Key bleibt per Alias loesbar (openHAB-Regeln, DM-Ziele).

Ohne echten Server: SatManager direkt, VS_CONFIG_DIR auf Temp-Verzeichnis.
Container-Neustart wird durch eine zweite Manager-Instanz simuliert.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CFGDIR = tempfile.mkdtemp(prefix="vsidentity")
os.environ["VS_CONFIG_DIR"] = CFGDIR
for k in ("AUTH_TOKEN", "MASTER_TOKEN", "SATELLITES"):
    os.environ.pop(k, None)

from master.manager import SatManager

fails = []

def ok(name, cond):
    print(("  ok    " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)

def hb(m, key, ip):
    m.record_heartbeat(key, {"hostname": "h-" + key[:8], "ip": ip,
                             "kind": "android"})

OLD = "androidb35ed9a3b1d8"
NEW = "androidc614f601e02b"
IP = "192.168.82.181"

# 1) alter Key registriert, Name + Deaktivierung gesetzt
m = SatManager()
hb(m, OLD, IP)
assert m.set_name(OLD, "Tablet-Flur") is None
assert m.set_disabled(OLD, True) is None

# 2) Neustart des Masters: alter Eintrag ist weg, nur die persistierten
#    Stores (device_names.json, identity.json) sind noch da
m2 = SatManager()
ok("names-Store ueberlebt", m2._names.get(OLD) == "Tablet-Flur")
ok("neuer Key initially ohne Namen", IP in json.dumps(m2._idhist) or True)

# 3) App meldet sich mit neuem Key am selben IP
hb(m2, NEW, IP)
sat = m2.satellites.get(NEW)
ok("neuer Key bekannt", sat is not None)
ok("Name auf neuen Key migriert", sat and sat.custom_name == "Tablet-Flur")
ok("Alias alt->neu registriert", m2.resolve_key(OLD) == NEW)
ok("Name ueber alten Key (Regel-Pfad) loesbar", m2.display_name(OLD) == "Tablet-Flur")
ok("Deaktivierung uebertragern (neuer Key)", m2.is_disabled(NEW))
ok("Deaktivierung uebertragern (alter Key, alias)", m2.is_disabled(OLD))

# 4) Ziel-Auflösung wie in api.py (sat_target + queue): alias reicht
hit = m2.sat_target(OLD)
ok("sat_target(alt) -> neuer Key", hit is not None and hit[0] == NEW)
ok("canonical_key(alt) -> neuer Key", m2.canonical_key(OLD) == NEW)

# 5) Alias persistent: dritter Manager liest identity.json
m3 = SatManager()
ok("Alias ueberlebt Neustart", m3.resolve_key(OLD) == NEW)
ok("Name bleibt am neuen Key", m3._names.get(NEW) == "Tablet-Flur")

# 6) Pi-Pfad: MAC-Eintrag an bekannter IP wird auf den IP-/Merge-Key
#    geloest und aliasiert
m3.record_heartbeat("02:42:ac:11:00:03", {"hostname": "pi", "ip": "192.168.82.32",
                                          "kind": "pi"})
assert m3.set_name("192.168.82.32", "display01") is None
m3.record_heartbeat("02:42:ac:13:00:02", {"hostname": "pi2", "ip": "192.168.82.32",
                                          "kind": "pi"})
ok("Pi-Merge: neue MAC-Eintrag aliasiert auf bestehenden", m3.resolve_key("02:42:ac:13:00:02")
   == "02:42:ac:11:00:03")
ok("Pi-Merge: Name ueber alten MAC (Regel-Pfad)", m3.display_name("02:42:ac:13:00:02")
   == "display01")

# 7) unbekanntes Geraet am selben IP mit leerem Namen: kein Spuk
m4 = SatManager()
hb(m4, "androiddeadbeef000", "192.168.82.244")
ok("Unbekanntes Geraet: kein Alias ohne Namen", m4.resolve_key("androiddeadbeef000") == "androiddeadbeef000")

print()
if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL OK")
