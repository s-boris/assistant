"""Voice-first: model fallback chain, continuing the last call (session resumption), partial answers
(voice-reply --more) and the [action] tag for the main session."""
import asyncio

import pytest

from util import RecFE, Rig, until
from voice_relay import session as sess
from voice_relay.calllog import NullLog
from voice_relay.providers import ProviderError, make_provider, model_gone
from voice_relay.server import instructions
from voice_relay.session import ACTION_TAG, VoiceSession


class RecLog(NullLog):
    def __init__(self):
        self.events = []

    def event(self, ev, **kw):
        self.events.append((ev, kw))

    def names(self):
        return [e for e, _ in self.events]


def run(coro):
    return asyncio.run(coro)


async def _start(r, log=None, mode="open"):
    fe, log = RecFE(), log or RecLog()
    s = VoiceSession(r.cfg, make_provider(r.cfg, instructions(r.cfg), mode), fe, r.relay.handoff, log=log)
    await s.start()
    return s, fe, log


def test_model_gone_reasons():
    assert model_gone("models/gemini-x is not found for API version v1beta, or is not supported for bidiGenerateContent.")
    assert not model_gone("Your prepayment credits are depleted")
    assert not model_gone("")


def test_rejected_model_falls_back_to_the_next_one(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), models={"live-b"}, gemini_model="live-a",
                           gemini_fallbacks=("live-a", "live-b"))
        try:
            s, fe, log = await _start(r)
            assert s.provider.model == "live-b" and s.provider.fallback_from == "live-a"
            assert [x["model"] for x in r.gem.log["setups"]] == ["models/live-a", "models/live-b"]
            assert ("model_fallback", {"model": "live-b", "configured": "live-a"}) in log.events
            await s.text_in("what's on my calendar tomorrow")          # the session works on the fallback
            assert await until(lambda: "Fake answer" in fe.said())
            await s.close()
        finally:
            await r.close()
    run(main())


def test_every_model_gone_raises(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), models=set(), gemini_model="a", gemini_fallbacks=("b",))
        try:
            p = make_provider(r.cfg, "x", "open")
            with pytest.raises(ProviderError):
                await p.connect()
            await p.close()
            assert r.gem.log["rejected"] == ["a", "b"]
        finally:
            await r.close()
    run(main())


def test_next_call_continues_the_last_conversation(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), resume_window_s=600)
        try:
            s1, _, _ = await _start(r)
            assert await until(lambda: s1.provider.resume_handle)
            await s1.text_in("hello")                                     # a conversation worth continuing
            assert await until(lambda: s1.model_live)
            await s1.close()
            assert sess.RESUME.handle == "h-1"
            s2, fe2, log2 = await _start(r)
            assert r.gem.log["setups"][1]["sessionResumption"] == {"handle": "h-1"}
            assert s2.provider.resumed and "resumed" in log2.names()
            assert await until(lambda: r.gem.log["notes"])
            assert r.gem.log["notes"][0].startswith("[new call] The previous call ended") and not r.gem.log["injects"]
            assert sess.RESUME.handle is None                            # taken once
            assert await until(lambda: s2.provider.resume_handle == "h-2")
            await s2.text_in("hello again")                              # the resumed model answers him
            assert await until(lambda: s2.model_live)
            await s2.close()
            assert sess.RESUME.handle == "h-2"
        finally:
            await r.close()
    run(main())


def test_resume_off_stale_or_refused_handles_start_fresh(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), resume_window_s=0)
        try:
            s1, _, _ = await _start(r)
            assert await until(lambda: s1.provider.resume_handle)
            await s1.close()
            assert sess.RESUME.handle is None                            # off: nothing kept
            r.cfg.resume_window_s = 600
            sess.RESUME.put("h-foreign")                                 # not issued by this server: refused
            s2, _, log2 = await _start(r)
            assert r.gem.log["rejected"] == ["handle"]
            assert r.gem.log["setups"][-1]["sessionResumption"] == {}
            assert not s2.provider.resumed and "resumed" not in log2.names()
            await s2.close()
            sess.RESUME.put("h-2")
            sess.RESUME.t -= 601                                         # too old
            s3, _, _ = await _start(r)
            assert r.gem.log["setups"][-1]["sessionResumption"] == {}
            await s3.close()
        finally:
            await r.close()
    run(main())


