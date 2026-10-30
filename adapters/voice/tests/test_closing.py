"""Closing flow (closing.py + session.py _idle): his thanks / done → "anything else?", his no / goodbye → goodbye +
hang-up, the silence timer after a completed answer, never ending while a tool runs or an answer is pending, and the
voice.call_end usage event. StubProvider + RecFE (and a fake app WebSocket); nothing reaches Gemini or Telegram."""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import time

import pytest

from util import RecFE, StubProvider, until
from voice_relay import closing
from voice_relay.calllog import CallLog, digest, load_calls, calls_dir
from voice_relay.config import Config
from voice_relay.server import WebFrontEnd
from voice_relay.session import IDLE_ASK, IDLE_BYE, IDLE_BYE_PENDING, VoiceSession

from test_endcall import Handoff, OpenStub, audio, ends


def run(coro):
    return asyncio.run(coro)


def mkcfg(tmp, **over):
    kw = dict(state_dir=str(tmp / "state"), inbox_dir=str(tmp / "inbox"), files_dir=str(tmp / "inbox" / "files"),
              fast_lane=False, decisions=False, track_id=False, phone_open=False, recap_grace_s=0, hangup_tail_s=0.05,
              closing_nudge_s=0.2, closing_bye_s=0.3, idle_prompt_s=0.4, idle_unanswered_s=30, idle_bye_s=0.4,
              bye_fallback_s=0.25, idle_pending_max_s=30, sync_wait_s=0.01, thinking_cue=False)
    kw.update(over)
    return Config(**kw)


class ULog:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return True


async def him(p, text):
    await p.q.put({"type": "transcript", "who": "user", "text": text})


async def says(p, text, ms=200):
    """One assistant turn: audio + its transcript, then turn_complete."""
    await p.q.put(audio(ms))
    await p.q.put({"type": "transcript", "who": "assistant", "text": text})
    await p.q.put({"type": "turn_complete"})


def end_row(cfg):
    return [r for r in ends(cfg) if r.get("ev") == "end"][0]


async def mk(tmp_path, ulog=None, kind="tg-in", handoff=None, fe=None, **over):
    cfg = mkcfg(tmp_path, **over)
    p = OpenStub()
    s = VoiceSession(cfg, p, fe or RecFE(), handoff or Handoff(), log=CallLog(cfg, kind, "owner"), ulog=ulog)
    await s.start()
    return cfg, p, s


# --- classification --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Thanks!", "thank you very much", "ok, perfect", "Done.", "great, thanks", "Super.",
                                  "Danke!", "merci vielmal", "Passt, danke.", "alles klar", "okay cool", "Perfekt.",
                                  "ja super, danke dir", "That's great, thank you."])
def test_thanks(text):
    assert closing.classify(text) == "thanks"


@pytest.mark.parametrize("text", ["Bye!", "ok bye", "Danke, tschüss.", "Tschau zäme", "that's all, thanks",
                                  "Das wär's.", "see you later", "uf widerluege", "das isch alles", "ciao"])
def test_bye(text):
    assert closing.classify(text) == ("bye" if text != "Tschau zäme" else None)   # "zäme" isn't closing vocab


@pytest.mark.parametrize("text", ["No.", "nope", "no thanks", "Nein danke.", "nei, nüt meh", "nothing else",
                                  "I'm good, thanks"])
def test_no(text):
    assert closing.classify(text) == "no"


@pytest.mark.parametrize("text", ["thanks, and turn off the lights", "ok, what's the weather tomorrow?",
                                  "no öppis", "danke, und wie isch s wätter?", "yes", "ja", "no, I meant Bern",
                                  "", "   ", "thanks " * 11, "perfect, now book the train"])
def test_requests_and_answers_are_not_closings(text):
    assert closing.classify(text) is None


