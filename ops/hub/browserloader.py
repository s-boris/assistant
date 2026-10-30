#!/usr/bin/env python3
"""browserloader: claude-hub-browser's loader (v2 Phase 3 P3-6b). Stdlib only, one file; the owner's browser paste
installs it read-only (HUB_ROOT/loader/browserloader.py). No browser code is baked into the image: this loader runs
the brain-built release named by RELEASES/CURRENT, only when that release carries a complete, untouched browser tree
(release.json entry "browser", every browser/* file present with its sha256, no symlinks), else it keeps the last
good one running (or idles with the reason). One child: python3 -I -B <release>/browser/run.py.

  browserloader.py --releases DIR --state DIR --config FILE [--poll S]
State: STATE/loader-status.json {state: running|idle|stopped, release, why, ts}, STATE/last-good, STATE/run/ (the
runner's own state and status.json). SIGTERM: the child gets SIGTERM and up to 25 s (Chrome closes cleanly).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time

FORMAT = 1
ENTRY = "browser/run.py"
RID = re.compile(r"^[0-9a-f]{7,40}$")
ATTEMPT = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
STOP_WAIT = 25.0
BACKOFF = (2, 5, 15, 30, 60)


def log(event: str, **kw: object) -> None:
    print(json.dumps({"ts": round(time.time(), 3), "event": event, **kw}), flush=True)


def parse_current(text: str) -> tuple[str, str] | None:
    parts = text.split()
    if not parts or len(parts) > 2 or not RID.match(parts[0]) or (len(parts) == 2 and not ATTEMPT.match(parts[1])):
        return None
    return parts[0], " ".join(parts)


def _sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify_release(releases: str, rid: str) -> str | None:
    """None when RELEASES/<rid> carries a complete, untouched browser tree; else why not."""
    if not RID.match(rid):
        return "bad id"
    d = os.path.join(os.path.realpath(releases), rid)
    try:
        with open(os.path.join(d, "release.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return "no readable release.json"
    if not isinstance(meta, dict) or meta.get("format") != FORMAT or meta.get("rid") != rid:
        return "release.json: wrong format or id"
    raw = meta.get("files")
    files: dict = raw if isinstance(raw, dict) else {}
    if (meta.get("entry") or {}).get("browser") != ENTRY or ENTRY not in files:
        return "release.json: no browser entry (a release without the browser tree)"
    n = 0
    for rel, info in files.items():
        if not rel.startswith("browser/"):
            continue
        segs = rel.split("/")
        if not all(SEGMENT.match(s) and s not in (".", "..") for s in segs):
            return f"release.json: bad path {rel[:80]}"
        q = d
        for s in segs:
            q = os.path.join(q, s)
            if os.path.islink(q):
                return f"{rel}: a symlink"
        if not os.path.isfile(q):
            return f"{rel}: missing"
        if _sha(q) != (info or {}).get("sha256"):
            return f"{rel}: sha256 mismatch"
        n += 1
    return None if n else "no browser files"


def _atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class Loader:
    def __init__(self, releases: str, state: str, config: str) -> None:
        self.releases, self.state, self.config = releases, state, config
        self.child: subprocess.Popen[bytes] | None = None
        self.rid = ""
        self.fails = 0
        self.next_try = 0.0
        self.stopping = False
        os.makedirs(os.path.join(state, "run"), mode=0o700, exist_ok=True)

    def status(self, state: str, why: str = "") -> None:
        _atomic(
            os.path.join(self.state, "loader-status.json"),
            json.dumps({"state": state, "release": self.rid or None, "why": why, "ts": round(time.time(), 3)}),
        )

    def wanted(self) -> tuple[str | None, str]:
        """(release id to run, why) from CURRENT, else the last good one."""
        try:
            with open(os.path.join(self.releases, "CURRENT"), encoding="utf-8") as f:
                cur = parse_current(f.read())
        except OSError:
            cur = None
        why = "no CURRENT"
        if cur is not None:
            bad = verify_release(self.releases, cur[0])
            if bad is None:
                return cur[0], ""
            why = f"{cur[0][:12]}: {bad}"
        try:
            with open(os.path.join(self.state, "last-good"), encoding="utf-8") as f:
                lg = f.read().strip()
        except OSError:
            lg = ""
        if lg and verify_release(self.releases, lg) is None:
            return lg, why + " (running the last good release)"
        return None, why

    def start(self, rid: str) -> None:
        entry = os.path.join(os.path.realpath(self.releases), rid, *ENTRY.split("/"))
        self.child = subprocess.Popen([sys.executable, "-I", "-B", entry, "--config", self.config, "--state",
                                       os.path.join(self.state, "run")])  # fmt: skip
        self.rid = rid
        log("child start", release=rid, pid=self.child.pid)

    def stop(self) -> None:
        c, self.child = self.child, None
        if c is None or c.poll() is not None:
            return
        c.send_signal(signal.SIGTERM)
        try:
            c.wait(STOP_WAIT)
        except subprocess.TimeoutExpired:
            c.kill()
            c.wait(5)
        log("child stop", release=self.rid)

    def step(self) -> None:
        rid, why = self.wanted()
        if rid is None:
            self.stop()
            self.status("idle", why)
            return
        if self.child is not None and self.child.poll() is not None:
            log("child exit", release=self.rid, code=self.child.returncode)
            self.child = None
            self.fails += 1
            self.next_try = time.time() + BACKOFF[min(self.fails, len(BACKOFF)) - 1]
        if self.child is not None and rid != self.rid:
            self.stop()  # a new release: restart on it
        if self.child is None and time.time() >= self.next_try:
            self.start(rid)
        if self.child is not None and self.child.poll() is None and time.time() - self._since() > 60:
            self.fails = 0
            _atomic(os.path.join(self.state, "last-good"), rid + "\n")
        self.status("running" if self.child is not None else "idle", why)

    def _since(self) -> float:
        try:
            return os.stat(f"/proc/{self.child.pid}").st_mtime if self.child else time.time()
        except OSError:
            return time.time()

    def run(self, poll: float) -> int:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        while not self.stopping:
            self.step()
            time.sleep(poll)
        self.stop()
        self.status("stopped")
        return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="browserloader")
    ap.add_argument("--releases", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--poll", type=float, default=5.0)
    a = ap.parse_args(argv)
    return Loader(a.releases, a.state, a.config).run(a.poll)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
