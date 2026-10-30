"""The FastMCP server `hub`: tool list = registry, schemas from the models, CLI = MCP parity, typed errors, the
trace id from the request's _meta, and a real stdio round trip (nothing but protocol on stdout)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from tools import cli, mcp_server
from tools.core import load

REPO = Path(__file__).resolve().parents[2]
TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def run(coro):
    return asyncio.run(coro)


def test_tool_list_and_schemas():
    reg = load()

    async def go():
        async with Client(mcp_server.build()) as c:
            return await c.list_tools()

    tools = {t.name: t for t in run(go())}
    assert set(tools) == {s.mcp_name for s in reg.typed.values()}
    for s in reg.typed.values():
        t = tools[s.mcp_name]
        want = s.input.model_json_schema()
        assert set(t.input_schema["properties"]) == set(want["properties"])
        assert set(t.input_schema.get("required", [])) == set(want.get("required", []))
        assert set(t.output_schema["properties"]) == set(s.output.model_json_schema()["properties"])
        assert t.annotations.read_only_hint == (s.effect == "read")
    assert tools["reminders_cancel"].annotations.destructive_hint is True


def test_cli_mcp_parity_and_errors(remind_env, capsys):
    async def go():
        async with Client(mcp_server.build()) as c:
            w = await c.call_tool("reminders_when", {"phrase": "tomorrow 9"}, meta={"traceparent": TP})
            a = await c.call_tool("reminders_add", {"text": "via mcp", "owner": True, "when": "2026-10-24 08:00"})
            ls = await c.call_tool("reminders_list", {"format": "detailed"})
            bad = await c.call_tool("reminders_add", {"text": "x", "owner": True}, raise_on_error=False)
            return w, a, ls, bad

    w, a, ls, bad = run(go())
    assert not w.is_error and w.structured_content == {"kind": "at", "at": "2026-10-21 09:00", "why": "tomorrow"}
    assert w.content[0].text.startswith("2026-10-21 09:00")
    assert a.structured_content["kind"] == "timed" and len(a.structured_content["id"]) == 8
    assert bad.is_error and bad.content[0].text.startswith("invalid_args:")
    for name, args, got in (
        ("reminders.when", {"phrase": "tomorrow 9"}, w),
        ("reminders.list", {"format": "detailed"}, ls),
    ):
        assert cli.main(["call", name, json.dumps(args), "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["data"] == got.structured_content and out["text"] == got.content[0].text
    with sqlite3.connect(remind_env / "state" / "gateway.db") as c:
        rows = c.execute("SELECT tool, surface, trace, trace_src FROM calls WHERE surface = 'mcp'").fetchall()
    assert ("reminders.when", "mcp", TP.split("-")[1], "meta") in rows
    assert all(r[3] in ("meta", "new", "env") for r in rows) and len(rows) == 4


def test_stdio_round_trip(remind_env):
    boot = "import sys; sys.path[:0] = sys.argv[1:]; from tools.cli import main; sys.exit(main(['serve']))"
    # -I drops PYTHONPATH: the hub CI runner's packages (fastmcp & co) sit on it (its toolchain's site), not in a venv
    site = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env = {k: v for k, v in os.environ.items() if k.startswith(("REMIND_", "TRIPCLOCK_", "TOOLS_", "USAGE"))}
    # ASSISTANT_OTLP=off: the server is a fresh process outside pytest's environment, so its span would otherwise
    # reach the live receiver
    env.update(HOME=os.environ["HOME"], PATH=os.environ.get("PATH", "/usr/bin:/bin"), ASSISTANT_OTLP="off")
    # the hub CI runner's uid has a passwd entry only through nss_wrapper, and scripts/remind reads pwd
    env.update({k: v for k, v in os.environ.items() if k.startswith("NSS_WRAPPER_")})
    if "nss_wrapper" in os.environ.get("LD_PRELOAD", ""):
        env["LD_PRELOAD"] = os.environ["LD_PRELOAD"]
    env["TRACEPARENT"] = TP  # Claude Code started the server inside some Bash call: its calls never join that trace
    tr = StdioTransport(
        command=sys.executable, args=["-I", "-c", boot, str(REPO), str(REPO / "packages"), *site], env=env
    )

    async def go():
        async with Client(tr) as c:
            names = {t.name for t in await c.list_tools()}
            r = await c.call_tool("reminders_when", {"phrase": "in 2 hours"})
            return names, r

    names, r = run(go())
    assert "reminders_add" in names and r.structured_content["at"] == "2026-10-20 14:00"
    with sqlite3.connect(remind_env / "state" / "gateway.db") as c:
        assert c.execute("SELECT surface, trace_src FROM calls").fetchall() == [("mcp", "new")]
