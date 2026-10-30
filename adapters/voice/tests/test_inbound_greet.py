"""His calls (Telegram tg-in, phone app) open with a short pick-up line, so a connected call never sounds dead.

REQUIREMENT:
inbound call: first audio <= ~1.5 s after the line is up, greet once (never through the model, never again after a
reconnect), cut when he talks over it, skipped when he speaks first; no clip cached -> an earcon at once.
Fake Gemini Live + fake Gemini TTS + fake call client; nothing rings, nothing leaves the box."""
import asyncio
import base64
import io
import json
import os
import time
import wave

from aiohttp import web

from util import Rig, until
from test_app_ticket import clients, frames_until, ticket
from voice_relay import greet
from voice_relay.audio import pickup_cue, resample, rms as rms_of, tone
from voice_relay.calllog import calls_dir, format_call, format_quality, load_calls, quality
from voice_relay.config import Config
from voice_relay.fakes import FakeCallClient, _serve
from voice_relay.providers import make_provider
from voice_relay.server import instructions
from voice_relay.tgcall import CallBridge

OWNER = 111222333
ON = dict(greet_inbound=True, greet_delay_s=0.1, fast_lane=False, decisions=False, recap_grace_s=0)


def run(coro):
    return asyncio.run(coro)


def speech(ms=800, lead_ms=200, tail_ms=300, rate=greet.RATE):
    """A stand-in for a spoken line: silence, a voiced stretch, silence (what a TTS model returns)."""
    sil = lambda n: b"\0\0" * (rate * n // 1000)
    return sil(lead_ms) + tone(rate, ms, 220.0) + sil(tail_ms)


def wav(pcm, rate=greet.RATE):
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(pcm)
    w.close()
    return buf.getvalue()


class FakeTTS:
    """Gemini generateContent for TTS models: answers each request with the next scripted reply
    ("wav", pcm) | ("raw", pcm) | ("http", status); the last one repeats."""
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    async def start(self):
        app = web.Application()
        app.router.add_post("/v1beta/models/{tail:.*}", self.gen)
        self.runner, self.port = await _serve(app)
        return "http://127.0.0.1:%d/v1beta" % self.port

    async def stop(self):
        await self.runner.cleanup()

    async def gen(self, request):
        body = await request.json()
        self.requests.append({"model": request.match_info["tail"].split(":")[0], "body": body,
                              "key": request.headers.get("x-goog-api-key")})
        kind, val = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if kind == "http":
            return web.json_response({"error": {"code": val}}, status=val)
        mime, data = ("audio/wav", wav(val)) if kind == "wav" else ("audio/L16;codec=pcm;rate=24000", val)
        return web.json_response({"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": mime, "data": base64.b64encode(data).decode()}}]}}]})


async def bridge(r, client=None):
    client = client or FakeCallClient()
    b = CallBridge(r.cfg, client, OWNER, r.relay.handoff, lambda: instructions(r.cfg), make_provider,
                   greeter=r.relay.greeter)
    await b.start()
    return b, client


def call_events(cfg, cid, ev=None):
    return [e for c in load_calls(calls_dir(cfg), 50) for e in c if e["call"] == cid and (ev is None or e["ev"] == ev)]


def cached(r, ms=800):
    """Put a pick-up clip in the relay's cache, as `voice-relay greet --refresh` would."""
    r.relay.greeter._save(greet.trim(speech(ms)), "test-tts")
    return len(resample(r.relay.greeter.clip, greet.RATE, 48000))


