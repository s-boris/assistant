"""Registry rules, generated files (schema snapshot, tools.toml, docs) and the gateway's checks."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tomllib
from pathlib import Path

import pytest
from pydantic import Field
from tools import gateway, gen
from tools.core import (
    EFFECTS,
    REGISTRY,
    Domain,
    Registry,
    ToolError,
    ToolInput,
    ToolOutput,
    load,
    mcp_name,
    tool,
)

REPO = Path(__file__).resolve().parents[2]
TP = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
TP2 = "00-11111111111111111111111111111111-2222222222222222-01"


def test_registry_shape():
    reg = load()
    assert {"reminders", "bindings"} <= set(reg.domains)
    names = [s.mcp_name for s in reg.typed.values()]
    assert len(names) == len(set(names))
    for s in reg.typed.values():
        assert re.fullmatch(r"[a-z0-9_]{1,64}", s.mcp_name), s.name  # Claude API tool-name rule
        assert s.doc and len(s.summary) < 120, s.name
        assert s.effect in EFFECTS and s.rate_limit, s.name
        assert "format" in s.input.model_json_schema()["properties"]
    assert {s.name for s in reg.in_domain("reminders")} == {
        "reminders.when",
        "reminders.list",
        "reminders.add",
        "reminders.snooze",
        "reminders.move",
        "reminders.cancel",
        "reminders.press",
        "reminders.fire",
    }
    assert reg.get("reminders_add") is reg.get("reminders.add")
    assert mcp_name("tg-cmd.bills") == "tg_cmd_bills"


def test_generated_files_fresh():
    assert gen.stale(REPO, load()) == [], "run `tool gen` (scripts/tool gen) and commit"


def test_tools_toml_is_the_bindings_registry():
    reg = load()
    d = tomllib.loads(gen.tools_toml(reg))
    assert d["schema"] == 1 and len(d["tools"]) == len(reg.argv) == 31
    for name, t in reg.argv.items():
        row = d["tools"][name]
        assert row["argv"] == list(t.argv) and row["effect"] == t.effect and row["description"] == t.description
    assert d["tools"]["remind.press"]["argv"] == ["~/bin/remind", "press"]
    assert d["tools"]["talk-button"] == {
        "argv": ["~/bin/talk-button"],
        "effect": "send_owner",
        "timeout_s": 120,
        "description": "The voice relay rings him once (Talk button)",
        "toast": "📞 Calling you…",
    }
    # every 0.1.22 instant command (+ /status) has its menu line; only the bound buttons with their own toast
    menus = {n: t["menu"] for n, t in d["tools"].items() if "menu" in t}
    assert len(menus) == 28 and menus["tg-status.show"] == "What I'm working on"
    assert menus["tg-cmd.queue"] == "What's downloading (Sonarr/Radarr)"
    assert sorted(n for n, t in d["tools"].items() if "toast" in t) == ["ha-autopilot-watch.fixed", "talk-button"]


def test_runtime_lock_matches_uv_lock():
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"] if "version" in p}
    req = (REPO / "packages" / "tools" / "requirements.lock").read_text()
    pins = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^ \\;]+)", req, re.M))
    assert pins["fastmcp"] == "4.1.3" and pins["pydantic"] == "2.13.5"
    for name, ver in pins.items():
        assert locked.get(name.lower().replace("_", "-")) == ver, name
    member = tomllib.loads((REPO / "packages" / "tools" / "pyproject.toml").read_text())
    assert member["project"]["dependencies"] == ["fastmcp==4.1.3", "pydantic==2.13.5", "cedarpy==4.12.2"]


def test_docs_and_tools_md_block():
    reg = load()
    md = gen.domain_md(reg, "reminders")
    assert "| `reminders.add` | `reminders_add` | home |" in md and "## CLI and cron" in md
    block = gen.tools_md_block(reg, "reminders")
    text = "intro\n" + block.replace("`reminders.add`", "OLD") + "\noutro\n"
    assert gen.patch_tools_md(text, reg, "reminders") == "intro\n" + block + "\noutro\n"
    with pytest.raises(ValueError):
        gen.patch_tools_md("no markers", reg, "reminders")


def test_bad_registrations_refused():
    reg = Registry()
    reg.add_domain(Domain("t", "T", "test", "gateway.t"))

    class In(ToolInput):
        n: int = 0

    class Out(ToolOutput):
        n: int

    with pytest.raises(ValueError):
        tool("t.x", effect="launch", registry=reg)(_fn(In, Out))

    def no_types(a):
        return Out(n=1)

    with pytest.raises(TypeError):
        tool("t.y", effect="read", registry=reg)(no_types)
    with pytest.raises(ValueError):
        tool("t.z", effect="read", rate="lots", registry=reg)(_fn(In, Out))
    tool("t.ok", effect="read", registry=reg)(_fn(In, Out))
    with pytest.raises(ValueError):
        tool("t.ok", effect="read", registry=reg)(_fn(In, Out))


def _fn(tin, tout, body=None):
    def f(a):
        return body(a) if body else tout(n=a.n)

    f.__annotations__ = {"a": tin, "return": tout}
    f.__doc__ = "A test tool.\nMore."
    return f


def _test_registry():
    reg = Registry()
    reg.add_domain(Domain("t", "T", "test", "gateway.t"))

    class In(ToolInput):
        n: int = Field(0, ge=0)

    class Out(ToolOutput):
        n: int
        extra: str = "detail"
        DETAIL = ("extra",)

    def boom(a):
        if a.n == 7:
            raise ToolError("not_found", "no seven", "try 8")
        raise RuntimeError("secret argument text")

    tool("t.echo", effect="read", rate="2/min", registry=reg)(_fn(In, Out))
    tool("t.boom", effect="read", registry=reg)(_fn(In, Out, boom))
    tool("t.gated", effect="home", approval="owner", registry=reg)(_fn(In, Out))
    return reg


def test_gateway_validation_rate_errors_and_log(remind_env, monkeypatch):
    reg = _test_registry()
    monkeypatch.setenv("TRACEPARENT", TP)
    r = gateway.call("t.echo", {"n": 3}, registry=reg)
    assert r.ok and r.data == {"n": 3} and r.trace_id == TP.split("-")[1] and r.trace_src == "env"
    assert gateway.call("t.echo", {"n": 3, "format": "detailed"}, registry=reg).data == {"n": 3, "extra": "detail"}
    third = gateway.call("t.echo", {"n": 1}, registry=reg)
    assert not third.ok and third.code == "rate_limited" and third.exit_code == 5
    bad = gateway.call("t.echo", {"n": -1}, registry=reg)
    assert bad.code == "invalid_args" and bad.exit_code == 2 and bad.error["message"].startswith("n:")
    assert gateway.call("t.echo", {"m": 1}, registry=reg).code == "invalid_args"  # extra fields refused
    nf = gateway.call("t.boom", {"n": 7}, registry=reg)
    assert nf.error == {"code": "not_found", "message": "no seven", "hint": "try 8"} and nf.exit_code == 3
    bug = gateway.call("t.boom", {"n": 1}, registry=reg)
    assert bug.code == "internal" and "secret" not in json.dumps(bug.as_json())
    assert gateway.call("t.nope", {}, registry=reg).code == "not_found"
    with sqlite3.connect(remind_env / "state" / "gateway.db") as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(calls)")]
        rows = c.execute("SELECT tool, code, trace, trace_src, surface FROM calls").fetchall()
    assert cols == ["ts", "tool", "surface", "trace", "trace_src", "ms", "code", "actor"]  # no args, no results
    assert ("t.echo", "ok", TP.split("-")[1], "env", "cli") in rows and len(rows) == 8
    st = gateway.stats(1)
    assert st["tools"]["t.echo"]["calls"] == 5 and st["tools"]["t.echo"]["errors"]["rate_limited"] == 1
    u = gateway._usage()
    if u is not None:  # one usage event per call: feature, via=gateway, trace id, code
        lines = [json.loads(x) for p in Path(u.SPOOL).glob("gateway-*.jsonl") for x in p.read_text().splitlines()]
        mine = [x for x in lines if x.get("at", {}).get("trace") == TP.split("-")[1]]
        assert {x["f"] for x in mine} == {"gateway.t", "gateway"}
        assert all(x["at"]["via"] == "gateway" and x["k"] == "invoke" and "ms" in x for x in mine)
        assert {x.get("a") for x in mine} == {"test"}  # USAGE_TEST=1 in CI: the actor is "test"


def test_gateway_trace_sources():
    tid, src = gateway.trace_id({"traceparent": TP})
    assert (tid, src) == (TP.split("-")[1], "meta")
    tid, src = gateway.trace_id({"traceparent": "00-" + "0" * 32 + "-b7ad6b7169203331-01"})
    assert src == "new" and re.fullmatch(r"[0-9a-f]{32}", tid)


def test_gateway_reads_the_env_traceparent_only_on_the_cli(remind_env, monkeypatch):
    """$TRACEPARENT is the caller's only for a one-shot `tool call`. The long-lived surfaces (the hub worker, the stdio
    MCP server) hold the one of whatever Bash call started them: each of their calls gets its own trace instead (the
    request's _meta.traceparent, else a new one)."""
    reg = _test_registry()
    monkeypatch.setenv("TRACEPARENT", TP)
    stale = TP.split("-")[1]
    assert gateway.trace_ctx(None) == (stale, "env", TP.split("-")[2])  # the CLI surface is the default
    for surface in ("hub", "mcp"):  # t.boom n=7: a quick typed error, and no 2/min limit as on t.echo
        a, b = (gateway.call("t.boom", {"n": 7}, surface=surface, registry=reg) for _ in range(2))
        assert a.trace_src == b.trace_src == "new" and len({a.trace_id, b.trace_id, stale}) == 3
        m = gateway.call("t.boom", {"n": 7}, surface=surface, registry=reg, meta={"traceparent": TP2})
        assert (m.trace_id, m.trace_src) == (TP2.split("-")[1], "meta")
        assert gateway.trace_ctx(None, surface)[1:] == ("new", None)
    assert gateway.call("t.boom", {"n": 7}, surface="cli", registry=reg).trace_id == stale
    with sqlite3.connect(remind_env / "state" / "gateway.db") as c:
        rows = c.execute("SELECT surface, trace_src FROM calls WHERE trace = ?", (stale,)).fetchall()
    assert rows == [("cli", "env")]


def test_drop_inherited_trace(monkeypatch):
    monkeypatch.setenv("TRACEPARENT", TP)
    monkeypatch.setenv("TRACESTATE", "k=v")
    assert gateway.drop_inherited_trace() == ["TRACEPARENT", "TRACESTATE"]
    assert "TRACEPARENT" not in os.environ and "TRACESTATE" not in os.environ
    assert gateway.drop_inherited_trace() == []


def test_gateway_approval(remind_env, monkeypatch):
    reg = _test_registry()
    r = gateway.call("t.gated", {"n": 1}, registry=reg)
    assert r.code == "approval_required" and r.exit_code == 4 and "approvals new" in r.error["hint"]
    stub = remind_env / "approvals"
    stub.write_text(f'#!/bin/sh\necho "$*" >> {remind_env / "ap.log"}\nexit ${{AP_RC:-0}}\n')
    stub.chmod(0o755)
    monkeypatch.setenv("TOOLS_APPROVALS_BIN", str(stub))
    assert gateway.call("t.gated", {"n": 1}, approval="abc123", registry=reg).ok
    assert (remind_env / "ap.log").read_text().split() == [
        "consume",
        "abc123",
        "--action",
        "tool:t.gated",
        "--by",
        "gateway",
    ]
    monkeypatch.setenv("AP_RC", "3")
    r = gateway.call("t.gated", {"n": 1}, approval="abc123", registry=reg)
    assert r.code == "approval_required" and "expired" in r.error["message"]


def test_global_registry_untouched_by_tests():
    assert all(not n.startswith("t.") for n in REGISTRY.typed)


def test_gateway_span_joins_the_caller_trace(remind_env, monkeypatch):
    """One OTLP span per call to the local receiver, parented to the traceparent's span; no args or results."""
    import http.server
    import threading

    hits: list = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ASSISTANT_OTLP_ENDPOINT", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setenv("TRACEPARENT", TP)
        reg = _test_registry()
        assert gateway.call("t.echo", {"n": 42}, registry=reg).ok
        assert gateway.call("t.boom", {"n": 7}, registry=reg).code == "not_found"
        monkeypatch.delenv("TRACEPARENT")
        assert gateway.call("t.echo", {"n": 43}, registry=reg, meta={"traceparent": TP}).trace_src == "meta"
        monkeypatch.setenv("ASSISTANT_OTLP", "off")
        assert gateway.call("t.echo", {"n": 44}, registry=reg).trace_src == "new"
    finally:
        srv.shutdown()
    spans = [rs["scopeSpans"][0]["spans"][0] for h in hits for rs in h["resourceSpans"]]
    assert len(spans) == 3  # the switch stops the fourth
    ok, boom, meta = spans
    assert (
        ok["name"] == "gateway.t.echo" and ok["traceId"] == TP.split("-")[1] and ok["parentSpanId"] == TP.split("-")[2]
    )
    assert ok["status"]["code"] == 1 and boom["status"]["code"] == 2 and meta["parentSpanId"] == TP.split("-")[2]
    attrs = {a["key"]: next(iter(a["value"].values())) for a in boom["attributes"]}
    assert attrs["gateway.code"] == "not_found" and attrs["gateway.domain"] == "t" and attrs["gateway.surface"] == "cli"
    keys = {a["key"] for sp in spans for a in sp["attributes"]}
    assert keys <= {f"gateway.{k}" for k in ("tool", "domain", "surface", "code", "trace_src", "actor")}
    assert "seven" not in json.dumps(hits)  # neither arguments nor results (nor error messages)
    assert int(ok["endTimeUnixNano"]) >= int(ok["startTimeUnixNano"])
