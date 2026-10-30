"""webtask.run's state machine (hubwf.run_next and friends), pure. hubwf imports DBOS and SQLAlchemy at the top,
so this loads it with stand-ins for both (their decorators are identities): only the pure helpers run here; the
workflow itself runs against a real hubd + Postgres in tests/hubstore/test_webtask_run.py."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def wf():
    fake_dbos = types.ModuleType("dbos")

    class DBOS:  # noqa: D401  (a stand-in)
        @staticmethod
        def step(*a, **kw):
            return lambda f: f

        @staticmethod
        def workflow(*a, **kw):
            return lambda f: f

    fake_dbos.DBOS = DBOS
    fake_dbos.SetWorkflowID = object
    saved = {k: sys.modules.get(k) for k in ("dbos", "sqlalchemy")}
    sys.modules["dbos"] = fake_dbos
    sys.modules["sqlalchemy"] = types.ModuleType("sqlalchemy")
    try:
        spec = importlib.util.spec_from_file_location("hubwf_p36", REPO / "packages/hub/hubwf.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


NONCE = "ab" * 16


def args(wf, **kw):
    return wf.webtask_run_args({"task": "wa1a2b3c", "kind": "order", **kw})


def test_args_defaults_and_refusals(wf):
    a = args(wf)
    assert a["pending_ttl"] == 900 and a["approved_ttl"] == 600 and a["idle"] == 2700 and a["revive"] == 7200
    assert a["notify"] == ["stale", "expired"] and a["dry"] is False
    assert args(wf, notify=["failed", "stale"])["notify"] == ["stale", "failed"]
    for bad in (
        {"task": "x y"},
        {"task": "wa1", "kind": "nope"},
        {"task": "wa1", "dry": "yes"},
        {"task": "wa1", "idle": 5},
        {"task": "wa1", "notify": ["done"]},
        {"task": "wa1", "pending_ttl": True},
    ):
        with pytest.raises(ValueError):
            wf.webtask_run_args(bad)
    wf.check_args("webtask.run", {"task": "wa1"})
    assert "webtask.run" in wf.NAMES and "webtask.run" in wf.WORKFLOWS


def test_happy_path_lease_steps_stop_approve_commit_done(wf):
    a = args(wf)
    s = wf.run_init(1000.0)
    s = wf.run_next(s, {"op": "lease", "sid": "ab-12cd34"}, 1001, a)
    s = wf.run_next(s, {"op": "step", "n": 1}, 1002, a)
    s = wf.run_next(s, {"op": "step", "n": 2}, 1003, a)
    assert s["lease"] == "ab-12cd34" and s["steps"] == 2 and s["phase"] == "running"
    s = wf.run_next(s, {"op": "stop", "stop": 1, "nonce": NONCE}, 1010, a)
    assert s["phase"] == "pending" and s["nonce"] == NONCE and s["deadline"] == 1910
    assert wf.run_next(s, {"op": "commit", "stop": 1}, 1011, a) is s  # no commit before his ✅
    s = wf.run_next(s, {"op": "decision", "decision": "approved", "via": "button", "stop": 1}, 1020, a)
    assert s["phase"] == "approved" and s["deadline"] == 1620 and s["via"] == "button"
    s = wf.run_next(s, {"op": "commit", "stop": 1}, 1030, a)
    assert s["phase"] == "executing" and s["committed"] is True
    s = wf.run_next(s, {"op": "done", "ok": True, "committed": True}, 1040, a)
    assert s["outcome"] == "done" and s["phase"] == "done"
    assert wf.run_next(s, {"op": "abort"}, 1050, a) is s  # an ended task stays ended
    r = wf.run_receipt(s, a, "webtask:wa1a2b3c", 40.0)
    assert r["kind"] == "workflow.webtask" and r["delivery"] == "mute" and r["idem_key"] == "wf:webtask:wa1a2b3c"
    assert r["payload"]["committed"] is True and r["payload"]["stops"] == 1 and "(committed)" in r["subject"]


def test_replaced_stop_and_stale_decisions(wf):
    a = args(wf)
    s = wf.run_next(wf.run_init(0.0), {"op": "stop", "stop": 1, "nonce": NONCE}, 10, a)
    s = wf.run_next(s, {"op": "decision", "decision": "replaced", "stop": 1}, 20, a)
    assert s["phase"] == "pending"
    s = wf.run_next(s, {"op": "stop", "stop": 2, "nonce": "cd" * 16}, 30, a)
    assert s["stop"] == 2 and s["nonce"] == "cd" * 16
    assert wf.run_next(s, {"op": "decision", "decision": "approved", "stop": 1}, 40, a) is s  # the old card's
    assert wf.run_decided(s, "declined", "hub approval", 50, a)["outcome"] == "declined"
    assert wf.run_decided(s, "stopped", "owner", 50, a)["outcome"] == "aborted"


def test_deadlines_expire_stale_revive(wf):
    a = args(wf, pending_ttl=60, approved_ttl=60, idle=100, revive=200)
    s = wf.run_next(wf.run_init(0.0), {"op": "stop", "stop": 1}, 10, a)
    assert wf.run_due(s, a) == 70 and wf.run_next(s, None, 69, a) is s
    assert wf.run_next(s, None, 70, a)["outcome"] == "expired"
    s = wf.run_next(s, {"op": "decision", "decision": "approved"}, 20, a)
    assert wf.run_next(s, None, 80, a)["outcome"] == "expired"  # approved but never used
    r = wf.run_init(0.0)
    r = wf.run_next(r, {"op": "wait", "until": 500}, 10, a)  # his vault ✅ wait pauses the idle clock
    assert wf.run_due(r, a) == 600 and wf.run_next(r, None, 599, a) is r
    r = wf.run_next(r, {"op": "wait"}, 300, a)  # wait over: the idle clock restarts now
    assert wf.run_due(r, a) == 400
    r = wf.run_next(r, None, 400, a)
    assert r["phase"] == "stale" and r["outcome"] is None and r["deadline"] == 600
    assert wf.run_next(r, {"op": "step"}, 410, a) is r  # a stale task takes no steps until revived
    r2 = wf.run_next(r, {"op": "revive"}, 420, a)
    assert r2["phase"] == "running" and r2["revived"] == 1 and wf.run_due(r2, a) == 520
    assert wf.run_next(r, None, 600, a)["outcome"] == "stale"
    a0 = args(wf, idle=100, revive=0)
    assert wf.run_next(wf.run_init(0.0), None, 100, a0)["outcome"] == "stale"


def test_continue_and_failure_and_junk(wf):
    a = args(wf, notify=["failed"])
    s = wf.run_next(wf.run_init(0.0), {"op": "stop", "stop": 1}, 1, a)
    s = wf.run_next(s, {"op": "decision", "decision": "approved"}, 2, a)
    s = wf.run_next(s, {"op": "done", "ok": True, "cont": True, "committed": True}, 3, a)
    assert s["phase"] == "running" and s["committed"] is True and s["outcome"] is None
    for junk in (None, "x", {"op": "nope"}, {"op": "stop", "stop": "1"}, {"op": "lease", "sid": "a b"}):
        nxt = wf.run_next(s, junk, 4, a) if junk is not None else s
        assert nxt["phase"] == "running"
    assert wf.run_next(s, {"op": "lease", "sid": "bad sid!"}, 4, a)["lease"] == "?"
    f = wf.run_next(s, {"op": "done", "ok": False}, 5, a)
    assert f["outcome"] == "failed"
    r = wf.run_receipt(f, a, "webtask:x", 5)
    assert r["delivery"] == "beat" and "webagent status" in r["next"]
    view = wf.run_view(f)
    assert set(view) == {"phase", "steps", "stop", "lease", "committed", "revived", "deadline", "outcome"}
