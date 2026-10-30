#!/usr/bin/env python3
"""Fake recogniser for `trackid set` tests: finds each window's tone by zero crossings and answers from $FAKE_SET,
a comma list of FREQ=Artist|Title@TRACKSTART (the track's start in the mix, s; Shazam-like offset = window start
- TRACKSTART), FREQ=none (no match) or FREQ=err (HTTP 503). The window start comes from the WAV name (set-S.wav).
Every call is logged to $FAKE_LOG (one line per wav)."""

import array
import json
import os
import re
import sys
import wave

plan = {}
for item in os.environ.get("FAKE_SET", "").split(","):
    f, _, ans = item.partition("=")
    plan[float(f)] = ans
for w in sys.argv[1:]:
    with wave.open(w) as fh:
        rate, a = fh.getframerate(), array.array("h", fh.readframes(fh.getnframes()))
    zc = sum(1 for x, y in zip(a, a[1:], strict=False) if (x < 0) != (y < 0))
    freq = zc / 2 / (len(a) / rate)
    near = min(plan, key=lambda f: abs(f - freq))
    ans = plan[near] if abs(near - freq) < 0.05 * near else "none"
    start = float(re.search(r"set-([0-9.]+)\.wav$", w).group(1))
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(f"{os.path.basename(w)} {freq:.0f} {ans}\n")
    if ans == "err":
        print(json.dumps({"file": w, "ok": False, "error": "HTTP 503"}))
        continue
    if ans == "none":
        print(json.dumps({"file": w, "ok": True, "response": {"matches": [], "tagid": "x"}}))
        continue
    name, _, ts = ans.partition("@")
    artist, _, title = name.partition("|")
    key = re.sub(r"[^a-z0-9]+", "", (artist + title).lower())
    resp = {
        "matches": [{"id": "1", "offset": start - float(ts)}],
        "track": {
            "key": key,
            "title": title,
            "subtitle": artist,
            "url": f"https://www.shazam.com/track/{key}",
            "sections": [{"metadata": [{"title": "Label", "text": "Test Label"}]}],
        },
    }
    print(json.dumps({"file": w, "ok": True, "response": resp}))
