"""DBOS in hubd. A throwaway Postgres + hubd (hubfix) run from a release-like app tree (the hub files, the typed
tool registry and the gateway, as `assistant deploy --target hub` lays them out) with the gateway on and a fake brain
executor that answers its calls. Covers: the engine switch (off by default, hot on), the /wf API, the brain's tools
and `hub wf`, the web-task approval wait (decision message, hub-held approval, deadline expiry with its ledger
event) surviving SIGKILL + restart, and watch schedules firing through the gateway as principal reflex
(Cedar log-only) before and after a restart. Never the real hub, never a real token."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import hubfix
import pytest

REPO = Path(__file__).resolve().parents[2]
CHUNK = "2"


def app_tree(root: Path) -> Path:
    """packages/hub/*.py -> app/, packages/tools (+ domains, policy) -> app/tools/: hubd imports tools.hubgw there."""
    app = root / "app"
    (app / "tools" / "domains").mkdir(parents=True)
    for f in (REPO / "packages" / "hub").glob("*.py"):
        shutil.copy(f, app / f.name)
    for pat in ("*.py", "*.cedar"):
        for f in (REPO / "packages" / "tools").glob(pat):
            shutil.copy(f, app / "tools" / f.name)
    for f in (REPO / "packages" / "tools" / "domains").glob("*.py"):
        shutil.copy(f, app / "tools" / "domains" / f.name)
    return app


class AppHubd(hubfix.Hubd):
    def __init__(self, root: Path, pg: hubfix.Pg, vendor: Path, app: Path, extra: dict[str, str]) -> None:
        super().__init__(root, pg, vendor, extra)
        self.app = app

    def start(self, wait: float = 60.0) -> AppHubd:
        f = open(self.log, "ab")  # noqa: SIM115
        import subprocess

        self.proc = subprocess.Popen(
            [hubfix.py313(), "-I", "-B", "-u", str(self.app / "hubd.py")],
            env=self.env(), stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                with hubfix.web.open(self.url + "/health", timeout=2) as r:
                    h = json.load(r)
                    if h.get("store", {}).get("state") == "ok" and (h.get("gateway") or {}).get("state") != "down":
                        return self
            except (OSError, ValueError):
                pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        raise RuntimeError("hubd did not come up: " + self.log.read_text()[-3000:])


class FakeExecutor:
    """The brain's `tool worker`, faked: long-polls /gw/claim and answers workflow.job like the real tool would."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url, token
        self.jobs: list[dict] = []
        self.stop = threading.Event()
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def post(self, path: str, body: dict, timeout: float = 30) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        with hubfix.web.open(req, timeout=timeout) as r:
            return json.load(r)

    def loop(self) -> None:
        while not self.stop.is_set():
            try:
                job = self.post("/gw/claim", {"wait": 2}, timeout=10).get("job")
            except (OSError, ValueError):
                time.sleep(0.3)
                continue
            if not job:
                continue
            self.jobs.append(job)
            a = job.get("args") or {}
            data = {"job": a.get("name"), "mode": a.get("mode"), "secs": 0.0, "would_run": "x"}
            res = {"ok": True, "tool": job.get("tool"), "data": data, "text": "ok", "code": "ok", "ms": 1}
            try:
                self.post("/gw/result", {"id": job["id"], "result": res}, timeout=10)
            except (OSError, ValueError):
                pass


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("hubwf")
    pg = hubfix.Pg(root).start()
    app = app_tree(root)
    gw_port = hubfix.free_port()
    extra = {"HUB_GW_PORT": str(gw_port), "HUB_GW_STATE": str(root / "gw"), "HUB_WF_CHUNK": CHUNK,
             "HUB_WF_SCHED_POLL": "1", "HUB_WF_NOTIFY_POLL": "0.2", "HUB_RELEASE_DIR": str(root)}  # fmt: skip
    h = AppHubd(root, pg, hubfix.vendor_tree(root / "vendor"), app, extra).start()
    ex = FakeExecutor(f"http://127.0.0.1:{gw_port}", h.tokens["brain"])
    yield {"hub": h, "pg": pg, "exec": ex, "root": root}
    ex.stop.set()
    h.stop()
    pg.discard()


def wait_for(fn, wait: float = 30.0, every: float = 0.25):
    end = time.monotonic() + wait
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out; last: {last!r}")


def restart(h: AppHubd, kill: bool = True) -> None:
    (h.kill if kill else h.stop)()
    h.start()
    wait_for(lambda: h.call("GET", "/wf/engine")[1].get("state") == "on", 60)


def status(h, wid: str) -> dict:
    code, r = h.call("GET", f"/wf/{wid}")
    assert code == 200, r
    return r


def done(h, wid: str, wait: float = 60.0) -> dict:
    return wait_for(lambda: (lambda r: r if r["status"] not in ("PENDING", "ENQUEUED") else None)(status(h, wid)),
                    wait)  # fmt: skip


def test_engine_off_by_default_then_on_hot(env):
    h = env["hub"]
    assert h.call("GET", "/health")[1]["workflows"]["state"] == "off"
    code, r = h.call("POST", "/wf/start", {"name": "hub.probe"})
    assert code == 503 and r["error"] == "workflows_off"
    assert h.call("GET", "/wf/engine", who="tg")[0] == 403
    code, r = h.call("POST", "/wf/engine", {"on": True, "why": "test"})
    assert code == 200 and r["state"] == "on", r
    assert json.loads((env["root"] / "state" / "workflows.json").read_text())["engine"] == "on"
    code, r = h.call("GET", "/wf/engine")
    assert r["state"] == "on" and r["version"] == "hubwf-1" and set(r["counts"]) == {"pending", "enqueued", "error"}
    assert h.call("GET", "/health")[1]["workflows"]["state"] == "on"


def test_probe_send_and_validation(env):
    h = env["hub"]
    code, r = h.call("POST", "/wf/start", {"name": "hub.probe", "args": {"ttl": 20}, "id": "probe-1"})
    assert code == 200 and r["id"] == "probe-1"
    assert (
        h.call("POST", "/wf/start", {"name": "hub.probe", "args": {"ttl": 20}, "id": "probe-1"})[1]["id"] == "probe-1"
    )
    assert h.call("POST", "/wf/probe-1/send", {"topic": "ping", "message": "hi", "key": "k1"})[0] == 200
    r = done(h, "probe-1")
    assert r["status"] == "SUCCESS" and r["output"]["got"] is True and r["output"]["message"] == "hi"
    assert h.call("POST", "/wf/start", {"name": "nope"})[0] == 400
    assert h.call("POST", "/wf/start", {"name": "hub.probe", "args": {"ttl": 9999}})[0] == 400
    assert h.call("POST", "/wf/start", {"name": "webtask.approval", "args": {"task": "x y"}})[0] == 400
    assert h.call("POST", "/wf/start", {"name": "hub.probe", "id": "bad id!"})[0] == 400
    assert h.call("GET", "/wf/nosuch")[0] == 404
    assert h.call("POST", "/wf/nosuch/send", {"topic": "ping", "message": 1})[0] == 404
    assert h.call("POST", "/wf/probe-1/send", {"topic": "Bad Topic"})[0] == 400
    assert h.call("GET", "/wf/engine", schema="t_x")[0] == 400
    rows = h.call("GET", "/wf?name=hub.probe&limit=5")[1]["rows"]
    assert any(x["id"] == "probe-1" for x in rows)


def test_webtask_decision_message(env):
    h = env["hub"]
    args = {"task": "t1", "stop": 1, "ttl": 600}
    assert h.call("POST", "/wf/start", {"name": "webtask.approval", "args": args, "id": "webtask:t1:1"})[0] == 200
    r = wait_for(lambda: status(h, "webtask:t1:1").get("events", {}).get("waiting"))
    assert r["task"] == "t1" and r["until"] > time.time() + 500
    h.call("POST", "/wf/webtask:t1:1/send", {"topic": "decision", "message": {"decision": "approved", "via": "button"}})
    r = done(h, "webtask:t1:1")
    assert r["output"]["outcome"] == "approved" and r["output"]["via"] == "button"
    assert r["events"]["outcome"]["outcome"] == "approved"


def test_webtask_follows_the_hub_held_approval(env):
    h = env["hub"]
    sha = hashlib.sha256(b"webagent t2 stop 1").hexdigest()
    code, ap = h.call("POST", "/approvals", {"action_sha": sha, "summary": "test", "ttl": 600, "ref": "webagent:t2"})
    assert code == 200, ap
    nonce = ap["nonce"]
    args = {"task": "t2", "stop": 1, "ttl": 600, "nonce": nonce}
    assert h.call("POST", "/wf/start", {"name": "webtask.approval", "args": args, "id": "webtask:t2:1"})[0] == 200
    wait_for(lambda: status(h, "webtask:t2:1").get("events", {}).get("waiting"))
    code, d = h.call("POST", f"/approvals/{nonce}/decide", {"decision": "n", "by": "12345", "via": "test"}, who="tg")
    assert code == 200, d
    r = done(h, "webtask:t2:1", 30)
    assert r["output"]["outcome"] == "declined" and r["output"]["via"] == "hub approval"


def test_kill_mid_wait_resumes_message_and_deadline(env):
    """SIGKILL while two waits run: after the restart one gets its decision, the other expires at its first
    deadline (not ttl after the restart) and reports the expiry once as a ledger event."""
    h = env["hub"]
    a1 = {"task": "t3", "stop": 2, "ttl": 600}
    a2 = {"task": "t4", "stop": 1, "ttl": 30, "notify": ["expired"]}
    assert h.call("POST", "/wf/start", {"name": "webtask.approval", "args": a1, "id": "webtask:t3:2"})[0] == 200
    t0 = time.time()
    assert h.call("POST", "/wf/start", {"name": "webtask.approval", "args": a2, "id": "webtask:t4:1"})[0] == 200
    until = wait_for(lambda: status(h, "webtask:t4:1").get("events", {}).get("waiting"))["until"]
    wait_for(lambda: status(h, "webtask:t3:2").get("events", {}).get("waiting"))
    time.sleep(3)
    restart(h, kill=True)
    r = status(h, "webtask:t3:2")
    assert r["status"] in ("PENDING", "ENQUEUED"), r  # recovery puts it back on DBOS's internal queue first
    h.call("POST", "/wf/webtask:t3:2/send", {"topic": "decision", "message": {"decision": "stopped"}})
    r = done(h, "webtask:t3:2")
    assert r["output"]["outcome"] == "stopped" and r["recoveries"] >= 1
    r = done(h, "webtask:t4:1", 60)
    assert r["output"]["outcome"] == "expired"
    assert until - 1 <= t0 + 30 <= until + 5  # the deadline was recorded once, before the kill
    assert 29 <= r["output"]["waited_s"] <= 30 + 3 * float(CHUNK) + 2, r["output"]
    rows = h.call("GET", "/admin/dump?table=events&limit=1000")[1]["rows"]
    ev = [x for x in rows if x.get("kind") == "workflow.webtask"]
    assert len(ev) == 1 and ev[0]["idem_key"] == "wf:webtask:t4:1"
    assert "t4" in ev[0]["subject"]


def test_cancel(env):
    h = env["hub"]
    h.call("POST", "/wf/start", {"name": "hub.probe", "args": {"ttl": 300}, "id": "probe-c"})
    code, r = h.call("POST", "/wf/probe-c/cancel")
    assert code == 200 and r["status"] == "CANCELLED"


def test_watch_schedules_fire_through_the_gateway_and_survive_a_restart(env):
    h, ex = env["hub"], env["exec"]
    body = {"name": "watch:watch", "job": "watch", "cron": "*/2 * * * * *", "tz": "UTC", "mode": "shadow"}
    code, r = h.call("POST", "/wf/schedules", body)
    assert code == 200 and r["status"] == "ACTIVE" and r["mode"] == "shadow", r
    assert h.call("POST", "/wf/schedules", {**body, "cron": "nope"})[0] == 400
    assert h.call("POST", "/wf/schedules", {**body, "mode": "loud"})[0] == 400
    assert h.call("POST", "/wf/schedules", {**body, "name": "other:x"})[0] == 400

    def ran() -> list[dict]:
        return [j for j in ex.jobs if j.get("tool") == "workflow.job"]

    wait_for(lambda: len(ran()) >= 2, 40)
    j = ran()[-1]
    assert j["principal"] == "reflex" and j["args"]["name"] == "watch" and j["args"]["mode"] == "shadow"
    rows = h.call("GET", "/wf?name=job.tick&status=SUCCESS&limit=50")[1]["rows"]
    assert rows and rows[0]["output"]["ok"] is True and rows[0]["output"]["job"] == "watch"
    n_before = len(ran())
    restart(h, kill=True)
    sch = h.call("GET", "/wf/schedules?runs=2")[1]["rows"]
    assert [s["name"] for s in sch] == ["watch:watch"] and sch[0]["status"] == "ACTIVE"
    wait_for(lambda: len(ran()) >= n_before + 2, 40)
    assert h.call("POST", "/wf/schedules/watch:watch/pause")[1]["status"] == "PAUSED"
    time.sleep(3)
    n_paused = len(ran())
    time.sleep(4)
    assert len(ran()) <= n_paused + 1
    assert h.call("POST", "/wf/schedules/watch:watch/delete")[1]["status"] == "DELETED"
    assert h.call("GET", "/wf/schedules")[1]["rows"] == []
    gwlog = sqlite_calls(env["root"] / "gw" / "gateway.db")
    assert gwlog and all(r == ("workflow", "reflex", "hub", "log") for r in gwlog), gwlog[:3]


