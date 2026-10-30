"""Continuing the last call (session resumption) without a mute model: no handle after the assistant's goodbye,
no chaining off a resumed call that never spoke, a fresh session in the same call when a resumed one stays mute,
and the [new call] cue. Replays the sequence of a drive-briefing call the model ended with end_call, then his
call back and a Talk call (both resumed mute before the fix). Fake Gemini (mute resumption modelled on the live
API) + fake call client; nothing rings."""
import asyncio
import types

import pytest

from util import RecFE, Rig, StubProvider, until
from voice_relay import session as sess
from voice_relay.audio import tone
from voice_relay.calllog import calls_dir, format_call, format_quality, load_calls, quality
from voice_relay.config import Config
from voice_relay.fakes import FakeCallClient
from voice_relay.providers import make_provider
from voice_relay.server import instructions
from voice_relay.session import ASSISTANT_ENDS, VoiceSession
from voice_relay.tgcall import CallBridge

OWNER = 111222333
FAST = dict(opening_wait_s=0.4, opening_quiet_s=0.25, opening_max_s=2.0, opening_prefetch=True, resume_window_s=900,
            resume_fallback_s=0.6, hangup_tail_s=0.05, recap_grace_s=0, fast_lane=False, decisions=False)


def run(coro):
    return asyncio.run(coro)


async def bridge(r, client=None):
    client = client or FakeCallClient()
    b = CallBridge(r.cfg, client, OWNER, r.relay.handoff, lambda: instructions(r.cfg), make_provider)
    await b.start()
    return b, client


def events(cfg, cid):
    return [e for c in load_calls(calls_dir(cfg), 50) for e in c if e["call"] == cid]


def names(cfg, cid):
    return [e["ev"] for e in events(cfg, cid)]


def setup_handle(r, i=-1):
    return (r.gem.log["setups"][i].get("sessionResumption") or {}).get("handle")


async def healthy_call_he_hangs_up(r, b, cl, cid="c0"):
    """An inbound call where the model answers and he hangs up mid-conversation: its handle is kept."""
    await cl.ring(cid, OWNER)
    await cl.speak(cid, tone(48000, 1200))
    assert await until(lambda: len(cl.sent[cid]) > 0, 5)
    await cl.end(cid)
    assert await until(lambda: not b.calls)
    assert sess.RESUME.handle


def test_drive_briefing_ended_by_end_call_then_his_call_back_and_talk_start_fresh(tmp_path):
    """19:57 tg-out briefing → model's end_call; 20:00 he calls back; 20:00 Talk rings him. Before the fix both
    later calls resumed the ended conversation and the model never spoke."""
    async def main():
        r = await Rig.make(str(tmp_path), script=["bye for now", "what's the weather tomorrow"], **FAST)
        try:
            b, cl = await bridge(r)
            # 19:57 drive briefing: picked up, greeting, he says bye, the model says goodbye + end_call
            res1 = await b.call_owner("drive briefing")
            assert res1["status"] == "answered"
            assert await until(lambda: any(e["ev"] == "greet" for e in events(r.cfg, res1["call"])), 3)
            await cl.speak(OWNER, tone(48000, 1200))
            assert await until(lambda: not b.calls, 6)
            ev1 = events(r.cfg, res1["call"])
            end1 = [e for e in ev1 if e["ev"] == "end"][0]
            assert end1["by"] == "assistant" and end1["why"] in ASSISTANT_ENDS
            assert {"ev": "resume_handle", "kept": False, "why": "assistant_end"}.items() <= \
                [e for e in ev1 if e["ev"] == "resume_handle"][0].items()
            assert sess.RESUME.handle is None                       # nothing to continue
            assert r.gem.poisoned                                   # (the fake: that conversation resumes mute)
            # 20:00 he calls back: a fresh conversation that answers him
            n = len(r.gem.log["setups"])
            await cl.ring("c2", OWNER)
            assert len(r.gem.log["setups"]) == n + 1 and setup_handle(r) is None
            await cl.speak("c2", tone(48000, 1200))
            assert await until(lambda: r.gem.log["tool_responses"], 5)      # call 1 had none
            assert await until(lambda: len(cl.sent["c2"]) > 0, 5)    # the model spoke
            cid2 = b.calls["c2"].log.id
            assert "resumed" not in names(r.cfg, cid2) and not r.gem.log.get("mute_sessions")
            await cl.end("c2")                                       # he hangs up mid-conversation: kept
            assert await until(lambda: not b.calls)
            assert sess.RESUME.handle and sess.RESUME.handle not in r.gem.poisoned
            # 20:00 Talk: rings him, continues his call-back's conversation, greets him
            sent = len(cl.sent[OWNER])
            res3 = await b.call_owner("talk")
            assert res3["status"] == "answered" and setup_handle(r) is not None
            assert await until(lambda: any(e["ev"] == "greet" for e in events(r.cfg, res3["call"])), 3)
            assert await until(lambda: len(cl.sent[OWNER]) > sent, 3)
            ev3 = names(r.cfg, res3["call"])
            assert "resumed" in ev3 and "resume_fallback" not in ev3
            cue = [x for x in r.gem.log["notes"] if x.startswith("[new call]")]
            assert cue and "calling him again" in cue[-1] and "Wait for him" not in cue[-1]
            await cl.end(OWNER)
            assert await until(lambda: not b.calls)
        finally:
            await r.close()
    run(main())


