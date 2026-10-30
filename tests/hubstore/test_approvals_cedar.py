"""The approval API under Cedar, log-only. hubd decides every approval route with tools.policy decide_api and
policy.cedar ("approvals-*") and logs it; the route table keeps enforcing (a tg request is still a 403). The log and
/health hold ids and codes only: never a nonce, an action or a summary. A throwaway Postgres + hubd (hubfix)."""

from __future__ import annotations

import hashlib
import json
import time
import uuid

import hubfix
import pytest


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("h6ap")
    pg = hubfix.Pg(root).start()
    h = hubfix.Hubd(root, pg, hubfix.vendor_tree(root / "vendor")).start()
    try:
        yield h
    finally:
        h.stop()
        pg.stop()


def policy_state(h, until=lambda p: bool(p.get("engine")), wait: float = 20.0) -> dict:
    end = time.time() + wait
    p: dict = {}
    while time.time() < end:
        st, body = h.call("GET", "/health")
        p = body.get("approvals_policy") or {}
        if until(p):
            return p
        time.sleep(0.2)
    return p


def log_rows(h) -> list[dict]:
    f = h.root / "state" / "approvals-policy.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def test_tg_request_is_logged_as_a_deny_and_still_refused_by_the_route_table(hub):
    p0 = policy_state(hub)
    if not p0.get("engine"):
        pytest.skip(f"no Cedar engine in this hubd ({p0.get('error')})")
    schema = "t_ap" + uuid.uuid4().hex[:8]
    st, body = hub.call(
        "POST",
        "/approvals",
        {"action_sha": "0" * 64, "summary": "secret-summary-x", "rid": "rid-" + uuid.uuid4().hex},
        who="tg",
        schema=schema,
    )
    assert st == 403  # the route table: requests are the brain's
    p1 = policy_state(hub, until=lambda p: p.get("deny", 0) > p0.get("deny", 0))
    assert p1["deny"] == p0.get("deny", 0) + 1
    assert p1["last_deny"]["op"] == "request" and p1["last_deny"]["principal"] == "binding"
    assert p1["last_deny"]["policies"] == ["approvals-tg-never-mints"]
    row = log_rows(hub)[-1]
    assert row["decision"] == "deny" and row["mode"] == "log" and row["test"] is True
    assert "secret-summary-x" not in json.dumps(log_rows(hub))


def test_brain_requests_tg_decides_both_allowed_and_brain_decide_denied(hub):
    p0 = policy_state(hub)
    if not p0.get("engine"):
        pytest.skip(f"no Cedar engine in this hubd ({p0.get('error')})")
    schema = "t_ap" + uuid.uuid4().hex[:8]
    sha = hashlib.sha256(b"book the table").hexdigest()
    st, body = hub.call(
        "POST",
        "/approvals",
        {"action_sha": sha, "summary": "Book", "chat": "1", "rid": "rid-" + uuid.uuid4().hex},
        who="brain",
        schema=schema,
    )
    assert st == 200, body
    nonce = body.get("nonce") or (body.get("approval") or {}).get("nonce")
    assert nonce
    st, _ = hub.call(
        "POST",
        f"/approvals/{nonce}/decide",
        {"decision": "n", "by": "1", "via": "test", "rid": "rid-" + uuid.uuid4().hex},
        who="brain",
        schema=schema,
    )
    assert st == 403  # the route table again: only his tap decides
    p1 = policy_state(hub, until=lambda p: p.get("deny", 0) > p0.get("deny", 0))
    assert p1["last_deny"]["op"] == "decide" and p1["last_deny"]["principal"] == "brain"
    assert p1["last_deny"]["policies"] == ["approvals-only-his-tap-decides"]
    st, body = hub.call(
        "POST",
        f"/approvals/{nonce}/decide",
        {"decision": "y", "by": "1", "via": "test", "rid": "rid-" + uuid.uuid4().hex},
        who="tg",
        schema=schema,
    )
    assert st == 200, body
    p2 = policy_state(hub, until=lambda p: p.get("allow", 0) >= p0.get("allow", 0) + 2)
    assert p2["allow"] >= p0.get("allow", 0) + 2  # the brain's request + his tap
    ops = [(r["op"], r["principal"], r["decision"]) for r in log_rows(hub)[-3:]]
    assert ops == [("request", "brain", "allow"), ("decide", "brain", "deny"), ("decide", "binding", "allow")]
    assert nonce not in json.dumps(log_rows(hub))
