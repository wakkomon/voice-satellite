#!/usr/bin/env python3
"""Schedule-Ebene: standalone Store/Scheduler + MasterSchedule (Fake-Manager).

Ohne Hardware/Netzwerk. Aufruf: python3 tests/test_schedule.py
"""
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("MASTER_URL", None)

from app import schedule as sched  # noqa: E402

fails = []


def ok(name, cond, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + name + (f"  [{detail}]" if (detail and not cond) else ""))
    if not cond:
        fails.append(name)


# ── standalone Store ───────────────────────────────────────────
tmp = tempfile.mkdtemp(prefix="vsched")
path = os.path.join(tmp, "schedule.json")
st = sched.Store(path)
st.load()

it = st.add("alarm", time.time() + 3600, "07:15 Uhr")
ok("store add -> id", isinstance(it.get("id"), str) and it["kind"] == "alarm")
ok("store not due", st.due() == [])
due_it = st.add("timer", time.time() + 0.1, "5 Minuten")
ok("store persist (2 items)",
   [i["kind"] for i in json.load(open(path))["items"]] == ["alarm", "timer"])
st2 = sched.Store(path)
ok("store reload (Persistenz ueberlebt)", len(st2.list()) == 2)
time.sleep(0.15)
due = st.due()
ok("store due fired+removed", [i["id"] for i in due] == [due_it["id"]]
   and st.list() == [dict(it)])
ok("store remove by_time", st.remove("alarm", by_time=it["fire_at"]) == 1)
ok("store empty", st.list() == [])
st2.load()
ok("store reload after removal", st2.list() == [])
ok("store missing file", sched.Store(path + "/nein").list() == [])

# ── standalone Scheduler (Fire-Callback) ───────────────────────
from app import config as vs_config  # noqa: E402
fired = []
ev = threading.Event()
vs_config.DATA_DIR = tmp
sched._store = None
sch_store = sched.get_store()
sch_store.add("reminder", time.time() + 0.3, "Miete zahlen")


def on_fire(item):
    fired.append(item)
    ev.set()


s = sched.Scheduler(on_fire)
s.start()
got = ev.wait(4)
s.stop()
ok("scheduler fires", got and len(fired) == 1 and fired[0]["text"] == "Miete zahlen")
ok("scheduler removed due item", sch_store.list() == [])

# ── MasterSchedule mit Fake-Manager ────────────────────────────
from master import schedule as msched  # noqa: E402


class FakeSat:
    def __init__(self, online=True, disabled=False):
        self.status = "online" if online else "offline"
        self.disabled = disabled
        self.hostname = "fakepi"
        self.ip = "192.168.82.32"
        self.web_port = 17321


class FakeManager:
    def __init__(self):
        self.satellites = {
            "aa:bb:cc:00:00:01": FakeSat(online=True),   # Pi
            "aa:bb:cc:00:00:02": FakeSat(online=True),   # App
            "aa:bb:cc:00:00:03": FakeSat(online=False),  # offline
        }
        self.queued = []

    def sat_kind(self, mac):
        return "android" if mac.endswith("02") else "pi"

    def queue_for(self, mac, text, speak=False, src="", kind="", **kw):
        self.queued.append((mac, text, kind, kw))
        return len(self.queued)

    def sat_target(self, mac):
        sat = self.satellites.get(mac)
        if sat and sat.status == "online" and not sat.disabled:
            return (mac, "https://192.168.82.32:17321")
        return None


# httpx-POST abfangen (Pi-Push)
import master.schedule as _ms  # noqa: E402
pushed = {}


class _FakeResp:
    status_code = 200


class _FakeHTTPXClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, **k):
        pushed["url"] = url
        pushed["json"] = json
        return _FakeResp()


_ms.httpx.Client = _FakeHTTPXClient

mgr = FakeManager()
ms = msched.MasterSchedule(mgr, os.path.join(tmp, "master.json"))
i1 = ms.add("aa:bb:cc:00:00:01", "alarm", time.time() + 0.2, "07:15 Uhr")
i2 = ms.add("aa:bb:cc:00:00:02", "reminder", time.time() + 0.2, "Miete zahlen")
ok("master add+persist", len(ms.list()) == 2
   and len(json.load(open(os.path.join(tmp, "master.json")))["items"]) == 2)

ms._deliver(i1)  # Pi online -> Push /notify
ok("master deliver pi -> /notify",
   pushed.get("url") == "https://192.168.82.32:17321/notify"
   and pushed.get("json", {}).get("kind") == "alarm")
