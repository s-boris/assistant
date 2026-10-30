"""tool worker: the brain-side executor of the hub gateway.

The hub's gateway (tools.hubgw) holds the MCP endpoint and the policy; the calls run here, where the CLIs and their
credentials live. THREADS threads long-poll POST /gw/claim on the hub with
the brain's token, run each call through tools.gateway.call (surface "hub": validation, rate, approval consume,
usage, gateway.db, spans, exactly as on the CLI and stdio paths) and POST /gw/result.

  tool worker run [--threads N]   in the foreground; one JSON line per event to the log (ids, codes, counts only)
  tool worker ensure              start it in the background unless one runs (flock on worker.lock); cron, every minute
  tool worker status [--json]     this side (pid, since, calls, errors) and the hub's view of the executor
  tool worker stop                SIGTERM to the running one (cron's next ensure starts it again unless worker-off)
A deploy that changes the code under a running worker (a tools file, the core release) makes it stop claiming, start
its replacement through the launcher (`tool worker ensure`) and then finish the calls in hand (drain, at most
DRAIN_MAX: a hub job may run up to an hour), so a long job's result still reaches the hub: new code runs within one
TICK plus one claim wait, and the hub's claim grace covers the hand-over.
Trace context comes per call from the hub's job meta only: `run` drops an inherited TRACEPARENT/TRACESTATE first (a
worker started from a Bash call would otherwise hand that call's trace to every call it serves, and to each respawn).
Env: HUB_GW_URL (default: HUB_URL's host on port 8701, else http://claude-hub:8701; tests must set one) ·
HUB_TOKEN_FILE (default ~/.secrets/hub-token, read here, never printed) · TOOLS_STATE (worker.lock, worker.json,
worker.log) · TOOLS_CONFIG (worker-off: ensure starts nothing, a running worker exits at its next poll) ·
TOOLS_LAUNCHER (the launcher a code change re-runs; default ~/bin/tool).
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from core import http as corehttp
from core import locks
from tools import gateway
from tools.core import Registry, load

GW_PORT = 8701
CLAIM_WAIT = 20.0
THREADS = 12  # the hub's job queues hold up to 8 (critical 2 + fast 4 + slow 2) + 4 for his calls
TICK = 15.0  # seconds between worker.json saves and code checks
JOIN_WAIT = 10.0  # past one claim wait, per thread, before a stopping worker moves on (busy ones drain later)
DRAIN_MAX = 3720.0  # a code change: how long the old worker keeps finishing calls in hand (longest hub job + margin)
LOG_MAX = 1 << 20
ACTOR = {"binding": "owner", "reflex": "script", "ci": "test"}  # usage's actor per principal; others: agent


def gw_url() -> str:
    u = os.environ.get("HUB_GW_URL", "").rstrip("/")
    if u:
        return u
    hub = os.environ.get("HUB_URL", "")
    if hub:
        p = urllib.parse.urlsplit(hub)
        return f"{p.scheme}://{p.hostname}:{GW_PORT}"
    if os.environ.get("ASSISTANT_TEST"):
        raise SystemExit("worker: ASSISTANT_TEST=1: set HUB_GW_URL (tests never reach the real hub)")
    return f"http://claude-hub:{GW_PORT}"


def token() -> str:
    p = os.environ.get("HUB_TOKEN_FILE") or str(gateway._home() / ".secrets" / "hub-token")
    try:
        t = Path(p).read_text(encoding="utf-8").strip()
    except OSError:
        t = ""
    if not t:
        raise SystemExit("worker: no hub token")
    return t


def _code_stamp() -> tuple[str, int, int]:
    """What code this process runs: the core release the lib's core symlink points at, and the newest tools file."""
    here = Path(gateway.__file__).resolve().parent
    files = [f for pat in ("*.py", "*.cedar", "*.lock") for f in here.glob(pat)] + list(here.glob("domains/*.py"))
    newest = 0
    for f in files:
        try:
            newest = max(newest, f.stat().st_mtime_ns)
        except OSError:
            pass
    return os.path.realpath(os.path.dirname(corehttp.__file__)), newest, len(files)


