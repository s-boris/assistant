#!/usr/bin/env python3
"""Fake trackid recogniser: answers per window in call order from $FAKE_PLAN (comma list of fixture names:
msm, alt, remix, none, err). Call order is kept in $FAKE_LOG (one line per wav) so re-checks consume the next entry."""

import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
plan = os.environ.get("FAKE_PLAN", "none").split(",")
log = os.environ["FAKE_LOG"]
n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
for w in sys.argv[1:]:
    name = plan[n] if n < len(plan) else "none"
    with open(log, "a") as f:
        f.write(f"{os.path.basename(w)} {name}\n")
    n += 1
    if name == "err":
        print(json.dumps({"file": w, "ok": False, "error": "HTTP 503"}))
        continue
    with open(os.path.join(here, f"resp-{name}.json")) as f:
        print(json.dumps({"file": w, "ok": True, "response": json.load(f)}))
