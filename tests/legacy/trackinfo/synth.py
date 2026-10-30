"""Synthetic test tracks for trackinfo: kick on every beat, off-beat hats and a chord progression in one key.
argv: OUT.wav BPM TONIC_PC MINOR(0|1) SECONDS. Run with the librosa venv's Python (needs numpy)."""

import sys
import wave

import numpy as np

out, bpm, tonic, minor, secs = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "1", float(sys.argv[5])
sr = 22050
n = int(sr * secs)
t = np.arange(n) / sr
y = np.zeros(n)
beat = 60.0 / bpm
# kick on every beat, hat on the off-beats
for k in range(int(secs / beat) + 1):
    s = int(k * beat * sr)
    m = min(n - s, int(0.15 * sr))
    if m <= 0:
        break
    tt = np.arange(m) / sr
    y[s : s + m] += 0.9 * np.sin(2 * np.pi * (50 + 60 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18)
    h = s + int(beat * sr / 2)
    hm = min(n - h, int(0.03 * sr))
    if hm > 0:
        y[h : h + hm] += 0.15 * np.random.default_rng(k).standard_normal(hm) * np.exp(-np.arange(hm) / sr * 120)
prog = [[0, 3, 7], [5, 8, 12], [7, 11, 14], [0, 3, 7]] if minor else [[0, 4, 7], [5, 9, 12], [7, 11, 14], [0, 4, 7]]
chord_len = 8 * beat
for c in range(int(secs / chord_len) + 1):
    s, e = int(c * chord_len * sr), min(n, int((c + 1) * chord_len * sr))
    if s >= n:
        break
    tt = t[s:e]
    ch = prog[c % len(prog)]
    for iv in ch:
        f = 440 * 2 ** ((48 + tonic + iv - 69) / 12)
        for hn, amp in ((1, 0.12), (2, 0.05), (3, 0.025)):
            y[s:e] += amp * np.sin(2 * np.pi * f * hn * tt)
    fb = 440 * 2 ** ((36 + tonic + ch[0] - 69) / 12)
    y[s:e] += 0.18 * np.sin(2 * np.pi * fb * tt)
y = y / np.max(np.abs(y)) * 0.5
with wave.open(out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes((y * 32767).astype("<i2").tobytes())
