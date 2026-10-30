"""Per-call log (local JSONL), outbound calls to the owner, and the hand-off prompt. Fake Gemini + fake call client."""
import asyncio
import json
import os
import stat
import time
from datetime import datetime, timedelta

import aiohttp
import pytest

from util import TOKEN, Rig, until
from voice_relay.audio import tone
from voice_relay.calllog import (CallLog, calls_dir, digest, format_call, load_calls, outgoing_summary,
                                  print_calls)
from voice_relay.fakes import FakeCallClient
from voice_relay.providers import ASK_TOOL, make_provider
from voice_relay.server import instructions
from voice_relay.tgcall import BridgeRefused, CallBridge, PyTgCallsClient

OWNER = 111222333


def run(coro):
    return asyncio.run(coro)


async def bridge(r, client=None, **kw):
    client = client or FakeCallClient()
    b = CallBridge(r.cfg, client, OWNER, r.relay.handoff, lambda: instructions(r.cfg), make_provider, **kw)
    await b.start()
    return b, client


def events(cfg):
    calls = load_calls(calls_dir(cfg), 50)
    return [e for c in calls for e in c]


def test_owner_call_is_logged_turn_by_turn(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["what is the current weather"])
        try:
            b, cl = await bridge(r)
            await cl.ring("c1", OWNER)
            await cl.speak("c1", tone(48000, 1200))
            assert await until(lambda: r.gem.log["tool_responses"], 5)
            await asyncio.sleep(0.2)
            await cl.end("c1")
            assert await until(lambda: not b.calls)
            d = calls_dir(r.cfg)
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
            f = os.path.join(d, datetime.now().strftime("%Y-%m-%d") + ".jsonl")
            assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
            evs = events(r.cfg)
            kinds = [e["ev"] for e in evs]
            assert kinds[0] == "start" and evs[0]["caller"] == "owner" and evs[0]["kind"] == "tg-in"
            assert kinds[-1] == "end" and evs[-1]["reason"] == "hangup" and evs[-1]["tools"] == 1
            assert evs[-1]["duration_s"] >= 0
            him = [e["text"] for e in evs if e["ev"] == "him"]
            assert him == ["what is the current weather"]
            assert [e["closing"] for e in evs if e["ev"] == "him"] == ["other"]
            assert all("closing" not in e for e in evs if e["ev"] == "gemini")
            assert any(e["ev"] == "gemini" and e["text"].startswith("Let me check") for e in evs)
            tool = next(e for e in evs if e["ev"] == "tool")
            assert tool["name"] == "ask_assistant" and tool["args"]["task"] == "what is the current weather"
            assert tool["handoff"].startswith("v") and tool["route"] == "channel" and tool["call_id"] == "fc-1"
            res = next(e for e in evs if e["ev"] == "result")
            assert res["handoff"] == tool["handoff"] and res["outcome"] == "answer"
            assert kinds.index("him") < kinds.index("tool") < kinds.index("result")
            out = []
            assert print_calls(r.cfg, 1, out=out.append) == 0
            text = out[0]
            assert "caller: owner" in text and "end: hangup" in text and "him: what is the current weather [other]" in text
            assert "tool ask_assistant → %s (channel)" % tool["handoff"] in text
        finally:
            await r.close()
    run(main())


