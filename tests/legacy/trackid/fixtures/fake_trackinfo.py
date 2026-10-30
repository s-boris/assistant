#!/usr/bin/env python3
"""Fake trackinfo for `trackid set --info`: BPM 120 + n, Camelot nA per "Artist - Title" arg; like the real one it
overwrites $TRACKINFO_STATE/last-list.json (trackid must restore it)."""

import json
import os
import sys

names = [a for a in sys.argv[1:] if not a.startswith("--")]
os.makedirs(os.environ["TRACKINFO_STATE"], exist_ok=True)
with open(os.path.join(os.environ["TRACKINFO_STATE"], "last-list.json"), "w") as f:
    json.dump(names, f)
out = []
for i, n in enumerate(names):
    artist, _, title = n.partition(" - ")
    out.append({"artist": artist, "title": title, "bpm": 120 + i, "camelot": f"{i + 1}A", "key": "Am", "energy": 6})
print(json.dumps(out))
