"""PCM16 mono helpers: resampling between front ends (16/48 kHz) and providers (16/24 kHz), framing, test tones."""
import math

import numpy as np


def resample(pcm, src, dst):
    if src == dst or not pcm:
        return pcm
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
    if src % dst == 0:                       # integer decimation: average groups (crude low-pass)
        k = src // dst
        n = len(x) // k
        y = x[: n * k].reshape(n, k).mean(axis=1)
    else:
        n_out = int(round(len(x) * dst / src))
        if n_out <= 0:
            return b""
        y = np.interp(np.arange(n_out) * (src / dst), np.arange(len(x)), x)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


def frames(pcm, rate, ms=10):
    step = rate * ms // 1000 * 2
    for i in range(0, len(pcm) - len(pcm) % step, step):
        yield pcm[i:i + step]


def tone(rate, ms, freq=440.0, amp=0.25):
    n = rate * ms // 1000
    t = np.arange(n) / rate
    return (np.sin(2 * math.pi * freq * t) * amp * 32767).astype("<i2").tobytes()


def cue(rate, amp=0.06):
    """The 'still working' cue: two soft rising notes (~180 ms), faded so they never click."""
    out = []
    for freq, ms in ((523.25, 70), (0, 35), (659.25, 75)):
        n = rate * ms // 1000
        if not freq:
            out.append(np.zeros(n))
            continue
        t = np.arange(n) / rate
        env = np.minimum(1.0, np.minimum(t, t[-1] - t) / 0.012)
        out.append(np.sin(2 * math.pi * freq * t) * env)
    return (np.concatenate(out) * amp * 32767).astype("<i2").tobytes()


def pickup_cue(rate, amp=0.12):
    """The inbound pick-up earcon (no greeting clip cached yet): two bright rising notes (~260 ms), unlike the
    softer 'still working' cue, so he hears that the line is up."""
    out = []
    for freq, ms in ((659.25, 90), (0, 25), (987.77, 140)):
        n = rate * ms // 1000
        if not freq:
            out.append(np.zeros(n))
            continue
        t = np.arange(n) / rate
        env = np.minimum(1.0, np.minimum(t, t[-1] - t) / 0.012)
        out.append(np.sin(2 * math.pi * freq * t) * env)
    return (np.concatenate(out) * amp * 32767).astype("<i2").tobytes()


def rms(pcm):
    if len(pcm) < 2:
        return 0.0
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(x * x)))