def test_inbound_call_first_audio_within_1_5_s_greets_once(tmp_path):
    """REQUIREMENT: inbound call: first audio <= ~1.5 s, greet once. The cached line goes out right after the line
    is up, as local audio (the model gets no prompt and no note), once: not again when he talks, nor after the
    Gemini socket drops and reconnects; `calls` and `stats` show it."""
    async def main():
        r = await Rig.make(str(tmp_path), script=["what's the weather tomorrow"], **ON)
        try:
            n48 = cached(r)
            b, cl = await bridge(r)
            t0 = time.monotonic()
            await cl.ring("c1", OWNER)
            assert cl.accepted == ["c1"]
            assert await until(lambda: cl.frame_times["c1"], 3)
            first_s = cl.frame_times["c1"][0] - t0
            assert first_s <= 1.5, first_s                                   # his ring -> first audio
            assert await until(lambda: len(cl.sent["c1"]) >= n48, 3)
            await asyncio.sleep(0.2)
            assert len(cl.sent["c1"]) == n48 + (-n48 % 960)                   # the whole line, once
            assert not r.gem.log["injects"] and not r.gem.log["notes"] and not r.gem.log["texts"]
            cid = b.calls["c1"].log.id
            await cl.speak("c1", tone(48000, 1200))                           # he asks; the model answers
            assert await until(lambda: r.gem.log["tool_responses"], 5)
            s, n0 = b.calls["c1"], len(r.gem.log["setups"])
            await r.gem.drop()                                                # Gemini drops, the call goes on
            assert await until(lambda: s.gen == 1 and len(r.gem.log["setups"]) > n0, 4)
            await asyncio.sleep(0.3)
            await cl.end("c1")
            assert await until(lambda: not b.calls)
            evs = call_events(r.cfg, cid)
            greets = [e for e in evs if e["ev"] == "greet"]
            assert len(greets) == 1, greets
            g = greets[0]
            assert g["inbound"] is True and g["via"] == "clip" and g["first_audio_ms"] <= 1500
            assert 0.08 <= g["first_audio_ms"] / 1000.0 <= 0.6                # greet_delay_s after the line is up
            ans = [e for e in evs if e["ev"] == "answer"][0]
            assert ans["provider_ms"] >= 0 and ans["connect_ms"] >= 0
            ain = [e for e in evs if e["ev"] == "audio_in"][0]
            assert ain["ms"] >= 1100 and ain["voiced_ms"] >= 1100 and ain["sent_ms"] >= 1100
            out = format_call(evs)
            assert "pick-up line (clip" in out and "his audio: 1.2 s in" in out and "answered: provider ready" in out
            q = quality([evs])
            assert q["greet_in_ms"]["n"] == 1 and q["greet_in_via"] == {"clip": 1} and q["no_voice_in"] == 0
            assert "his calls: line up → pick-up line ms median" in format_quality(q)
            # the next call gets its own line (once per CALL)
            await cl.ring("c2", OWNER)
            assert await until(lambda: len(cl.sent["c2"]) >= n48, 3)
            cid2 = b.calls["c2"].log.id
            await cl.end("c2")
            assert await until(lambda: not b.calls)
            assert len(call_events(r.cfg, cid2, "greet")) == 1
        finally:
            await r.close()
    run(main())


def test_he_talks_over_the_line_it_is_cut_and_his_words_reach_the_model(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["what's the weather tomorrow"], **ON)
        try:
            n48 = cached(r, ms=2000)                                           # a long line: time to talk over it
            b, cl = await bridge(r)
            await cl.ring("c1", OWNER)
            assert await until(lambda: len(cl.sent["c1"]) > 0, 3)
            cid = b.calls["c1"].log.id
            await cl.speak("c1", tone(48000, 300))                             # "hey, ..." over it
            assert await until(lambda: call_events(r.cfg, cid, "greet_cut"), 2)
            await asyncio.sleep(0.3)
            assert len(cl.sent["c1"]) < n48 * 0.7                              # the rest was dropped
            assert r.gem.log["audio_bytes"] > 0                                # his audio went to the model all along
            await cl.end("c1")
            assert await until(lambda: not b.calls)
            assert "pick-up line cut: he talked over it" in format_call(call_events(r.cfg, cid))
            assert quality([call_events(r.cfg, cid)])["greet_cut"] == 1
        finally:
            await r.close()
    run(main())


def test_he_speaks_first_the_line_is_skipped(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), **dict(ON, greet_delay_s=0.4))
        try:
            cached(r)
            b, cl = await bridge(r)
            await cl.ring("c1", OWNER)
            cid = b.calls["c1"].log.id
            await cl.speak("c1", tone(48000, 200))                             # "Hallo?" before the line plays
            assert await until(lambda: call_events(r.cfg, cid, "greet"), 2)
            g = call_events(r.cfg, cid, "greet")[0]
            assert g["via"] == "skipped" and "first_audio_ms" not in g
            assert not cl.sent["c1"]
            await cl.end("c1")
            assert await until(lambda: not b.calls)
        finally:
            await r.close()
    run(main())


def test_a_noisy_line_neither_skips_nor_cuts_the_line(tmp_path):
    """Steady background (car, street) louder than voice_rms from the moment the line is up: still his pick-up line,
    whole; his real voice over it still cuts it."""
    async def main():
        r = await Rig.make(str(tmp_path), **dict(ON, greet_delay_s=0.3))
        try:
            n48 = cached(r, ms=1000)
            b, cl = await bridge(r)
            noise = tone(48000, 10, 150.0, amp=0.03)                           # ~700 RMS, above voice_rms 500
            assert rms_of(noise) > r.cfg.voice_rms
            await cl.ring("c1", OWNER)
            cid = b.calls["c1"].log.id
            for _ in range(160):                                               # 1.6 s of it, in real time
                await cl.speak("c1", noise)
                await asyncio.sleep(0.01)
            assert len(cl.sent["c1"]) >= n48 and not call_events(r.cfg, cid, "greet_cut")
            assert call_events(r.cfg, cid, "greet")[0]["via"] == "clip"
            await cl.end("c1")
            assert await until(lambda: not b.calls)
            await cl.ring("c2", OWNER)                                         # same noise, then he talks over it
            cid2 = b.calls["c2"].log.id
            for i in range(60):
                await cl.speak("c2", noise if i < 40 else tone(48000, 10, 300.0))
                await asyncio.sleep(0.01)
            assert await until(lambda: call_events(r.cfg, cid2, "greet_cut"), 2)
            await cl.end("c2")
            assert await until(lambda: not b.calls)
        finally:
            await r.close()
    run(main())