def test_strangers_and_busy_are_logged_without_ids(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            b, cl = await bridge(r)
            await cl.ring("x1", 999)
            evs = events(r.cfg)
            assert [e["ev"] for e in evs] == ["start", "end"]
            assert evs[0]["caller"] == "declined: not the owner" and "999" not in json.dumps(evs)
        finally:
            await r.close()
    run(main())


def test_old_logs_are_pruned_and_last_n(tmp_path):
    class C:
        state_dir = str(tmp_path / "state")
    d = calls_dir(C)
    os.makedirs(d)
    today = datetime.now().date()
    old = os.path.join(d, (today - timedelta(days=20)).isoformat() + ".jsonl")
    keep = os.path.join(d, (today - timedelta(days=13)).isoformat() + ".jsonl")
    for p in (old, keep):
        with open(p, "w") as fh:
            fh.write(json.dumps({"ts": time.time() - 13 * 86400, "call": "cold", "ev": "start", "kind": "tg-in",
                                 "caller": "owner"}) + "\n")
    lg = CallLog(C, "tg-in", "owner")
    lg.text("user", "hallo ")
    lg.text("user", "wie geht's")
    lg.text("assistant", "Gut, ")
    lg.text("assistant", "danke!")
    lg.end("hangup")
    lg.event("him", text="after the end is ignored")
    assert not os.path.exists(old) and os.path.exists(keep)
    calls = load_calls(d, 2)
    assert [c[0]["call"] for c in calls] == ["cold", lg.id]
    last = format_call(calls[-1])
    assert "him: hallo wie geht's" in last and "gemini: Gut, danke!" in last and "after the end" not in last
    assert "no end recorded" in format_call(calls[0])


def test_outbound_call_answered_opens_with_the_reason(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            b, cl = await bridge(r)
            res = await b.call_owner("the parcel that arrived")
            assert res["status"] == "answered" and cl.dialed == [OWNER]
            assert b.calls.get(OWNER) is not None
            assert await until(lambda: r.gem.log["injects"], 3)
            op = r.gem.log["injects"][0]
            assert op.startswith("[outbound call]") and "the parcel that arrived" in op
            assert "never read it out word for word" in op and "Greet him only once" in op
            assert "you won't hear it" in op                                  # prefetched at pick-up, played after the wait
            assert await until(lambda: len(cl.sent[OWNER]) > 0, 3)           # the opening is spoken on the call
            assert (await b.call_owner("again"))["status"] == "in_call"
            await cl.ring("c2", OWNER)                                         # his own call while on: busy
            assert cl.declined == ["c2"]
            await cl.end(OWNER)
            assert await until(lambda: not b.calls)
            evs = [e for e in events(r.cfg) if e["call"] == res["call"]]
            assert evs[0]["kind"] == "tg-out" and evs[0]["reason"] == "the parcel that arrived"
            assert [e["outcome"] for e in evs if e["ev"] == "ring"] == ["answered"]
            assert any(e["ev"] == "opening" for e in evs) and evs[-1]["ev"] == "end"
            assert [e.get("heard") for e in evs if e["ev"] == "pickup"] == [False]
            assert evs[-1]["reason"] == "hangup" and evs[-1]["by"] == "him"
            assert "end: hangup (by him)" in format_call(evs)
            d = digest(evs)                                                    # what the hub hands out
            assert (d["outcome"], d["answered"], d["ring"][0]) == ("answered", True, "answered")
        finally:
            await r.close()
    run(main())


@pytest.mark.parametrize("outcome,status", [("no_answer", "no_answer"), ("declined", "declined"),
                                            ("busy", "busy"), ("error", "failed")])
def test_outbound_not_answered(tmp_path, outcome, status):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            b, cl = await bridge(r, FakeCallClient(dial_outcome=outcome))
            res = await b.call_owner("test call")
            assert res["status"] == status and not b.calls and not b.ringing
            assert cl.hung == ([OWNER] if status == "failed" else [])        # failed: make sure nothing rings on
            evs = [e for e in events(r.cfg) if e["call"] == res["call"]]
            assert evs[-1]["ev"] == "end" and evs[-1]["reason"] == status.replace("_", " ")
            assert r.gem.log["injects"] == []                                  # Gemini never spoke
            d = digest(evs)            # the relay's own answer to the caller = the digest's outcome, never answered
            assert (d["outcome"], d["answered"], d["talked"]) == (status, False, False)
        finally:
            await r.close()
    run(main())


def test_outbound_rate_limit_and_endpoint(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            b, cl = await bridge(r, FakeCallClient(dial_outcome="no_answer"))
            async with aiohttp.ClientSession() as h:
                url = "http://127.0.0.1:%d/internal/call" % r.iport
                hdr = {"Authorization": "Bearer " + TOKEN}
                async with h.post(url, json={"reason": "x"}, headers=hdr) as resp:
                    assert (await resp.json())["status"] == "unavailable"          # no bridge on this relay yet
                r.relay._bridge = b
                for _ in range(3):
                    async with h.post(url, json={"reason": "test call"}, headers=hdr) as resp:
                        assert (await resp.json())["status"] == "no_answer"
                async with h.post(url, json={"reason": "test call"}, headers=hdr) as resp:
                    assert (await resp.json())["status"] == "rate_limited"
                async with h.post(url, json={"reason": "x"}) as resp:
                    assert resp.status == 401
            assert cl.dialed == [OWNER] * 3
            ds = [digest(c) for c in load_calls(calls_dir(r.cfg), 50)]         # the 4th never rang
            assert [d["outcome"] for d in ds] == ["no_answer"] * 3 + ["rate_limited"]
            assert outgoing_summary(ds) == {"calls": 4, "answered": 0, "not_answered": 4, "open": 0,
                                            "by": {"no_answer": 3, "rate_limited": 1}}
        finally:
            await r.close()
    run(main())


def test_dial_refuses_anyone_but_the_owner():
    c = PyTgCallsClient({})
    with pytest.raises(BridgeRefused):
        run(c.dial(OWNER, 45))                     # no callee set yet
    c.callee = OWNER
    with pytest.raises(BridgeRefused):
        run(c.dial(999, 45))


def test_handoff_prompt_and_tool_description(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            text = instructions(r.cfg)
        finally:
            await r.close()
        return text
    text = run(main())
    assert "EVERY request" in text and "weather" in text and "Never drop one" in text
    assert 'Never say "done", "I\'ll do that"' in text and "without calling ask_assistant" in text
    assert "[outbound call]" in text
    d = ASK_TOOL["description"]
    assert "EVERY request" in d and "Never say something is done" in d
