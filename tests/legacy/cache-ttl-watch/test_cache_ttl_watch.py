#!/usr/bin/env python3
"""Offline tests for ~/bin/cache-ttl-watch: TTL split, dedupe of multi-block responses (also across runs),
subagents and sidechains ignored, incremental offsets + partial lines, threshold/min-tokens, one note per
20 h, window cut, old files skipped, truncation reset, show."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CTW = os.environ.get("CTW") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "cache-ttl-watch"
)
P = F = 0
NOW = 1_792_000_000.0  # fixed clock


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1
        print(f"ok   - {name}")
    else:
        F += 1
        print(f"FAIL - {name}\n       got: {str(got)[:500]}")


def rec(mid, t, m5, h1, side=False, block="text", typed=True):
    u = {"input_tokens": 2, "cache_creation_input_tokens": m5 + h1, "cache_read_input_tokens": 1000, "output_tokens": 5}
    if typed:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": m5, "ephemeral_1h_input_tokens": h1}
    return (
        json.dumps(
            {
                "type": "assistant",
                "isSidechain": side,
                "timestamp": iso(t),
                "version": "2.1.300",
                "requestId": "req_" + mid,
                "uuid": mid + block,
                "message": {
                    "id": "msg_" + mid,
                    "model": "claude-opus-5-5",
                    "content": [{"type": block, "text": "SECRETCONTENT"}],
                    "usage": u,
                },
            }
        )
        + "\n"
    )


class Env:
    def __init__(self):
        self.d = Path(tempfile.mkdtemp(prefix="ctw-"))
        self.proj, self.state, self.inbox = self.d / "proj", self.d / "state", self.d / "inbox"
        for p in (self.proj, self.state, self.inbox):
            p.mkdir()
        (self.proj / "sess" / "subagents").mkdir(parents=True)

    def write(self, name, text, mode="a", mtime=None):
        p = self.proj / name
        with open(p, mode) as f:
            f.write(text)
        os.utime(p, (mtime or NOW, mtime or NOW))
        return p

    def run(self, *extra, now=NOW):
        r = subprocess.run(
            [
                sys.executable,
                CTW,
                *extra,
                "--json",
                "--projects-dir",
                str(self.proj),
                "--state-dir",
                str(self.state),
                "--inbox-dir",
                str(self.inbox),
                "--now",
                str(now),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode:
            return {"rc": r.returncode, "err": r.stderr}
        return json.loads(r.stdout)

    def notes(self):
        return sorted(self.inbox.glob("cache-ttl-watch-*.md"))


# 1: all-1h, multi-block dedupe, sidechain + subagent ignored
e = Env()
e.write(
    "a.jsonl",
    rec("1", NOW - 600, 0, 5000, block="thinking")
    + rec("1", NOW - 598, 0, 5000, block="tool_use")
    + rec("2", NOW - 300, 0, 700)
    + rec("3", NOW - 200, 9000, 0, side=True)
    + '{"type":"user","message":{"content":"x"}}\n'
    + "not json\n",
)
e.write("sess/subagents/agent-x.jsonl", rec("9", NOW - 100, 50000, 0))
r = e.run()
ok("1h only counted once per response", r.get("cache_write_1h") == 5700 and r.get("cache_write_5m") == 0, r)
ok("responses deduped, sidechain/subagent ignored", r.get("responses") == 2, r)
ok("no note when all 1h", not e.notes() and r.get("over_threshold") is False, r)

# 2: incremental: second run with no change scans 0 lines, same totals
r2 = e.run()
ok("incremental: nothing rescanned", r2.get("lines_scanned") == 0 and r2.get("cache_write_1h") == 5700, r2)

# 3: response split across runs (same message id) is not double counted; partial line waits
e.write("a.jsonl", rec("2", NOW - 290, 0, 700, block="tool_use") + rec("4", NOW - 100, 400, 0)[:-20])
r3 = e.run()
ok(
    "same id across runs not recounted, partial line skipped",
    r3.get("cache_write_1h") == 5700 and r3.get("cache_write_5m") == 0,
    r3,
)
e.write("a.jsonl", rec("4", NOW - 100, 400, 0)[-20:])
r4 = e.run()
ok("partial line completed next run", r4.get("cache_write_5m") == 400, r4)
ok(
    "below min-tokens share 6.5% but total >= 2000 -> over",
    r4.get("over_threshold") is True and len(e.notes()) == 1,
    r4,
)
body = e.notes()[0].read_text() if e.notes() else ""
ok("note has numbers, no content", "400" in body and "5,700" in body and "SECRETCONTENT" not in body, body)

# 4: rate limit: another run 1 h later adds 5m writes, no second note; 21 h later yes
e.write("a.jsonl", rec("5", NOW + 3000, 3000, 0))
r5 = e.run(now=NOW + 3600)
ok("one note per 20 h", r5.get("over_threshold") and len(e.notes()) == 1, r5)
r6 = e.run(now=NOW + 21 * 3600)
ok("note again after 20 h while still over", len(e.notes()) == 2, r6)

# 5: window: 26 h later the old 5m writes fall out
e.write("a.jsonl", rec("6", NOW + 26 * 3600, 0, 80000), mtime=NOW + 26 * 3600)
r7 = e.run(now=NOW + 26 * 3600 + 60)
ok("window drops hours older than 24 h", r7.get("cache_write_5m") == 0 and r7.get("cache_write_1h") == 80000, r7)
r8 = e.run(now=NOW + 20 * 3600)
ok("earlier window (show-style re-eval) still has 5m", r8.get("cache_write_5m") == 3400, r8)

# 6: threshold edge + min tokens + --no-inbox + show + untyped
e = Env()
e.write("b.jsonl", rec("1", NOW - 60, 40, 960))  # 4% of 1000
r = e.run()
ok("4% under threshold", r.get("over_threshold") is False and abs(r["share_5m_pct"] - 4.0) < 0.01, r)
e = Env()
e.write("b.jsonl", rec("1", NOW - 60, 500, 500))  # 50% but only 1000 tokens
r = e.run()
ok("below min-tokens -> no alert", r.get("over_threshold") is False and not e.notes(), r)
e.write("b.jsonl", rec("2", NOW - 50, 5000, 0) + rec("3", NOW - 40, 0, 0, typed=False))
r = e.run("--no-inbox")
ok("--no-inbox: over but no note", r.get("over_threshold") is True and not e.notes(), r)
s = e.run("show")
ok("show reads state, scans nothing", s.get("cache_write_5m") == 5500 and s.get("lines_scanned") == 0, s)

# 7: old file (mtime > 24 h) is skipped; truncated file is rescanned from 0
e = Env()
e.write("old.jsonl", rec("1", NOW - 3600, 9000, 0), mtime=NOW - 30 * 3600)
r = e.run()
ok("files older than window skipped", r.get("cache_write_5m") == 0 and r.get("lines_scanned") == 0, r)
e.write("c.jsonl", rec("1", NOW - 60, 0, 3000) + rec("2", NOW - 50, 0, 3000))
e.run()
e.write("c.jsonl", rec("7", NOW - 10, 0, 100), mode="w")
r = e.run()
ok("truncated file rescanned from start", r.get("cache_write_1h") == 6100, r)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
