"""Call quality pass: the greeting prefetched during the pick-up hold, slow "now" hand-offs ("takes a bit longer",
the cue goes on, the answer lands live), recovery when the Gemini socket drops mid-call (resumption handle, buffered
audio, stale tool ids), and `voice-relay stats`. Fake Gemini + fake call client; nothing rings, nothing leaves."""
import asyncio
import json

from util import RecFE, Rig, until
from voice_relay import calllog
from voice_relay.audio import tone
from voice_relay.calllog import calls_dir, load_calls
from voice_relay.config import Config
from voice_relay.fakes import FakeCallClient
from voice_relay.providers import GeminiLive, make_provider
from voice_relay.server import instructions
from voice_relay.session import RECONNECTED, SLOW_SAY, VoiceSession
from voice_relay.tgcall import CallBridge

OWNER = 111222333
HOLD = dict(opening_wait_s=0.5, opening_quiet_s=0.25, opening_max_s=2.0)


def run(coro):
    return asyncio.run(coro)


async def bridge(r):
    cl = FakeCallClient()
    b = CallBridge(r.cfg, cl, OWNER, r.relay.handoff, lambda: instructions(r.cfg), make_provider)
    await b.start()
    return b, cl


def events(cfg, cid, ev=None):
    out = [e for c in load_calls(calls_dir(cfg), 50) for e in c if e["call"] == cid]
    return [e for e in out if e["ev"] == ev] if ev else out


# --- greeting prefetch ----------------------------------------------------------------------------------------

def test_greeting_is_generated_during_the_hold_and_plays_when_it_ends(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), **HOLD)
        try:
            b, cl = await bridge(r)
            res = await b.call_owner("the parcel that arrived")
            assert res["status"] == "answered"
            assert await until(lambda: r.gem.log["injects"], 0.3)        # sent at pick-up, not after the hold
            assert "you won't hear it" in r.gem.log["injects"][0]
            await asyncio.sleep(0.2)
            assert len(cl.sent[OWNER]) == 0                              # generated, but held back
            assert await until(lambda: len(cl.sent[OWNER]) > 0, 2)
            s = b.calls[OWNER]
            assert await until(lambda: not s.out_held and not s.turn_open, 1)   # its turn closed out after play
            assert s.answered is True
            await cl.speak(OWNER, tone(48000, 200))
            assert await until(lambda: r.gem.log["audio_bytes"] > 0, 2)  # his audio flows after the hold
            assert len(r.gem.log["injects"]) == 1                        # one opening
            await cl.end(OWNER)
            assert await until(lambda: not b.calls)
            pk = events(r.cfg, res["call"], "pickup")[0]
            gr = events(r.cfg, res["call"], "greet")[0]
            assert pk["prefetch"] is True and pk["ready_ms"] < pk["waited_ms"]
            assert gr["prefetch"] is True and gr["first_audio_ms"] - pk["waited_ms"] < 150
        finally:
            await r.close()
    run(main())


def test_his_hello_during_the_hold_never_reaches_the_model_and_one_greeting_plays(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), **HOLD)
        try:
            b, cl = await bridge(r)
            res = await b.call_owner("", say="the test call")
            await cl.speak(OWNER, tone(48000, 300))                      # "Hello?"
            await asyncio.sleep(0.1)
            assert r.gem.log["audio_bytes"] == 0
            assert await until(lambda: len(cl.sent[OWNER]) > 0, 3)
            assert len(r.gem.log["injects"]) == 1 and '"the test call"' in r.gem.log["injects"][0]
            await cl.end(OWNER)
            assert await until(lambda: not b.calls)
            assert events(r.cfg, res["call"], "pickup")[0]["heard"] is True
        finally:
            await r.close()
    run(main())


def test_prefetch_off_keeps_the_old_order(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), opening_prefetch=False, **HOLD)
        try:
            b, cl = await bridge(r)
            res = await b.call_owner("x")
            await asyncio.sleep(0.25)
            assert not r.gem.log["injects"]                              # the opening waits for the hold
            assert await until(lambda: len(cl.sent[OWNER]) > 0, 3)
            await cl.end(OWNER)
            assert await until(lambda: not b.calls)
            gr = events(r.cfg, res["call"], "greet")[0]
            assert "prefetch" not in gr and gr["first_audio_ms"] >= 400
        finally:
            await r.close()
    run(main())


# --- slow hand-offs ---------------------------------------------------------------------------------------------

async def _session(r, mode="ptt"):
    fe = RecFE()
    s = VoiceSession(r.cfg, make_provider(r.cfg, instructions(r.cfg), mode), fe, r.relay.handoff,
                     log=calllog.CallLog(r.cfg, "page", "test"))
    await s.start()
    return s, fe