def test_job_schedule_timeout_reaches_the_tool_and_the_row(env):
    """A job schedule made with its job's limit passes it to workflow.job as timeout_s (the hub waits that long
    plus grace); one made without it passes nothing and waits 600 s. Bad limits are refused."""
    h, ex = env["hub"], env["exec"]
    assert h.call("GET", "/wf/engine")[1]["job_timeout_max"] == 3600
    body = {"name": "job:evals", "job": "evals", "cron": "*/2 * * * * *", "tz": "UTC", "mode": "shadow",
            "queue": "jobs-slow", "timeout": 1500}  # fmt: skip
    for bad in (0, 3601, True, "900", 1.5):
        assert h.call("POST", "/wf/schedules", {**body, "timeout": bad})[0] == 400, bad
    code, r = h.call("POST", "/wf/schedules", body)
    assert code == 200 and r["timeout"] == 1500, r
    plain = {k: v for k, v in body.items() if k != "timeout"} | {
        "name": "job:events-tg-metrics",
        "job": "events-tg-metrics",
    }
    code, r = h.call("POST", "/wf/schedules", plain)
    assert code == 200 and r["timeout"] == 600, r

    def ran(job: str) -> list[dict]:
        return [j for j in ex.jobs if j.get("tool") == "workflow.job" and (j.get("args") or {}).get("name") == job]

    wait_for(lambda: ran("evals") and ran("events-tg-metrics"), 40)
    assert ran("evals")[-1]["args"]["timeout_s"] == 1500
    assert "timeout_s" not in ran("events-tg-metrics")[-1]["args"]
    rows = {x["name"]: x for x in h.call("GET", "/wf/schedules")[1]["rows"]}
    assert rows["job:evals"]["timeout"] == 1500 and rows["job:events-tg-metrics"]["timeout"] == 600
    for n in ("job:evals", "job:events-tg-metrics"):
        assert h.call("POST", f"/wf/schedules/{n}/delete")[1]["status"] == "DELETED"


