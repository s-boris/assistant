"""Routing of adapter-run tools in the hub gateway (tools.hubgw.Gateway.route): voice.* go to the voice adapter's
claim queue only while that adapter is live (a claim poll within OFFLINE_AFTER); otherwise they run on the brain's
executor. The adapter's own brain-side client (voice-hub: principal reflex, stamp t=voice-hub) never
falls back: the brain's executor would hand its call back to it. Fake executors on the gateway's queues, a synthetic
registry, no HTTP, no relay, no token on disk."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from tools import hubgw
from tools.core import Domain, Registry, ToolInput, ToolOutput, tool
from tools.policy import stamp_mac

TOKENS = {"brain": b"B" * 40, "voice": b"V" * 40, "phone": b"P" * 40}


class In(ToolInput):
    x: int = 0


class Out(ToolOutput):
    n: int = 0


def reg() -> Registry:
    r = Registry()
    r.add_domain(Domain("voice", "Voice", "synthetic voice", "test"))
    r.add_domain(Domain("phone", "Phone", "synthetic phone", "test"))
    r.add_domain(Domain("e", "E", "synthetic", "test"))

    def run(a: In) -> Out:
        return Out(n=1)

    for name, effect in (("voice.reply", "send_owner"), ("voice.status", "read"), ("voice.call", "send_owner"),
                         ("phone.open", "send_owner"), ("e.look", "read")):  # fmt: skip
        tool(name, effect=effect, registry=r)(run)
    return r


def stamp(mcp_name: str, p: str = "brain", t: str = "") -> dict[str, Any]:
    ts = int(time.time())
    return {"p": p, "a": "", "t": t, "s": "S", "ts": ts, "m": stamp_mac(TOKENS["brain"], p, "", t, "S", ts, mcp_name)}


class Fake:
    """Long-polls one of the gateway's queues like the brain's worker or the voice relay; records what it ran."""

    def __init__(self, ex: hubgw.Exec, name: str, ran: list[tuple[str, str]]) -> None:
        self.ex, self.name, self.ran = ex, name, ran
        self.task: asyncio.Task | None = None

    async def loop(self) -> None:
        while True:
            job = await self.ex.claim(0.2)
            if job:
                self.ran.append((self.name, job["tool"]))
                self.ex.done(job["id"], {"ok": True, "data": {"n": 1}, "text": "n=1", "ms": 1})

    def start(self) -> Fake:
        self.task = asyncio.get_running_loop().create_task(self.loop())
        return self

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


@pytest.fixture
def gw(tmp_path, monkeypatch):
    monkeypatch.setattr(hubgw, "CLAIM_GRACE", 0.5)
    return hubgw.Gateway(TOKENS, state=tmp_path / "gw", registry=reg())


async def _call(gw: hubgw.Gateway, name: str, p: str = "brain", t: str = "") -> dict[str, Any]:
    mcp = name.replace(".", "_")
    return await gw.call(name, {"x": 1, "_caller": stamp(mcp, p, t)}, caller="brain")


def _route(gw: hubgw.Gateway) -> str:
    return gw.health()["adapters"]["voice"]["route"]


def test_no_adapter_runs_voice_tools_on_the_brain_executor(gw):
    """No claude-hub-voice polling: voice.* run on the brain's executor."""

    async def go():
        ran: list[tuple[str, str]] = []
        brain = Fake(gw.exec, "brain", ran).start()
        await asyncio.sleep(0.05)
        out = [await _call(gw, n) for n in ("voice.reply", "voice.status", "voice.call", "e.look")]
        out.append(await _call(gw, "voice.reply", p="reflex", t="remind"))  # a reflex other than voice-hub too
        await brain.stop()
        return ran, out

    ran, out = asyncio.run(go())
    assert all(r["ok"] for r in out), out
    assert ran == [("brain", t) for t in ("voice.reply", "voice.status", "voice.call", "e.look", "voice.reply")]
    assert _route(gw) == "brain" and gw.health()["adapters"]["voice"]["online"] is False


