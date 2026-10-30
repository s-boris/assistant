import asyncio
import glob
import os
import stat
import sys
import time

import pytest

from util import RecFE, Rig, StubProvider, until
from voice_relay.audio import tone
from voice_relay.providers import GeminiLive, OpenAIRealtime, ProviderError, make_provider
from voice_relay.server import instructions
from voice_relay.session import VoiceSession

LAT = {}


def run(coro):
    return asyncio.run(coro)


async def _session(r, mode="ptt", fe=None):
    fe = fe or RecFE()
    s = VoiceSession(r.cfg, make_provider(r.cfg, instructions(r.cfg), mode), fe, r.relay.handoff)
    await s.start()
    return s, fe


def test_ptt_roundtrip_answer_is_filtered_and_spoken(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            s, fe = await _session(r)
            await s.ptt(True)
            pcm = tone(16000, 400)
            for i in range(0, len(pcm), 1280):
                await s.audio_in(pcm[i:i + 1280])
            t0 = time.monotonic()
            await s.ptt(False)
            assert await until(lambda: "Fake answer" in fe.said())
            LAT["ptt_release_to_answer_text_ms"] = round((time.monotonic() - t0) * 1000, 1)
            setup = r.gem.log["setups"][0]
            assert setup["model"] == "models/" + r.cfg.gemini_model
            assert setup["realtimeInputConfig"]["automaticActivityDetection"]["disabled"] is True
            decl = setup["tools"][0]["functionDeclarations"][0]
            assert decl["name"] == "ask_assistant" and decl["behavior"] == "NON_BLOCKING"
            assert decl["parameters"]["type"] == "OBJECT"
            assert "ask_assistant" in setup["systemInstruction"]["parts"][0]["text"]
            resp = r.gem.log["tool_responses"][0]["response"]
            assert resp["scheduling"] == "WHEN_IDLE"
            assert "(phone withheld)" in resp["answer"] and "079" not in resp["answer"]
            assert r.gem.log["audio_bytes"] >= len(pcm) - 1280
            assert len(fe.audio) > 0
            assert r.inbox.results[0]["delivered"] == "sync"
            m = [e for e in fe.events if e["type"] == "metric"][0]
            LAT["tool_call_to_answer_ms(fake session 50ms)"] = m["tool_to_answer_ms"]
            LAT["relay_handoff_to_answer_ms"] = r.inbox.results[0]["handoff_to_answer_ms"]
            await s.close()
            assert fe.hung == "closed"
        finally:
            await r.close()
    run(main())
    print("LATENCY", LAT, file=sys.stderr)


def test_slow_answer_is_pushed_into_the_live_session(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), slow_delay=0.6, sync_wait_s=0.2)
        try:
            s, fe = await _session(r)
            await s.text_in("slow: check the backups")
            assert await until(lambda: r.gem.log["tool_responses"])
            assert r.gem.log["tool_responses"][0]["response"]["status"] == "working"
            assert await until(lambda: r.gem.log["injects"], 3)
            assert r.gem.log["injects"][0].startswith("[assistant result] Fake answer")
            assert await until(lambda: r.inbox.results, 3)        # the hand-off's reply lands after the inject
            assert r.inbox.results[0]["delivered"] == "live"
            st = [e["working"] for e in fe.events if e["type"] == "status"]
            assert 1 in st and st[-1] == 0
            await s.close()
        finally:
            await r.close()
    run(main())


def test_answer_after_hangup_reports_not_delivered(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), slow_delay=0.5, sync_wait_s=0.1)
        try:
            s, fe = await _session(r)
            await s.text_in("slow: summarize the week")
            assert await until(lambda: r.gem.log["tool_responses"])
            await s.close("hangup")
            assert await until(lambda: r.inbox.results, 3)
            assert r.inbox.results[0]["delivered"] == "none"
        finally:
            await r.close()
    run(main())


def test_channel_down_falls_back_to_an_inbox_note(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        r.cfg.plugin_url = "http://127.0.0.1:9/voice"
        try:
            s, fe = await _session(r)
            await s.text_in("add oat milk to the shopping list")
            assert await until(lambda: r.gem.log["tool_responses"])
            assert r.gem.log["tool_responses"][0]["response"]["status"] == "queued"
            notes = glob.glob(os.path.join(r.cfg.inbox_dir, "voice-*.md"))
            assert len(notes) == 1 and stat.S_IMODE(os.stat(notes[0]).st_mode) == 0o600
            body = open(notes[0]).read()
            assert "oat milk" in body and "voice-reply v" in body
            await s.close()
        finally:
            await r.close()
    run(main())


def test_later_urgency_returns_at_once_then_reports(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), delay=0.4)
        try:
            s, fe = await _session(r)
            t0 = time.monotonic()
            await s.text_in("research later: quiet NAS fans")
            assert await until(lambda: r.gem.log["tool_responses"])
            assert time.monotonic() - t0 < 0.35
            assert r.gem.log["tool_responses"][0]["response"]["status"] == "working"
            assert await until(lambda: r.gem.log["injects"], 3)
            await s.close()
        finally:
            await r.close()
    run(main())


