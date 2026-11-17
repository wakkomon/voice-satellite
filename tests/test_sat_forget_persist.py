"""Funktionstest: Persistierung der Satelliten-Liste + forget().

Laedt master.manager direkt (Stdlib-only auf Modulebene), SatManager mit
isoliertem VS_CONFIG_DIR + HOME. Testet: _restore_sats nach 'Rebuild',
forget saeubert RAM UND alle JSON-Stores, Alias-Ketten, Unbekannt -> None.
"""
import json
import os
import sys
import tempfile

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp(prefix="vs_sat_forget_")
os.environ["VS_CONFIG_DIR"] = os.path.join(tmp, "config")
os.environ["HOME"] = tmp  # ~/.vs-ssh-Key-Kopie landet im Sandkasten

from master import manager as mgrmod  # noqa: E402

m = mgrmod.SatManager()

# --- Ausgangslage: ein Heartbeat-Sat + Android-App mit Historie ---------
m.satellites["aa:bb:cc:dd:ee:ff"] = mgrmod.Satellite(
    mac="aa:bb:cc:dd:ee:ff", ip="192.168.82.32", hostname="vpntest",
    status="online")
m.satellites["android+abc123"] = mgrmod.Satellite(
    mac="android+abc123", ip="192.168.82.50", hostname="pixel",
    kind="android", status="online")
m._idhist["aa:bb:cc:dd:ee:ff"] = "192.168.82.32"
m._idhist["android+abc123"] = "192.168.82.50"
m._idhist["android+old999"] = "192.168.82.50"   # geloeschter Alt-Key in Historie
m._aliases["android+old999"] = "android+abc123"
m._names["aa:bb:cc:dd:ee:ff"] = "Wohnzimmer-Pi"
m._disabled.add("android+abc123")
m._save_identity(); m._save_names(); m._save_disabled()
m.queue_for("android+abc123", "test", speak=False)

# --- 'Rebuild': frischer Manager aus denselben Dateien -------------------
m2 = mgrmod.SatManager()
assert m2.satellites == {}, "frischer Manager duerfte nichts im RAM haben"
m2._restore_sats()
keys = set(m2.satellites)
assert "aa:bb:cc:dd:ee:ff" in keys and "android+abc123" in keys, keys
assert "android+old999" in keys  # Alias-Alt-Key bleibt als Historie erhalten
s = m2.satellites["android+abc123"]
assert s.ip == "192.168.82.50" and s.status == "offline" and s.disabled
assert m2.satellites["aa:bb:cc:dd:ee:ff"].custom_name == "Wohnzimmer-Pi"
print("PASS restore:keys + ip/name/disabled/status offline")

# --- forget auf Alias-Ziel loest auch Alt-Key + Persistenz --------------
gone = m2.forget("android+OLD999")  # gross/klein + Alias-Auflsung
assert gone == "android+abc123", gone
assert "android+abc123" not in m2.satellites and "android+old999" not in m2.satellites
assert "android+abc123" not in m2._idhist and "android+old999" not in m2._idhist
assert "android+old999" not in m2._aliases
assert "android+abc123" not in m2._disabled and "android+abc123" not in m2._pending
on_disk = json.load(open(m2._disabled_path, encoding="utf-8"))
assert "android+abc123" not in on_disk["disabled"], on_disk
hist = json.load(open(m2._identity_path, encoding="utf-8"))
assert "android+abc123" not in hist["history"]
assert "android+old999" not in hist["aliases"]
print("PASS forget: RAM+Stores+Dateien sauber, Alias-Aufloesung, Gross/Klein")

# --- Restdaten bleiben erhalten + Unbekannt -> None ---------------------
assert "aa:bb:cc:dd:ee:ff" in m2.satellites
assert m2.forget("gibt:es:nicht") is None
assert m2._names.get("aa:bb:cc:dd:ee:ff") == "Wohnzimmer-Pi"
print("PASS untouched-keys + None fuer unbekannt")

# --- forget eines reinen Historie-Keys (nicht im RAM) -------------------
assert m2.forget("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"
assert "aa:bb:cc:dd:ee:ff" not in m2._names
print("PASS forget ohne RAM-Eintrag moeglich")


# ── statische Checks: Route/Handler/UI-Verkabelung ─────────────────
api_src = pathlib.Path(__file__).resolve().parents[1] / "master/api.py"
api = api_src.read_text(encoding="utf-8")
assert 'Route("/api/satellite/{mac}/delete", _sat_delete, methods=["POST"])' in api
assert "async def _sat_delete" in api
ui = (pathlib.Path(__file__).resolve().parents[1] / "master/static/index.html").read_text(encoding="utf-8")
assert "/delete" in ui and "delSat(" in ui and "Löschen</a>" in ui
print("PASS static route+ui wiring")

print("ALL OK")
