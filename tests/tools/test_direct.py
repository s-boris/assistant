"""The per-domain rollback switch: direct-<domain> (or direct-all) in the config dir makes that domain's tools refuse
with a CLI hint (no restart), the MCP server leaves them out, `tool direct` lists and flips it; others keep working."""

from __future__ import annotations

import asyncio

from fastmcp import Client
from tools import cli, gateway, mcp_server
from tools.core import load


def test_direct_switch(stubs, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TOOLS_CONFIG", str(tmp_path / "cfg"))
    stubs.on("media", ["queue"], {"movies": [], "tv": []})
    stubs.on("cat-autopilot", ["stock", "show"], {})
    assert gateway.call("media.queue").ok and gateway.direct_domains() == set()

    assert cli.main(["direct", "media", "on"]) == 0
    r = gateway.call("media.queue")
    assert not r.ok and r.error["code"] == "unavailable" and "direct" in r.error["message"]
    assert "tool direct media off" in r.error["hint"]
    assert gateway.call("cat.stock").ok  # other domains unaffected
    assert stubs.calls("media") == [["queue", "--json"]]  # the refused call never ran the CLI

    async def names():
        async with Client(mcp_server.build()) as c:
            return {t.name for t in await c.list_tools()}

    served = asyncio.run(names())
    reg = load()
    assert not {s.mcp_name for s in reg.in_domain("media")} & served
    assert {s.mcp_name for s in reg.in_domain("cat")} <= served

    capsys.readouterr()
    assert cli.main(["direct"]) == 0
    rows = dict(line.split() for line in capsys.readouterr().out.splitlines())
    assert rows["media"] == "direct" and rows["cat"] == "gateway" and "bindings" not in rows

    assert cli.main(["direct", "all", "on"]) == 0
    assert gateway.call("cat.stock").error["code"] == "unavailable"
    assert asyncio.run(names()) == set()
    assert cli.main(["direct", "all", "off"]) == 0
    assert cli.main(["direct", "media", "off"]) == 0
    assert gateway.call("media.queue").ok and gateway.direct_domains() == set()
    assert cli.main(["direct", "nosuch", "on"]) == 2 and cli.main(["direct", "media", "maybe"]) == 2
