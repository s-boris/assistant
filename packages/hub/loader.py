#!/usr/bin/env python3
"""hubloader: runs the brain-built hub release that RELEASES/CURRENT names. Stdlib only, one file.

The owner's install paste copies this file into the hub's appdata (root-owned) and the hub and ci containers mount it
read-only: the brain changes releases, never the loader. A new loader comes only with a paste (HUB_LOADER in .env
changes, so compose recreates hub and ci).

  hubloader.py --role hub --releases /srv/releases --state /var/lib/hub --port 8700
  hubloader.py --role ci  --releases /srv/releases --state /var/lib/ci --heartbeat /tmp/heartbeat

CURRENT holds one line, "<release id> <attempt>". Every --poll seconds the loader reads it and, when it names
another release than the one running, verifies RELEASES/<id>/release.json (format 1, its own id, the role's entry
among its files, every listed file a regular file inside the release with the listed sha256, no symlinks) and runs
`python3 -I -B -u <entry>` with HUB_RELEASE, HUB_ROLE, HUB_PORT, HUB_HEARTBEAT, HUB_LOADER_SHA and HUB_LOADER_STATE
added to its environment, stdin closed.

Healthy = the hub child answers GET 127.0.0.1:PORT/health with its release id (ci: the heartbeat file is touched
after the start) within --start-timeout seconds; that release becomes last_good. A release that fails to verify, to
start or to become healthy is marked bad for that CURRENT line (a new deploy writes a new attempt) and the last
good release runs again. A running child that exits, or misses 4 checks in a row, is restarted; 3 restarts within
10 min mark it bad too, except the last good release itself, which is retried every 60 s instead.
While no hub child serves, the loader answers /health itself with 503 {"ok": false, "release": null, "loader": ...}.

State: --state/loader.json (last_good, bad attempts with reasons, running id). Log: one JSON line per event on
stdout: ids, reasons and counts only. SIGTERM/SIGINT: the child gets SIGTERM, then SIGKILL after 7 s.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

FORMAT = 1
RID = re.compile(r"^[0-9a-f]{7,40}$")
ATTEMPT = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
REL_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
ROLES = ("hub", "ci")
FAILS_BEFORE_RESTART = 4
RESTART_WINDOW = 600.0
RESTARTS_BAD = 3
LAST_GOOD_RETRY = 60.0
BAD_KEEP = 20
STOP_WAIT = 7.0


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_current(text: str) -> tuple[str, str] | None:
    """'<rid> <attempt>' (attempt optional) -> (rid, line); None when it isn't one."""
    parts = text.strip().split()
    if not parts or len(parts) > 2 or not RID.match(parts[0]):
        return None
    if len(parts) == 2 and not ATTEMPT.match(parts[1]):
        return None
    return parts[0], " ".join(parts)