def test_slow_now_answer_says_it_takes_longer_keeps_the_cue_and_lands_live(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), slow_delay=1.2, sync_wait_s=0.3, thinking_after_s=0.1,
                           thinking_every_s=0.15, slow_cue_max_s=5)
        try:
            s, fe = await _session(r)
            await s.text_in("slow: check the backups")
            assert await until(lambda: r.gem.log["tool_responses"])
            resp = r.gem.log["tool_responses"][0]["response"]
            assert resp["status"] == "working" and resp["say"] == SLOW_SAY
            pid = next(iter(s.working))
            assert pid in s.waiting_on                                   # the cue goes on after the sync wait
            assert await until(lambda: r.gem.log["injects"], 3)
            assert r.gem.log["injects"][0].startswith("[assistant result] Fake answer")
            assert pid not in s.waiting_on and s.metrics.get("cues", 0) >= 2
            evs = events(r.cfg, s.log.id)
            hm = [e for e in evs if e["ev"] == "handoff_ms"][0]
            late = [e for e in evs if e["ev"] == "late"][0]
            assert hm["outcome"] == "slow" and 250 <= hm["ms"] < 1000
            assert late["ms"] >= 1000
            await s.close()
        finally:
            await r.close()
    run(main())


def test_quick_answer_logs_its_time_and_no_slow_line(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), delay=0.05)
        try:
            s, fe = await _session(r)
            await s.text_in("what's the weather")
            assert await until(lambda: r.gem.log["tool_responses"])
            assert "answer" in r.gem.log["tool_responses"][0]["response"]
            hm = [e for e in events(r.cfg, s.log.id) if e["ev"] == "handoff_ms"][0]
            assert hm["outcome"] == "answer" and hm["ms"] < 1500 and not s.waiting_on
            await s.close()
        finally:
            await r.close()
    run(main())


# --- recovery after a dropped provider socket -----------------------------------------------------------------

def test_dropped_gemini_socket_reconnects_with_the_handle_and_the_call_goes_on(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), **HOLD)
        try:
            b, cl = await bridge(r)
            res = await b.call_owner("x")
            assert await until(lambda: len(cl.sent[OWNER]) > 0, 3)
            s = b.calls[OWNER]
            n0 = len(r.gem.log["setups"])
            await r.gem.drop()
            assert await until(lambda: len(r.gem.log["setups"]) > n0, 3)
            assert await until(lambda: any(RECONNECTED in i for i in r.gem.log["injects"]), 3)
            setup = r.gem.log["setups"][-1]
            assert setup["sessionResumption"].get("handle")              # the conversation continues
            assert OWNER in b.calls and not s.closed and s.gen == 1
            await cl.speak(OWNER, tone(48000, 200))
            assert await until(lambda: r.gem.log["audio_bytes"] > 0, 2)
            await cl.end(OWNER)
            assert await until(lambda: not b.calls)
            rc = [e for e in events(r.cfg, res["call"], "reconnect") if e.get("why") == "dropped"][0]
            assert rc["ok"] is True and rc["resumed"] is True and rc["tries"] == 1
            assert events(r.cfg, res["call"], "end")[0]["reason"] == "hangup"
        finally:
            await r.close()
    run(main())


def test_reconnect_gives_up_after_its_tries_and_ends_the_call(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), reconnect_tries=2, reconnect_timeout_s=1.0, **HOLD)
        try:
            b, cl = await bridge(r)
            res = await b.call_owner("x")
            assert await until(lambda: len(cl.sent[OWNER]) > 0, 3)
            r.gem.fail_setups = 5
            await r.gem.drop()
            assert await until(lambda: not b.calls, 8)
            rc = [e for e in events(r.cfg, res["call"], "reconnect") if e.get("why") == "dropped"][0]
            assert rc["ok"] is False and rc["tries"] == 2
            assert events(r.cfg, res["call"], "end")[0]["reason"] == "provider ended"
        finally:
            await r.close()
    run(main())


def test_reconnect_off_ends_the_call_as_before(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), reconnect_tries=0, **HOLD)
        try:
            b, cl = await bridge(r)
            res = await b.call_owner("x")
            assert await until(lambda: len(cl.sent[OWNER]) > 0, 3)
            n0 = len(r.gem.log["setups"])
            await r.gem.drop()
            assert await until(lambda: not b.calls, 3)
            assert len(r.gem.log["setups"]) == n0 and not events(r.cfg, res["call"], "reconnect")
        finally:
            await r.close()
    run(main())


