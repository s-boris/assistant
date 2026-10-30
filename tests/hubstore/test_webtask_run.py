"""A whole web task as one DBOS workflow (webtask.run) in a throwaway hubd + Postgres (hubfix): messages
move it through lease, steps, the approval wait (the hub-held approval decides it), commit and done, with one receipt
row in the ledger; it survives SIGKILL + restart mid-wait at its last step boundary; an unanswered card expires at
its first deadline and reports once (beat); dry runs write no row. Never the real hub, never a real token."""

from __future__ import annotations

import hashlib
import time

import hubfix
import pytest
from test_workflows import CHUNK, AppHubd, FakeExecutor, app_tree, done, restart, status, wait_for


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("p36wf")
    pg = hubfix.Pg(root).start()
    app = app_tree(root)
    gw_port = hubfix.free_port()
    extra = {"HUB_GW_PORT": str(gw_port), "HUB_GW_STATE": str(root / "gw"), "HUB_WF_CHUNK": CHUNK,
             "HUB_WF_SCHED_POLL": "1", "HUB_WF_NOTIFY_POLL": "0.2", "HUB_RELEASE_DIR": str(root)}  # fmt: skip
    h = AppHubd(root, pg, hubfix.vendor_tree(root / "vendor"), app, extra).start()
    ex = FakeExecutor(f"http://127.0.0.1:{gw_port}", h.tokens["brain"])
    code, r = h.call("POST", "/wf/engine", {"on": True, "why": "test"})
    assert code == 200 and r["state"] == "on", r
    yield {"hub": h, "pg": pg, "exec": ex, "root": root}
    ex.stop.set()
    h.stop()
    pg.discard()


def send(h, wid: str, msg: dict, key: str) -> None:
    code, r = h.call("POST", f"/wf/{wid}/send", {"topic": "task", "message": msg, "key": key})
    assert code == 200, r


def phase(h, wid: str) -> dict:
    return status(h, wid).get("events", {}).get("phase") or {}


def receipts(h, wid: str) -> list[dict]:
    rows = h.call("GET", "/admin/dump?table=events&limit=1000")[1]["rows"]
    return [x for x in rows if x.get("kind") == "workflow.webtask" and x.get("idem_key") == f"wf:{wid}"]


def test_start_refusals(env):
    h = env["hub"]
    assert h.call("POST", "/wf/start", {"name": "webtask.run", "args": {"task": "a b"}})[0] == 400
    assert h.call("POST", "/wf/start", {"name": "webtask.run", "args": {"task": "wa1", "idle": 1}})[0] == 400


def test_whole_task_with_hub_approval_and_a_restart_mid_wait(env):
    h = env["hub"]
    wid = "webtask:wa0p36a"
    args = {"task": "wa0p36a", "kind": "order", "pending_ttl": 600}
    assert h.call("POST", "/wf/start", {"name": "webtask.run", "args": args, "id": wid})[0] == 200
    wait_for(lambda: phase(h, wid).get("phase") == "running")
    send(h, wid, {"op": "lease", "sid": "ab-p36x01"}, "k-lease")
    send(h, wid, {"op": "step", "n": 1}, "k-s1")
    send(h, wid, {"op": "step", "n": 2}, "k-s2")
    send(h, wid, {"op": "step", "n": 2}, "k-s2")  # a resend with the same key is delivered once
    wait_for(lambda: phase(h, wid).get("steps") == 2)
    sha = hashlib.sha256(b"webagent wa0p36a stop 1").hexdigest()
    code, ap = h.call("POST", "/approvals", {"action_sha": sha, "summary": "test", "ttl": 600, "ref": "webagent:x"})
    assert code == 200, ap
    send(h, wid, {"op": "stop", "stop": 1, "nonce": ap["nonce"]}, "k-stop1")
    p = wait_for(lambda: (lambda v: v if v.get("phase") == "pending" else None)(phase(h, wid)))
    assert p["stop"] == 1 and p["lease"] == "ab-p36x01" and p["deadline"] > time.time() + 500
    restart(h, kill=True)  # SIGKILL while the approval wait runs
    p2 = wait_for(lambda: (lambda v: v if v.get("phase") == "pending" else None)(phase(h, wid)))
    assert p2["deadline"] == p["deadline"] and p2["steps"] == 2  # the same boundary, the same deadline
    code, d = h.call("POST", f"/approvals/{ap['nonce']}/decide", {"decision": "y", "by": "12345", "via": "t"}, who="tg")
    assert code == 200 and d["ok"], d
    wait_for(lambda: phase(h, wid).get("phase") == "approved", 30)
    send(h, wid, {"op": "commit", "stop": 1}, "k-commit1")
    wait_for(lambda: phase(h, wid).get("phase") == "executing")
    send(h, wid, {"op": "done", "ok": True, "committed": True}, "k-done")
    r = done(h, wid)
    assert r["status"] == "SUCCESS" and r["recoveries"] >= 1, r
    out = r["output"]
    assert out["outcome"] == "done" and out["committed"] is True and out["steps"] == 2 and out["stops"] == 1
    ev = receipts(h, wid)
    assert len(ev) == 1 and ev[0]["delivery"] == "mute" and "(committed)" in ev[0]["subject"]


def test_unanswered_card_expires_once_and_dry_runs_write_no_row(env):
    h = env["hub"]
    wid, dry = "webtask:wa0p36b", "webtask:wa0p36c"
    a = {"task": "wa0p36b", "pending_ttl": 30}
    assert h.call("POST", "/wf/start", {"name": "webtask.run", "args": a, "id": wid})[0] == 200
    b = {"task": "wa0p36c", "dry": True}
    assert h.call("POST", "/wf/start", {"name": "webtask.run", "args": b, "id": dry})[0] == 200
    send(h, wid, {"op": "stop", "stop": 1}, "k-b-stop")
    send(h, dry, {"op": "abort", "reason": "owner"}, "k-c-abort")
    r = done(h, dry)
    assert r["output"]["outcome"] == "aborted" and r["output"]["via"] == "owner"
    r = done(h, wid, 90)
    assert r["output"]["outcome"] == "expired"
    ev = receipts(h, wid)
    assert len(ev) == 1 and ev[0]["delivery"] == "beat" and "webagent sweep" in ev[0]["next"]
    assert receipts(h, dry) == []
    rows = h.call("GET", "/wf?name=webtask.run&limit=10")[1]["rows"]
    assert {x["id"] for x in rows} >= {wid, dry}
