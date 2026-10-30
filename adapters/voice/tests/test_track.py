"""identify_track: rolling call-audio buffer → temp WAV → matcher → spoken answer (fake audio, fake matcher;
the trackid CLI test uses trackid's own fake recogniser, so nothing leaves the box)."""
import asyncio
import glob
import json
import os
import tempfile
import wave

import pytest

from voice_relay import track
from voice_relay.audio import tone
from voice_relay.calllog import CallLog, calls_dir, format_call, load_calls
from voice_relay.config import Config
from voice_relay.session import VoiceSession

from util import RecFE, StubProvider, until

RATE = 48000
HIT = {"status": "match", "track": {"artist": "Avicii", "title": "Levels"}, "confidence": {"level": "high"},
       "id": "tid-12345678", "stored": True}
LOW = {"status": "match", "track": {"artist": "Somebody", "title": "Maybe"}, "confidence": {"level": "low"},
       "id": None, "stored": False}
MISS = {"status": "no_match"}
ERR = {"status": "error", "error": "HTTP 503"}
TRACKID_TESTS = os.path.expanduser("~/opt/trackid-tests/fixtures")


class FakeMatcher:
    """Answers from a plan; checks each WAV while it exists (48 kHz mono PCM16) and remembers its path."""

    def __init__(self, *plan):
        self.plan, self.seen = list(plan), []

    async def __call__(self, wav):
        with wave.open(wav, "rb") as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE)
            secs = w.getnframes() / RATE
        assert oct(os.stat(os.path.dirname(wav)).st_mode & 0o777) == "0o700"
        self.seen.append((wav, secs))
        return self.plan.pop(0) if self.plan else MISS


class AudioStub(StubProvider):
    sent = 0

    async def send_audio(self, pcm):
        self.sent += len(pcm)                        # his audio still goes to the provider as before


def mkcfg(tmp, **over):
    kw = dict(state_dir=os.path.join(str(tmp), "state"), inbox_dir=os.path.join(str(tmp), "inbox"),
              recap_grace_s=0, fast_lane=False, decisions=False)
    kw.update(over)
    return Config(**kw)


async def feed(s, seconds):
    frame = tone(RATE, 10)                          # 10 ms frames, like py-tgcalls delivers them
    for i in range(int(seconds * 100)):
        await s.audio_in(frame)
        if i % 50 == 0:
            await asyncio.sleep(0)


async def session(tmp, matcher, monkeypatch, **over):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    cfg = mkcfg(tmp, **over)
    prov = AudioStub()
    s = VoiceSession(cfg, prov, RecFE(in_rate=RATE, out_rate=RATE), None, log=CallLog(cfg, "tg-in", "owner"),
                     track_matcher=matcher)
    s.handoff = type("H", (), {"detach": lambda self, owner: None})()
    await s.start()
    return cfg, prov, s


def ask(prov, i=0):
    return prov.q.put({"type": "tool_call", "id": "fc-%d" % i, "name": "identify_track", "args": {}})


def leftovers(tmp):
    return glob.glob(os.path.join(str(tmp), "vr-track-*"))


# --- buffer and answers -------------------------------------------------------------------------------------

def test_ring_buffer_keeps_only_the_last_seconds():
    b = track.RingBuffer(1000, 2)
    stream = bytes(range(256)) * 30                 # 7680 bytes = 3.84 s at 1 kHz
    for i in range(0, len(stream), 334):
        b.add(stream[i:i + 334])
    assert b.size == 4000 and b.seconds() == 2.0 and b.total == len(stream)
    assert b.snapshot() == stream[-4000:]
    b.clear()
    assert b.size == 0 and b.snapshot() == b"" and b.total == len(stream)


def test_speakable_answers():
    assert track.speakable(HIT) == (True, "Avicii – Levels")
    assert track.speakable(dict(HIT, confidence={"level": "medium"}))[0] is True
    assert track.speakable(LOW) == (False, "No clear match; it might be Somebody – Maybe.")
    assert track.speakable(MISS) == (False, track.NO_MATCH)
    assert track.speakable(ERR) == (False, track.LOOKUP_FAILED)
    assert track.speakable(None) == (False, track.NO_MATCH)


# --- in a call ----------------------------------------------------------------------------------------------

