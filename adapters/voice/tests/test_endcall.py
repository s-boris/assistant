"""end_call tool and the silence fallback: the relay hangs up from its side after the goodbye has played out.
StubProvider + RecFE / the real CallFrontEnd with FakeCallClient; nothing reaches Gemini or Telegram."""
import asyncio
import json
import os
import time
import types

from util import RecFE, StubProvider, until
from voice_relay.calllog import CallLog
from voice_relay.config import Config
from voice_relay.fakes import FakeCallClient
from voice_relay.providers import END_TOOL, make_provider
from voice_relay.session import IDLE_ASK, IDLE_BYE, IDLE_BYE_PENDING, REST_ON_TELEGRAM, VoiceSession
from voice_relay.tgcall import CallFrontEnd


def run(coro):
    return asyncio.run(coro)


def mkcfg(tmp, **over):
    kw = dict(state_dir=str(tmp / "state"), inbox_dir=str(tmp / "inbox"), files_dir=str(tmp / "inbox" / "files"),
              fast_lane=False, decisions=False, recap_grace_s=0, hangup_tail_s=0.05, idle_prompt_s=0)
    kw.update(over)
    return Config(**kw)


class Handoff:
    def __init__(self, answer="Fake answer."):
        self.ans, self.pending, self.n = answer, {}, 0

    async def submit(self, task, urgency, deliver_late=None, owner=None):
        self.n += 1
        p = types.SimpleNamespace(id="v%04d" % self.n, route="channel")
        self.pending[p.id] = p
        return p

    async def wait(self, p, timeout):
        if self.ans is not None:
            self.pending.pop(p.id, None)
            return self.ans
        return None

    def detach(self, owner):
        pass


class OpenStub(StubProvider):
    mode, transcribes_input = "open", True


def audio(ms):   # assistant audio at the stub's 24 kHz
    return {"type": "audio", "pcm": b"\1\0" * (24 * ms)}


def ends(cfg):
    rows = []
    for name in os.listdir(os.path.join(cfg.state_dir, "calls")):
        with open(os.path.join(cfg.state_dir, "calls", name)) as fh:
            rows += [json.loads(x) for x in fh if x.strip()]
    return rows


def test_tool_is_declared_and_prompted():
    cfg = Config(fast_lane=False, decisions=False, track_id=False, phone_open=False)
    assert [t["name"] for t in make_provider(cfg, "x").tools] == ["ask_assistant", "end_call"]
    assert END_TOOL["parameters"]["required"] == ["reason"]
    text = open(cfg.prompt_file).read()
    assert "end_call" in text and "I'll send the rest on Telegram" in text and '"[call]"' in text


def test_goodbye_plays_out_then_the_relay_leaves_the_call(tmp_path):
    async def main():
        cfg = mkcfg(tmp_path)
        client = FakeCallClient()
        fe = CallFrontEnd(client, 555)
        p, log = StubProvider(), CallLog(cfg, "tg-in", "owner")
        ended = []
        s = VoiceSession(cfg, p, fe, Handoff(), log=log, on_end=lambda _s, secs: ended.append(secs))
        await s.start()
        await p.q.put(audio(400))                          # "Bye, talk soon!" (0.4 s)
        await p.q.put({"type": "tool_call", "id": "e1", "name": "end_call", "args": {"reason": "he said bye"}})
        t0 = time.monotonic()
        assert await until(lambda: s.closed, timeout=5)
        took = time.monotonic() - t0
        assert took >= 0.4 + 0.05                          # the goodbye audio was played out first
        assert client.hung == [555] and len(client.sent[555]) == 400 * 96   # 48 kHz, every frame sent
        assert p.results == [("e1", "end_call", {"status": "ending"})] and p.scheduling == ["SILENT"]
        assert ended
        rows = ends(cfg)
        assert [r for r in rows if r.get("ev") == "end"][0]["reason"] == "assistant_hangup"
        assert any(r.get("ev") == "hangup" and r.get("why") == "end_call" for r in rows)
    run(main())


def test_pending_answer_says_rest_on_telegram_first_and_recap_still_runs(tmp_path):
    async def main():
        cfg = mkcfg(tmp_path, sync_wait_s=0.01)
        p, fe = StubProvider(), RecFE()
        s = VoiceSession(cfg, p, fe, Handoff(answer=None), log=CallLog(cfg, "tg-in", "owner"))
        await s.start()
        await s._tool({"id": "a1", "name": "ask_assistant", "args": {"task": "is the server ok", "urgency": "now"}})
        assert s.pending_now()
        await p.q.put({"type": "tool_call", "id": "e1", "name": "end_call", "args": {"reason": "bye"}})
        assert await until(lambda: len(p.results) == 2)
        assert p.results[1][2] == {"status": "ending", "say": REST_ON_TELEGRAM} and p.scheduling[-1] is None
        await asyncio.sleep(0.2)
        assert not s.closed                                # waits for the Telegram line to be spoken
        await p.q.put(audio(300))
        assert await until(lambda: s.closed, timeout=5)
        notes = os.listdir(cfg.inbox_dir)
        assert any(n.startswith("voice-recap-") for n in notes)   # the open hand-off is still reported
    run(main())