def sqlite_calls(db: Path) -> list[tuple]:
    import sqlite3

    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT domain, principal, caller, mode FROM calls WHERE tool='workflow.job'").fetchall()
    finally:
        con.close()


def test_switch_off_keeps_pending_and_resumes_on(env):
    h = env["hub"]
    h.call("POST", "/wf/start", {"name": "hub.probe", "args": {"ttl": 300}, "id": "probe-off"})
    wait_for(lambda: status(h, "probe-off")["status"] == "PENDING")
    assert h.call("POST", "/wf/engine", {"on": False, "why": "test"})[1]["state"] == "off"
    assert h.call("GET", "/wf/probe-off")[0] == 503
    assert h.call("POST", "/wf/engine", {"on": True, "why": "test"})[1]["state"] == "on"
    h.call("POST", "/wf/probe-off/send", {"topic": "ping", "message": 7})
    r = done(h, "probe-off")
    assert r["output"]["message"] == 7


@pytest.fixture
def brain(env, tmp_path, monkeypatch):
    """The brain's side in process: core.hub's client on the throwaway hubd (HOME, HUB_URL, the brain token)."""
    h = env["hub"]
    home = tmp_path / "home"
    home.mkdir()
    for k, v in {"HOME": str(home), "ASSISTANT_TEST": "1", "HUB_URL": h.url,
                 "HUB_TOKEN_FILE": str(h.secrets / "token_brain")}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("HUB_SCHEMA", raising=False)
    return home


