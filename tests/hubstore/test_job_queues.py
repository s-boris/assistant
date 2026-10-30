"""Job queues instead of concurrency 1: a long job (held like a 240 s report) on jobs-slow must not delay the
every-minute class on jobs-critical; on a single hub-jobs queue it does. Throwaway Postgres + hubd (hubfix),
an executor that holds every "slow" call until the test releases it (at most 240 s). Never the real hub."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime

import hubfix
import pytest
from test_workflows import CHUNK, AppHubd, app_tree, wait_for

HOLD = 240.0  # the slow job's length when nobody releases it


class HoldingExecutor:
    """The brain's tool worker, faked with N claim threads: "slow" calls block until released, the rest answer at
    once. Records (job, slot, claimed_at) per call."""

    def __init__(self, url: str, token: str, threads: int = 8) -> None:
        self.url, self.token = url, token
        self.calls: list[tuple[str, str, float]] = []
        self.release = threading.Event()
        self.stop = threading.Event()
        self.ts = [threading.Thread(target=self.loop, daemon=True) for _ in range(threads)]
        for t in self.ts:
            t.start()

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
            a = job.get("args") or {}
            self.calls.append((a.get("name"), a.get("slot") or "", time.time()))
            if a.get("name") == "slow":
                self.release.wait(HOLD)
            data = {"job": a.get("name"), "mode": a.get("mode"), "secs": 0.0, "would_run": "x"}
            res = {"ok": True, "tool": job.get("tool"), "data": data, "text": "ok", "code": "ok", "ms": 1}
            try:
                self.post("/gw/result", {"id": job["id"], "result": res}, timeout=10)
            except (OSError, ValueError):
                pass


@pytest.fixture(scope="module")
def qenv(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("p31q")
    pg = hubfix.Pg(root).start()
    app = app_tree(root)
    gw_port = hubfix.free_port()
    extra = {"HUB_GW_PORT": str(gw_port), "HUB_GW_STATE": str(root / "gw"), "HUB_WF_CHUNK": CHUNK,
             "HUB_WF_SCHED_POLL": "1", "HUB_WF_NOTIFY_POLL": "0.2", "HUB_RELEASE_DIR": str(root)}  # fmt: skip
    h = AppHubd(root, pg, hubfix.vendor_tree(root / "vendor"), app, extra).start()
    ex = HoldingExecutor(f"http://127.0.0.1:{gw_port}", h.tokens["brain"])
    code, r = h.call("POST", "/wf/engine", {"on": True, "why": "p31 test"})
    assert code == 200, r
    wait_for(lambda: h.call("GET", "/wf/engine")[1].get("state") == "on", 60)
    yield {"hub": h, "exec": ex}
    ex.release.set()
    ex.stop.set()
    h.stop()
    pg.discard()


def sched(h, job: str, queue: str | None) -> None:
    body = {"name": f"job:{job}", "job": job, "cron": "*/2 * * * * *", "tz": "UTC", "mode": "shadow"}
    if queue:
        body["queue"] = queue
    code, r = h.call("POST", "/wf/schedules", body)
    assert code == 200 and r["status"] == "ACTIVE", r


def slot_at(slot: str) -> float:
    return datetime.fromisoformat(slot).timestamp()


def lags(ex: HoldingExecutor, job: str, since: float) -> list[float]:
    out = []
    for name, slot, at in list(ex.calls):
        if name == job and at >= since and slot:
            out.append(at - slot_at(slot))
    return out


def clear(h) -> None:
    for name in ("job:slow", "job:crit"):
        h.call("POST", f"/wf/schedules/{name}/delete")


def drain(h, ex: HoldingExecutor) -> None:
    """Run out what an earlier test left queued: deleting a schedule keeps its enqueued ticks (a held slow job's
    backlog on jobs-slow), and one of them claimed after the executor holds again would pass for this test's own."""
    ex.release.set()

    def idle() -> bool:
        c = h.call("GET", "/wf/engine")[1].get("counts") or {}
        return c.get("pending") == 0 and c.get("enqueued") == 0

    wait_for(idle, 60)


def test_queue_validation(qenv):
    h = qenv["hub"]
    body = {"name": "job:x", "job": "x", "cron": "0 3 * * *", "tz": "UTC", "mode": "shadow"}
    assert h.call("POST", "/wf/schedules", {**body, "queue": "jobs-nope"})[0] == 400
    assert h.call("POST", "/wf/schedules", {**body, "name": "nope:x"})[0] == 400
    code, r = h.call("POST", "/wf/schedules", {**body, "queue": "jobs-slow"})
    assert code == 200 and r.get("queue") == "jobs-slow", r
    assert [s["name"] for s in h.call("GET", "/wf/schedules")[1]["rows"]] == ["job:x"]
    assert h.call("POST", "/wf/schedules/job:x/delete")[1]["status"] == "DELETED"


def test_critical_never_waits_behind_a_slow_job(qenv):
    h, ex = qenv["hub"], qenv["exec"]
    ex.release.clear()
    t0 = time.time()
    sched(h, "slow", "jobs-slow")
    wait_for(lambda: sum(1 for c in ex.calls if c[0] == "slow" and c[2] >= t0) >= 2, 40)  # both slow slots held
    t1 = time.time()
    sched(h, "crit", "jobs-critical")
    wait_for(lambda: len(lags(ex, "crit", t1)) >= 5, 60)
    lag = lags(ex, "crit", t1)
    held = sum(1 for c in ex.calls if c[0] == "slow" and c[2] >= t0)
    clear(h)
    ex.release.set()
    assert held == 2, f"jobs-slow runs 2 at a time, saw {held}"
    lag.sort()
    assert lag[len(lag) * 95 // 100 - 1 if len(lag) > 1 else 0] <= 10.0, lag  # start lag p95 <= 10 s (the SLO)


def test_one_queue_would_block(qenv):
    """The contrast: on a single hub-jobs queue (concurrency 1) a held job stops every other job on it."""
    h, ex = qenv["hub"], qenv["exec"]
    drain(h, ex)
    ex.release.clear()
    t0 = time.time()
    sched(h, "slow", None)  # no queue: hub-jobs
    # hub-jobs is held only once a tick of this schedule (its slot after t0) is claimed: crit made before that
    # shares slow's first tick and may be dequeued first
    wait_for(lambda: any(c[0] == "slow" and c[1] and slot_at(c[1]) >= t0 for c in ex.calls), 40)
    t1 = time.time()
    sched(h, "crit", None)
    time.sleep(8)
    got = lags(ex, "crit", t1)
    clear(h)
    ex.release.set()
    assert got == [], got