@pytest.mark.parametrize("text,more", [("Gern geschehen. Süsch no öppis?", True), ("You're welcome! Anything else?", True),
                                       ("Kann ich sonst noch etwas für dich tun?", True), ("Noch etwas?", True),
                                       ("Is there anything else I can do for you?", True),
                                       ("It's 14 degrees and sunny.", False),
                                       ("Anything else is on Telegram. The train leaves at 8.", False)])
def test_asks_more(text, more):
    assert closing.asks_more(text) is more


@pytest.mark.parametrize("text,bye", [("Tschüss!", True), ("Bye, talk soon!", True), ("Have a good evening!", True),
                                      ("Gern. En schöne Abig!", True), ("Your train leaves at 18:04.", False),
                                      ("I'll put a confirm card on Telegram.", False),
                                      ("Anything else?", False)])
def test_says_bye(text, bye):
    assert closing.says_bye(text) is bye


def test_end_why_and_ref():
    assert closing.end_why("assistant_hangup", "assistant", "closing_silence") == "closing_silence"
    assert closing.end_why("assistant_hangup", "assistant") == "end_call"
    assert [closing.end_why(r, None) for r in ("hangup", "app closed", "page closed", "time limit", "relay stopped",
                                                "provider ended", "provider failed: x", "weird")] == \
        ["him", "him", "him", "time_limit", "relay", "error", "error", "other"]
    import hashlib
    assert closing.call_ref("c1234abcd") == "call:" + hashlib.sha1(b"c1234abcd").hexdigest()[:8]


# --- the flow in a session ---------------------------------------------------------------------------------------

def test_thanks_then_model_asks_itself_then_silence_ends_it(tmp_path):
    async def main():
        ul = ULog()
        cfg, p, s = await mk(tmp_path, ulog=ul)
        await says(p, "It's 14 degrees and sunny.")
        await him(p, "Danke, passt.")
        await asyncio.sleep(0.05)
        await says(p, "Gern geschehen. Süsch no öppis?")       # the prompt's way: the model asks itself
        assert await until(lambda: s.idle_stage == 1)
        assert s.bye_wait == cfg.closing_bye_s and s.stage_why == "closing_silence"
        assert await until(lambda: getattr(p, "prompts", None), timeout=3)
        assert p.prompts == [IDLE_BYE]                          # no nudge: the model did answer his thanks
        await says(p, "Tschüss!")
        await p.q.put({"type": "tool_call", "id": "e1", "name": "end_call", "args": {"reason": "silence"}})
        assert await until(lambda: s.closed, timeout=5)
        row = end_row(cfg)
        assert (row["reason"], row["by"], row["why"]) == ("assistant_hangup", "assistant", "closing_silence")
        (feat, kind), kw = ul.calls[0]
        assert (feat, kind, kw["outcome"], kw["actor"], kw["channel"]) == \
            ("voice.call_end", "invoke", "closing_silence", "assistant", "voice")
        assert kw["ref"] == closing.call_ref(s.log.id) and kw["ext_id"] == "callend:" + kw["ref"][5:]
        assert kw["attrs"]["asked"] == 1 and kw["attrs"]["nudged"] == 0 and kw["attrs"]["fallback"] is False
        assert len(ul.calls) == 1
    run(main())


def test_silent_model_after_thanks_is_nudged_then_no_ends_it_and_relay_hangs_up_itself(tmp_path):
    async def main():
        ul = ULog()
        cfg, p, s = await mk(tmp_path, ulog=ul)
        await says(p, "The vacuum is docked.")
        await him(p, "ok thanks")
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [closing.CLOSING_ASK] and s.idle_stage == 1 and s.bye_wait == cfg.closing_bye_s
        await says(p, "Anything else?")
        await asyncio.sleep(0.05)
        await him(p, "No, that's it.")                       # "that's it" = bye vocabulary
        assert await until(lambda: len(p.prompts) == 2, timeout=2)
        assert p.prompts[1] == closing.CLOSING_BYE and s.idle_stage == 2
        await says(p, "Bye!")                                 # ...but the model never calls end_call
        assert await until(lambda: s.closed, timeout=3)
        row = end_row(cfg)
        assert (row["by"], row["why"]) == ("silence", "goodbye")
        assert any(r.get("ev") == "idle" and r.get("stage") == "nudge" for r in ends(cfg))
        kw = ul.calls[0][1]
        assert kw["outcome"] == "goodbye" and kw["attrs"]["fallback"] is True and kw["attrs"]["nudged"] == 2
    run(main())