def test_live_adapter_takes_its_tools(gw):
    """The relay polls with the voice token and runs voice.*; everything else stays the brain's."""

    async def go():
        ran: list[tuple[str, str]] = []
        brain = Fake(gw.exec, "brain", ran).start()
        relay = Fake(gw.execs["voice"], "voice", ran).start()
        await asyncio.sleep(0.05)
        route = _route(gw)
        out = [await _call(gw, n) for n in ("voice.reply", "voice.status", "e.look", "phone.open")]
        out.append(await _call(gw, "voice.call", p="reflex", t="voice-hub"))
        await relay.stop()
        await brain.stop()
        return ran, out, route

    ran, out, route = asyncio.run(go())
    assert all(r["ok"] for r in out), out
    assert route == "adapter"
    assert ran == [("voice", "voice.reply"), ("voice", "voice.status"), ("brain", "e.look"), ("brain", "phone.open"),
                   ("voice", "voice.call")]  # fmt: skip


def test_stale_adapter_falls_back_to_the_brain_but_never_for_voice_hub(gw):
    """The relay stopped polling (held, crashed, rolled back): after OFFLINE_AFTER voice.* go back to the brain's
    executor (its CLIs: voice-relay, which hands them to voice-hub). voice-hub's own calls stay
    on the dead queue and answer unavailable at once (voice-hub: exit 2, relay down): no round through the brain."""

    async def go():
        ran: list[tuple[str, str]] = []
        brain = Fake(gw.exec, "brain", ran).start()
        relay = Fake(gw.execs["voice"], "voice", ran).start()
        await asyncio.sleep(0.05)
        await relay.stop()
        gw.execs["voice"].last_poll = time.monotonic() - hubgw.OFFLINE_AFTER - 1  # its last poll is stale
        route = _route(gw)
        fell = await _call(gw, "voice.reply")
        t0 = time.monotonic()
        closed = await _call(gw, "voice.reply", p="reflex", t="voice-hub")
        dt = time.monotonic() - t0
        await brain.stop()
        return ran, route, fell, closed, dt

    ran, route, fell, closed, dt = asyncio.run(go())
    assert route == "brain" and fell["ok"] and ran == [("brain", "voice.reply")]
    assert closed["ok"] is False and closed["code"] == "unavailable" and dt < 0.4
    assert "voice adapter is offline" in closed["error"]["message"] and "hub voice status" in closed["error"]["hint"]


def test_adapter_gone_inside_the_grace_fails_closed(gw):
    """Polled less than OFFLINE_AFTER ago but no longer claiming: still the adapter's call (no second runner): it
    answers unavailable after CLAIM_GRACE, naming the adapter, and the brain never runs it."""

    async def go():
        ran: list[tuple[str, str]] = []
        brain = Fake(gw.exec, "brain", ran).start()
        gw.execs["voice"].last_poll = time.monotonic() - 1
        res = await _call(gw, "voice.status")
        await brain.stop()
        return ran, res

    ran, res = asyncio.run(go())
    assert ran == [] and res["code"] == "unavailable"
    assert res["error"]["message"] == "the voice adapter did not pick the call up"


def test_no_brain_executor_either_answers_unavailable_naming_the_brain(gw):
    async def go():
        return await _call(gw, "voice.reply")

    res = asyncio.run(go())
    assert res["code"] == "unavailable" and res["error"]["message"] == "the brain's executor is offline"


def test_phone_tools_stay_brain_run(gw):
    """phone.* are not adapter-run; the phone token has no claim queue (its /gw/claim answers 403)."""
    assert "phone" not in hubgw.ADAPTER_RUN and not [t for t in hubgw.RUNNER if t.startswith("phone.")]
    assert "phone" not in gw.execs and set(gw.health()["adapters"]) == {"voice"}
    assert set(hubgw.ADAPTER_CLIENT) <= set(hubgw.ADAPTER_RUN)
