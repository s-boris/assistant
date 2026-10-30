"""An in-hub caller (hubwf's job.tick) may set a call's own limit (timeout_s, at most CALL_TIMEOUT_MAX): the
gateway waits that long plus ANSWER_GRACE for a claimed call instead of the tool's timeout_s. Calls without it keep
the tool's limit. A fake executor that answers late, a synthetic registry, no HTTP, no token on disk."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from tools import hubgw
from tools.core import Domain, Registry, ToolInput, ToolOutput, tool
from tools.policy import stamp_mac

TOKENS = {"brain": b"B" * 40}


class In(ToolInput):
    x: int = 0


class Out(ToolOutput):
    n: int = 0


def reg() -> Registry:
    r = Registry()
    r.add_domain(Domain("e", "E", "synthetic", "test"))

    def run(a: In) -> Out:
        return Out(n=1)

    tool("e.short", effect="read", timeout_s=1, registry=r)(run)
    tool("e.long", effect="read", timeout_s=3, registry=r)(run)
    return r


def stamp(mcp_name: str) -> dict[str, Any]:
    ts = int(time.time())
    mac = stamp_mac(TOKENS["brain"], "brain", "", "", "S", ts, mcp_name)
    return {"p": "brain", "a": "", "t": "", "s": "S", "ts": ts, "m": mac}


@pytest.fixture
def gw(tmp_path, monkeypatch):
    monkeypatch.setattr(hubgw, "CLAIM_GRACE", 0.5)
    monkeypatch.setattr(hubgw, "ANSWER_GRACE", 0.1)
    return hubgw.Gateway(TOKENS, state=tmp_path / "gw", registry=reg())


def _run(gw: hubgw.Gateway, name: str, delay: float, **kw: Any) -> dict[str, Any]:
    """One call (one event loop per gateway: the fixture makes a fresh one per test)."""

    async def go() -> dict[str, Any]:
        async def executor() -> None:
            while True:
                job = await gw.exec.claim(0.2)
                if job:
                    await asyncio.sleep(delay)
                    gw.exec.done(job["id"], {"ok": True, "data": {"n": 1}, "text": "n=1", "ms": 1})

        t = asyncio.get_running_loop().create_task(executor())
        await asyncio.sleep(0.05)
        try:
            return await gw.call(name, {"x": 1, "_caller": stamp(name.replace(".", "_"))}, caller="brain", **kw)
        finally:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    return asyncio.run(go())


def test_in_time_without_override(gw):
    assert _run(gw, "e.short", 0.2)["ok"]


def test_tool_limit_without_override(gw):
    assert _run(gw, "e.short", 1.5)["code"] == "timeout"  # the tool's 1 s + 0.1 s grace


def test_longer_per_call_limit_waits(gw):
    assert _run(gw, "e.short", 1.5, timeout_s=3)["ok"]


def test_shorter_per_call_limit_gives_up_sooner(gw):
    t0 = time.monotonic()
    assert _run(gw, "e.long", 1.8, timeout_s=1)["code"] == "timeout"  # 1 s + grace, not the tool's 3 s
    assert time.monotonic() - t0 < 1.8


def test_cap(monkeypatch):
    assert hubgw.CALL_TIMEOUT_MAX == 3600.0