def test_names_the_track_from_the_buffer_and_deletes_the_wav(tmp_path, monkeypatch):
    async def main():
        m = FakeMatcher(HIT)
        cfg, prov, s = await session(tmp_path, m, monkeypatch)
        await feed(s, 12)
        await ask(prov)
        assert await until(lambda: prov.results)
        assert prov.results[0] == ("fc-0", "identify_track", {"answer": "Avicii – Levels"})
        assert len(m.seen) == 1 and abs(m.seen[0][1] - 12) < 0.05
        assert not os.path.exists(m.seen[0][0]) and leftovers(tmp_path) == []
        assert prov.injects == [] and s.track.buf.size > 0 and prov.sent == 12 * 16000 * 2
        await s.close("hung up")
        assert s.track.buf.size == 0                 # the call's audio is gone with the call
        evs = load_calls(calls_dir(cfg))[0]
        tool = [e for e in evs if e["ev"] == "tool"][0]
        assert tool["lane"] == "track" and isinstance(tool["ms"], int) and "error" not in tool
        assert "identify_track (track id, " in format_call(evs)
    asyncio.run(main())


def test_buffer_holds_the_last_20_seconds(tmp_path, monkeypatch):
    async def main():
        m = FakeMatcher(HIT)
        cfg, prov, s = await session(tmp_path, m, monkeypatch)
        await feed(s, 27)
        await ask(prov)
        assert await until(lambda: prov.results)
        assert abs(m.seen[0][1] - 20) < 0.05
        await s.close("hung up")
    asyncio.run(main())


def test_asked_right_away_listens_then_answers_late(tmp_path, monkeypatch):
    async def main():
        m = FakeMatcher(HIT)
        cfg, prov, s = await session(tmp_path, m, monkeypatch)
        await feed(s, 1)
        await ask(prov)
        assert await until(lambda: prov.results)
        assert prov.results[0][2] == {"status": "listening", "say": track.LISTEN_SAY_FIRST}
        assert m.seen == []                          # too little audio: no lookup yet
        await feed(s, 10.5)
        assert await until(lambda: prov.injects, timeout=5)
        assert prov.injects == ["identify_track: Avicii – Levels"]
        assert len(m.seen) == 1 and m.seen[0][1] >= 11 and leftovers(tmp_path) == []
        await s.close("hung up")
        out = format_call(load_calls(calls_dir(cfg))[0])
        assert "retry: short buffer" in out and 'track id retry (' in out and "Avicii – Levels" in out
    asyncio.run(main())


def test_no_sure_match_retries_once(tmp_path, monkeypatch):
    async def main():
        m = FakeMatcher(LOW, MISS, HIT)
        cfg, prov, s = await session(tmp_path, m, monkeypatch)
        await feed(s, 12)
        await ask(prov)
        assert await until(lambda: prov.results)
        assert prov.results[0][2] == {"status": "listening", "say": track.LISTEN_SAY}
        await feed(s, 10.5)
        assert await until(lambda: prov.injects, timeout=5)
        assert prov.injects == ["identify_track: " + track.NO_MATCH]
        assert len(m.seen) == 2                      # one retry only
        # asked again: a fresh try (the low guess was never stored or spoken as a hit)
        await ask(prov, 1)
        assert await until(lambda: len(prov.results) > 1)
        assert prov.results[1][2] == {"answer": "Avicii – Levels"}
        await s.close("hung up")
        assert leftovers(tmp_path) == []
    asyncio.run(main())


def test_low_guess_on_the_retry_is_said_as_unsure(tmp_path, monkeypatch):
    async def main():
        m = FakeMatcher(MISS, LOW)
        cfg, prov, s = await session(tmp_path, m, monkeypatch)
        await feed(s, 8)
        await ask(prov)
        assert await until(lambda: prov.results)
        await feed(s, 10.5)
        assert await until(lambda: prov.injects, timeout=5)
        assert prov.injects == ["identify_track: No clear match; it might be Somebody – Maybe."]
        await s.close("hung up")
    asyncio.run(main())


def test_lookup_error_answers_at_once_and_busy_while_listening(tmp_path, monkeypatch):
    async def main():
        m = FakeMatcher(ERR)
        cfg, prov, s = await session(tmp_path, m, monkeypatch)
        await feed(s, 8)
        await ask(prov)
        assert await until(lambda: prov.results)
        assert prov.results[0][2] == {"answer": track.LOOKUP_FAILED} and len(m.seen) == 1
        s.track.clear()
        await ask(prov, 1)                           # nothing buffered: listening …
        assert await until(lambda: len(prov.results) > 1)
        await ask(prov, 2)                           # … and a second ask meanwhile is told so
        assert await until(lambda: len(prov.results) > 2)
        assert prov.results[2][2] == {"status": "working", "say": track.BUSY_SAY}
        await s.close("hung up")
    asyncio.run(main())