def test_open_mic_mode_uses_automatic_vad(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["when is my next train"])
        try:
            s, fe = await _session(r, mode="open")
            pcm = tone(16000, 1200)
            for i in range(0, len(pcm), 1280):
                await s.audio_in(pcm[i:i + 1280])
            assert await until(lambda: "Fake answer about: when is my next train" in fe.said())
            auto = r.gem.log["setups"][0]["realtimeInputConfig"]["automaticActivityDetection"]
            assert auto["disabled"] is False and auto["silenceDurationMs"] == 500
            await s.close()
        finally:
            await r.close()
    run(main())


def test_small_talk_stays_local(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            s, fe = await _session(r)
            await s.text_in("hello there")
            assert await until(lambda: "Hi!" in fe.said())
            assert r.inbox.tasks == []
            await s.close()
        finally:
            await r.close()
    run(main())


def test_wrong_key_is_rejected(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), key="wrong-key")
        try:
            with pytest.raises(ProviderError):
                await _session(r)
            assert r.gem.log["auth_fail"] == 1
        finally:
            await r.close()
    run(main())


def test_unknown_tool_empty_task_and_rate_limit(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), max_asks_per_min=2)
        try:
            p, fe = StubProvider(), RecFE()
            s = VoiceSession(r.cfg, p, fe, r.relay.handoff)
            await s._tool({"id": "a", "name": "delete_everything", "args": {}})
            await s._tool({"id": "b", "name": "ask_assistant", "args": {"task": "  "}})
            for i in range(3):
                await s._tool({"id": "c%d" % i, "name": "ask_assistant", "args": {"task": "what's up %d" % i}})
            assert p.results[0][2] == {"error": "unknown tool"}
            assert p.results[1][2] == {"error": "empty task"}
            assert "answer" in p.results[2][2] and "answer" in p.results[3][2]
            assert "a lot at once" in p.results[4][2]["answer"]
            assert len(r.inbox.tasks) == 2
        finally:
            await r.close()
    run(main())


def test_worker_lane_answers_or_hands_off(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text("import sys\nq=sys.stdin.read()\nprint('HANDOFF' if 'hard' in q else 'It is sunny.')\n")
    async def main():
        r = await Rig.make(str(tmp_path), handoff_order=("worker", "channel", "inbox"),
                           worker_cmd="%s %s" % (sys.executable, worker), worker_cwd=str(tmp_path))
        try:
            p, fe = StubProvider(), RecFE()
            s = VoiceSession(r.cfg, p, fe, r.relay.handoff)
            t0 = time.monotonic()
            await s._tool({"id": "w1", "name": "ask_assistant", "args": {"task": "weather now?"}})
            LAT["worker_lane_python_stub_ms"] = round((time.monotonic() - t0) * 1000, 1)
            assert p.results[0][2] == {"answer": "It is sunny."} and r.inbox.tasks == []
            await s._tool({"id": "w2", "name": "ask_assistant", "args": {"task": "a hard one"}})
            assert "Fake answer" in p.results[1][2]["answer"] and len(r.inbox.tasks) == 1
        finally:
            await r.close()
    run(main())


def test_openai_adapter_normalizes_events():
    o = OpenAIRealtime.__new__(OpenAIRealtime)
    ev = o.normalize({"type": "response.function_call_arguments.done", "call_id": "c1", "name": "ask_assistant",
                      "arguments": '{"task": "x", "urgency": "later"}'})
    assert ev == [{"type": "tool_call", "id": "c1", "name": "ask_assistant", "args": {"task": "x", "urgency": "later"}}]
    assert o.normalize({"type": "response.output_audio_transcript.delta", "delta": "hi"})[0]["text"] == "hi"
    assert o.normalize({"type": "input_audio_buffer.speech_started"}) == [{"type": "interrupted"}]
    g = GeminiLive.__new__(GeminiLive)
    g.resume_handle = None
    g.normalize({"sessionResumptionUpdate": {"newHandle": "h9", "resumable": True}})
    assert g.resume_handle == "h9"
    assert g.normalize({"toolCallCancellation": {"ids": ["x"]}}) == [{"type": "tool_cancel", "ids": ["x"]}]