def test_no_to_anything_else_ends_and_plain_no_without_the_question_is_a_thanks(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path)
        await says(p, "Done, the reminder is set.")
        await him(p, "no")                                    # no question was asked: treated like a thanks
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [closing.CLOSING_ASK]
        await says(p, "Okay. Anything else?")
        await asyncio.sleep(0.05)
        await him(p, "nein danke")
        assert await until(lambda: len(p.prompts) == 2, timeout=2)
        assert p.prompts[1] == closing.CLOSING_BYE and s.end_why == "nothing_else"
        await says(p, "Tschüss!")
        await p.q.put({"type": "tool_call", "id": "e1", "name": "end_call", "args": {"reason": "done"}})
        assert await until(lambda: s.closed, timeout=3)
        assert end_row(cfg)["why"] == "nothing_else"
    run(main())


def test_goodbye_said_by_the_model_without_end_call_is_hung_up(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, closing_nudge_s=5)
        await says(p, "Your train leaves at 18:04.")
        await him(p, "Perfect, tschüss!")
        await asyncio.sleep(0.05)
        await says(p, "Tschüss, gute Reise!")
        assert await until(lambda: s.idle_stage == 2)
        assert await until(lambda: s.closed, timeout=3)
        assert not getattr(p, "prompts", None)                # nothing to nudge: it said goodbye itself
        assert end_row(cfg)["why"] == "goodbye"
    run(main())


def test_model_end_call_after_his_goodbye_is_logged_as_goodbye(tmp_path):
    async def main():
        ul = ULog()
        cfg, p, s = await mk(tmp_path, ulog=ul, kind="app")
        await him(p, "Danke, das wär's.")
        await p.q.put(audio(200))
        await p.q.put({"type": "tool_call", "id": "e1", "name": "end_call", "args": {"reason": "he said bye"}})
        assert await until(lambda: s.closed, timeout=3)
        kw = ul.calls[0][1]
        assert kw["outcome"] == "goodbye" and kw["channel"] == "app" and kw["attrs"]["call_kind"] == "app"
    run(main())


def test_completed_answer_then_silence_asks_once_then_ends(tmp_path):
    async def main():
        ul = ULog()
        cfg, p, s = await mk(tmp_path, ulog=ul, closing_bye_s=5)
        await him(p, "what's on my calendar")
        await says(p, "Nothing until the gym at six.")
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [IDLE_ASK] and s.bye_wait == cfg.idle_bye_s
        await says(p, "Anything else?")                       # its question doesn't restart the stage
        assert s.end_flags["asked"] == 1
        assert await until(lambda: len(p.prompts) == 2, timeout=2)
        assert p.prompts[1] == IDLE_BYE
        assert await until(lambda: s.closed, timeout=3)       # no end_call: the relay hangs up
        assert end_row(cfg)["why"] == "idle_silence" and ul.calls[0][1]["outcome"] == "idle_silence"
    run(main())


def test_no_answer_yet_uses_the_long_timer(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, idle_unanswered_s=1.0)
        await him(p, "hm, let me think")
        await asyncio.sleep(0.6)
        assert not getattr(p, "prompts", None)                # idle_prompt_s (0.4) only counts after an answer
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [IDLE_ASK]
        await s.close("test")
    run(main())


def test_thanks_inside_a_request_is_not_a_closing(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, idle_prompt_s=0)
        await says(p, "It's sunny.")
        await him(p, "thanks, and turn off the lights")
        await asyncio.sleep(0.6)
        assert not getattr(p, "prompts", None) and s.idle_stage == 0
        await s.close("test")
    run(main())


