"""Inbound pick-up greeting. When HE calls (Telegram kind "tg-in", phone-app kind "app"), Gemini Live stays silent
until he speaks, so a call that connected sounded like one that hadn't. Now a short line plays as soon as the line
is up: cfg.greet_text (1-3 words, default "Hey, was gibt's?") in the call voice (cfg.gemini_voice).

The line is spoken once by a Gemini TTS model (cfg.greet_tts_models, same prebuilt voice as the Live session) and
cached: state_dir/greet/<key>.pcm (24 kHz PCM16 mono, silence trimmed) + <key>.json (text, voice, model, ms); the
key covers voice + text, so a voice or text change makes a new clip. It plays as LOCAL audio from memory: no wait
on the Live session, and the model never hears or generates it (no extra turn, so it can't start talking more).
No clip (not generated yet, TTS unreachable): a short local earcon (audio.pickup_cue) plays instead and, when
autorefresh is on (the running relay), a background refresh is tried, at most every RETRY_S.
`voice-relay greet [--refresh] [--json]` shows or regenerates the clip."""
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import time
import wave

import numpy as np

from .audio import pickup_cue, resample
from .config import read_secret

RATE = 24000                 # cached clip rate (the Live output rate)
RETRY_S = 600.0              # a failed generation is retried this much later at the earliest
VOICED_RMS = 300.0           # a 10 ms window louder than this is speech (trim + validation)
LEAD_MS, TAIL_MS = 20, 150   # silence kept before the first / after the last voiced window
FADE_IN_MS, FADE_OUT_MS = 5, 25


def clip_key(voice, text):
    return hashlib.sha1(("v1|%s|%s" % (voice, " ".join(str(text).split()))).encode("utf-8")).hexdigest()[:12]


def _words(text):
    return max(1, len(re.findall(r"\w+", str(text))))