def test_telegram_line_already_said_and_double_end_call(tmp_path):
    async def main():
        cfg = mkcfg(tmp_path, sync_wait_s=0.01)
        p = StubProvider()
        s = VoiceSession(cfg, p, RecFE(), Handoff(answer=None))
        await s.start()
        await s._tool({"id": "a1", "name": "ask_assistant", "args": {"task": "x", "urgency": "now"}})
        await p.q.put({"type": "transcript", "who": "assistant", "text": "I'll send the rest on Telegram. Bye!"})
        await p.q.put({"type": "tool_call", "id": "e1", "name": "end_call", "args": {"reason": "bye"}})
        await p.q.put({"type": "tool_call", "id": "e2", "name": "end_call", "args": {"reason": "bye"}})
        assert await until(lambda: s.closed, timeout=5)
        assert p.results[1][2] == {"status": "ending"} and p.results[2][2] == {"status": "already ending"}
    run(main())


def test_barge_in_cancels_the_hangup_but_late_goodbye_transcript_does_not(tmp_path):
    async def main():
        cfg = mkcfg(tmp_path, hangup_tail_s=0.3)
        p = StubProvider()
        s = VoiceSession(cfg, p, RecFE(), Handoff(), log=CallLog(cfg, "tg-in", "owner"))
        await s.start()
        await p.q.put(audio(500))
        await p.q.put({"type": "tool_call", "id": "e1", "name": "end_call", "args": {"reason": "bye"}})
        assert await until(lambda: s.ending is not None)
        await p.q.put({"type": "transcript", "who": "user", "text": "tschüss"})   # trailing transcript of his bye
        await asyncio.sleep(0.1)
        assert s.ending is not None
        await p.q.put({"type": "interrupted"})            # he talks over the goodbye: keep the call
        assert await until(lambda: s.ending is None)
        await asyncio.sleep(1.2)
        assert not s.closed
        await s.close("test")
        assert any(r.get("ev") == "hangup_cancelled" for r in ends(cfg))
    run(main())


def test_silence_asks_once_then_says_bye_and_hangs_up(tmp_path):
    async def main():
        cfg = mkcfg(tmp_path, idle_prompt_s=0.3, idle_unanswered_s=0.3, idle_bye_s=0.3)
        p = OpenStub()
        s = VoiceSession(cfg, p, RecFE(), Handoff(), log=CallLog(cfg, "tg-in", "owner"))
        await s.start()
        assert await until(lambda: getattr(p, "prompts", None), timeout=3)
        assert p.prompts == [IDLE_ASK]
        await p.q.put({"type": "transcript", "who": "user", "text": "yes, one more"})   # he answers: clock restarts
        await asyncio.sleep(0.2)
        assert p.prompts == [IDLE_ASK] and s.idle_stage == 0
        assert await until(lambda: len(p.prompts) == 3, timeout=4)
        assert p.prompts[1:] == [IDLE_ASK, IDLE_BYE]
        await p.q.put(audio(200))
        await p.q.put({"type": "tool_call", "id": "e1", "name": "end_call", "args": {"reason": "silence"}})
        assert await until(lambda: s.closed, timeout=5)
        assert [r for r in ends(cfg) if r.get("ev") == "end"][0]["reason"] == "assistant_hangup"
    run(main())


def test_pending_answer_is_never_asked_away_only_the_pending_cap_ends_it(tmp_path):
    async def main():
        cfg = mkcfg(tmp_path, idle_prompt_s=0.1, idle_unanswered_s=0.1, idle_bye_s=0.1, bye_fallback_s=0.2,
                    idle_pending_max_s=0.8, sync_wait_s=0.01)
        p = OpenStub()
        s = VoiceSession(cfg, p, RecFE(), Handoff(answer=None), log=CallLog(cfg, "tg-in", "owner"))
        await s.start()
        await s._tool({"id": "a1", "name": "ask_assistant", "args": {"task": "x", "urgency": "now"}})
        await asyncio.sleep(0.5)                                  # well past ask + bye: nothing while it's pending
        assert not getattr(p, "prompts", None) and not s.closed
        assert await until(lambda: s.closed, timeout=6)          # the model never calls end_call
        assert p.prompts == [IDLE_BYE_PENDING]
        rows = ends(cfg)
        assert any(r.get("ev") == "hangup" and r.get("why") == "silence" for r in rows)
        assert [r for r in rows if r.get("ev") == "end"][0]["reason"] == "assistant_hangup"
    run(main())


def test_no_silence_prompts_in_push_to_talk_or_without_transcripts(tmp_path):
    async def main():
        cfg = mkcfg(tmp_path, idle_prompt_s=0.05, idle_bye_s=0.05)
        for prov in (StubProvider(), type("NoTr", (StubProvider,), {"mode": "open"})()):
            s = VoiceSession(cfg, prov, RecFE(), Handoff())
            await s.start()
            await asyncio.sleep(0.4)
            assert not getattr(prov, "prompts", None) and not s.closed
            await s.close("test")
    run(main())