def respawn(threads: int) -> bool:
    """Start the replacement through the launcher (a fresh interpreter: new code, the lock's venv)."""
    launcher = os.environ.get("TOOLS_LAUNCHER") or str(gateway._home() / "bin" / "tool")
    try:
        r = subprocess.run(
            [launcher, "worker", "ensure", "--threads", str(threads)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _paths() -> dict[str, Path]:
    d = gateway.state_dir()
    return {
        "lock": d / "worker.lock",
        "state": d / "worker.json",
        "log": d / "worker.log",
        "off": gateway.config_dir() / "worker-off",
    }


class Worker:
    def __init__(
        self, threads: int = THREADS, registry: Registry | None = None, claim_wait: float = CLAIM_WAIT
    ) -> None:
        self.claim_wait = claim_wait
        self.url = urllib.parse.urlsplit(gw_url())
        self.auth = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
        self.threads = max(1, threads)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.id = f"w-{os.getpid()}-{secrets.token_hex(3)}"
        self.since = time.time()
        self.calls = 0
        self.errors = 0
        self.restart = False  # set when the code under it changed: cmd_run starts the replacement
        self.ts: list[threading.Thread] = []
        self.last_error = ""
        self.paths = _paths()
        self.reg = registry or load()

    # -- plumbing
    def log(self, event: str, **kw: Any) -> None:
        line = json.dumps({"ts": round(time.time(), 3), "event": event, **kw}, separators=(",", ":"))
        p = self.paths["log"]
        try:
            p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > LOG_MAX:
                os.replace(p, p.with_suffix(".log.1"))
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def save(self) -> None:
        st = {
            "pid": os.getpid(),
            "id": self.id,
            "since": round(self.since),
            "calls": self.calls,
            "errors": self.errors,
            "last_error": self.last_error,
            "url": gw_url(),
            "at": round(time.time()),
        }
        p = self.paths["state"]
        try:
            tmp = p.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(st) + "\n", encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass

    def conn(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            self.url.hostname or "", self.url.port or GW_PORT, timeout=self.claim_wait + 15
        )

    def post(self, c: http.client.HTTPConnection, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        c.request("POST", path, body=json.dumps(body).encode(), headers=self.auth)
        r = c.getresponse()
        raw = r.read()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return r.status, data if isinstance(data, dict) else {}

    # -- one call
    def execute(self, job: dict[str, Any]) -> dict[str, Any]:
        actor = "test" if job.get("test") else ACTOR.get(str(job.get("principal")), "agent")
        res = gateway.call(
            str(job.get("tool")),
            job.get("args") or {},
            surface="hub",
            meta=job.get("meta") or None,
            approval=job.get("approval"),
            registry=self.reg,
            actor=actor,
            consume=bool(job.get("consume")),
        )
        out = res.as_json()
        out["code"] = res.code
        return out

    def loop(self) -> None:
        c: http.client.HTTPConnection | None = None
        backoff = 1.0
        while not self.stop.is_set():
            if self.paths["off"].exists():
                self.log("off_switch")
                self.stop.set()
                break
            try:
                c = c or self.conn()
                st, got = self.post(c, "/gw/claim", {"wait": self.claim_wait, "worker": self.id})
                if st in (401, 403):
                    self.note(f"claim {st}")
                    self.stop.wait(60)
                    continue
                if st != 200:
                    raise OSError(f"claim {st}")
                backoff = 1.0
                job = got.get("job")
                if not isinstance(job, dict) or not isinstance(job.get("id"), str):
                    continue
                t0 = time.monotonic()
                try:
                    res = self.execute(job)
                except Exception as e:  # never leave the hub waiting for a timeout
                    res = {
                        "ok": False,
                        "code": "internal",
                        "error": {"code": "internal", "message": f"executor: {type(e).__name__}", "hint": "a bug"},
                    }
                with self.lock:
                    self.calls += 1
                for attempt in (1, 2):
                    try:
                        c = c or self.conn()
                        self.post(c, "/gw/result", {"id": job["id"], "result": res})
                        break
                    except (OSError, http.client.HTTPException):
                        c = _close(c)
                        if attempt == 2:
                            self.note("result post failed")
                self.log(
                    "call",
                    tool=job.get("tool"),
                    code=res.get("code"),
                    ms=int((time.monotonic() - t0) * 1000),
                    principal=job.get("principal"),
                )
            except (OSError, http.client.HTTPException, ValueError) as e:
                c = _close(c)
                self.note(type(e).__name__)
                self.stop.wait(backoff)
                backoff = min(30.0, backoff * 2)
        _close(c)

    def note(self, why: str) -> None:
        with self.lock:
            self.errors += 1
            self.last_error = f"{time.strftime('%H:%M:%S')} {why}"[:120]

    def run(self) -> int:
        self.log("start", id=self.id, threads=self.threads)
        ts = self.ts = [threading.Thread(target=self.loop, name=f"claim-{i}", daemon=True) for i in range(self.threads)]
        for t in ts:
            t.start()
        code = _code_stamp()
        while not self.stop.wait(TICK):
            self.save()
            if _code_stamp() != code:
                self.log("code_changed", id=self.id)
                self.restart = True
                self.stop.set()
        for t in ts:
            t.join(self.claim_wait + JOIN_WAIT)
        self.save()
        self.log("stop", id=self.id, calls=self.calls)
        return 0

    def drain(self, limit: float = DRAIN_MAX) -> int:
        """After the replacement started: wait (at most limit s) for the calls still in hand; their threads post the
        results and end. How many still run at the end (the process exits anyway)."""
        busy = [t for t in self.ts if t.is_alive()]
        if not busy:
            return 0
        self.log("draining", id=self.id, calls=len(busy))
        end = time.monotonic() + limit
        for t in busy:
            t.join(max(0.0, end - time.monotonic()))
        left = sum(t.is_alive() for t in busy)
        self.log("drained", id=self.id, left=left)
        return left


def _close(c: http.client.HTTPConnection | None) -> None:
    if c is not None:
        try:
            c.close()
        except OSError:
            pass
    return None


def _locked(p: Path) -> int | None:
    """The fd holding the exclusive lock (keep it open), or None when another process holds it."""
    return locks.acquire(p, wait=0, dir_mode=0o700)


def cmd_run(threads: int) -> int:
    gateway.drop_inherited_trace()
    held = _locked(_paths()["lock"])
    if held is None:
        print("worker: another one runs (tool worker status)", file=sys.stderr)
        return 3
    w = Worker(threads)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: w.stop.set())
    code = w.run()
    if w.restart:
        locks.release(held)
        w.log("respawn", ok=respawn(w.threads))
        w.drain()
    return code


def cmd_ensure(threads: int) -> int:
    p = _paths()
    if p["off"].exists():
        return 0
    held = _locked(p["lock"])
    if held is None:
        return 0  # running
    locks.release(held)
    token()  # fail here (exit 1) rather than in the child
    gw_url()
    if os.fork() != 0:
        return 0
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    try:
        code = cmd_run(threads)
    except BaseException:
        code = 1
    os._exit(code)


def status() -> dict[str, Any]:
    p = _paths()
    held = _locked(p["lock"])
    running = held is None
    locks.release(held)
    try:
        st = json.loads(p["state"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    hub: dict[str, Any] = {}
    try:
        with corehttp.open(gw_url() + "/gw/health", timeout=4) as r:
            hub = json.load(r).get("executor") or {}
    except (OSError, ValueError, SystemExit) as e:
        hub = {"error": type(e).__name__}
    return {"running": running, "off": p["off"].exists(), "worker": st if running else {}, "hub": hub}


def cmd_status(as_json: bool) -> int:
    s = status()
    if as_json:
        print(json.dumps(s, indent=1))
    else:
        w = s["worker"]
        print(f"worker: {'running' if s['running'] else 'stopped'}" + (" (worker-off)" if s["off"] else ""))
        if w:
            print(
                f"  pid {w.get('pid')} since {time.strftime('%d.%m %H:%M', time.localtime(w.get('since', 0)))}"
                f" · calls {w.get('calls')} · errors {w.get('errors')} {w.get('last_error') or ''}".rstrip()
            )
        h = s["hub"]
        print(
            "  hub: "
            + (
                f"online={h.get('online')} claimers={h.get('claimers')} pending={h.get('pending')} "
                f"served={h.get('served')}"
                if "error" not in h
                else f"unreachable ({h['error']})"
            )
        )
    return 0 if s["running"] else 3


def cmd_stop() -> int:
    s = status()
    pid = (s.get("worker") or {}).get("pid")
    if not s["running"] or not isinstance(pid, int):
        print("worker: not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    print(f"worker: SIGTERM to {pid}")
    return 0


def main(sub: str, *, threads: int = THREADS, as_json: bool = False) -> int:
    if sub == "run":
        return cmd_run(threads)
    if sub == "ensure":
        return cmd_ensure(threads)
    if sub == "status":
        return cmd_status(as_json)
    if sub == "stop":
        return cmd_stop()
    raise SystemExit(f"worker: unknown {sub!r}")
