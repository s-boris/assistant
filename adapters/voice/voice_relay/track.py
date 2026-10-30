"""identify_track: "what track is this?" inside a call, from his incoming call audio.

The session keeps a rolling buffer of his last ~20 s of incoming audio (PCM16 mono at the front end's rate, 48 kHz
for Telegram calls), in memory only; it is cleared when the call ends. On the tool call the buffer is written to a
temp WAV (in a private 0700 temp dir), trackid's matcher runs on it (`trackid WAV --json --source call
--min-store medium`: Shazam gets only the audio fingerprint, like any trackid lookup), and the WAV is deleted right
after. A sure match lands in trackid's store (ids.json, source "call"), so the night recap and the ➕ Crate button
work as for a clip he sends. No clear match (or too little audio yet): the tool answers "listening", the buffer
takes ~10 s more, and one retry runs; its result comes back as a late "[assistant result]".
"""
import asyncio
import json
import os
import shutil
import tempfile
import wave
from collections import deque

TOOL = {"name": "identify_track",
        "description": "Name the music playing around him right now (\"what track is this?\", \"welcher Song ist "
                       "das?\", \"Shazam that\"): listens to the last seconds of the call audio and returns "
                       "artist and title, or no match. Local, a few seconds; may ask him to hold the phone "
                       "towards the music and answer about ten seconds later.",
        "parameters": {"type": "object", "properties": {}}}
TOOLS = [TOOL]
NAMES = frozenset(t["name"] for t in TOOLS)

LISTEN_SAY = "Listening a few seconds more: hold the phone towards the music."
LISTEN_SAY_FIRST = "Listening: hold the phone towards the music for about ten seconds."
BUSY_SAY = "Still listening for the track."
NO_MATCH = "No match: I couldn't name this track."
NO_AUDIO = "I couldn't hear enough music to name the track."
LOOKUP_FAILED = "The track lookup failed right now; try again in a minute or send a clip on Telegram."


class RingBuffer:
    """The last `seconds` of PCM16 mono at `rate`, in memory only. `total` counts every byte ever added."""

    def __init__(self, rate, seconds):
        self.rate = int(rate)
        self.max_bytes = int(self.rate * float(seconds)) * 2
        self.chunks = deque()
        self.size = 0
        self.total = 0

    def add(self, pcm):
        if not pcm or self.max_bytes <= 0:
            return
        pcm = bytes(pcm[: len(pcm) // 2 * 2])
        self.chunks.append(pcm)
        self.size += len(pcm)
        self.total += len(pcm)
        while self.size > self.max_bytes and self.chunks:
            extra = self.size - self.max_bytes
            head = self.chunks[0]
            if len(head) <= extra:
                self.chunks.popleft()
                self.size -= len(head)
            else:
                cut = extra + (extra & 1)
                self.chunks[0] = head[cut:]
                self.size -= cut

    def seconds(self):
        return self.size / 2.0 / self.rate

    def snapshot(self):
        return b"".join(self.chunks)

    def clear(self):
        self.chunks.clear()
        self.size = 0


def write_wav(path, pcm, rate):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm)


def speakable(out):
    """trackid --json output → (hit, text). Only a sure match (high/medium) counts as a hit."""
    st = (out or {}).get("status")
    if st == "error":
        return False, LOOKUP_FAILED
    if st != "match":
        return False, NO_MATCH
    t = out.get("track") or {}
    name = "%s – %s" % (str(t.get("artist") or "").strip(), str(t.get("title") or "").strip())
    if ((out.get("confidence") or {}).get("level")) in ("high", "medium"):
        return True, name
    return False, "No clear match; it might be %s." % name


class TrackMatcher:
    """Runs trackid on a WAV. Returns its --json dict ({"status": match | no_match | error, …})."""

    def __init__(self, cfg):
        self.cfg = cfg

    async def __call__(self, wav):
        argv = [self.cfg.track_cmd, wav, "--json", "--windows", "3", "--source", "call", "--min-store", "medium"]
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), float(self.cfg.track_timeout_s))
        except asyncio.TimeoutError:
            return {"status": "error", "error": "timeout"}
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode == 2:
            return {"status": "no_match", "error": "bad input"}
        try:
            d = json.loads(out.decode("utf-8", "replace"))
        except ValueError:
            return {"status": "error", "error": "trackid exit %s" % proc.returncode}
        return d if isinstance(d, dict) else {"status": "error", "error": "bad output"}


class TrackTool:
    """Per call: the ring buffer plus one identify at a time."""

    def __init__(self, cfg, rate, matcher=None, tmp_root=None):
        self.cfg = cfg
        self.buf = RingBuffer(rate, cfg.track_buffer_s)
        self.matcher = matcher or TrackMatcher(cfg)
        self.tmp_root = tmp_root
        self.busy = False

    def add(self, pcm):
        self.buf.add(pcm)

    def clear(self):
        self.buf.clear()

    async def attempt(self):
        """One lookup on the buffer now → (hit, text, out). The temp WAV never outlives the lookup."""
        pcm = self.buf.snapshot()
        if len(pcm) < self.cfg.track_min_s * self.buf.rate * 2:
            return False, NO_AUDIO, {"status": "short"}
        d = tempfile.mkdtemp(prefix="vr-track-", dir=self.tmp_root)
        try:
            wav = os.path.join(d, "call.wav")
            write_wav(wav, pcm, self.buf.rate)
            del pcm
            out = await self.matcher(wav)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        hit, text = speakable(out)
        return hit, text, out

    async def more(self, sleep=asyncio.sleep, step=0.1):
        """Wait until ~track_more_s seconds of new audio came in (bounded by wall time)."""
        want = self.buf.total + int(self.cfg.track_more_s * self.buf.rate) * 2
        waited, limit = 0.0, float(self.cfg.track_more_s) + 4.0
        while self.buf.total < want and waited < limit:
            await sleep(step)
            waited += step
