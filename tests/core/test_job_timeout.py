"""A hub schedule waits for its job's own limit (registry timeout, at most 3600 s) instead of the tool's 600 s.
Brain side: workflow.job ends the job by the caller's timeout_s when that is shorter than the registry's; the
registry refuses a hub job over the hub's cap; workflow.schedule sends the job's limit and refuses to put a job longer
than 600 s live on a hub that does not wait that long. Temp HOMEs and a faked hub only."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from core import schedules, watchjobs  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    for k in ("HUB_URL", "HUB_SCHEMA"):
        monkeypatch.delenv(k, raising=False)
    return h


def _hub_module(name: str) -> Any:
    """A hub file read as text (hubwf needs DBOS, which the brain's venv lacks): its top-level int constants."""
    import ast

    tree = ast.parse((REPO / "packages" / "hub" / f"{name}.py").read_text())
    out: dict[str, Any] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, int):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n.value.value
    return out


def test_caps_agree_and_every_hub_job_fits():
    assert schedules.HUB_TIMEOUT_MAX == _hub_module("hubwfapi")["JOB_TIMEOUT_MAX"] == 3600
    assert _hub_module("hubwf")["JOB_TIMEOUT_MAX"] == schedules.HUB_TIMEOUT_MAX
    assert _hub_module("hubwf")["JOB_LIMIT"] == schedules.HUB_JOB_LIMIT == 600
    reg = schedules.load()
    assert all(e.timeout <= schedules.HUB_TIMEOUT_MAX for e in reg.jobs.values() if e.runs_on == "hub")
    # the long collectors fit under the cap and above the old 600 s limit (the reason they waited for per-job limits)
    for n in ("evals", "self-review", "recall-index", "recall-embed", "scraper-canary", "memory2-extract"):
        assert schedules.HUB_JOB_LIMIT < reg.entry(n).timeout <= schedules.HUB_TIMEOUT_MAX, n  # type: ignore[union-attr]


def test_registry_refuses_a_hub_job_over_the_cap(tmp_path):
    t = tmp_path / "s.toml"
    body = (
        'schema = 1\ntz = "UTC"\n[jobs.x]\nline = "x"\ncron = "0 3 * * *"\nclass = "M"\nqueue = "slow"\n'
        'where = "brain"\nruns_on = "{on}"\nargv = ["~/bin/x"]\ntimeout = {t}\ncron_line = "0 3 * * * x"\n'
    )
    t.write_text(body.format(on="cron", t=4000))
    assert schedules.load(t).entry("x").timeout == 4000  # type: ignore[union-attr]  (cron: its own limit)
    t.write_text(body.format(on="hub", t=4000))
    with pytest.raises(schedules.RegistryError, match="at most 3600"):
        schedules.load(t)
    t.write_text(body.format(on="hub", t=3600))
    assert schedules.load(t).entry("x").runs_on == "hub"  # type: ignore[union-attr]


def _sleeper(home: Path, name: str) -> None:
    s = home / "bin" / name
    s.write_text("#!/bin/sh\nexec sleep 20\n")
    s.chmod(0o755)


def test_job_ends_by_the_callers_timeout(home):
    """The hub passes how long it waits: the job is killed by then, not at its (longer) registry limit."""
    from tools.core import ToolError
    from tools.domains import workflow as W

    _sleeper(home, "watch")
    assert watchjobs.runnable("watch").timeout_s > 1
    t0 = time.monotonic()
    with pytest.raises(ToolError) as e:
        W.job(W.JobIn(name="watch", mode="live", timeout_s=1))
    assert e.value.code == "failed" and "no exit after 1 s" in e.value.message
    assert time.monotonic() - t0 < 10


def test_registry_limit_wins_when_shorter(home, monkeypatch):
    from dataclasses import replace

    from tools.core import ToolError
    from tools.domains import workflow as W

    _sleeper(home, "watch")
    short = replace(watchjobs.runnable("watch"), timeout_s=1)
    monkeypatch.setattr(watchjobs, "runnable", lambda name: short)
    with pytest.raises(ToolError) as e:
        W.job(W.JobIn(name="watch", mode="live", timeout_s=3600))
    assert "no exit after 1 s" in e.value.message