def _windows(x, rate):
    win = max(1, rate // 100)
    n = len(x) // win
    if n == 0:
        return np.zeros(0), win
    w = x[: n * win].reshape(n, win)
    return np.sqrt(np.mean(w * w, axis=1)), win


def voiced_span(pcm, rate=RATE):
    """(first, last) sample index of speech (10 ms windows louder than VOICED_RMS), or None when there is none."""
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
    r, win = _windows(x, rate)
    loud = np.nonzero(r > VOICED_RMS)[0]
    if not len(loud):
        return None
    return int(loud[0] * win), int((loud[-1] + 1) * win)


def trim(pcm, rate=RATE):
    """Cut leading/trailing silence (keep LEAD_MS / TAIL_MS) and fade both ends so the clip never clicks."""
    span = voiced_span(pcm, rate)
    if span is None:
        return b""
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
    a = max(0, span[0] - rate * LEAD_MS // 1000)
    b = min(len(x), span[1] + rate * TAIL_MS // 1000)
    y = x[a:b].copy()
    n_in, n_out = min(len(y), rate * FADE_IN_MS // 1000), min(len(y), rate * FADE_OUT_MS // 1000)
    if n_in:
        y[:n_in] *= np.linspace(0.0, 1.0, n_in, endpoint=False)
    if n_out:
        y[-n_out:] *= np.linspace(1.0, 0.0, n_out)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


def check(pcm, text, rate=RATE):
    """'' when the clip sounds like the line; else why not. A model that read out more than the line (a prompt, a
    sentence of its own) runs far longer than ~0.5 s per word."""
    span = voiced_span(pcm, rate)
    if span is None:
        return "no speech"
    secs = (span[1] - span[0]) / float(rate)
    if secs < 0.15:
        return "too short (%.2f s)" % secs
    limit = 0.5 * _words(text) + 1.0
    if secs > limit:
        return "too long (%.1f s for %d words)" % (secs, _words(text))
    return ""


def decode_audio(mime, data):
    """Gemini TTS inline audio → (PCM16 mono bytes, rate): audio/wav (RIFF) or raw audio/L16;rate=N."""
    mime = str(mime or "").lower()
    if data[:4] == b"RIFF" or "wav" in mime:
        w = wave.open(io.BytesIO(data))
        if w.getsampwidth() != 2:
            raise ValueError("unsupported sample width %d" % w.getsampwidth())
        pcm, rate, ch = w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
        if ch > 1:
            pcm = np.frombuffer(pcm, dtype="<i2").reshape(-1, ch).mean(axis=1).astype("<i2").tobytes()
        return pcm, rate
    m = re.search(r"rate=(\d+)", mime)
    return data[: len(data) // 2 * 2], int(m.group(1)) if m else RATE


async def tts(cfg, text, model, http, key, timeout=30.0):
    """One generateContent call on a TTS model: the plain line (no style prompt: TTS models read prompts aloud too),
    the call voice. → (PCM16 mono at RATE, untrimmed). Raises RuntimeError with a short reason (never the key)."""
    import aiohttp
    body = {"contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                                     "voiceName": cfg.gemini_voice}}}}}
    url = "%s/models/%s:generateContent" % (cfg.gemini_rest_url.rstrip("/"), model)
    try:
        async with http.post(url, json=body, headers={"x-goog-api-key": key},
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            raw = await r.read()
            status = r.status
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise RuntimeError("%s: %s" % (model, type(e).__name__)) from None
    if status != 200:
        raise RuntimeError("%s: HTTP %s" % (model, status))
    try:
        d = json.loads(raw.decode("utf-8"))
        parts = d["candidates"][0]["content"]["parts"]
        inl = next(p["inlineData"] for p in parts if p.get("inlineData", {}).get("data"))
        pcm, rate = decode_audio(inl.get("mimeType"), base64.b64decode(inl["data"]))
    except (ValueError, KeyError, IndexError, TypeError, StopIteration, wave.Error, EOFError):
        raise RuntimeError("%s: no audio in the answer" % model) from None
    return resample(pcm, rate, RATE)


class Greeter:
    """The cached pick-up line for one relay. audio(rate) never waits: clip from memory, else the earcon."""

    def __init__(self, cfg, autorefresh=False):
        self.cfg = cfg
        self.autorefresh = autorefresh       # the running relay: a missing clip is generated in the background
        self.key = clip_key(cfg.gemini_voice, cfg.greet_text)
        self.dir = os.path.join(cfg.state_dir, "greet")
        self.pcm_path = os.path.join(self.dir, self.key + ".pcm")
        self.meta_path = os.path.join(self.dir, self.key + ".json")
        self.clip, self.meta, self._mtime = None, {}, None
        self._rates = {}                     # out rate -> resampled clip
        self._lock = asyncio.Lock()
        self._next_try = 0.0
        self._task = None
        self.last_error = ""
        self.load()

    def load(self):
        """(Re)load the clip from disk when the file changed (`voice-relay greet --refresh` while the relay runs)."""
        try:
            mtime = os.stat(self.pcm_path).st_mtime
        except OSError:
            return self.clip is not None
        if mtime == self._mtime and self.clip is not None:
            return True
        try:
            with open(self.pcm_path, "rb") as fh:
                pcm = fh.read()
            try:
                with open(self.meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                meta = {}
        except OSError:
            return self.clip is not None
        if len(pcm) < RATE // 10:            # < 50 ms: a broken file
            return self.clip is not None
        self.clip, self.meta, self._mtime, self._rates = pcm, meta, mtime, {}
        return True

    def audio(self, out_rate):
        """(PCM16 mono at out_rate, via): via "clip" (the cached line) or "tone" (the earcon: no clip yet)."""
        self.load()
        if self.clip is not None:
            if out_rate not in self._rates:
                self._rates[out_rate] = resample(self.clip, RATE, out_rate)
            return self._rates[out_rate], "clip"
        self.refresh_soon()
        return pickup_cue(out_rate), "tone"

    def refresh_soon(self):
        """Running relay, no clip: start a background generation unless one runs or the last failed < RETRY_S ago."""
        if not self.autorefresh or (self._task is not None and not self._task.done()) \
                or time.monotonic() < self._next_try:
            return None
        try:
            self._task = asyncio.ensure_future(self.ensure())
        except RuntimeError:                 # no running loop
            self._task = None
        return self._task

    def status(self):
        self.load()
        return {"clip": self.clip is not None, "text": self.cfg.greet_text, "voice": self.cfg.gemini_voice,
                "model": self.meta.get("model"), "ms": round(len(self.clip) / 2.0 / RATE * 1000) if self.clip else None,
                "created": self.meta.get("created"), "error": self.last_error or None}

    async def ensure(self, force=False, key=None):
        """The clip is in memory (from disk, else generated now). True on success. force = regenerate."""
        async with self._lock:
            if not force and self.load():
                return True
            models = [m for m in tuple(self.cfg.greet_tts_models or ()) if m]
            if not models:
                self.last_error = "no TTS model configured"
                return False
            key = key or read_secret(self.cfg.gemini_key_file)
            if not key:
                self.last_error = "no Gemini API key"
                self._next_try = time.monotonic() + RETRY_S
                return False
            import aiohttp
            errors = []
            async with aiohttp.ClientSession() as http:
                for model in models:
                    for _ in range(2):           # a misread line (too long/short) gets one more try per model
                        try:
                            raw = await tts(self.cfg, self.cfg.greet_text, model, http, key)
                        except RuntimeError as e:
                            errors.append(str(e))
                            break                # HTTP / network error: the next model
                        why = check(raw, self.cfg.greet_text)
                        if why:
                            errors.append("%s: %s" % (model, why))
                            continue
                        self._save(trim(raw), model)
                        self.last_error = ""
                        return True
            self.last_error = "; ".join(errors)[-300:]
            self._next_try = time.monotonic() + RETRY_S
            return False

    def _save(self, pcm, model):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        meta = {"text": self.cfg.greet_text, "voice": self.cfg.gemini_voice, "model": model, "rate": RATE,
                "ms": round(len(pcm) / 2.0 / RATE * 1000), "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        for path, data in ((self.pcm_path, pcm), (self.meta_path, json.dumps(meta).encode("utf-8"))):
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        for name in os.listdir(self.dir):    # clips of an earlier voice / text
            if not name.startswith(self.key + ".") and name.endswith((".pcm", ".json")):
                try:
                    os.unlink(os.path.join(self.dir, name))
                except OSError:
                    pass
        self.clip, self.meta, self._rates = pcm, meta, {}
        try:
            self._mtime = os.stat(self.pcm_path).st_mtime
        except OSError:
            self._mtime = None