ms._deliver(i2)  # App online -> Pull-Queue
ok("master deliver app -> queue_for",
   mgr.queued and mgr.queued[-1][0] == "aa:bb:cc:00:00:02"
   and mgr.queued[-1][2] == "reminder")

# offline -> Retry (fire_at in die Zukunft), dann Awerfen nach MAX_RETRIES
i3 = ms.add("aa:bb:cc:00:00:03", "alarm", time.time() + 0.2, "Test")
ms._deliver(i3)
ok("master offline retry", i3["tries"] == 1 and i3["fire_at"] > time.time())
for _ in range(msched.MAX_RETRIES):
    ms._deliver(i3)
ok("master offline dropped", i3["tries"] >= msched.MAX_RETRIES)

# delete
ok("master delete by kind+mac",
   ms.delete("aa:bb:cc:00:00:02", "reminder") >= 1)
ok("master list after delete",
   all(i["kind"] != "reminder" for i in ms.list("aa:bb:cc:00:00:02")))

# ── Aufgaben (task, wiederkehrend) ─────────────────────────────
# standalone: next_occurrence + Reschedule nach Fire
_n = time.time()
_cand = sched.next_occurrence("daily", 3, 0, now=_n)
ok("task next_occurrence daily", _cand > _n and
   time.localtime(_cand).tm_hour == 3 and
   0 < _cand - _n <= 86400 + 7200, str(_cand))
_wd = time.localtime(_cand).tm_wday
_cw = sched.next_occurrence("weekly", 9, 0, dow=(_wd + 3) % 7, now=_n)
ok("task next_occurrence weekly",
   time.localtime(_cw).tm_wday == (_wd + 3) % 7 and _cw > _n and
   _cw - _cand < 7 * 86400 + 7200, str(_cw))
ti = st.add("task", 0.0, "boote", recur="weekly", hh=4, mm=30, dow=1)
ok("task standalone add", ti["fire_at"] > _n and ti["recur"] == "weekly"
   and time.localtime(ti["fire_at"]).tm_min == 30)
st.remove("task", all_=True)
# Scheduler-Reschedule: faellige Aufgabe wird neu eingeplant, nicht geloescht
ti2 = st.add("task", 0.0, "resched", recur="daily", hh=0, mm=1)
st.items = [i for i in st.items if i["id"] != ti2["id"]]
st.add("task", time.time() - 1, "resched", recur="daily", hh=0, mm=1)
time.sleep(1.6)
_res = [i for i in st.list() if i["kind"] == "task"]
ok("task standalone reschedule",
   len(_res) == 1 and _res[0]["fire_at"] > time.time(), repr(_res))
st.remove("task", all_=True)

# master: task-Ablauf mit task_runner (Fake-LLM) -> Queue + Reschedule
mgr.queued.clear()
ms.task_runner = lambda it: {"answer": "Fertig: Boiler an", "audio_b64": None}
mi = ms.add("aa:bb:cc:00:00:02", "task", 0.0, "Boiler an",
            recur="daily", hh=0, mm=5)
ok("task master add", mi["fire_at"] > time.time() and mi["recur"] == "daily")
mi["fire_at"] = time.time() - 1
ms._deliver(mi)
_q = [q for q in mgr.queued if q[2] == "task"]
ok("task master deliver answer", len(_q) == 1 and _q[0][1] == "Fertig: Boiler an",
   repr(mgr.queued))
_ok_items = [i for i in ms.list() if i["kind"] == "task"]
ok("task master reschedule", len(_ok_items) == 1
   and _ok_items[0]["fire_at"] > time.time(), repr(_ok_items))

# update (Einstellungen-Seite): Text+Zeit ändern, naechster Termin neu
_up = _ok_items[0]
r_up = ms.update("aa:bb:cc:00:00:02", _up["id"], text="Neuer Prompt",
                 recur="weekly", hh=6, mm=45, dow=2)
ok("task update", r_up is not None and r_up["text"] == "Neuer Prompt"
   and r_up["recur"] == "weekly"
   and time.localtime(r_up["fire_at"]).tm_min == 45, repr(r_up))
# delete by id
ok("task delete by id", ms.delete("aa:bb:cc:00:00:02", "task",
                                  by_id=_up["id"]) == 1
   and not [i for i in ms.list() if i["kind"] == "task"])

print()
if fails:
    print(f"{len(fails)} FEHLGESCHLAGEN:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ALL SCHEDULE TESTS PASSED")
