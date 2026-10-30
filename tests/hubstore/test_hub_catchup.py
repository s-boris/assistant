"""Hub schedule catch-up with a real hubd + DBOS on a throwaway Postgres (hubfix), a fake brain executor recording
each workflow.job call: a hub down across a daily tick runs that tick once after the restart (its own slot, the
scheduler's workflow id), a second restart doesn't run it again; catch_up false, a schedule made (or edited) after
its tick and an every-2-s one never catch up; a tick missed longer ago than the grace is skipped; and `hub wf show`
reads a run id with "+hh:mm".
Daily crons here are DBOS's six fields (seconds first) in UTC, so a "daily" tick lands seconds from now."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import hubfix
import pytest
from test_workflows import CHUNK, AppHubd, FakeExecutor, app_tree, wait_for


@pytest.fixture(scope="module")
def cenv(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("catchup")
    pg = hubfix.Pg(root).start()
    app = app_tree(root)
    gw_port = hubfix.free_port()
    extra = {"HUB_GW_PORT": str(gw_port), "HUB_GW_STATE": str(root / "gw"), "HUB_WF_CHUNK": CHUNK,
             "HUB_WF_SCHED_POLL": "1", "HUB_WF_NOTIFY_POLL": "0.2", "HUB_RELEASE_DIR": str(root)}  # fmt: skip
    h = AppHubd(root, pg, hubfix.vendor_tree(root / "vendor"), app, extra).start()
    ex = FakeExecutor(f"http://127.0.0.1:{gw_port}", h.tokens["brain"])
    code, r = h.call("POST", "/wf/engine", {"on": True, "why": "catch-up test"})
    assert code == 200, r
    wait_for(lambda: h.call("GET", "/wf/engine")[1].get("state") == "on", 60)
    yield {"hub": h, "exec": ex}
    ex.stop.set()
    h.stop()
    pg.discard()


def daily(at: datetime) -> str:
    """A once-a-day cron (six fields, UTC) whose tick is `at`."""
    return f"{at.second} {at.minute} {at.hour} * * *"


def slot(at: datetime) -> str:
    return at.replace(microsecond=0).isoformat()


def ran(ex: FakeExecutor, job: str) -> list[dict]:
    return [j["args"] for j in ex.jobs if j.get("tool") == "workflow.job" and (j.get("args") or {}).get("name") == job]


def sched(h, name: str, job: str, cron: str, **kw) -> dict:
    code, r = h.call("POST", "/wf/schedules", {"name": name, "job": job, "cron": cron, "tz": "UTC", "mode": "shadow",
                                               **kw})  # fmt: skip
    assert code == 200, r
    return r


def down_across(h: AppHubd, tick: datetime, after: float = 1.5) -> dict:
    """Kill the hub before `tick`, keep it down until `after` s past it, start it: the new launch's catch-up."""
    h.kill()
    assert datetime.now(UTC) < tick, "the hub went down after the tick: the test machine is too slow"
    time.sleep(max(0.0, (tick - datetime.now(UTC)).total_seconds() + after))
    h.start()
    return wait_for(lambda: h.call("GET", "/wf/engine")[1].get("catch_up"), 60)


