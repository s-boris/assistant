"""traces.* typed tools over a stub `traces` CLI (canned JSON per verb) and a temp gateway log."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from tools import gateway

TID = "0af7651916cd43dd8448eb211c80319c"
REPORT = {
    "main_cost": 10.0,
    "agent_cost": 20.5,
    "total_cost": 30.5,
    "by_trigger": {"owner": {"turns": 4, "main_cost": 2.0, "agent_cost": 1.0, "agents": 1}},
    "owner_turns": 4,
    "owner_first_p50": 9000.0,
    "owner_first_p95": 30000.0,
    "tools": 100,
    "errors": 2,
    "error_rate": 0.02,
    "hook_blocks": 1,
    "error_counts": {"Bash:exit_code": 2},
    "slow_turns": [{"id": "s:1", "trigger": "owner", "start_kind": "text", "dur_ms": 60000, "tools": 9, "cost": 1.2}],
    "top_agents": [{"id": "a1", "description": "Build X", "effort": "xhigh", "cost": 3.1, "dur_ms": 600000}],
    "tool_counts": {"Bash": 80},
    "latency": {},
}
SUMMARY = {
    "trace_id": TID,
    "root": "claude_code.interaction",
    "ms": 12000,
    "spans": 4,
    "llm_calls": 1,
    "tool_calls": 1,
    "gateway_calls": 1,
    "agents": 0,
    "errors": 0,
    "tokens": 1200,
    "slowest_tools": [{"tool": "Bash", "ms": 3000}],
}


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    canned = {
        "report": REPORT,
        "status": {
            "url": "http://phoenix.invalid:6006",
            "project": "brain",
            "health": "OK",
            "off": False,
            "core_otlp": True,
            "forwarder": {
                "received": 10,
                "forwarded": 9,
                "lost": 1,
                "failures": 1,
                "last_err": "URLError",
                "dropped_keys": {"x user_prompt": 3},
            },
            "retention": {"policy": "Default", "max_days": 30.0},
            "traces": 7,
            "joined": {"owner_turns": 4, "owner_turns_traced": 4, "gateway_calls": 2, "gateway_calls_in_cc_traces": 1},
        },
        "trace": {"summary": SUMMARY, "spans": [{"name": "claude_code.interaction", "ms": 12000}]},
        "slow": [SUMMARY],
    }
    (tmp_path / "canned.json").write_text(json.dumps(canned))
    exe = tmp_path / "traces"
    exe.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        f"c = json.load(open({str(tmp_path / 'canned.json')!r}))\n"
        "a = sys.argv[1:]\n"
        "open(%r, 'a').write(' '.join(a) + '\\n')\n"
        % str(tmp_path / "argv.log")
        + "if '719' in a: sys.stderr.write('traces phoenix: Phoenix unreachable\\n'); sys.exit(4)\n"
        "if a[0] == 'report': print(json.dumps(c['report']))\n"
        "else: print(json.dumps(c[a[1]]))\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("TOOLS_TRACES_BIN", str(exe))
    monkeypatch.setenv("TOOLS_STATE", str(tmp_path / "state"))
    return tmp_path


def test_report_merges_gateway_stats(stub: Path):
    gateway.call("traces.phoenix", {})  # one gateway row so the stats are not empty
    r = gateway.call("traces.report", {"days": 2})
    assert r.ok, r.error
    assert r.data["cost"]["by_trigger"]["owner"] == {"turns": 4, "cost": 3.0}
    assert r.data["owner"]["first_p95"] == 30000.0 and r.data["errors"]["top"] == {"Bash:exit_code": 2}
    assert r.data["gateway"]["calls"] >= 1 and "slow_turns" not in r.data  # concise drops the detail
    assert "first reply p50 9.0 s" in r.text and "agent: Build X" in r.text
    d = gateway.call("traces.report", {"format": "detailed"})
    assert d.data["agents"][0]["description"] == "Build X" and "traces.phoenix" in d.data["gateway_tools"]
    assert "report --days 2 --json" in (stub / "argv.log").read_text()


def test_phoenix_trace_slow(stub: Path):
    p = gateway.call("traces.phoenix", {"hours": 6})
    assert p.ok and p.data["forwarding"]["forwarded"] == 9 and "kept_out" not in p.data
    assert "his turns traced 4/4" in p.text and "last error URLError" in p.text
    t = gateway.call("traces.trace", {"trace_id": TID})
    assert t.ok and t.data["summary"]["tokens"] == 1200 and "slowest: Bash 3.0 s" in t.text
    assert gateway.call("traces.trace", {"trace_id": "last"}).ok
    assert gateway.call("traces.trace", {"trace_id": "not-hex"}).code == "invalid_args"
    s = gateway.call("traces.slow", {"hours": 48, "top": 3})
    assert s.ok and s.data["traces"][0]["trace_id"] == TID
    log = (stub / "argv.log").read_text()
    assert "phoenix status --hours 6 --json" in log and "phoenix slow --hours 48 --top 3 --json" in log


def test_phoenix_down_is_unavailable(stub: Path):
    bad = gateway.call("traces.slow", {"hours": 719})
    assert bad.code == "unavailable" and "traces.report" in bad.error["hint"]
    assert gateway.call("traces.report", {}).ok  # traces.db still answers