def verify_release(releases: str, rid: str, role: str) -> tuple[str | None, str]:
    """-> (absolute entry path, "") when RELEASES/<rid> is a complete, untouched release for role; else (None, why)."""
    if not RID.match(rid):
        return None, "bad id"
    root = os.path.realpath(releases)
    d = os.path.join(root, rid)
    try:
        st = os.lstat(d)
    except OSError:
        return None, "missing"
    if not stat.S_ISDIR(st.st_mode):
        return None, "not a directory"
    try:
        with open(os.path.join(d, "release.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None, "no readable release.json"
    if not isinstance(meta, dict) or meta.get("format") != FORMAT or meta.get("rid") != rid:
        return None, "release.json: wrong format or id"
    files = meta.get("files")
    entry = (meta.get("entry") or {}).get(role) if isinstance(meta.get("entry"), dict) else None
    if not isinstance(files, dict) or not files or not isinstance(entry, str) or entry not in files:
        return None, f"release.json: no {role} entry"
    for rel, info in files.items():
        if not isinstance(rel, str) or not REL_PATH.match(rel) or ".." in rel.split("/"):
            return None, "release.json: bad path"
        p = os.path.join(d, rel)
        try:
            fst = os.lstat(p)
        except OSError:
            return None, f"{rel}: missing"
        if not stat.S_ISREG(fst.st_mode) or os.path.realpath(p) != os.path.normpath(p):
            return None, f"{rel}: not a plain file"
        want = info.get("sha256") if isinstance(info, dict) else None
        if not isinstance(want, str) or sha256_file(p) != want:
            return None, f"{rel}: sha256 mismatch"
    return os.path.join(d, entry), ""


class Fallback:
    """The loader's own /health while no hub child serves the port."""

    def __init__(self, port: int, status) -> None:
        self.port = port
        self.status = status
        self.server: http.server.ThreadingHTTPServer | None = None

    def start(self) -> bool:
        if self.server is not None:
            return True
        status = self.status

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                if self.path.split("?")[0] != "/health":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(status(), sort_keys=True).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a) -> None:  # quiet: the loader logs events itself
                pass

        try:
            srv = http.server.ThreadingHTTPServer(("0.0.0.0", self.port), H)
        except OSError:
            return False
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        self.server = srv
        return True

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


class Loader:
    def __init__(self, a: argparse.Namespace) -> None:
        self.role: str = a.role
        self.releases: str = a.releases
        self.state_dir: str = a.state
        self.port: int = a.port
        self.heartbeat: str = a.heartbeat
        self.poll: float = a.poll
        self.start_timeout: float = a.start_timeout
        self.check_every: float = a.check_every
        self.state_file = os.path.join(self.state_dir, "loader.json")
        self.sha = sha256_file(os.path.realpath(__file__))
        self.proc: subprocess.Popen | None = None
        self.running: str | None = None  # release id of the child
        self.running_line: str | None = None
        self.started = 0.0
        self.phase = "starting"
        self.fails = 0
        self.restarts: list[float] = []
        self.next_check = 0.0
        self.backoff: dict[str, float] = {}
        self.stopping = False
        self.st = self._load_state()
        self.fallback = Fallback(self.port, self.status) if self.role == "hub" else None

    # ------------------------------------------------------------ state + log
    def _load_state(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("bad", {})
                return d
        except (OSError, ValueError):
            pass
        return {"last_good": None, "bad": {}, "running": None}

    def _save(self) -> None:
        self.st["running"] = self.running
        bad = self.st.get("bad") or {}
        if len(bad) > BAD_KEEP:
            for k in sorted(bad, key=lambda k: bad[k].get("ts", 0))[: len(bad) - BAD_KEEP]:
                del bad[k]
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = f"{self.state_file}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, sort_keys=True)
        os.replace(tmp, self.state_file)

    def log(self, event: str, **kw) -> None:
        rec = {"ts": round(time.time(), 3), "event": event, "role": self.role, **kw}
        print("hubloader " + json.dumps(rec, sort_keys=True), flush=True)

    def status(self) -> dict:
        return {
            "ok": False,
            "release": None,
            "role": self.role,
            "loader": {
                "sha": self.sha[:12],
                "kit": os.environ.get("HUB_KIT", ""),
                "phase": self.phase,
                "last_good": self.st.get("last_good"),
                "bad": sorted(self.st.get("bad", {})),
            },
        }

    def mark_bad(self, line: str, why: str) -> None:
        self.st.setdefault("bad", {})[line] = {"why": why[:120], "ts": round(time.time(), 3)}
        self._save()
        self.log("bad", attempt=line, why=why[:120])

    # ------------------------------------------------------------ child
    def read_current(self) -> tuple[str, str] | None:
        try:
            with open(os.path.join(self.releases, "CURRENT"), encoding="utf-8") as f:
                return parse_current(f.read(256))
        except OSError:
            return None

    def healthy(self, rid: str) -> bool:
        if self.role == "hub":
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    d = json.load(r)
                return isinstance(d, dict) and d.get("release") == rid
            except (OSError, ValueError, urllib.error.URLError):
                return False
        try:
            return os.stat(self.heartbeat).st_mtime >= self.started - 1
        except OSError:
            return False

    def fresh(self) -> bool:
        """ci: the heartbeat is younger than 30 s."""
        try:
            return time.time() - os.stat(self.heartbeat).st_mtime < 30
        except OSError:
            return False

    def spawn(self, rid: str, line: str, entry: str, rdir: str) -> bool:
        if self.fallback:
            self.fallback.stop()
        env = dict(os.environ)
        env.update(
            {
                "HUB_RELEASE": rid,
                "HUB_ROLE": self.role,
                "HUB_PORT": str(self.port),
                "HUB_HEARTBEAT": self.heartbeat,
                "HUB_LOADER_SHA": self.sha[:12],
                "HUB_LOADER_STATE": self.state_file,
            }
        )
        if self.role == "ci":
            try:
                os.unlink(self.heartbeat)
            except OSError:
                pass
        self.started = time.time()
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-B", "-u", entry], cwd=rdir, env=env, stdin=subprocess.DEVNULL
            )
        except OSError as e:
            self.proc = None
            self.log("spawn_failed", release=rid, why=type(e).__name__)
            if self.fallback:
                self.fallback.start()
            return False
        self.running, self.running_line = rid, line
        self.phase = "starting"
        self.log("start", release=rid, attempt=line, pid=self.proc.pid)
        end = time.monotonic() + self.start_timeout
        while time.monotonic() < end and not self.stopping:
            if self.proc.poll() is not None:
                self.log("exited", release=rid, code=self.proc.returncode)
                break
            if self.healthy(rid):
                self.phase = "running"
                self.fails = 0
                self.next_check = time.monotonic() + self.check_every
                self.log("healthy", release=rid, after_s=round(time.time() - self.started, 2))
                return True
            time.sleep(0.25)
        self.stop_child()
        return False

    def stop_child(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(STOP_WAIT)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
        self.proc = None
        self.running = None
        self.running_line = None
        self.phase = "stopped"
        if self.fallback and not self.stopping:
            self.fallback.start()

    # ------------------------------------------------------------ decisions
    def load(self, rid: str, line: str, is_want: bool) -> bool:
        """Verify, then (only then) stop the running child and start rid. A wanted release that fails is marked bad
        for its CURRENT line, unless it is the last good one (that one gets a retry delay from the caller)."""
        entry, why = verify_release(self.releases, rid, self.role)
        if entry is None:
            self.log("verify_failed", release=rid, why=why)
            if is_want and rid != self.st.get("last_good"):
                self.mark_bad(line, f"verify: {why}")
            return False
        self.stop_child()
        if self.spawn(rid, line, entry, os.path.join(os.path.realpath(self.releases), rid)):
            if is_want:
                self.st["last_good"] = rid
            self.backoff.pop(rid, None)
            self._save()
            return True
        if is_want and rid != self.st.get("last_good"):
            self.mark_bad(line, "not healthy after start")
        return False

    def step(self) -> None:
        now = time.monotonic()
        cur = self.read_current()
        want = cur if cur is not None and cur[1] not in self.st.get("bad", {}) else None
        if self.proc is not None:
            if want and want[0] != self.running:
                self.restarts = []
                self.load(want[0], want[1], True)  # switched; or verify failed (old child runs on); next step recovers
                return
            if want:
                self.running_line = want[1]
            self.watch(want)
            return
        lg = self.st.get("last_good")
        cands = [(want[0], want[1], True)] if want else []
        if lg and not (want and want[0] == lg):
            cands.append((lg, f"{lg} last_good", False))
        for rid, line, is_want in cands:
            if now < self.backoff.get(rid, 0):
                continue
            if self.load(rid, line, is_want):
                return
            if not is_want or rid == lg:
                self.backoff[rid] = now + LAST_GOOD_RETRY
        self.phase = "backoff" if cands else "no_release"

    def watch(self, want: tuple[str, str] | None) -> None:
        p = self.proc
        rid, line = self.running, self.running_line or ""
        if p is None or rid is None:
            return
        dead = p.poll() is not None
        if not dead and time.monotonic() >= self.next_check:
            self.next_check = time.monotonic() + self.check_every
            ok = self.healthy(rid) if self.role == "hub" else self.fresh()
            self.fails = 0 if ok else self.fails + 1
            dead = self.fails >= FAILS_BEFORE_RESTART
            if dead:
                self.log("unresponsive", release=rid, checks=self.fails)
        if not dead:
            return
        if p.poll() is not None:
            self.log("exited", release=rid, code=p.returncode)
        now = time.monotonic()
        self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW] + [now]
        is_want = bool(want and want[0] == rid)
        self.stop_child()
        if len(self.restarts) >= RESTARTS_BAD:
            self.restarts = []
            if is_want and rid != self.st.get("last_good"):
                self.mark_bad(line, f"{RESTARTS_BAD} restarts in {int(RESTART_WINDOW / 60)} min")
            else:
                self.backoff[rid] = now + LAST_GOOD_RETRY
            return
        self.load(rid, line, is_want)

    # ------------------------------------------------------------ main
    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._sig)
        signal.signal(signal.SIGINT, self._sig)
        self.log("loader_start", sha=self.sha[:12], last_good=self.st.get("last_good"))
        if self.fallback:
            self.fallback.start()
        while not self.stopping:
            try:
                self.step()
            except Exception as e:  # keep the loader alive; the event says what broke
                self.log("loop_error", why=type(e).__name__)
            t = time.monotonic() + self.poll
            while not self.stopping and time.monotonic() < t:
                time.sleep(0.1)
        self.stop_child()
        if self.fallback:
            self.fallback.stop()
        self.log("loader_stop")
        return 0

    def _sig(self, *_a) -> None:
        self.stopping = True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hubloader", description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--role", choices=ROLES, required=True)
    ap.add_argument("--releases", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--heartbeat", default="/tmp/heartbeat")
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--start-timeout", type=float, default=30.0)
    ap.add_argument("--check-every", type=float, default=15.0)
    return Loader(ap.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