def test_no_nudge_when_the_model_answers_his_thanks(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, idle_prompt_s=0)
        await says(p, "Done.")
        await him(p, "thanks")
        await asyncio.sleep(0.05)
        await says(p, "You're welcome.")                      # answered, no question: the plain timers apply
        await asyncio.sleep(0.5)
        assert not getattr(p, "prompts", None)
        await s.close("test")
    run(main())


def test_thanks_while_an_answer_is_pending_neither_nudges_nor_ends(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, handoff=Handoff(answer=None))
        await s._tool({"id": "a1", "name": "ask_assistant", "args": {"task": "check the backups", "urgency": "now"}})
        await says(p, "It's being handled.")
        await him(p, "ok, thanks")
        await asyncio.sleep(1.2)                              # past nudge, ask and bye times
        assert not getattr(p, "prompts", None) and not s.closed and s.idle_stage == 0
        await s.close("test")
    run(main())


def test_bye_while_an_answer_is_pending_says_rest_on_telegram(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, handoff=Handoff(answer=None))
        await s._tool({"id": "a1", "name": "ask_assistant", "args": {"task": "check the backups", "urgency": "now"}})
        await says(p, "It's being handled.")
        await him(p, "bye")
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [closing.CLOSING_BYE_PENDING] and s.end_why == "goodbye"   # he goes: the rest on Telegram
        await s.close("test")
    run(main())


def test_running_tool_blocks_every_timer(tmp_path):
    async def main():
        gate = asyncio.Event()

        class SlowHandoff(Handoff):
            async def wait(self, p, timeout):
                await gate.wait()
                return "Backups are fine."

        cfg, p, s = await mk(tmp_path, handoff=SlowHandoff(), idle_unanswered_s=0.3)
        await says(p, "One sec.")
        await him(p, "danke")
        task = asyncio.ensure_future(s._tool({"id": "a1", "name": "ask_assistant",
                                              "args": {"task": "check the backups", "urgency": "now"}}))
        await asyncio.sleep(1.0)
        assert not getattr(p, "prompts", None) and not s.closed
        gate.set()
        await task
        await s.close("test")
    run(main())


def test_late_answer_restarts_the_clock(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path)
        await says(p, "Done.")
        assert await until(lambda: s.idle_stage == 1, timeout=2)   # asked "anything else?"
        await s._deliver_late("v0001", "The backups are fine.")
        assert s.idle_stage == 0 and not s.answered and p.injects
        await s.close("test")
    run(main())


def test_he_carries_on_after_anything_else(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, idle_prompt_s=0)
        await says(p, "Done.")
        await him(p, "danke")
        await asyncio.sleep(0.05)
        await says(p, "Gern. Süsch no öppis?")
        assert await until(lambda: s.idle_stage == 1)
        await him(p, "ja, wie isch s wätter morn?")             # a new request: no ending
        assert await until(lambda: s.idle_stage == 0 and s.closing is None)
        assert s.end_why is None and not s.answered
        await asyncio.sleep(0.6)
        assert not getattr(p, "prompts", None) and not s.closed
        await s.close("test")
    run(main())


def test_closing_off_switch(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, closing=False, idle_prompt_s=0)
        await says(p, "Done.")
        await him(p, "thanks")
        await asyncio.sleep(0.05)
        await says(p, "Anything else?")
        await asyncio.sleep(0.6)
        assert not getattr(p, "prompts", None) and s.idle_stage == 0 and s.closing is None
        await s.close("test")
    run(main())


def test_push_to_talk_page_has_no_closing_flow(tmp_path):
    async def main():
        cfg = mkcfg(tmp_path)
        p = StubProvider()                                    # ptt
        s = VoiceSession(cfg, p, RecFE(), Handoff())
        await s.start()
        await says(p, "Done.")
        await him(p, "thanks")
        await asyncio.sleep(0.6)
        assert not getattr(p, "prompts", None) and not s.closed
        await s.close("test")
    run(main())


