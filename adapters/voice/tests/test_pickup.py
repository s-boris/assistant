"""Outbound pick-up (wait for his hello, greet once, reason paraphrased or --say), who ended a call, `live` in
status counting Telegram calls, and reply latency in the call log. Fake Gemini + fake call client; nothing rings."""
import asyncio

import aiohttp

from util import TOKEN, Rig, until
from voice_relay.audio import tone
from voice_relay.calllog import CallLog, calls_dir, ended_by, format_call, load_calls
from voice_relay.config import Config
from voice_relay.fakes import FakeCallClient
from voice_relay.providers import ASK_TOOL, make_provider
from voice_relay.server import instructions
from voice_relay.tgcall import CallBridge, opening_text

OWNER = 111222333
FAST = dict(opening_wait_s=0.4, opening_quiet_s=0.25, opening_max_s=2.0, opening_prefetch=False)
PREFETCH = dict(FAST, opening_prefetch=True)


def run(coro):
    return asyncio.run(coro)


async def bridge(r, client=None):
    client = client or FakeCallClient()
    b = CallBridge(r.cfg, client, OWNER, r.relay.handoff, lambda: instructions(r.cfg), make_provider)
    await b.start()
    return b, client


def call_events(cfg, cid):
    return [e for c in load_calls(calls_dir(cfg), 50) for e in c if e["call"] == cid]


def test_opening_paraphrases_the_reason_or_uses_say():
    op = opening_text("test call: you asked to test calling now")
    assert "test call, you asked to test calling now" in op and "never read it out word for word" in op
    assert "I'm calling about test call" not in op and op.count("Hi, it's Claude") == 1
    op = opening_text("test call: you asked", say="the test call you asked for", heard=True)
    assert '"the test call you asked for"' in op and "note for you" not in op and "short hello" in op
    assert "ask briefly what he needs" in opening_text()
    for o in (op, opening_text("x"), opening_text()):
        assert "don't start it again" in o                         # interrupted: answer him, no second greeting
    assert "One sec." in ASK_TOOL["description"]                   # filler while the hand-off runs


def test_his_hello_is_held_back_then_one_opening(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), **FAST)
        try:
            b, cl = await bridge(r)
            res = await b.call_owner("the parcel that arrived")
            assert res["status"] == "answered"
            await cl.speak(OWNER, tone(48000, 400))                # "Hello?" on pick-up
            await asyncio.sleep(0.1)
            assert r.gem.log["audio_bytes"] == 0 and not r.gem.log["injects"]   # held: the model never heard it
            assert await until(lambda: r.gem.log["injects"], 3)
            assert "short hello" in r.gem.log["injects"][0]
            assert await until(lambda: len(cl.sent[OWNER]) > 0, 3)
            await cl.speak(OWNER, tone(48000, 200))                # after the opening his audio flows again
            assert await until(lambda: r.gem.log["audio_bytes"] > 0, 2)
            await asyncio.sleep(0.2)
            assert len(r.gem.log["injects"]) == 1                  # the opening went out once
            await cl.end(OWNER)
            assert await until(lambda: not b.calls)
            evs = call_events(r.cfg, res["call"])
            pk = [e for e in evs if e["ev"] == "pickup"]
            assert pk and pk[0]["heard"] is True and pk[0]["waited_ms"] >= 250
            rep = [e for e in evs if e["ev"] == "reply"]
            assert rep and 200 <= rep[0]["after_ms"] < 2000       # his hello's end → the greeting's first audio
            out = format_call(evs)
            assert "picked up: he said hello" in out and "reply latency (his end of speech" in out
        finally:
            await r.close()
    run(main())


def test_status_live_counts_the_telegram_call_and_calls_shows_still_on(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), **FAST)
        try:
            b, cl = await bridge(r)
            r.relay._bridge = b
            hdr = {"Authorization": "Bearer " + TOKEN}
            base = "http://127.0.0.1:%d/internal/" % r.iport
            async with aiohttp.ClientSession() as h:
                async with h.post(base + "call", json={"reason": "test call: you asked", "say": "the test call"},
                                  headers=hdr) as resp:
                    res = await resp.json()
                assert res["status"] == "answered"
                async with h.get(base + "status", headers=hdr) as resp:
                    st = await resp.json()
                assert st["live"] == 1 and st["call_ids"] == [res["call"]] and st["call_on"] is True
                evs = call_events(r.cfg, res["call"])
                assert evs[0]["say"] == "the test call"
                assert "still on (live now)" in format_call(evs, live=st["call_ids"])
                assert "the relay stopped mid-call" in format_call(evs, live=[])
                assert "the relay is not running" in format_call(evs, live=None)
                assert await until(lambda: r.gem.log["injects"], 3)
                assert '"the test call"' in r.gem.log["injects"][0]
                await cl.end(OWNER)
                assert await until(lambda: not b.calls)
                async with h.get(base + "status", headers=hdr) as resp:
                    st = await resp.json()
                assert st["live"] == 0 and st["call_ids"] == []
        finally:
            await r.close()
    run(main())


def test_end_records_who_ended_the_call(tmp_path):
    cfg = Config(state_dir=str(tmp_path / "state"))
    assert ended_by("hangup") == "him" and ended_by("assistant_hangup") == "assistant"
    assert ended_by("provider failed: 1011") == "error" and ended_by("time limit") == "time limit"
    assert ended_by("declined") is None
    for reason, by, want in (("assistant_hangup", "silence", "silence"), ("hangup", None, "him"),
                             ("provider ended", None, "error")):
        log = CallLog(cfg, "tg-out", "outbound to the owner")
        log.end(reason, by)
        log.end("again")                                           # only the first end counts
        evs = call_events(cfg, log.id)
        ends = [e for e in evs if e["ev"] == "end"]
        assert len(ends) == 1 and ends[0]["by"] == want
        assert "(by %s)" % want in format_call(evs)


def test_reply_latency_marks_turns_after_a_tool_call(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["what's on my calendar tomorrow"], **FAST)
        try:
            b, cl = await bridge(r)
            res = await b.call_owner("")
            assert await until(lambda: r.gem.log["injects"], 3)      # silent pick-up: greeting after the wait
            await asyncio.sleep(0.3)
            await cl.speak(OWNER, tone(48000, 1100))                 # ≥ 1 s: the fake model hears a request
            assert await until(lambda: r.gem.log["tool_responses"], 5)
            await asyncio.sleep(0.3)
            await cl.end(OWNER)
            evs = call_events(r.cfg, res["call"])
            rep = [e for e in evs if e["ev"] == "reply"]
            assert rep and rep[0]["tool"] == "ask_assistant" and rep[0]["after_ms"] >= 0
            assert "after a tool call" in format_call(evs)
        finally:
            await r.close()
    run(main())