def test_mute_resumed_inbound_call_reopens_fresh_and_replays_his_words(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["what's the weather", "what's the weather tomorrow",
                                                  "what's the weather tomorrow"], **FAST)
        try:
            b, cl = await bridge(r)
            await healthy_call_he_hangs_up(r, b, cl)
            r.gem.mute_resumed = True                                # this time Gemini resumes it mute
            await cl.ring("c2", OWNER)
            s = b.calls["c2"]
            assert s.provider.resumed and s.resume_armed
            assert r.gem.log["mute_sessions"] == 1
            assert await until(lambda: [x for x in r.gem.log.get("mute_prompts", [])
                                        if x.startswith("[new call]") and "calling again" in x], 3)
            n0 = len(r.gem.log["tool_responses"])
            await cl.speak("c2", tone(48000, 1200))                  # "what's the weather tomorrow"
            assert await until(lambda: s.him_t > 0, 3)                # transcribed, no answer
            assert await until(lambda: "resume_fallback" in names(r.cfg, s.log.id), 3)
            assert setup_handle(r) is None                            # reopened without the handle
            assert await until(lambda: len(r.gem.log["tool_responses"]) > n0, 5)  # the fresh session heard him
            assert "what's the weather tomorrow" in r.gem.log["tool_responses"][-1]["response"]["answer"]
            assert await until(lambda: len(cl.sent["c2"]) > 0, 5)
            fb = [e for e in events(r.cfg, s.log.id) if e["ev"] == "resume_fallback"][0]
            assert fb["trigger"] == "him" and fb["ok"] is True and fb["replay_ms"] >= 1000
            assert 500 <= fb["waited_ms"] < 2000
            assert not s.provider.resumed and s.model_live and not s.resume_buf
            await cl.end("c2")
            assert await until(lambda: not b.calls)
            assert sess.RESUME.handle is not None and sess.RESUME.handle not in r.gem.poisoned
            out = format_call(events(r.cfg, s.log.id))
            assert "(resumed)" in out and "stayed mute after his words: reopened fresh" in out
            q = quality(load_calls(calls_dir(r.cfg), 10))
            assert q["resumed"] == 1 and q["resume_fallback_ok"] == 1
            assert "mute resumes reopened fresh 1 / failed 0" in format_quality(q)
        finally:
            await r.close()
    run(main())


def test_mute_resumed_outbound_call_sends_the_opening_again_on_a_fresh_session(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["what's the weather"], **FAST)
        try:
            b, cl = await bridge(r)
            await healthy_call_he_hangs_up(r, b, cl)
            r.gem.mute_resumed = True
            sent = len(cl.sent[OWNER])
            res = await b.call_owner("your parcel arrived")
            assert res["status"] == "answered"
            assert await until(lambda: any(x.startswith("[outbound call]") for x in r.gem.log.get("mute_prompts", [])),
                               3)                                    # the mute session got the opening: silence
            assert await until(lambda: "resume_fallback" in names(r.cfg, res["call"]), 4)
            assert await until(lambda: any(x.startswith("[outbound call]") for x in r.gem.log["injects"]), 3)
            assert await until(lambda: len(cl.sent[OWNER]) > sent, 3)   # the fresh session greets him
            evs = events(r.cfg, res["call"])
            fb = [e for e in evs if e["ev"] == "resume_fallback"][0]
            assert fb["trigger"] == "opening" and fb["ok"] and fb.get("replay_ms") is None
            assert any(e["ev"] == "greet" for e in evs)
            assert setup_handle(r) is None
            await cl.end(OWNER)
            assert await until(lambda: not b.calls)
        finally:
            await r.close()
    run(main())