def test_no_clip_yet_the_earcon_plays_at_once_and_the_clip_is_made_in_the_background(tmp_path):
    async def main():
        tts = FakeTTS([("wav", speech(700))])
        url = await tts.start()
        r = await Rig.make(str(tmp_path), **dict(ON, gemini_rest_url=url, greet_tts_models=("tts-a",)))
        try:
            r.relay.greeter.autorefresh = True                                  # as in the running relay
            b, cl = await bridge(r)
            await cl.ring("c1", OWNER)
            cue48 = pickup_cue(48000)
            assert await until(lambda: len(cl.sent["c1"]) >= len(cue48), 2)
            cid = b.calls["c1"].log.id
            await cl.end("c1")
            assert await until(lambda: not b.calls)
            g = call_events(r.cfg, cid, "greet")[0]
            assert g["via"] == "tone" and g["first_audio_ms"] <= 1500
            assert await until(lambda: r.relay.greeter.clip is not None, 3)    # generated meanwhile
            req = tts.requests[0]
            assert req["model"] == "tts-a" and req["key"] == "fake-key"
            assert req["body"]["contents"][0]["parts"][0]["text"] == "Hey, was gibt's?"   # the plain line only
            vc = req["body"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
            assert vc["voiceName"] == r.cfg.gemini_voice
            await cl.ring("c2", OWNER)
            assert await until(lambda: cl.sent["c2"], 2)
            cid2 = b.calls["c2"].log.id
            await cl.end("c2")
            assert await until(lambda: not b.calls)
            assert call_events(r.cfg, cid2, "greet")[0]["via"] == "clip"
            assert len(tts.requests) == 1                                       # cached, not made per call
        finally:
            await r.close()
            await tts.stop()
    run(main())


def test_tts_line_is_checked_trimmed_cached_per_voice_and_text(tmp_path):
    async def main():
        # 1st reply: the model read out far more than three words -> refused; 2nd: HTTP error on model a -> model b
        tts = FakeTTS([("wav", speech(5000)), ("http", 500), ("raw", speech(700))])
        url = await tts.start()
        cfg = Config(state_dir=str(tmp_path / "state"), gemini_key_file=str(tmp_path / "k"), gemini_rest_url=url,
                     greet_tts_models=("tts-a", "tts-b"), gemini_voice="Orus")
        with open(cfg.gemini_key_file, "w") as fh:
            fh.write("fake-key\n")
        try:
            g = greet.Greeter(cfg)
            assert g.audio(48000)[1] == "tone" and not g.autorefresh and g._task is None   # no network unasked
            assert await g.ensure() is True
            assert [x["model"] for x in tts.requests] == ["tts-a", "tts-a", "tts-b"]
            span = greet.voiced_span(g.clip)
            assert span[0] <= greet.RATE * 0.03 and 0.65 <= len(g.clip) / 2.0 / greet.RATE <= 0.9   # trimmed
            assert g.meta["model"] == "tts-b" and g.meta["voice"] == "Orus" and g.meta["text"] == "Hey, was gibt's?"
            assert oct(os.stat(g.pcm_path).st_mode & 0o777) == "0o600"
            assert g.audio(48000)[1] == "clip" and g.status()["clip"] is True
            g2 = greet.Greeter(cfg)                                             # a restart: from disk, no request
            assert g2.audio(24000) == (g.clip, "clip") and len(tts.requests) == 3
            other = greet.Greeter(Config(**dict(cfg.__dict__, gemini_voice="Charon")))
            assert other.key != g.key and other.clip is None                    # a new voice needs its own line
            assert greet.clip_key("Orus", "Ja?") != greet.clip_key("Orus", "Hey, was gibt's?")
            assert greet.check(speech(5000), "Hey, was gibt's?").startswith("too long")
            assert greet.check(b"\0\0" * 24000, "Ja?") == "no speech"
            # every model failing: no clip, an error to show, and no retry storm
            tts.replies = [("http", 503)]
            bad = greet.Greeter(Config(**dict(cfg.__dict__, greet_text="Ja?")))
            assert await bad.ensure() is False and "HTTP 503" in bad.status()["error"]
            bad.autorefresh = True
            assert bad.refresh_soon() is None                                   # backed off after the failure
        finally:
            await tts.stop()
    run(main())


def test_app_call_greets_once_like_a_telegram_call(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), **ON)
        c = await clients(r)
        try:
            cached(r)
            n24 = len(r.relay.greeter.clip)                                    # the app plays 24 kHz
            _, body = await ticket(r, {"via": "app"})
            t0 = time.monotonic()
            ws = await c.ws_connect("/ws?ticket=" + body["ticket"])
            await ws.send_str(json.dumps({"type": "start"}))
            evs, audio = await frames_until(ws, lambda e, a: a >= n24, timeout=3)
            assert any(x["type"] == "state" and x["state"] == "live" for x in evs) and audio >= n24
            assert time.monotonic() - t0 <= 1.5
            assert not r.gem.log["injects"] and not r.gem.log["notes"]
            await ws.send_str(json.dumps({"type": "hangup"}))
            await ws.close()
            assert await until(lambda: not r.relay.live)
            recs = [e for cl in load_calls(calls_dir(r.cfg), 10) for e in cl]
            gs = [e for e in recs if e["ev"] == "greet"]
            assert len(gs) == 1 and gs[0]["inbound"] and gs[0]["via"] == "clip"
            # an in-app ring he answered opens like an outbound call: no pick-up line on top
            _, body = await ticket(r, {"via": "app", "reason": "test", "say": ""})
            ws = await c.ws_connect("/ws?ticket=" + body["ticket"])
            await ws.send_str(json.dumps({"type": "start"}))
            await frames_until(ws, lambda e, a: a > 0, timeout=3)
            await ws.close()
            assert await until(lambda: not r.relay.live)
            recs = [e for cl in load_calls(calls_dir(r.cfg), 10) for e in cl]
            out = [e["call"] for e in recs if e["ev"] == "start" and e.get("kind") == "app-out"]
            assert out and not [e for e in recs if e["ev"] == "greet" and e["call"] == out[0] and e.get("inbound")]
        finally:
            await c.close()
            await r.close()
    run(main())


