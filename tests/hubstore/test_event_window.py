"""The event.window clock in hubd (DBOS over a throwaway Postgres, the gateway on, a fake brain executor that
answers workflow.window). The grid is 2 s here (HUB_WF_EVW_MIN=1, every=2) instead of a minute. Covers: the durable
sleep to the window's start, one tick per grid slot with kind/key/slot, the brain's done ending it, next_s skipping
ahead, a miss (brain unavailable) keeping the clock going until the cap, the canonical id (a repeat start is the same
workflow; a wrong id or bad args refused), and cancel. Never the real hub, never a real token."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.request
from datetime import UTC, datetime

import hubfix
import pytest
from test_workflows import AppHubd, app_tree, done


class TickExecutor:
    """The brain's `tool worker`, faked: answers workflow.window with the next queued answer (default active)."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url, token
        self.calls: list[dict] = []
        self.answers: dict[str, list[dict]] = {}
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
            a = job.get("args") or {}
            self.calls.append({"tool": job.get("tool"), "args": a, "t": time.time()})
            q = self.answers.get(a.get("key"), [])
            ans = q.pop(0) if q else {"decision": "active"}
            if ans.get("code"):
                res = {"ok": False, "tool": job.get("tool"), "code": ans["code"], "text": "x", "ms": 1}
            else:
                data = {"kind": a.get("kind"), "key": a.get("key"), "ran": ["event-card"], **ans}
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
    root = tmp_path_factory.mktemp("p35wf")
    pg = hubfix.Pg(root).start()
    app = app_tree(root)
    gw_port = hubfix.free_port()
    extra = {"HUB_GW_PORT": str(gw_port), "HUB_GW_STATE": str(root / "gw"), "HUB_WF_CHUNK": "2",
             "HUB_WF_SCHED_POLL": "1", "HUB_WF_NOTIFY_POLL": "0.2", "HUB_RELEASE_DIR": str(root),
             "HUB_WF_EVW_MIN": "1"}  # fmt: skip
    h = AppHubd(root, pg, hubfix.vendor_tree(root / "vendor"), app, extra).start()
    assert h.call("POST", "/wf/engine", {"on": True, "why": "test"})[1]["state"] == "on"
    ex = TickExecutor(f"http://127.0.0.1:{gw_port}", h.tokens["brain"])
    yield {"hub": h, "exec": ex}
    ex.stop.set()
    h.stop()
    pg.discard()


def wid(kind: str, key: str, start: float) -> str:
    h = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"evw:{kind}:{h}:{datetime.fromtimestamp(start, UTC).strftime('%Y%m%d%H%M')}"


def start(h, kind: str, key: str, s: float, e: float, every: int = 2, w: str | None = None):
    args = {"kind": kind, "key": key, "start": int(s), "end": int(e), "every": every}
    return h.call("POST", "/wf/start", {"name": "event.window", "id": w or wid(kind, key, int(s)), "args": args})


def ticks(ex, key: str) -> list[dict]:
    return [c for c in ex.calls if c["tool"] == "workflow.window" and c["args"].get("key") == key]


def test_sleeps_to_start_ticks_on_the_grid_until_done(env):
    h, ex = env["hub"], env["exec"]
    s = int(time.time()) + 3
    ex.answers["n1@day"] = [{"decision": "active"}, {"decision": "active"}, {"decision": "done"}]
    code, r = start(h, "night", "n1@day", s, s + 120)
    assert code == 200 and r["status"] == "PENDING", r
    assert start(h, "night", "n1@day", s, s + 120)[1]["id"] == wid("night", "n1@day", s)  # the same clock
    out = done(h, wid("night", "n1@day", s), 60)
    assert out["status"] == "SUCCESS" and out["output"]["outcome"] == "done" and out["output"]["ticks"] == 3, out
    calls = ticks(ex, "n1@day")
    assert len(calls) == 3 and calls[0]["t"] >= s
    assert all(c["args"]["kind"] == "night" and c["args"]["at"] % 2 == 0 for c in calls)
    assert [c["args"]["at"] for c in calls] == sorted({c["args"]["at"] for c in calls})  # one tick per slot
    assert out["output"]["last"]["decision"] == "done"


def test_next_s_skips_ahead(env):
    h, ex = env["hub"], env["exec"]
    s = int(time.time())
    ex.answers["skip"] = [{"decision": "active", "next_s": 8}, {"decision": "done"}]
    start(h, "sleepin", "skip", s, s + 120)
    out = done(h, wid("sleepin", "skip", s), 60)
    calls = ticks(ex, "skip")
    assert out["output"]["ticks"] == 2 and calls[1]["args"]["at"] - calls[0]["args"]["at"] >= 8, calls


def test_a_miss_keeps_the_clock_going_until_the_cap(env):
    h, ex = env["hub"], env["exec"]
    s = int(time.time())
    ex.answers["cap"] = [{"code": "unavailable"}]  # then active until the cap
    start(h, "drive", "cap", s, s + 7)
    out = done(h, wid("drive", "cap", s), 60)["output"]
    assert out["outcome"] == "capped" and out["misses"] == 1 and out["ticks"] >= 3, out
    assert max(c["args"]["at"] for c in ticks(ex, "cap")) <= s + 7


def test_wrong_id_and_bad_args_are_refused(env):
    h = env["hub"]
    s = int(time.time()) + 60
    assert start(h, "night", "k", s, s + 60, w="evw:night:abcdefabcdef:x")[0] == 400
    assert start(h, "beach", "k", s, s + 60, w="evw:beach:x:y")[0] == 400
    assert start(h, "night", "bad key!", s, s + 60, w=wid("night", "bad key!", s))[0] == 400
    assert start(h, "night", "k", s, s + 49 * 3600)[0] == 400  # longer than EVW_SPAN_MAX
    assert start(h, "night", "k", s, s + 60, every=1000)[0] == 400


def test_cancel_stops_the_clock(env):
    h, ex = env["hub"], env["exec"]
    s = int(time.time()) + 3
    start(h, "party", "p1", s, s + 60)
    assert h.call("POST", f"/wf/{wid('party', 'p1', s)}/cancel")[1]["status"] == "CANCELLED"
    time.sleep(5)
    assert ticks(ex, "p1") == []
    code, r = start(h, "party", "p1", s, s + 60, w=wid("party", "p1", s) + ":2")  # a cancelled window's next clock
    assert code == 200 and r["status"] in ("PENDING", "ENQUEUED")
    h.call("POST", f"/wf/{wid('party', 'p1', s)}:2/cancel")
