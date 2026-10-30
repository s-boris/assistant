"""Web tools against stub webagent / abx / securefill: reads only, parsing, and the boundary: no tool reaches a
command that clicks, approves, starts, stops or fills (an argv allowlist sweep over every tool and example)."""

from __future__ import annotations

from tools import gateway
from tools.core import load

TASK = {
    "id": "wa0a1b2c",
    "state": "pending",
    "mode": "live",
    "kind": "booking",
    "title": "Book a test flight",
    "url": "https://shop.example/booking",
    "steps": 4,
    "stops": 1,
    "pending": {"label": "Pay now", "category": "pay", "what": "pays", "shot": "/tmp/x.png"},
    "expires": "2026-10-22 10:15",
    "created": "2026-10-22 10:00",
    "result": None,
    "fill_session": "live",
}


def call(name, **args):
    return gateway.call(name, args)


def test_tasks_check_stats(stubs):
    stubs.on("webagent", ["status", "--json"], [TASK])
    stubs.on("webagent", ["status", "--json", "wa0a1b2c"], TASK)
    stubs.on(
        "webagent",
        ["check", "Pay now"],
        {"commit": True, "tier": "hard", "category": "pay", "matched": ["pay now"], "exempt": None},
        rc=3,
    )
    stubs.on(
        "webagent", ["check", "Next"], {"commit": False, "tier": None, "category": None, "matched": [], "exempt": None}
    )
    stubs.on(
        "webagent",
        ["stats"],
        {
            "days": 7,
            "tasks": 3,
            "completed": 1,
            "committed": 1,
            "reached_card": 2,
            "approvals": 1,
            "median_approval_s": 90,
            "guard_blocks": 0,
        },
    )
    t = call("web.tasks")
    assert t.ok and t.data["count"] == 1 and "shot" not in t.data["tasks"][0]["pending"]  # concise: no screenshot path
    assert "waits for his ✅ on pay “Pay now” until 2026-10-22 10:15" in t.text
    assert call("web.tasks", id="wa0a1b2c", format="detailed").data["tasks"][0]["pending"]["shot"] == "/tmp/x.png"
    c = call("web.check", label="Pay now")
    assert c.ok and c.data["commit"] is True and c.text.startswith("COMMITTING (hard, pay)")
    assert call("web.check", label="Next").text == "not committing"
    assert "3 started" in call("web.stats", days=7).text
    calls = stubs.calls("webagent")
    assert ["status", "--json", "--all"] not in calls and ["stats", "--json", "--days", "7"] in calls
    call("web.tasks", all=True)
    assert ["status", "--json", "--all"] in stubs.calls("webagent")


def test_sessions_and_fill(stubs):
    stubs.on(
        "abx", ["list"], "ab-1a2b3c  task wa0a1b2c  idle 3 min  tab open\nab-4d5e6f  task -  idle 40 min  tab gone\n"
    )
    stubs.on("abx", ["list", "--task", "wa0a1b2c"], "ab-1a2b3c\n")
    stubs.on("abx", ["list", "--task", "wa000000"], "", rc=4, err="abx: no session for task wa000000\n")
    stubs.on(
        "securefill",
        ["status"],
        {
            "sessions": [],
            "pending_requests": [],
            "live_fingerprints": 2,
            "psp_hosts": 33,
            "site_profiles": ["shop.example"],
        },
    )
    stubs.on(
        "securefill",
        ["session", "status"],
        {"ok": True, "session": "live", "minutes_left": 12, "released": {"name": 1}},
    )
    s = call("web.sessions")
    assert s.data["sessions"][1] == {"sid": "ab-4d5e6f", "idle_min": 40, "tab": "gone"}
    assert call("web.sessions", task="wa0a1b2c").data["sessions"] == [{"sid": "ab-1a2b3c", "task": "wa0a1b2c"}]
    assert call("web.sessions", task="wa000000").data == {"count": 0, "sessions": []}
    f = call("web.fill_status")
    assert f.ok and "site_profiles" not in f.data and f.text.endswith("2 live redaction fingerprint(s)")
    assert call("web.fill_session", id="wa0a1b2c").data["state"]["minutes_left"] == 12


def test_no_tool_can_act_on_a_page(stubs):
    """Every web tool is a read, ids are pattern-checked, labels never become options, and every argv any example
    produces stays on the read-only allowlist."""
    reg = load()
    specs = reg.in_domain("web")
    assert specs and {s.effect for s in specs} == {"read"} and all(s.approval == "none" for s in specs)
    for name in ("webagent", "abx", "securefill"):
        stubs.on(name, [], {"ok": True})
    stubs.on("abx", ["list"], "")
    for s in specs:
        for ex in s.examples:
            gateway.call(s.name, ex)
    allowed = {
        "webagent": {("status",), ("check",), ("stats",)},
        "abx": {("list",)},
        "securefill": {("status",), ("session", "status")},
    }
    for name, ok in allowed.items():
        for argv in stubs.calls(name):
            assert tuple(argv[:1]) in ok or tuple(argv[:2]) in ok, (name, argv)
    assert call("web.check", label="--approve").error["code"] == "invalid_args"
    assert call("web.tasks", id="wa0a1b2c; rm").error["code"] == "invalid_args"
    assert call("web.fill_session", id="../x").error["code"] == "invalid_args"
    assert {s.name for s in specs} == {  # a new web tool is a deliberate change to this list (and its review)
        "web.tasks",
        "web.check",
        "web.stats",
        "web.sessions",
        "web.fill_status",
        "web.fill_session",
    }


def test_errors(stubs):
    stubs.on("webagent", ["status", "--json", "wa111111"], "", rc=2, err="webagent: no task wa111111\n")
    stubs.on("securefill", ["status"], "", rc=3, err="securefill: vault: connection refused\n")
    assert call("web.tasks", id="wa111111").error["code"] == "not_found"
    assert call("web.fill_status").error["code"] == "unavailable"