def test_partial_answer_is_spoken_now_and_the_rest_follows(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        r.inbox.auto = False
        try:
            s, fe, log = await _start(r)
            await s.text_in("when is my next train home")
            assert await until(lambda: r.inbox.tasks)
            vid = r.inbox.tasks[0]["id"]
            res = await r.inbox.reply(vid, "The next train is at five forty.", more=True)
            assert res["delivered"] == "sync" and res["open"] is True
            assert await until(lambda: r.gem.log["tool_responses"])
            resp = r.gem.log["tool_responses"][0]["response"]
            assert resp["answer"].startswith("The next train") and "more" in resp
            assert vid in s.working and "partial" in log.names()
            res2 = await r.inbox.reply(vid, "It runs on time, platform four.")
            assert res2["delivered"] == "live"
            assert await until(lambda: r.gem.log["injects"])
            assert r.gem.log["injects"][0] == "[assistant result] It runs on time, platform four."
            assert vid not in s.working and s.items[0]["status"] == "answered"
            res3 = await r.inbox.reply(vid, "late")                     # closed: unknown
            assert res3["delivered"] == "unknown"
            await s.close()
        finally:
            await r.close()
    run(main())


def test_more_after_the_sync_window_is_pushed_live_and_keeps_it_open(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), sync_wait_s=0.1)
        r.inbox.auto = False
        try:
            s, fe, log = await _start(r)
            await s.text_in("check the backups")
            assert await until(lambda: r.inbox.tasks)
            vid = r.inbox.tasks[0]["id"]
            assert await until(lambda: r.gem.log["tool_responses"])      # "working" after 0.1 s
            res = await r.inbox.reply(vid, "Backups ran last night.", more=True)
            assert res["delivered"] == "live" and res["open"]
            assert vid in s.working
            res2 = await r.inbox.reply(vid, "All three passed.")
            assert res2["delivered"] == "live" and vid not in s.working
            await s.close()
        finally:
            await r.close()
    run(main())


def test_actions_reach_the_main_session_tagged(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            s, fe, log = await _start(r)
            await s.text_in("buy cat litter on Galaxus")
            await s.text_in("what's the weather in Bern tomorrow")
            assert await until(lambda: len(r.inbox.tasks) == 2)
            tasks = sorted(t["task"] for t in r.inbox.tasks)
            assert tasks[0].startswith(ACTION_TAG) and "litter" in tasks[0]
            assert not tasks[1].startswith(ACTION_TAG)
            assert all(not it["task"].startswith(ACTION_TAG) for it in s.items)   # recap keeps his words
            await s.close()
        finally:
            await r.close()
    run(main())


def test_thinking_cue_fills_a_silent_wait_and_stops_at_the_answer(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), slow_delay=1.0, sync_wait_s=3.0, thinking_after_s=0.2, thinking_every_s=0.3)
        try:
            s, fe, log = await _start(r)
            await s.text_in("slow: is the server ok")
            assert await until(lambda: s.metrics.get("cues", 0) >= 2, 3)
            assert await until(lambda: r.gem.log["tool_responses"], 3)
            n = s.metrics["cues"]
            await asyncio.sleep(0.6)
            assert s.metrics["cues"] == n and not s.waiting_on              # the answer stopped it
            await s.close()
        finally:
            await r.close()
    run(main())


def test_no_cue_for_quick_answers_later_tasks_or_when_off(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), delay=0.05, thinking_after_s=0.5)
        try:
            s, fe, log = await _start(r)
            await s.text_in("what's on my calendar tomorrow")
            assert await until(lambda: r.gem.log["tool_responses"])
            await asyncio.sleep(0.6)
            assert s.metrics.get("cues", 0) == 0
            await s.close()
            r.cfg.thinking_cue, r.cfg.thinking_after_s = False, 0.1
            s2, _, _ = await _start(r)
            await s2.text_in("slow: check the backups")
            await asyncio.sleep(0.5)
            assert s2.metrics.get("cues", 0) == 0
            await s2.close()
        finally:
            await r.close()
    run(main())