def test_timeout_s_bounds():
    from pydantic import ValidationError
    from tools.domains import workflow as W

    for bad in (0, schedules.HUB_TIMEOUT_MAX + 1):
        with pytest.raises(ValidationError):
            W.JobIn(name="fx-watch", mode="live", timeout_s=bad)
    assert W.JobIn(name="fx-watch", timeout_s=schedules.HUB_TIMEOUT_MAX).timeout_s == 3600
    assert W.JobIn(name="fx-watch").timeout_s is None


class FakeHub:
    def __init__(self, engine: dict[str, Any]) -> None:
        self.engine, self.calls = engine, []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 20.0) -> dict:
        self.calls.append((method, path, body))
        if path == "/wf/engine":
            return self.engine
        if method == "POST" and path == "/wf/schedules":
            assert body is not None
            return {"name": body["name"], "job": body["job"], "mode": body["mode"], "cron": body["cron"],
                    "status": "ACTIVE", "queue": body.get("queue"), "timeout": body.get("timeout")}  # fmt: skip
        return {"rows": []}


@pytest.fixture
def tool(home, monkeypatch):
    from tools.domains import workflow as W

    monkeypatch.setattr(W, "_changed", lambda *a, **k: None)
    return W


def test_schedule_sends_the_jobs_limit(tool, monkeypatch):
    hub = FakeHub({"state": "on", "job_timeout_max": 3600})
    monkeypatch.setattr(tool, "_hub", hub)
    got = tool.schedule(tool.ScheduleIn(job="evals", state="live", why="p33 test"))
    posted = [b for m, p, b in hub.calls if p == "/wf/schedules"]
    assert posted and posted[0]["timeout"] == schedules.load().entry("evals").timeout == 1800  # type: ignore[union-attr]
    assert posted[0]["name"] == "job:evals" and got.timeout == 1800


def test_long_job_live_needs_a_hub_that_waits(tool, monkeypatch):
    from tools.core import ToolError

    old = FakeHub({"state": "on", "version": "hubwf-1"})  # a hub release without job_timeout_max
    monkeypatch.setattr(tool, "_hub", old)
    with pytest.raises(ToolError) as e:
        tool.schedule(tool.ScheduleIn(job="evals", state="live", why="p33 test"))
    assert e.value.code == "unavailable" and "600" in e.value.message
    assert not [c for c in old.calls if c[0] == "POST"]  # nothing created
    # shadow only says what it would run: fine on any hub (a later `live` re-sends the limit)
    tool.schedule(tool.ScheduleIn(job="evals", state="shadow", why="p33 test"))
    # a job within 600 s goes live on the old hub as before
    tool.schedule(tool.ScheduleIn(job="events-tg-metrics", state="live", why="p33 test"))
    assert [b["job"] for m, p, b in old.calls if m == "POST"] == ["evals", "events-tg-metrics"]
    short = FakeHub({"state": "on", "job_timeout_max": 900})
    monkeypatch.setattr(tool, "_hub", short)
    with pytest.raises(ToolError):
        tool.schedule(tool.ScheduleIn(job="evals", state="live", why="p33 test"))


def test_live_schedule_marks_the_board_only_for_live(tool, monkeypatch, home):
    hub = FakeHub({"state": "on", "job_timeout_max": 3600})
    monkeypatch.setattr(tool, "_hub", hub)
    log = home / "mark.log"
    (home / "bin").mkdir(exist_ok=True)
    stub = home / "bin" / "board"
    stub.write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
    stub.chmod(0o755)
    tool.schedule(tool.ScheduleIn(job="events-tg-metrics", state="shadow", why="p33 test"))
    assert not log.exists()
    tool.schedule(tool.ScheduleIn(job="events-tg-metrics", state="live", why="p33 test"))
    assert log.read_text() == "poke live events-tg-metrics\n"
    stub.write_text("#!/bin/sh\nexit 9\n")
    assert tool.schedule(tool.ScheduleIn(job="events-tg-metrics", state="live", why="p33 test")).status == "ACTIVE"
