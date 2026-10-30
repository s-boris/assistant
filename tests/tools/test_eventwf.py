"""`eventwf` (sync, status, tick, fallback, run-job) and the workflow.window tool against a temp HOME (stub jobs in
~/bin), fake windows (EVENTWF_WINDOWS) and a fake hub /wf API.
Covers: clocks only for open or upcoming windows (the hub's canonical ids), stale ones cancelled; a tick runs the jobs
due at that minute, each due job once per slot even with two windows open; done / gone; the fallback (hub down at
window start, the hub dying mid-window, the hub recovering with a stale slot: each job's slot runs once; a silent clock
with the hub up goes stale and runs). No real hub, no real job, no send."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parents[2]
EVENTWF = REPO / "scripts" / "eventwf"
TZ = ZoneInfo("Europe/Zurich")
JOBS = ("event-card", "lastcall", "battery-guard", "party", "drive-brief", "sleepin")


class FakeHub:
    """/wf/start (idempotent per id), /wf/<id>/cancel, GET /wf?prefix= (pending)."""

    def __init__(self) -> None:
        self.wf: dict[str, dict] = {}
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code: int, body: dict) -> None:
                b = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                pre = self.path.split("prefix=")[-1] if "prefix=" in self.path else ""
                rows = [{"id": k, "status": v["status"]} for k, v in hub.wf.items()
                        if v["status"] == "PENDING" and k.startswith(pre)]  # fmt: skip
                self.reply(200, {"rows": rows})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/wf/start":
                    w = hub.wf.setdefault(body["id"], {"status": "PENDING", "args": body["args"]})
                    return self.reply(200, {"id": body["id"], "status": w["status"]})
                wid = self.path.split("/")[2]
                if wid in hub.wf:
                    hub.wf[wid]["status"] = "CANCELLED"
                return self.reply(200, {"id": wid, "status": "CANCELLED"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def pending(self, kind: str = "") -> dict[str, dict]:
        return {k: v for k, v in self.wf.items() if v["status"] == "PENDING" and k.startswith(f"evw:{kind}")}


def ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())


def wid(kind: str, key: str, start: int) -> str:
    h = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"evw:{kind}:{h}:{datetime.fromtimestamp(start, UTC).strftime('%Y%m%d%H%M')}"


@pytest.fixture
def ew(tmp_path: Path):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    runs = tmp_path / "runs.log"
    slots = tmp_path / "slots.log"
    for j in JOBS:  # the registry's argv resolve ~/bin/<job> against this HOME: stubs that only log (+ the slot)
        p = home / "bin" / j
        p.write_text(f'#!/bin/sh\necho "{j} $*" >> {runs}\necho "{j} $EVENTWF_SLOT" >> {slots}\n')
        p.chmod(0o755)
    (home / "bin" / "events").write_text("#!/bin/sh\nexit 0\n")
    (home / "bin" / "events").chmod(0o755)
    tok = tmp_path / "tok"
    tok.write_text("test-token\n")
    hub = FakeHub()
    wins = tmp_path / "windows.json"
    env = dict(os.environ, HOME=str(home),
               EVENTWF_STATE=str(tmp_path / "state"), EVENTWF_WINDOWS=str(wins), EVENTWF_LOGS=str(tmp_path / "logs"),
               HUB_URL=hub.url, HUB_TOKEN_FILE=str(tok), EVENTWF_SPAWN="inline", USAGE="off",
               JOB_EVENTS=str(home / "bin" / "events"))  # fmt: skip
    env.pop("ASSISTANT_SCHEDULES", None)
    env["ASSISTANT_TZ"] = "Europe/Zurich"  # the registry names no zone: the owner's (core.todo.owner_tz)

    def run(*args: str, now: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        e = dict(env, **({"EVENTWF_NOW": str(ts(now))} if now else {}))
        p = subprocess.run([str(EVENTWF), *args], capture_output=True, text=True, env=e, timeout=60, check=False)
        if check:
            assert p.returncode == 0, p.stderr
        return p

    def windows(d: dict) -> None:
        wins.write_text(json.dumps(d))

    return {"run": run, "hub": hub, "windows": windows, "runs": runs, "slots": slots, "tmp": tmp_path, "env": env}


def ticks(ew) -> list[dict]:
    p = ew["tmp"] / "state" / "ticks.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


NIGHT = {"key": "evt1@2026-10-30", "start": ts("2026-10-30 21:00"), "end": ts("2026-10-31 04:45")}


def test_sync_starts_canonical_clocks_and_cancels_stale_ones(ew):
    later = {"key": "evt2@2026-11-02", "start": ts("2026-11-02 21:00"), "end": ts("2026-11-03 04:45")}
    closed = {"key": "evt0@2026-10-29", "start": ts("2026-10-29 20:00"), "end": ts("2026-10-31 04:45"), "closed": True}
    ew["windows"]({"night": [NIGHT, later, closed]})
    ew["run"]("sync", now="2026-10-30 20:00")
    pend = ew["hub"].pending("night")
    assert list(pend) == [wid("night", NIGHT["key"], NIGHT["start"])]  # beyond 26 h and closed: no clock
    args = next(iter(pend.values()))["args"]
    assert args["every"] == 60 and args["start"] == NIGHT["start"] and args["end"] > NIGHT["end"]
    ew["run"]("sync", now="2026-10-30 20:05")
    assert len(ew["hub"].pending("night")) == 1  # kept, not restarted
    st = json.loads(ew["run"]("status", "--json", now="2026-10-30 20:05").stdout)
    assert st["clocks"]["night"] == 1 and sorted(w["closed"] for w in st["kinds"]["night"]["windows"]) == [False, True]
    assert st["sync"]["night"] == {"started": 0, "kept": 1, "cancelled": 0}
    moved = dict(NIGHT, start=ts("2026-10-30 21:30"))
    ew["windows"]({"night": [moved]})
    ew["run"]("sync", now="2026-10-30 20:10")
    assert list(ew["hub"].pending("night")) == [wid("night", NIGHT["key"], moved["start"])]  # re-timed clock
    ew["windows"]({"night": []})
    ew["run"]("sync", now="2026-10-30 20:15")
    assert ew["hub"].pending() == {}  # the window is gone: its clock cancelled


def test_tick_runs_the_due_jobs_and_records_them(ew):
    ew["windows"]({"night": [NIGHT]})
    even, odd = ts("2026-10-30 22:00"), ts("2026-10-30 22:01")
    k = NIGHT["key"]
    r1 = json.loads(ew["run"]("tick", "night", k, "--at", str(even), "--json", now="2026-10-30 22:00").stdout)
    r2 = json.loads(ew["run"]("tick", "night", k, "--at", str(odd), "--json", now="2026-10-30 22:01").stdout)
    assert r1["decision"] == "active" and r1["ran"] == ["event-card", "lastcall"]
    assert r2["ran"] == ["battery-guard"]
    assert ew["runs"].read_text().splitlines() == ["event-card run", "lastcall run", "battery-guard run"]
    rows = ticks(ew)
    assert [r["due"] for r in rows] == [["event-card", "lastcall"], ["battery-guard"]]
    assert all("key" not in r and len(r["k"]) == 12 for r in rows)  # the key's hash only
    done = json.loads(ew["run"]("tick", "night", NIGHT["key"], "--at", str(NIGHT["end"]), "--json",
                                now="2026-10-31 04:45").stdout)  # fmt: skip
    assert done["decision"] == "done" and done["ran"] == []
    gone = json.loads(ew["run"]("tick", "night", "nope", "--at", str(even), "--json", now="2026-10-30 22:00").stdout)
    assert gone["decision"] == "gone"


def test_live_runs_each_due_job_once_per_slot(ew):
    other = dict(NIGHT, key="evt9@2026-10-30")
    ew["windows"]({"night": [NIGHT, other], "sleepin": [{"key": "2026-10-31", "start": ts("2026-10-31 06:00"),
                                                         "end": ts("2026-10-31 12:45")}]})  # fmt: skip
    at = str(ts("2026-10-30 22:00"))
    for key in (NIGHT["key"], other["key"]):  # two windows, one minute
        ew["run"]("tick", "night", key, "--at", at, now="2026-10-30 22:00")
    lines = ew["runs"].read_text().splitlines()
    assert sorted(lines) == ["event-card run", "lastcall run"], lines
    ew["run"]("tick", "sleepin", "2026-10-31", "--at", str(ts("2026-10-31 06:05")), now="2026-10-31 06:05")
    ew["run"]("tick", "sleepin", "2026-10-31", "--at", str(ts("2026-10-31 06:07")), now="2026-10-31 06:07")
    assert ew["runs"].read_text().splitlines()[2:] == ["sleepin flush"]  # */5 only


def test_registry_knows_every_window_job():
    import sys

    sys.path.insert(0, str(REPO))
    from core import schedules

    reg = schedules.load()
    src = EVENTWF.read_text()
    for name in ("event-card", "lastcall", "battery-guard", "party-tick", "drive-brief", "sleepin-flush"):
        e = reg.entry(name)
        assert e is not None and e.runs_on == "window" and f"'{name}'" in src, name
    assert reg.entry("eventwf-sync").runs_on == "hub" and "window" in schedules.RUNS_ON


def test_workflow_window_tool_parses_the_tick(tmp_path, monkeypatch):
    from tools.domains import workflow

    stub = tmp_path / "eventwf"
    ans = {"decision": "active", "ran": ["event-card"], "late_s": 2.5}
    stub.write_text(f"#!/bin/sh\necho '{json.dumps(ans)}'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("TOOLS_EVENTWF_BIN", str(stub))
    out = workflow.window(workflow.WindowIn(kind="night", key="evt1@2026-10-30", at=ts("2026-10-30 22:00")))
    assert (out.decision, out.ran, out.late_s) == ("active", ["event-card"], 2.5)
    stub.write_text("#!/bin/sh\necho 'eventwf: --at: the slot' >&2\nexit 2\n")
    with pytest.raises(Exception) as ei:
        workflow.window(workflow.WindowIn(kind="night", key="k", at=ts("2026-10-30 22:00")))
    assert getattr(ei.value, "code", "") == "invalid_args"


# ------------------------------------------------------------------------------------------------ the fallback
DEAD = "http://127.0.0.1:9"  # nothing listens: /health fails at once (the hub is down)


def fb(ew, now: float, hub: bool = True) -> dict:
    """One `eventwf fallback` (the keeper's minute) at instant now, the hub's /health up (the fake hub) or down."""
    env = dict(ew["env"], EVENTWF_NOW=str(now), HUB_URL=ew["hub"].url if hub else DEAD)
    p = subprocess.run([str(EVENTWF), "fallback", "--json"], capture_output=True, text=True, env=env, timeout=60,
                       check=False)  # fmt: skip
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def hub_tick(ew, key: str, slot: int, now: float) -> dict:
    """The hub's clock: its tick for `slot`, landing at instant now (workflow.window -> eventwf tick)."""
    env = dict(ew["env"], EVENTWF_NOW=str(now))
    p = subprocess.run([str(EVENTWF), "tick", "night", key, "--at", str(slot), "--json"], capture_output=True,
                       text=True, env=env, timeout=60, check=False)  # fmt: skip
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def ran_slots(ew) -> list[tuple[str, str]]:
    """(job, HH:MM of its slot) for every job the stubs saw run, in order."""
    p = ew["slots"]
    rows = [x.split() for x in p.read_text().splitlines()] if p.exists() else []
    return [(j, datetime.fromtimestamp(int(s), TZ).strftime("%H:%M")) for j, s in rows]


def test_fallback_runs_the_jobs_when_the_hub_is_down_at_window_start(ew):
    ew["windows"]({"night": [NIGHT]})
    s = NIGHT["start"]  # 21:00
    assert fb(ew, s - 30, hub=False)["night"]["state"] == "idle"  # before the window
    r = fb(ew, s + 1, hub=False)["night"]  # no clock ticked yet, /health fails: run the 21:00 jobs now
    assert (r["state"], r["ran"]) == ("hub-down", ["event-card", "lastcall"])
    assert fb(ew, s + 61, hub=False)["night"]["ran"] == ["battery-guard"]  # 21:01
    assert fb(ew, s + 75, hub=False)["night"]["ran"] == []  # the same minute again: each slot once
    assert fb(ew, s + 121, hub=False)["night"]["ran"] == ["event-card", "lastcall"]  # 21:02
    assert ran_slots(ew) == [("event-card", "21:00"), ("lastcall", "21:00"), ("battery-guard", "21:01"),
                             ("event-card", "21:02"), ("lastcall", "21:02")]  # fmt: skip
    rows = [r for r in ticks(ew) if r.get("src") == "fallback"]
    assert len(rows) == 3 and {r["why"] for r in rows} == {"hub-down"} and all("key" not in r for r in rows)
    assert sum(len(r["ran"]) for r in rows) == 5 and not [r for r in ticks(ew) if r.get("src") != "fallback"]
    st = json.loads(ew["run"]("status", "--json", now="2026-10-30 21:03").stdout)
    assert st["fallback"]["kinds"]["night"]["state"] == "hub-down"


def test_fallback_takes_over_when_the_hub_dies_mid_window_and_never_doubles_on_recovery(ew):
    ew["windows"]({"night": [NIGHT]})
    k, t0 = NIGHT["key"], ts("2026-10-30 22:00")
    hub_tick(ew, k, t0, t0 + 2)  # the hub's clock: each tick lands 2 s after its minute
    assert fb(ew, t0 + 61)["night"]["state"] == "healthy"
    hub_tick(ew, k, t0 + 60, t0 + 62)
    # the hub dies after its 22:01 tick: 22:02's tick never comes
    assert fb(ew, t0 + 121, hub=False)["night"]["state"] == "healthy"  # 22:02's tick is not due before 22:02:20
    r = fb(ew, t0 + 181, hub=False)["night"]  # 22:03: 22:02 had no tick and /health fails
    assert (r["state"], r["ran"]) == ("hub-down", ["event-card", "lastcall", "battery-guard"])  # 22:02 late + 22:03
    assert fb(ew, t0 + 241, hub=False)["night"]["ran"] == ["event-card", "lastcall"]  # 22:04
    r = fb(ew, t0 + 301, hub=False)["night"]  # 3 min without a tick: stale, no probe needed
    assert (r["state"], r["ran"]) == ("stale", ["battery-guard"])  # 22:05
    # the hub is back: DBOS wakes the clock with the slot it slept toward (22:02) at 22:05:20, then 22:06 on time
    late = hub_tick(ew, k, t0 + 120, t0 + 320)
    assert late["decision"] == "active" and late["ran"] == []  # 22:02's jobs ran from the fallback already
    assert fb(ew, t0 + 361)["night"]["state"] == "healthy"  # the hub ticked since 22:05: nothing from here
    assert hub_tick(ew, k, t0 + 360, t0 + 362)["ran"] == ["event-card", "lastcall"]  # 22:06
    assert hub_tick(ew, k, t0 + 300, t0 + 365)["ran"] == []  # a repeated 22:05 (a replayed step): already ran
    got = ran_slots(ew)
    assert len(got) == len(set(got)), got  # no job ran twice for one slot
    assert sorted(got) == sorted([("event-card", "22:00"), ("lastcall", "22:00"), ("battery-guard", "22:01"),
                                  ("event-card", "22:02"), ("lastcall", "22:02"), ("battery-guard", "22:03"),
                                  ("event-card", "22:04"), ("lastcall", "22:04"), ("battery-guard", "22:05"),
                                  ("event-card", "22:06"), ("lastcall", "22:06")])  # fmt: skip
    rows = [r for r in ticks(ew) if r.get("src") == "fallback"]
    assert sum(len(r["ran"]) for r in rows) == 6 and sorted(r["why"] for r in rows) == ["hub-down"] * 3 + ["stale"]


def test_fallback_waits_for_a_late_clock_then_runs_a_stale_one(ew):
    ew["windows"]({"night": [NIGHT]})
    k, s = NIGHT["key"], NIGHT["start"]
    # /health answers but no clock ticks (say the sync never started it): it waits for the clock first
    assert fb(ew, s + 1)["night"]["state"] == "lagging"
    assert fb(ew, s + 121)["night"]["state"] == "lagging"
    assert not ew["runs"].exists()  # lagging runs nothing
    r = fb(ew, s + 181)["night"]  # 21:03: no tick for 3 min: stale; it runs the latest due slots itself
    assert (r["state"], r["ran"]) == ("stale", ["event-card", "lastcall", "battery-guard"])
    late = hub_tick(ew, k, s + 180, s + 190)  # the clock starts: its 21:03 tick finds the slot claimed
    assert late["due"] == ["battery-guard"] and late["ran"] == []
    assert hub_tick(ew, k, s + 240, s + 242)["ran"] == ["event-card", "lastcall"]  # 21:04
    assert fb(ew, s + 245)["night"]["state"] == "healthy"
    assert ran_slots(ew) == [("event-card", "21:02"), ("lastcall", "21:02"), ("battery-guard", "21:03"),
                             ("event-card", "21:04"), ("lastcall", "21:04")]  # fmt: skip
    rows = [r for r in ticks(ew) if r.get("src") == "fallback"]
    assert len(rows) == 2 and {r["why"] for r in rows} == {"stale"}