class FakeWS:
    closed = False

    def __init__(self):
        self.text, self.bin = [], 0

    async def send_str(self, s):
        self.text.append(json.loads(s))

    async def send_bytes(self, b):
        self.bin += len(b)

    async def close(self):
        self.closed = True


def test_app_talk_session_gets_the_hangup_frame(tmp_path):
    async def main():
        ws = FakeWS()
        cfg, p, s = await mk(tmp_path, kind="app", fe=WebFrontEnd(ws))
        await says(p, "Done.")
        await him(p, "merci, tschüss")
        await asyncio.sleep(0.05)
        await says(p, "Tschüss!")
        assert await until(lambda: ws.closed, timeout=3)     # close() sends the frame after it marks the session
        assert {"type": "hangup", "reason": "assistant_hangup"} in ws.text and s.closed and ws.bin > 0
    run(main())


def test_usage_event_passes_the_real_sanitizer(tmp_path, monkeypatch):
    """The real ~/bin/lib/usage.py (private state dir): every attr survives its token filter; no text."""
    path = os.path.join(os.path.expanduser("~"), "bin", "lib", "usage.py")
    if not os.path.exists(path):
        pytest.skip("no usage lib")
    monkeypatch.setenv("USAGE_STATE", str(tmp_path / "product"))
    monkeypatch.delenv("USAGE", raising=False)
    loader = importlib.machinery.SourceFileLoader("usage_under_test", path)
    spec = importlib.util.spec_from_loader("usage_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    async def main():
        cfg, p, s = await mk(tmp_path, ulog=mod.log)
        await says(p, "Done.")
        await him(p, "danke, tschüss")
        await asyncio.sleep(0.05)
        await says(p, "Tschüss!")
        assert await until(lambda: s.closed, timeout=3)
    run(main())
    spool = tmp_path / "product" / "spool"
    rows = [json.loads(x) for f in os.listdir(spool) for x in open(spool / f) if x.strip()]
    assert len(rows) == 1
    r = rows[0]
    assert (r["f"], r["k"], r["o"], r["a"], r["ch"]) == ("voice.call_end", "invoke", "goodbye", "assistant", "voice")
    assert r["r"].startswith("call:") and r["x"].startswith("callend:")
    assert set(r["at"]) == {"by", "call_kind", "asked", "nudged", "fallback", "pending", "stage", "dur_s", "resumes",
                            "gap_ms"}
    assert r["at"]["by"] == "silence" and r["at"]["fallback"] is True and r["at"]["call_kind"] == "tg-in"
    assert "tschüss" not in json.dumps(r).lower()


def test_model_goodbye_after_thanks_does_not_end_while_an_answer_is_pending(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, handoff=Handoff(answer=None), closing_nudge_s=5)
        await s._tool({"id": "a1", "name": "ask_assistant", "args": {"task": "check the backups", "urgency": "now"}})
        await says(p, "It's being handled.")
        await him(p, "ok thanks")
        await asyncio.sleep(0.05)
        await says(p, "You're welcome, bye!")                 # its goodbye; his answer is still on its way
        await asyncio.sleep(0.8)
        assert not s.closed and s.idle_stage < 2
        await s.close("test")
    run(main())


def test_no_spoken_over_the_question_counts_as_answering_it(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path)
        await says(p, "Done.")
        await him(p, "thanks")
        await asyncio.sleep(0.05)
        await p.q.put(audio(300))
        await p.q.put({"type": "transcript", "who": "assistant", "text": "Sure. Anything else?"})
        await asyncio.sleep(0.05)
        await him(p, "no")                                     # over the question, before turn_complete
        await p.q.put({"type": "interrupted"})
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [closing.CLOSING_BYE] and s.end_why == "nothing_else"
        await s.close("test")
    run(main())


def test_background_noise_delays_the_nudge_at_most_twice(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, closing_nudge_s=0.3, idle_prompt_s=0)
        await says(p, "Done.")
        await him(p, "thanks")
        loud = b"\xff\x3f" * 320                               # RMS far above voice_rms: a noisy line

        async def sink(pcm):
            pass
        p.send_audio = sink
        t0 = asyncio.get_event_loop().time()
        while not getattr(p, "prompts", None) and asyncio.get_event_loop().time() - t0 < 2:
            await s.audio_in(loud)
            await asyncio.sleep(0.02)
        took = asyncio.get_event_loop().time() - t0
        assert p.prompts == [closing.CLOSING_ASK] and 0.55 <= took <= 1.0
        await s.close("test")
    run(main())


# --- his answer to "anything else?": the short ending, unless it is a real new request ----------------------------

@pytest.mark.parametrize("text,cls", [("Thanks!", "thanks"), ("ok, merci", "thanks"), ("Tschüss!", "bye"),
                                      ("Nein, danke.", "no"), ("ja, wie isch s wätter morn?", "other"),
                                      ("thanks, and turn off the lights", "other"), ("...", "none"), ("", "none")])
def test_line_class(text, cls):
    assert closing.line_class(text) == cls


def test_no_to_the_models_anything_else_then_a_reply_without_goodbye_ends_short(tmp_path):
    """The model asked "anything else?" itself, he said no, it replied without a goodbye: closing_bye_s of quiet →
    goodbye, not the long path (idle_prompt_s, a second "anything else?", idle_bye_s)."""
    async def main():
        ul = ULog()
        cfg, p, s = await mk(tmp_path, ulog=ul, kind="app", idle_prompt_s=2.0, idle_bye_s=2.0)
        await him(p, "how do I get home")
        await asyncio.sleep(0.05)
        await says(p, "The next train leaves at 12:13. Kann ich sonst noch etwas für dich tun?")
        assert await until(lambda: s.idle_stage == 1 and s.stage_why == "asked_silence")
        await him(p, "Nein, danke.")
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        await says(p, "Gern geschehen!")
        assert await until(lambda: getattr(p, "prompts", None), timeout=3)
        took = time.monotonic() - t0
        assert p.prompts == [closing.CLOSING_BYE] and s.idle_stage == 2 and s.end_why == "closing_silence"
        assert cfg.closing_bye_s <= took < 1.5                 # quiet counted from the reply; not the 2 s + 2 s path
        await says(p, "Tschüss!")
        await p.q.put({"type": "tool_call", "id": "e1", "name": "end_call", "args": {"reason": "done"}})
        assert await until(lambda: s.closed, timeout=3)
        rows = ends(cfg)
        assert end_row(cfg)["why"] == "closing_silence" and ul.calls[0][1]["outcome"] == "closing_silence"
        idle = [(r["stage"], r.get("why"), r.get("cls")) for r in rows if r.get("ev") == "idle"]
        assert idle == [("asked", "asked_silence", None), ("closing_answer", "closing_silence", "no")]
        assert [r["closing"] for r in rows if r.get("ev") == "him"] == ["other", "no"]
        (d,) = [digest(evs) for evs in load_calls(calls_dir(cfg), last=1)]
        assert d["closing"] == ["other", "no"]
        assert [i[:2] for i in d["idle"]][-1] == ["closing_answer", "closing_silence"]
        assert "nein" not in json.dumps(d).lower() and "home" not in json.dumps(d).lower()
    run(main())


def test_thanks_to_the_relays_anything_else_then_a_reply_ends_short(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, closing_bye_s=0.2, idle_prompt_s=0.6, idle_bye_s=0.6)
        await him(p, "what's on my calendar")
        await asyncio.sleep(0.05)
        await says(p, "Nothing until the gym at six.")
        assert await until(lambda: getattr(p, "prompts", None), timeout=3)
        assert p.prompts == [IDLE_ASK]                          # the relay's own "anything else?"
        await says(p, "Anything else?")
        await asyncio.sleep(0.05)
        await him(p, "ok, merci")
        await asyncio.sleep(0.05)
        await says(p, "Gern!")                                  # no goodbye, no question
        assert await until(lambda: len(p.prompts) == 2, timeout=3)
        assert p.prompts[1] == closing.CLOSING_BYE and s.end_why == "closing_silence"
        assert await until(lambda: s.closed, timeout=3)       # no end_call: the relay hangs up after the goodbye
        assert end_row(cfg)["why"] == "closing_silence"
    run(main())


def test_thanks_to_anything_else_with_a_silent_model_says_goodbye_not_a_second_question(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path)
        await says(p, "The vacuum is docked. Anything else?")
        assert await until(lambda: s.idle_stage == 1)
        await him(p, "ok danke")
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [closing.CLOSING_BYE] and s.end_why == "thanks" and s.idle_stage == 2
        await s.close("test")
    run(main())


def test_a_real_request_after_anything_else_keeps_the_plain_timers(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, idle_prompt_s=0.8)
        await says(p, "Done. Süsch no öppis?")
        assert await until(lambda: s.idle_stage == 1)
        await him(p, "ja, wie isch s wätter morn?")
        await asyncio.sleep(0.05)
        await says(p, "Morn wird's sunnig und 18 Grad.")
        await asyncio.sleep(0.6)                               # past closing_bye_s + the reply: no short ending
        assert not getattr(p, "prompts", None) and s.idle_stage == 0 and not s.closing_reply
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [IDLE_ASK]                         # the plain silence clock
        await s.close("test")
    run(main())


def test_a_reply_that_asks_him_something_keeps_the_plain_timers(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, idle_prompt_s=0.8)
        await says(p, "Done. Anything else?")
        assert await until(lambda: s.idle_stage == 1)
        await him(p, "no thanks")
        await asyncio.sleep(0.05)
        await says(p, "Okay. Should I send you the link on Telegram?")
        await asyncio.sleep(0.6)
        assert not getattr(p, "prompts", None) and s.idle_stage == 0
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [IDLE_ASK]
        await s.close("test")
    run(main())


def test_a_tool_call_after_his_closing_answer_is_a_request(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, idle_prompt_s=0.8)
        await says(p, "Done. Anything else?")
        assert await until(lambda: s.idle_stage == 1)
        await him(p, "no thanks")
        await asyncio.sleep(0.05)
        await p.q.put({"type": "tool_call", "id": "a1", "name": "ask_assistant",
                       "args": {"task": "check the backups", "urgency": "now"}})
        assert await until(lambda: p.results, timeout=2)
        await says(p, "The backups are fine.")
        await asyncio.sleep(0.6)
        assert not getattr(p, "prompts", None) and not s.closing_reply
        assert await until(lambda: getattr(p, "prompts", None), timeout=2)
        assert p.prompts == [IDLE_ASK]
        await s.close("test")
    run(main())


def test_he_carries_on_during_the_short_wait(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, closing_bye_s=0.5)
        await says(p, "Done. Anything else?")
        assert await until(lambda: s.idle_stage == 1)
        await him(p, "nein danke")
        await asyncio.sleep(0.05)
        await says(p, "Gern!")
        assert await until(lambda: s.closing_reply)
        await him(p, "doch, wie spät isch es?")                 # within closing_bye_s: a new request
        assert await until(lambda: not s.closing_reply and s.closing is None)
        await asyncio.sleep(0.8)
        assert not getattr(p, "prompts", None) and not s.closed and s.idle_stage == 0
        await s.close("test")
    run(main())


def test_a_late_answer_after_the_reply_restarts_the_plain_clock(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, idle_prompt_s=0.8)
        await says(p, "Done. Anything else?")
        assert await until(lambda: s.idle_stage == 1)
        await him(p, "no thanks")
        await asyncio.sleep(0.05)
        await says(p, "Okay.")
        assert await until(lambda: s.closing_reply)
        await s._deliver_late("v0001", "The backups are fine.")   # a new answer: he may react to it
        assert not s.closing_reply and p.injects
        await says(p, "By the way, the backups are fine.")
        await asyncio.sleep(0.6)
        assert not getattr(p, "prompts", None) and not s.closing_reply
        await s.close("test")
    run(main())