def test_a_restart_across_a_daily_tick_runs_it_once(cenv, tmp_path, monkeypatch, capsys):
    h, ex = cenv["hub"], cenv["exec"]
    # an edit resets `since`: daily-edit's new cron has a tick after it was first made, before the edit
    sched(h, "job:daily-edit", "daily-edit", daily(datetime.now(UTC) + timedelta(hours=3)))
    time.sleep(2.5)
    sched(h, "job:daily-edit", "daily-edit", daily((datetime.now(UTC) - timedelta(seconds=1)).replace(microsecond=0)))
    tick = (datetime.now(UTC) + timedelta(seconds=6)).replace(microsecond=0)
    gone = (datetime.now(UTC) - timedelta(seconds=30)).replace(microsecond=0)
    r = sched(h, "job:daily-a", "daily-a", daily(tick))
    assert r["catch_up"] is True and r["status"] == "ACTIVE", r  # daily: on by default
    assert sched(h, "job:daily-off", "daily-off", daily(tick), catch_up=False)["catch_up"] is False
    sched(h, "job:daily-new", "daily-new", daily(gone))  # made after its tick: nothing was missed
    sched(h, "job:fast", "fast", "*/2 * * * * *")  # every 2 s: down across many ticks, never caught up
    assert h.call("POST", "/wf/schedules", {"name": "job:x", "job": "x", "cron": daily(tick), "mode": "shadow",
                                            "catch_up": "yes"})[0] == 400  # fmt: skip

    cu = down_across(h, tick)
    assert cu["run"] == ["job:daily-a"] and cu["late"] == [] and cu["errors"] == [], cu
    wait_for(lambda: ran(ex, "daily-a"), 40)
    assert [a["slot"] for a in ran(ex, "daily-a")] == [slot(tick)]  # the missed slot, as the scheduler names it
    assert ran(ex, "daily-off") == [] and ran(ex, "daily-new") == [] and ran(ex, "daily-edit") == []

    # the run's id is the scheduler's own: sched-<name>-<slot>, "+00:00" and all (literal or %2B)
    wid = f"sched-job:daily-a-{slot(tick)}"
    for path in (f"/wf/{wid}", f"/wf/{quote(wid, safe=':._-')}"):
        code, st = h.call("GET", path)
        assert code == 200 and st["id"] == wid and st["name"] == "job.tick", (path, st)
    assert h.call("GET", "/wf/no%2Fslash")[0] == 404

    # `hub wf show` (the brain's CLI) on that id
    home = tmp_path / "home"
    home.mkdir()
    for k, v in {"HOME": str(home), "ASSISTANT_TEST": "1", "HUB_URL": h.url,
                 "HUB_TOKEN_FILE": str(h.secrets / "token_brain")}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("HUB_SCHEMA", raising=False)
    from hubkit import cli

    assert cli.main(["wf", "show", wid]) == 0
    assert wid in capsys.readouterr().out

    # a second restart: that tick ran, nothing runs again (job:fast just goes on ticking)
    h.kill()
    h.start()
    cu = wait_for(lambda: h.call("GET", "/wf/engine")[1].get("catch_up"), 60)
    assert cu["run"] == [] and cu["errors"] == [], cu
    time.sleep(3)
    assert len(ran(ex, "daily-a")) == 1 and ran(ex, "daily-off") == [] and ran(ex, "daily-edit") == []
    assert ran(ex, "fast")
    rows = {s["name"]: s for s in h.call("GET", "/wf/schedules")[1]["rows"]}
    assert rows["job:daily-a"]["catch_up"] is True and rows["job:daily-off"]["catch_up"] is False
    for name in ("job:daily-a", "job:daily-off", "job:daily-new", "job:daily-edit", "job:fast"):
        assert h.call("POST", f"/wf/schedules/{name}/delete")[1]["status"] == "DELETED"


def test_a_tick_missed_longer_ago_than_the_grace_is_skipped(cenv):
    """HUB_WF_CATCHUP_MAX=1: the grace is 1 s, the hub stays down 4 s past the tick."""
    h, ex = cenv["hub"], cenv["exec"]
    tick = (datetime.now(UTC) + timedelta(seconds=5)).replace(microsecond=0)
    sched(h, "job:daily-late", "daily-late", daily(tick))
    h.extra_env["HUB_WF_CATCHUP_MAX"] = "1"
    try:
        cu = down_across(h, tick, after=4.0)
        assert cu["late"] == ["job:daily-late"] and cu["run"] == [], cu
        time.sleep(3)
        assert ran(ex, "daily-late") == []
    finally:
        h.extra_env.pop("HUB_WF_CATCHUP_MAX", None)
        h.kill()
        h.start()
        wait_for(lambda: h.call("GET", "/wf/engine")[1].get("state") == "on", 60)
    assert h.call("POST", "/wf/schedules/job:daily-late/delete")[1]["status"] == "DELETED"
