"""The reminder.due timer in hubd (DBOS over a throwaway Postgres, the gateway on, a fake brain executor that
answers reminders.fire). Covers: the durable sleep to the due instant, one fire call with the slot, the canonical id
(a repeat start is the same workflow; a wrong id is refused), a retry when the brain can't take the call, cancel,
and SIGKILL mid-sleep -> the timer fires once at its original instant. Never the real hub, never a real token."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import hubfix
import pytest
from test_workflows import AppHubd, app_tree, done, restart, wait_for


class FireExecutor:
    """The brain's `tool worker`, faked: answers reminders.fire with the next queued decision (default fired)."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url, token
        self.calls: list[dict] = []
        self.answers: list[dict] = []
        self.stop = threading.Event()
        threading.Thread(target=self.loop, daemon=True).start()

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
            self.calls.append({"tool": job.get("tool"), "args": job.get("args") or {}, "t": time.time()})
            a = job.get("args") or {}
            ans = self.answers.pop(0) if self.answers else {"decision": "fired"}
            if ans.get("code"):
                res = {"ok": False, "tool": job.get("tool"), "code": ans["code"], "text": "x", "ms": 1}
            else:
                data = {"id": a.get("id"), "at": a.get("at"), "decision": ans["decision"], "mode": "live",
                        "late_s": 1.0}  # fmt: skip
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
    root = tmp_path_factory.mktemp("p34wf")
    pg = hubfix.Pg(root).start()
    app = app_tree(root)
    gw_port = hubfix.free_port()
    extra = {"HUB_GW_PORT": str(gw_port), "HUB_GW_STATE": str(root / "gw"), "HUB_WF_CHUNK": "2",
             "HUB_WF_SCHED_POLL": "1", "HUB_WF_NOTIFY_POLL": "0.2", "HUB_RELEASE_DIR": str(root),
             "HUB_WF_REM_RETRY": "2"}  # fmt: skip
    h = AppHubd(root, pg, hubfix.vendor_tree(root / "vendor"), app, extra).start()
    assert h.call("POST", "/wf/engine", {"on": True, "why": "test"})[1]["state"] == "on"
    ex = FireExecutor(f"http://127.0.0.1:{gw_port}", h.tokens["brain"])
    yield {"hub": h, "exec": ex}
    ex.stop.set()
    h.stop()
    pg.discard()


def start(h, rid: str, at: str, ts: float, wid: str | None = None):
    wid = wid or f"rem:{rid}:{at.replace('-', '').replace(' ', '').replace(':', '')}"
    return h.call("POST", "/wf/start", {"name": "reminder.due", "id": wid, "args": {"rid": rid, "at": at, "ts": ts}})


def fires(ex, rid: str) -> list[dict]:
    return [c for c in ex.calls if c["tool"] == "reminders.fire" and c["args"].get("id") == rid]


def test_timer_sleeps_to_its_instant_then_fires_once(env):
    h, ex = env["hub"], env["exec"]
    ts = time.time() + 3
    code, r = start(h, "a1b2c3d4", "2026-10-23 10:30", ts)
    assert code == 200 and r["status"] == "PENDING", r
    assert start(h, "a1b2c3d4", "2026-10-23 10:30", ts)[1]["id"] == "rem:a1b2c3d4:202610231030"  # same workflow
    out = done(h, "rem:a1b2c3d4:202610231030")
    assert out["status"] == "SUCCESS" and out["output"]["decision"] == "fired" and out["output"]["tries"] == 1, out
    calls = fires(ex, "a1b2c3d4")
    assert len(calls) == 1 and calls[0]["args"] == {"id": "a1b2c3d4", "at": "2026-10-23 10:30"}
    assert ts <= calls[0]["t"] <= ts + 6  # woke at the instant (+ the pad), not before
    assert out["events"]["waiting"]["until"] == pytest.approx(ts)


def test_wrong_id_and_bad_args_are_refused(env):
    h = env["hub"]
    assert start(h, "a1b2c3d5", "2026-10-23 10:30", time.time() + 60, wid="rem:a1b2c3d5:202610231031")[0] == 400
    assert start(h, "XYZ", "2026-10-23 10:30", time.time() + 60, wid="rem:XYZ:202610231030")[0] == 400
    assert start(h, "a1b2c3d5", "tomorrow", time.time() + 60, wid="rem:a1b2c3d5:x")[0] == 400


def test_unavailable_is_retried_then_the_store_decides(env):
    h, ex = env["hub"], env["exec"]
    ex.answers += [{"code": "unavailable"}, {"decision": "gone"}]
    start(h, "b1b2c3d4", "2026-10-23 11:00", time.time() + 1)
    out = done(h, "rem:b1b2c3d4:202610231100")["output"]
    assert out["decision"] == "gone" and out["tries"] == 2 and len(fires(ex, "b1b2c3d4")) == 2, out


def test_cancel_before_its_time_never_fires(env):
    h, ex = env["hub"], env["exec"]
    start(h, "c1b2c3d4", "2026-10-23 12:00", time.time() + 4)
    assert h.call("POST", "/wf/rem:c1b2c3d4:202610231200/cancel")[1]["status"] == "CANCELLED"
    time.sleep(6)
    assert fires(ex, "c1b2c3d4") == []
    # a cancelled slot's timer comes back as :2 (the brain's sync does that when the slot is wanted again)
    code, r = start(h, "c1b2c3d4", "2026-10-23 12:00", time.time() + 1, wid="rem:c1b2c3d4:202610231200:2")
    assert code == 200 and done(h, "rem:c1b2c3d4:202610231200:2")["output"]["decision"] == "fired"


def test_kill_mid_sleep_fires_once_at_the_original_instant(env):
    h, ex = env["hub"], env["exec"]
    ts = time.time() + 12
    start(h, "d1b2c3d4", "2026-10-23 13:00", ts)
    time.sleep(1)
    restart(h, kill=True)
    out = done(h, "rem:d1b2c3d4:202610231300", 90)
    assert out["status"] == "SUCCESS" and out["output"]["decision"] == "fired", out
    calls = fires(ex, "d1b2c3d4")
    assert len(calls) == 1 and calls[0]["t"] >= ts and calls[0]["t"] <= ts + 30
    assert wait_for(lambda: h.call("GET", "/wf?name=reminder.due&status=PENDING")[0] == 200)