def test_on_by_default_short_german_line_and_the_prompt_knows(tmp_path):
    cfg = Config()
    assert cfg.greet_inbound is True and 1 <= len(cfg.greet_text.split()) <= 3 and cfg.greet_tts_models
    assert cfg.greet_delay_s <= 0.5                                     # well inside the ~1.5 s budget
    assert Config.from_env({"VOICE_GREET_INBOUND": "0", "VOICE_GREET_TTS_MODELS": ""}).greet_inbound is False
    assert Config.from_env({"VOICE_GREET_TTS_MODELS": ""}).greet_tts_models == ()
    with open(cfg.prompt_file, encoding="utf-8") as fh:
        p = fh.read()
    assert "already answered him with a short hello in your voice" in p and "Don't greet him again" in p


def test_off_switch_keeps_the_old_silent_pick_up(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), **dict(ON, greet_inbound=False))
        try:
            cached(r)
            b, cl = await bridge(r)
            await cl.ring("c1", OWNER)
            cid = b.calls["c1"].log.id
            await asyncio.sleep(0.4)
            assert not cl.sent["c1"] and not call_events(r.cfg, cid, "greet")
            await cl.end("c1")
            assert await until(lambda: not b.calls)
            ain = call_events(r.cfg, cid, "audio_in")[0]                 # still logged: did his audio arrive
            assert ain["ms"] == 0 and ain["voiced_ms"] == 0
        finally:
            await r.close()
    run(main())


def test_greet_cli_uses_the_relays_voice_and_shows_the_clip(tmp_path, monkeypatch, capsys):
    from voice_relay import server
    env = tmp_path / "relay.env"
    env.write_text("VOICE_GEMINI_VOICE=Orus\nVOICE_STATE_DIR=%s\nVOICE_GREET_TTS_MODELS=\nOTHER=x\n" % (tmp_path / "st"))
    monkeypatch.setenv("VOICE_RELAY_ENV", str(env))
    for k in ("VOICE_GEMINI_VOICE", "VOICE_STATE_DIR", "VOICE_GREET_TTS_MODELS"):
        monkeypatch.delenv(k, raising=False)
    assert server.main(["greet", "--json"]) == 1                      # nothing cached, no TTS model: no network
    st = json.loads(capsys.readouterr().out)
    assert st["clip"] is False and st["voice"] == "Orus" and st["error"] == "no TTS model configured"
    cfg = Config(state_dir=str(tmp_path / "st"), gemini_voice="Orus")
    greet.Greeter(cfg)._save(greet.trim(speech(600)), "tts-x")
    assert server.main(["greet"]) == 0
    out = capsys.readouterr().out
    assert "voice Orus" in out and "ms clip (tts-x" in out