def test_resumed_call_that_never_spoke_is_not_chained(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["what's the weather", "hello?"], **dict(FAST, resume_fallback_s=0))
        try:
            b, cl = await bridge(r)
            await healthy_call_he_hangs_up(r, b, cl)
            r.gem.mute_resumed = True
            await cl.ring("c2", OWNER)
            s = b.calls["c2"]
            assert s.provider.resumed and not s.resume_armed          # fallback off
            await cl.speak("c2", tone(48000, 1200))
            assert await until(lambda: s.him_t > 0, 3)
            await asyncio.sleep(0.3)
            assert not cl.sent["c2"]                                  # mute
            await cl.end("c2")
            assert await until(lambda: not b.calls)
            assert sess.RESUME.handle is None                         # its (poisoned) handle is not kept
            rh = [e for e in events(r.cfg, s.log.id) if e["ev"] == "resume_handle"][0]
            assert rh["kept"] is False and rh["why"] == "mute_resume"
            n = len(r.gem.log["setups"])
            await cl.ring("c3", OWNER)                                # the next call starts fresh
            assert len(r.gem.log["setups"]) == n + 1 and setup_handle(r) is None
            assert not b.calls["c3"].provider.resumed
            await cl.end("c3")
            assert await until(lambda: not b.calls)
        finally:
            await r.close()
    run(main())


def test_failed_fresh_reopen_goes_the_dropped_socket_way(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["what's the weather", "hello?"],
                           **dict(FAST, reconnect_tries=1, reconnect_timeout_s=1.0))
        try:
            b, cl = await bridge(r)
            await healthy_call_he_hangs_up(r, b, cl)
            r.gem.mute_resumed = True
            await cl.ring("c2", OWNER)
            s = b.calls["c2"]
            r.gem.fail_setups = 2                                    # the fresh reopen and the one retry fail
            await cl.speak("c2", tone(48000, 1200))
            assert await until(lambda: s.closed, 8)
            evs = events(r.cfg, s.log.id)
            fb = [e for e in evs if e["ev"] == "resume_fallback"][0]
            assert fb["ok"] is False and fb.get("error")
            rc = [e for e in evs if e["ev"] == "reconnect"]
            assert rc and rc[0]["ok"] is False
            assert [e for e in evs if e["ev"] == "end"][0]["why"] == "error"
            assert sess.RESUME.handle is None
        finally:
            await r.close()
    run(main())


class _Prov(StubProvider):
    def __init__(self, handle="h-9", resumed=False):
        super().__init__()
        self.resume_handle, self.resumed = handle, resumed


@pytest.mark.parametrize("why,stage,resumed,live,kept", [
    ("him", 0, False, False, True),                  # he hung up mid-conversation
    ("him", 1, False, True, True),                   # ...after "anything else?" (no goodbye yet)
    ("him", 2, False, True, False),                  # ...while the goodbye was playing
    ("network", 0, False, True, True),               # the line dropped
    ("him", 0, True, True, True),                    # a resumed call that worked
    ("him", 0, True, False, False),                  # a resumed call that never spoke: no chaining
] + [(w, 0, False, True, False) for w in sorted(ASSISTANT_ENDS)])
def test_handle_kept_only_when_the_call_ended_mid_conversation(tmp_path, why, stage, resumed, live, kept):
    cfg = Config(state_dir=str(tmp_path / "state"), inbox_dir=str(tmp_path / "inbox"), fast_lane=False,
                 decisions=False, resume_window_s=900)
    s = VoiceSession(cfg, _Prov(resumed=resumed), RecFE(), types.SimpleNamespace(detach=lambda o: None))
    s.idle_stage, s.model_live = stage, live
    s.him_t = 1.0                                    # he said something in that call
    sess.RESUME.put("h-old")
    s._keep_handle(why)
    assert sess.RESUME.handle == ("h-9" if kept else None)


@pytest.mark.parametrize("said,live,kept", [(False, False, False), (True, False, True), (False, True, True)])
def test_a_call_where_nothing_was_said_keeps_no_handle(tmp_path, said, live, kept):
    """He called, heard nothing and hung up before anyone spoke: that empty conversation is not continued."""
    cfg = Config(state_dir=str(tmp_path / "state"), inbox_dir=str(tmp_path / "inbox"), fast_lane=False,
                 decisions=False, resume_window_s=900)
    s = VoiceSession(cfg, _Prov(), RecFE(), types.SimpleNamespace(detach=lambda o: None))
    s.model_live, s.him_t = live, (1.0 if said else 0.0)
    sess.RESUME.put("h-old")
    s._keep_handle("him")
    assert sess.RESUME.handle == ("h-9" if kept else None)


def test_resume_off_keeps_nothing_and_new_call_cue_wording():
    cfg = Config(resume_window_s=0, fast_lane=False, decisions=False)
    s = VoiceSession(cfg, _Prov(), RecFE(), types.SimpleNamespace(detach=lambda o: None))
    s._keep_handle("him")
    assert sess.RESUME.handle is None
    cin, cout = sess.new_call_cue(False), sess.new_call_cue(True)
    assert cin.startswith("[new call] The previous call ended; he is calling again") and "answer him" in cin
    assert cout.startswith("[new call] The previous call ended;") and "calling him again" in cout
    assert "Wait for him" not in cin + cout
    with open(Config().prompt_file, encoding="utf-8") as fh:
        assert '"[new call]"' in fh.read()