def test_hang_up_while_listening_drops_everything(tmp_path, monkeypatch):
    async def main():
        m = FakeMatcher(MISS, HIT)
        cfg, prov, s = await session(tmp_path, m, monkeypatch)
        await feed(s, 8)
        await ask(prov)
        assert await until(lambda: prov.results)
        await s.close("hung up")
        await asyncio.sleep(0.2)
        assert prov.injects == [] and len(m.seen) == 1 and s.track.buf.size == 0 and leftovers(tmp_path) == []
        await s.audio_in(tone(RATE, 1000))           # late frames after the end are not kept
        assert s.track.buf.size == 0
    asyncio.run(main())


def test_off_switch_keeps_no_buffer(tmp_path, monkeypatch):
    async def main():
        cfg, prov, s = await session(tmp_path, FakeMatcher(), monkeypatch, track_id=False)
        assert s.track is None
        await feed(s, 1)
        await s.close("hung up")
    asyncio.run(main())


def test_prompt_mentions_the_tool():
    text = open(Config().prompt_file).read()
    assert "identify_track" in text and "welcher Song" in text and "never ask_assistant" in text


# --- the real matcher command ---------------------------------------------------------------------------------

def _wav(path, secs):
    track.write_wav(path, tone(RATE, int(secs * 1000), freq=330.0), RATE)


@pytest.mark.skipif(not os.path.isdir(TRACKID_TESTS), reason="trackid test fixtures missing")
def test_trackid_cli_stores_call_hits_only_when_sure(tmp_path, monkeypatch):
    bin_ = os.environ.get("TRACKID_BIN") or os.path.expanduser("~/bin/trackid")
    state = os.path.join(str(tmp_path), "tid")
    monkeypatch.setenv("TRACKID_STATE_DIR", state)
    monkeypatch.setenv("TRACKID_DEEZER_API", "off")
    monkeypatch.setenv("RAVE_RADAR_CONFIG_DIR", TRACKID_TESTS)
    monkeypatch.setenv("TRACKID_RECOGNIZER", "python3 %s/fake_recognizer.py" % TRACKID_TESTS)
    monkeypatch.setenv("FAKE_LOG", os.path.join(str(tmp_path), "fake.log"))
    wav = os.path.join(str(tmp_path), "clip.wav")
    _wav(wav, 20)
    mt = track.TrackMatcher(mkcfg(tmp_path, track_cmd=bin_))

    monkeypatch.setenv("FAKE_PLAN", "msm,msm,msm")
    out = asyncio.run(mt(wav))
    assert out["status"] == "match" and out["stored"] and track.speakable(out)[0]
    with open(os.path.join(state, "ids.json")) as fh:
        rec = json.load(fh)["ids"][out["id"]]
    assert rec["source"] == "call" and rec["crate"] == out["crate"]

    os.unlink(os.environ["FAKE_LOG"])
    os.unlink(os.path.join(state, "ids.json"))
    monkeypatch.setenv("FAKE_PLAN", "alt,msm,none,none")
    out = asyncio.run(mt(wav))
    assert out["confidence"]["level"] == "low" and not out["stored"] and out["id"] is None
    assert not os.path.exists(os.path.join(state, "ids.json"))


def test_matcher_timeout_and_garbage(tmp_path):
    slow = os.path.join(str(tmp_path), "slow")
    with open(slow, "w") as fh:
        fh.write("#!/bin/sh\nsleep 5\n")
    os.chmod(slow, 0o700)
    junk = os.path.join(str(tmp_path), "junk")
    with open(junk, "w") as fh:
        fh.write("#!/bin/sh\necho not json\nexit 3\n")
    os.chmod(junk, 0o700)
    wav = os.path.join(str(tmp_path), "c.wav")
    _wav(wav, 1)
    out = asyncio.run(track.TrackMatcher(mkcfg(tmp_path, track_cmd=slow, track_timeout_s=0.3))(wav))
    assert out == {"status": "error", "error": "timeout"}
    out = asyncio.run(track.TrackMatcher(mkcfg(tmp_path, track_cmd=junk))(wav))
    assert out["status"] == "error" and track.speakable(out) == (False, track.LOOKUP_FAILED)