def test_answer_for_a_call_from_before_the_drop_goes_in_as_a_note(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), slow_delay=1.0, sync_wait_s=3.0)
        try:
            s, fe = await _session(r)
            await s.text_in("slow: check the backups")             # tool call on socket 1, answer due in 1 s
            assert await until(lambda: s.working, 2)
            await r.gem.drop()
            assert await until(lambda: s.gen == 1, 4)
            assert await until(lambda: any("Fake answer" in i for i in r.gem.log["injects"]), 4)
            assert not any("Fake answer" in str(t.get("response")) for t in r.gem.log["tool_responses"])
            await s.close()
        finally:
            await r.close()
    run(main())


def test_provider_tool_result_routes_by_connection():
    async def main():
        g = GeminiLive(Config(), "x", [], "open")
        sent = []

        async def send(obj):
            sent.append(obj)
        g._send = send
        g.normalize({"toolCall": {"functionCalls": [{"id": "a", "name": "ask_assistant", "args": {}}]}})
        await g.tool_result("a", "ask_assistant", {"answer": "fine"})
        assert "toolResponse" in sent[-1]
        await g.tool_result("a", "ask_assistant", {"answer": "again"})        # answered already → a note
        assert "clientContent" in sent[-1] and "again" in json.dumps(sent[-1])
        n = len(sent)
        await g.tool_result("zz", "end_call", {"status": "ending"}, scheduling="SILENT")
        assert len(sent) == n                                                # nothing to say: dropped
    run(main())


# --- voice-relay stats ------------------------------------------------------------------------------------------

def test_quality_counts_and_timings_never_text():
    secret = "my secret words"
    c1 = [{"ts": 100.0, "call": "c1", "ev": "start", "kind": "tg-out", "reason": secret},
          {"ts": 104.0, "call": "c1", "ev": "ring", "outcome": "answered"},
          {"ts": 105.5, "call": "c1", "ev": "pickup", "heard": False, "waited_ms": 1500, "prefetch": True},
          {"ts": 105.5, "call": "c1", "ev": "greet", "first_audio_ms": 1520, "prefetch": True},
          {"ts": 106.0, "call": "c1", "ev": "him", "text": secret},
          {"ts": 107.0, "call": "c1", "ev": "reply", "after_ms": 900},
          {"ts": 107.1, "call": "c1", "ev": "tool", "name": "ask_assistant", "handoff": "v1", "args": {"task": secret}},
          {"ts": 110.0, "call": "c1", "ev": "result", "handoff": "v1", "outcome": "answer", "text": secret},
          {"ts": 111.0, "call": "c1", "ev": "tool", "name": "weather", "lane": "fast", "ms": 300},
          {"ts": 112.0, "call": "c1", "ev": "tool", "name": "parcels", "lane": "fast", "error": "timeout"},
          {"ts": 112.1, "call": "c1", "ev": "tool", "name": "ask_assistant", "handoff": "v2"},
          {"ts": 112.2, "call": "c1", "ev": "handoff_ms", "handoff": "v2", "ms": 8000, "outcome": "slow"},
          {"ts": 125.0, "call": "c1", "ev": "late", "handoff": "v2", "ms": 12900, "text": secret},
          {"ts": 126.0, "call": "c1", "ev": "reconnect", "why": "dropped", "ok": True},
          {"ts": 130.0, "call": "c1", "ev": "end", "reason": "hangup", "by": "him", "why": "him", "duration_s": 30}]
    c2 = [{"ts": 200.0, "call": "c2", "ev": "start", "kind": "tg-out"},
          {"ts": 220.0, "call": "c2", "ev": "ring", "outcome": "declined"},
          {"ts": 220.0, "call": "c2", "ev": "end", "reason": "declined", "duration_s": 20}]
    q = calllog.quality([c1, c2])
    assert q["calls"] == 2 and q["connected"] == 1 and q["ring"] == {"answered": 1, "declined": 1}
    assert q["handoffs"] == 2 and q["answered_in_call"] == 1 and q["late_live"] == 1 and q["slow"] == 1
    assert q["handoff_ms"]["n"] == 2 and q["handoff_ms"]["max"] == 12900
    assert q["fast_hits"] == 1 and q["fast_fallbacks"] == 1 and q["reconnect_ok"] == 1
    assert q["greet_ms"]["median"] == 1520 and q["greet_prefetch"] == 1 and q["duration_s"]["n"] == 1
    out = calllog.format_quality(q, 14) + json.dumps(q)
    assert secret not in out and "fast lane: 1 hits, 1 fell back" in out
    assert calllog.quality([c1, c2], since=150)["calls"] == 1