def test_tools_against_the_hub(env, brain):
    from tools.core import ToolError
    from tools.domains import workflow as W

    r = W.start(W.StartIn(name="hub.probe", args={"ttl": 30}, id="probe-tool"))
    assert r.id == "probe-tool" and r.status in ("PENDING", "ENQUEUED")
    assert W.send(W.SendIn(id="probe-tool", topic="ping", message="x")).sent
    wait_for(lambda: W.status(W.StatusIn(id="probe-tool")).runs[0].status == "SUCCESS")
    out = W.status(W.StatusIn(name="hub.probe", limit=50))
    assert any(x.id == "probe-tool" for x in out.runs) and "probe-tool" in out.render()
    with pytest.raises(ToolError) as e:
        W.send(W.SendIn(id="nosuch", topic="ping"))
    assert e.value.code == "not_found"
    s = W.schedule(W.ScheduleIn(job="fx-watch", state="shadow", why="workflow test"))
    assert s.name == "watch:fx-watch" and s.mode == "shadow" and s.cron == "20 17 * * *"
    assert [x.name for x in W.schedules(W.SchedulesIn()).schedules] == ["watch:fx-watch"]
    assert W.schedule(W.ScheduleIn(job="fx-watch", state="paused", why="workflow test")).status == "PAUSED"
    W.schedule(W.ScheduleIn(job="fx-watch", state="absent", why="workflow test"))
    assert W.schedules(W.SchedulesIn()).schedules == []
    with pytest.raises(ToolError) as e:
        W.schedule(W.ScheduleIn(job="no-such-job", state="shadow", why="workflow test"))
    assert e.value.code == "not_found"
    c = W.cancel(W.CancelIn(id="probe-tool"))
    assert c.status == "SUCCESS"  # a finished workflow stays as it ended


def test_hub_wf_cli(env, brain, capsys):
    from hubkit import cli

    (brain / "crontab").write_text("PATH=/bin\n# */15 * * * * /x/bin/watch run >> log\n"
                                   "*/15 * * * * /x/bin/watch run >> /x/log 2>&1\n")  # fmt: skip
    assert cli.main(["wf", "status"]) == 0
    assert "engine: on" in capsys.readouterr().out
    assert cli.main(["wf", "probe", "--ttl", "20"]) == 0
    assert "probe ok" in capsys.readouterr().out
    assert cli.main(["wf", "cron"]) == 0
    out = capsys.readouterr().out
    assert "watch: ~/crontab:3:" in out and "train-watch: no cron line" in out
    assert cli.main(["wf", "watches", "live", "--jobs", "watch"]) == 2
    assert "refused" in capsys.readouterr().out
    assert cli.main(["wf", "watches", "live", "--jobs", "train-watch", "--dry-run"]) == 0
    assert cli.main(["wf", "watches", "shadow", "--jobs", "train-watch,drive-watch"]) == 0
    assert cli.main(["wf", "watches", "off", "--jobs", "train-watch,drive-watch,fx-watch"]) == 0
    out = capsys.readouterr().out
    assert "watch:fx-watch: absent" in out and "watch:train-watch: DELETED" in out


def test_job_tool_shadow_and_live(tmp_path, monkeypatch):
    """workflow.job on the brain side: shadow says what it would run; live runs the job as cron would."""
    from tools.core import ToolError
    from tools.domains import workflow as W

    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    script = home / "bin" / "watch"
    script.write_text('#!/bin/sh\necho "ran $1 job=$HUB_JOB home=$HOME"\nexit 3\n')
    script.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    r = W.job(W.JobIn(name="watch"))
    assert r.mode == "shadow" and r.would_run.startswith("~/bin/watch run") and r.exit is None
    r = W.job(W.JobIn(name="watch", mode="live", slot="2026-01-01T00:00:00+00:00"))
    assert r.mode == "live" and r.exit == 3
    log = (home / ".local/state/watch/cron.log").read_text()
    assert f"ran run job=watch home={home}" in log
    with pytest.raises(ToolError):
        W.job(W.JobIn(name="nope"))
    assert os.environ["HOME"] == str(home)
