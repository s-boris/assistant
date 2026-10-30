"""hubgw: the gateway inside the hub: FastMCP over streamable HTTP, Cedar policy, labels.

hubd starts it in its own event loop (start()), port HUB_GW_PORT (8701), on the hub's docker networks: the brain
reaches http://claude-hub:8701/mcp; nothing is published on the host or LAN. Nothing here runs a tool: the brain's
executor (`tool worker`, kept up by cron) long-polls /gw/claim, runs the call through the brain's own gateway
(validation, rate, approval consume, usage, spans; the CLIs' credentials stay in the brain container) and
posts /gw/result. Adapter-run tools (ADAPTER_RUN: voice.*) go to that adapter's own claim queue instead, but only
while the adapter is live (Gateway.route): whenever it is held or stops polling they run on the brain's executor.

Per MCP call: caller from the bearer token (the hub's token_<caller> files: brain | tg | ci | voice | phone) → principal
(brain token: the PreToolUse stamp `_caller` says brain or subagent, policy.verify_stamp; no valid stamp = an
unattributed subagent; tg = binding; ci = ci; voice = voice) → Cedar (policy.cedar, default deny) → the domain's
mode: `log` (the decision is only logged, the call runs: the start state) or `enforce` (a deny answers
`policy_denied: reason (hint: …)`); principal voice is always enforced (ALWAYS_ENFORCE) → input validation → the
executor → the result's labels join the caller's taint (FIDES-style: what was read in that context; key = Claude
Code session + agent). Reserved arguments, stripped before validation: `_caller`
(the stamp) and `_approval` (an approval nonce; the executor consumes it when the decision depends on it).

HTTP (bearer token on everything but /gw/health):
  POST /mcp                  MCP streamable HTTP (stateless, JSON answers): the typed tools of the registry
  GET  /gw/health            {ok, tools, policy, executor, adapters, mode, calls, overhead_ms}: counts and ids only
  POST /gw/claim             brain: {"wait": s ≤ 25} → {"job": {...} | null} (the executor's long poll)
  POST /gw/result            brain: {"id", "result": {...}}
  GET  /gw/policy            brain: per-domain report (calls, denies, unexpected, ready, mode), recent denies
  POST /gw/policy/mode       brain: {"domain": D | "all", "mode": "log" | "enforce", "force"?, "why"?}; enforce
                             needs ≥ 100 calls and 0 unexpected denies in that domain unless force
  POST /gw/policy/ack        brain: {"ids": [...], "why"}: denies that were expected (no longer "unexpected")
  POST /gw/policy/check      brain: {"tool", "principal", "approved"?, "tainted"?} → the decision (dry run, unlogged)
Test calls: header X-Hub-Test: 1 marks the row as a test (left out of the report and the gate); with it,
X-Hub-Test-Mode: log|enforce overrides the domain's mode for that call only. Redaction: when hubd passes
`redact` (hubredact.Redactor.obj, protected), every result (data, text, errors) goes through it before it leaves the
gateway; a redaction that fails answers `internal` instead of the unredacted result (fail closed). /gw/health counts
redacted results (counts only: it needs no token); /gw/policy (brain) names the last secrets hit. State (decisions,
taint, modes):
SQLite + JSON in HUB_GW_STATE (default <hub state>/gateway). The log holds tool names, principal kinds, decisions,
codes and timings: never arguments, results or text.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tools.core import Registry, ToolError, load
from tools.policy import Engine, Principal, verify_stamp

PORT = 8701
CLAIM_WAIT_MAX = 25.0
CLAIM_GRACE = 5.0  # an online executor picks a call up within this, or the call answers unavailable
ANSWER_GRACE = 20.0  # past the tool's limit, before a claimed call answers timeout
CALL_TIMEOUT_MAX = 3600.0  # the longest per-call limit an in-hub caller may set (hubwf: a job's own timeout)
OFFLINE_AFTER = 40.0  # no claim poll for this long: the executor is offline (calls fail at once)
GATE_CALLS = 100
KEEP_DAYS = 90
MODES = ("log", "enforce")
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{6,128}")
# hub = hubd's own calls (hub schedules); voice = the voice adapter's in-call lookups
CALLER_PRINCIPAL = {"tg": "binding", "ci": "ci", "hub": "reflex", "voice": "voice", "phone": "phone"}
# principals whose denies always hold, whatever the domain's mode (log is the start state of every domain):
# the voice relay acts on untrusted speech, and its whole surface is voice.lookup + phone.open (policy.cedar).
# the phone gateway too (it acts on what arrives from the internet route; its surface is phone.lookup).
ALWAYS_ENFORCE = frozenset({"voice", "phone"})
# tools an adapter runs. While that adapter is live (its executor long-polls /gw/claim with its own token: the
# voice relay in claude-hub-voice; a poll within OFFLINE_AFTER) their calls wait for it instead of the brain's executor.
# Not live (held or crashed): they run on the brain's executor, whose CLIs reach voice-hub, and voice-hub's own calls
# never come back to the brain (ADAPTER_CLIENT): so a dead adapter fails closed (unavailable = relay down), never a
# second runner. Everything else stays the brain's.
# The phone.* tools stay the brain's (its
# `phone` CLI reaches claude-hub-phone's internal API with its own token, and keeps the profile, the Telegram link
# fallback and the usage log, which the phone container does not have).
ADAPTER_RUN = {
    "voice": ("voice.reply", "voice.call", "voice.status", "voice.calls", "voice.drive_update", "voice.talk"),
}
RUNNER = {t: a for a, ts in ADAPTER_RUN.items() for t in ts}
# each adapter's brain-side client, by its `_caller` stamp (principal reflex, t = the script): the brain's own way
# to that adapter. Its calls stay on the adapter's queue even when the adapter is not live (they answer unavailable at
# once): on the brain's executor they would run the CLI that hands them back to this client, and round.
ADAPTER_CLIENT = {"voice": "voice-hub"}
OPEN_PATHS = ("/gw/health",)
VERSION = "0.2.0"


def state_dir() -> Path:
    v = os.environ.get("HUB_GW_STATE", "")
    if v:
        return Path(v)
    ls = os.environ.get("HUB_LOADER_STATE", "")
    base = Path(ls).parent if ls else Path(os.environ.get("TMPDIR", "/tmp"))
    return base / "gateway"


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(p * len(s)))], 2)


# ------------------------------------------------------------------------------------------------ state


class Store:
    """Decision log, taint and modes. One connection, used from the event loop's thread only."""

    def __init__(self, d: Path) -> None:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.dir = d
        self.db = sqlite3.connect(d / "gateway.db", timeout=5, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, ts REAL NOT NULL, tool TEXT, domain TEXT,"
            " effect TEXT, caller TEXT, principal TEXT, attributed INTEGER, decision TEXT, policies TEXT,"
            " reason TEXT, mode TEXT, enforced INTEGER, approved INTEGER, consume INTEGER, code TEXT,"
            " ms_total INTEGER, ms_exec INTEGER, us_policy INTEGER, trace TEXT, test INTEGER, acked TEXT)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts)")
        self.db.execute("CREATE TABLE IF NOT EXISTS taint (key TEXT PRIMARY KEY, labels TEXT, ts REAL)")
        self.mode_file = d / "policy-mode.json"
        self._modes: dict[str, Any] = {}
        self._mode_mtime = -1.0
        self._mode_bad = False
        self.taints: dict[str, frozenset[str]] = {}

    # -- decisions
    def log(self, row: dict[str, Any]) -> int:
        cols = list(row)
        cur = self.db.execute(
            f"INSERT INTO calls ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
        )
        if secrets.randbelow(200) == 0:
            self.db.execute("DELETE FROM calls WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
        return int(cur.lastrowid or 0)

    def ack(self, ids: list[int], why: str) -> int:
        n = 0
        for i in ids:
            n += self.db.execute(
                "UPDATE calls SET acked = ? WHERE id = ? AND decision = 'deny' AND acked IS NULL", (why[:200], i)
            ).rowcount
        return n

    def report(self, days: float = 30.0) -> dict[str, Any]:
        since = time.time() - days * 86400
        rows = self.db.execute(
            "SELECT id, ts, tool, domain, principal, decision, policies, reason, enforced, acked, code, ms_total,"
            " ms_exec, us_policy FROM calls WHERE ts > ? AND test = 0 ORDER BY id",
            (since,),
        ).fetchall()
        doms: dict[str, dict[str, Any]] = {}
        princ: dict[str, int] = {}
        denies: list[dict[str, Any]] = []
        over: list[float] = []
        pol: list[float] = []
        for i, ts, tool, dom, p, dec, pols, reason, enf, acked, code, mst, mse, usp in rows:
            d = doms.setdefault(dom or "?", {"calls": 0, "denies": 0, "unexpected": 0, "enforced": 0})
            d["calls"] += 1
            princ[p] = princ.get(p, 0) + 1
            if dec == "deny":
                d["denies"] += 1
                d["enforced"] += 1 if enf else 0
                if acked is None:
                    d["unexpected"] += 1
                denies.append(
                    {
                        "id": i,
                        "ts": round(ts),
                        "tool": tool,
                        "principal": p,
                        "policies": pols,
                        "reason": reason,
                        "enforced": bool(enf),
                        "acked": acked,
                    }
                )
            if mst is not None and mse is not None and code not in ("unavailable", "policy_denied"):
                over.append(max(0, mst - mse))
            if usp is not None:
                pol.append(usp / 1000)
        modes = self.modes()
        for name, d in doms.items():
            d["mode"] = self.mode(name)
            d["ready"] = d["calls"] >= GATE_CALLS and d["unexpected"] == 0
        return {
            "days": days,
            "gate": {"calls": GATE_CALLS, "unexpected": 0},
            "modes": modes,
            "domains": dict(sorted(doms.items())),
            "principals": dict(sorted(princ.items())),
            "denies": denies[-20:],
            "overhead_ms": {"n": len(over), "p50": _pct(over, 0.5), "p95": _pct(over, 0.95)},
            "policy_ms": {"p50": _pct(pol, 0.5), "p95": _pct(pol, 0.95)},
        }

    # -- taint
    def taint(self, key: str) -> frozenset[str]:
        if key not in self.taints:
            r = self.db.execute("SELECT labels FROM taint WHERE key = ?", (key,)).fetchone()
            self.taints[key] = frozenset(json.loads(r[0])) if r else frozenset()
        return self.taints[key]

    def add_taint(self, key: str, labels: tuple[str, ...]) -> None:
        cur = self.taint(key)
        new = cur | set(labels)
        if new != cur:
            self.taints[key] = frozenset(new)
            self.db.execute(
                "INSERT INTO taint (key, labels, ts) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
                " labels = excluded.labels, ts = excluded.ts",
                (key, json.dumps(sorted(new)), time.time()),
            )

    # -- modes (the log-only switch: a file, read again when it changes; no restart)
    def modes(self) -> dict[str, Any]:
        try:
            mt = self.mode_file.stat().st_mtime
        except OSError:
            self._modes, self._mode_mtime, self._mode_bad = {}, -1.0, False
            return {"default": "log", "domains": {}}
        if mt != self._mode_mtime:
            try:
                m = json.loads(self.mode_file.read_text(encoding="utf-8"))
                ok = isinstance(m, dict) and isinstance(m.get("domains", {}), dict)
            except (OSError, ValueError):
                m, ok = {}, False
            self._modes, self._mode_mtime, self._mode_bad = (m if ok else {}), mt, not ok
        return {"default": self._modes.get("default", "log"), "domains": dict(self._modes.get("domains") or {})}

    def mode(self, domain: str) -> str:
        m = self.modes()
        if self._mode_bad:
            return "log"  # an unreadable switch file never enforces
        v = m["domains"].get(domain, m["default"])
        return v if v in MODES else "log"

    def set_mode(self, domain: str, mode: str, why: str) -> dict[str, Any]:
        m = self.modes()
        if domain == "all":
            m = {"default": mode, "domains": {}}
        else:
            m["domains"][domain] = mode
        hist = list((self._modes.get("history") or [])[-19:])
        hist.append({"ts": round(time.time()), "domain": domain, "mode": mode, "why": why[:200]})
        m["history"] = hist
        tmp = self.mode_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.mode_file)
        return self.modes()


# ------------------------------------------------------------------------------------------------ executor queue


@dataclass
class Job:
    payload: dict[str, Any]
    fut: asyncio.Future
    claimed: asyncio.Event = field(default_factory=asyncio.Event)


class Exec:
    """Calls waiting for an executor (the brain's, or an adapter's); it long-polls claim() and answers done()."""

    def __init__(
        self,
        who: str = "the brain's executor",
        hint: str = "`tool worker status`",
        offline_hint: str = "`tool worker ensure` (cron starts it)",
    ) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.jobs: dict[str, Job] = {}
        self.claimers = 0
        self.last_poll = -1e9
        self.served = 0
        self.who, self.hint, self.offline_hint = who, hint, offline_hint

    def online(self) -> bool:
        return self.claimers > 0 or time.monotonic() - self.last_poll < OFFLINE_AFTER

    def state(self) -> dict[str, Any]:
        seen = time.monotonic() - self.last_poll
        return {
            "online": self.online(),
            "claimers": self.claimers,
            "pending": len(self.jobs),
            "seen_s": None if seen > 1e8 else round(seen, 1),
            "served": self.served,
        }

    async def run(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.online():
            raise ToolError("unavailable", f"{self.who} is offline", self.offline_hint)
        jid = secrets.token_hex(8)
        job = Job(dict(payload, id=jid), asyncio.get_running_loop().create_future())
        self.jobs[jid] = job
        await self.queue.put(jid)
        try:
            try:
                await asyncio.wait_for(job.claimed.wait(), CLAIM_GRACE)
            except TimeoutError:
                raise ToolError("unavailable", f"{self.who} did not pick the call up", self.hint) from None
            try:
                return await asyncio.wait_for(job.fut, timeout)
            except TimeoutError:
                raise ToolError("timeout", f"no answer from {self.who}", "retry once") from None
        finally:
            self.jobs.pop(jid, None)

    async def claim(self, wait: float) -> dict[str, Any] | None:
        self.claimers += 1
        self.last_poll = time.monotonic()
        try:
            end = time.monotonic() + max(0.0, min(wait, CLAIM_WAIT_MAX))
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    return None
                try:
                    jid = await asyncio.wait_for(self.queue.get(), left)
                except TimeoutError:
                    return None
                job = self.jobs.get(jid)
                if job is None or job.claimed.is_set():
                    continue  # the caller gave up already
                job.claimed.set()
                return job.payload
        finally:
            self.claimers -= 1
            self.last_poll = time.monotonic()

    def done(self, jid: str, result: dict[str, Any]) -> bool:
        job = self.jobs.get(jid)
        if job is None or job.fut.done():
            return False
        job.fut.set_result(result)
        self.served += 1
        return True


# ------------------------------------------------------------------------------------------------ the gateway


def _err(code: str, message: str, hint: str = "") -> dict[str, Any]:
    return {"ok": False, "code": code, "error": {"code": code, "message": message, "hint": hint}}


def error_text(res: dict[str, Any]) -> str:
    e = res.get("error") or {}
    return f"{e.get('code')}: {e.get('message')}" + (f" (hint: {e['hint']})" if e.get("hint") else "")


Redact = Callable[[Any], tuple[Any, list[str]]]


class Gateway:
    def __init__(
        self,
        tokens: dict[str, bytes],
        state: Path | None = None,
        registry: Registry | None = None,
        redact: Redact | None = None,
    ) -> None:
        self.tokens = dict(tokens)
        self.redact = redact
        self.redacted: dict[str, Any] = {"results": 0, "hits": 0, "failed": 0, "last": deque(maxlen=5)}
        self.reg = registry or load()
        self.engine = Engine(self.reg)
        self.invalid = self.engine.validate()
        self.store = Store(state or state_dir())
        self.exec = Exec()
        self.execs = {  # one queue per adapter
            a: Exec(f"the {a} adapter", f"`hub {a} status`", f"`hub {a} status`") for a in ADAPTER_RUN
        }
        self.since = time.time()
        self.calls = 0
        self.overhead: deque[float] = deque(maxlen=2000)

    # -- who calls
    def caller(self, header: str | None) -> str | None:
        if not header or not header.startswith("Bearer "):
            return None
        got = header[7:].strip().encode()
        who = None
        for name, tok in self.tokens.items():  # every token compared: no early exit
            if hmac.compare_digest(got, tok):
                who = name
        return who

    def principal(self, caller: str, stamp: Any, tool: str) -> Principal:
        if caller in CALLER_PRINCIPAL:
            return Principal(CALLER_PRINCIPAL[caller])
        if caller == "brain":
            p = verify_stamp(stamp, tool, self.tokens.get("brain"))
            if p is not None:
                return p
        agent = ""
        if isinstance(stamp, dict) and isinstance(stamp.get("a"), str):
            agent = stamp["a"][:64]
        return Principal("subagent", agent_id=agent, attributed=False)

    def route(self, tool: str, p: Principal) -> Exec:
        """Who runs a call: an ADAPTER_RUN tool goes to its adapter's queue while that adapter is live, else to
        the brain's executor; the adapter's own brain-side client (ADAPTER_CLIENT) never falls back."""
        a = RUNNER.get(tool)
        if a is None:
            return self.exec
        ex = self.execs[a]
        if ex.online() or (p.kind == "reflex" and p.agent_type == ADAPTER_CLIENT.get(a)):
            return ex
        return self.exec

    # -- one call
    async def call(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        caller: str,
        meta: dict[str, Any] | None = None,
        test: bool = False,
        test_mode: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """One call. timeout_s (in-hub callers only, never from MCP: hubwf's job.tick passes a job's own limit,
        1-CALL_TIMEOUT_MAX) replaces the tool's timeout_s for how long a claimed call may take."""
        t0 = time.monotonic()
        args = dict(arguments or {})
        stamp = args.pop("_caller", None)
        nonce = args.pop("_approval", None)
        spec = self.reg.get(name)
        tool = spec.mcp_name if spec else name[:64]
        p = self.principal(caller, stamp, tool)
        row: dict[str, Any] = {
            "ts": time.time(),
            "tool": spec.name if spec else name[:64],
            "domain": spec.domain if spec else None,
            "effect": spec.effect if spec else None,
            "caller": caller,
            "principal": p.label,
            "attributed": int(p.attributed),
            "test": int(bool(test)),
            "trace": _trace(meta),
        }
        approved = isinstance(nonce, str) and bool(NONCE_RE.fullmatch(nonce))
        res: dict[str, Any]
        ms_exec = None
        try:
            if spec is None:
                row.update(decision="deny", policies="", reason="no such tool", mode="enforce", enforced=1)
                raise ToolError("not_found", f"no tool {name[:64]!r}", "tools/list names them")
            if nonce is not None and not approved:
                raise ToolError("invalid_args", "_approval: not a nonce", "pass the nonce `approvals new` printed")
            d = self.engine.decide(p, spec, approved=approved, tainted=self.store.taint(p.taint_key()))
            mode = test_mode if (test and test_mode in MODES) else self.store.mode(spec.domain)
            if p.kind in ALWAYS_ENFORCE:
                mode = "enforce"  # never log-only, not even a test call's header
            enforce = mode == "enforce"
            row.update(
                decision="allow" if d.allowed else "deny",
                policies=",".join(d.policies),
                reason=d.reason or None,
                mode=mode,
                enforced=int(enforce and not d.allowed),
                approved=int(approved),
                consume=int(d.approval_needed),
                us_policy=d.us,
            )
            if enforce and not d.allowed:
                raise ToolError("policy_denied", f"{p.label} → {spec.name}: {d.reason}", d.hint)
            try:
                spec.input.model_validate(args)
            except ValidationError as e:
                first = e.errors()[0]
                where = ".".join(str(x) for x in first.get("loc", ())) or "input"
                raise ToolError("invalid_args", f"{where}: {first.get('msg')}", f"`tool show {spec.name}`") from None
            payload = {
                "tool": spec.name,
                "args": args,
                "approval": nonce if approved else None,
                "consume": bool(enforce and d.approval_needed),
                "principal": p.kind,
                "agent_type": p.agent_type,
                "meta": {"traceparent": (meta or {}).get("traceparent")} if meta else {},
                "test": bool(test),
            }
            runner = self.route(spec.name, p)
            limit = spec.timeout_s if timeout_s is None else max(1.0, min(float(timeout_s), CALL_TIMEOUT_MAX))
            res = await runner.run(payload, limit + ANSWER_GRACE)
            ms_exec = res.get("ms") if isinstance(res.get("ms"), int) else None
            if res.get("ok") and spec.labels_out:
                self.store.add_taint(p.taint_key(), spec.labels_out)
        except ToolError as e:
            res = _err(e.code, e.message, e.hint)
        except Exception as e:  # a bug: the type only
            res = _err("internal", f"gateway: {type(e).__name__}", "a bug: report it")
        if self.redact is not None:
            res = self._redacted(res)
        res.setdefault("code", "ok" if res.get("ok") else (res.get("error") or {}).get("code", "internal"))
        ms = int((time.monotonic() - t0) * 1000)
        row.update(code=res["code"], ms_total=ms, ms_exec=ms_exec)
        row.setdefault("decision", "deny")
        try:
            self.store.log(row)
        except sqlite3.Error:
            pass
        self.calls += 1
        if ms_exec is not None:
            self.overhead.append(max(0, ms - ms_exec))
        return res

    def _redacted(self, res: dict[str, Any]) -> dict[str, Any]:
        """The result with every hub-held secret value masked; fail closed: no unredacted result on a fault."""
        try:
            out, names = self.redact(res) if self.redact is not None else (res, [])
            if not isinstance(out, dict):
                raise TypeError("redaction returned no object")
        except Exception as e:
            self.redacted["failed"] += 1
            return _err("internal", f"gateway: redaction failed ({type(e).__name__})", "a bug: report it")
        if names:
            self.redacted["results"] += 1
            self.redacted["hits"] += len(names)
            self.redacted["last"].append({"ts": round(time.time()), "names": sorted(set(names))[:5]})
        return out

    async def mcp_call(self, name: str, arguments: dict[str, Any]) -> Any:
        from fastmcp.server.dependencies import get_http_request
        from fastmcp.tools.base import ToolResult

        from tools.mcp_server import request_meta

        req = get_http_request()
        caller = str(req.scope.get("hub_caller") or "")
        test = req.headers.get("x-hub-test") == "1"
        res = await self.call(
            name,
            arguments,
            caller=caller,
            meta=request_meta(),
            test=test,
            test_mode=req.headers.get("x-hub-test-mode") if test else None,
        )
        if res.get("ok"):
            return ToolResult(content=res.get("text") or "ok", structured_content=res.get("data"))
        return ToolResult(content=error_text(res), is_error=True)

    def health(self) -> dict[str, Any]:
        m = self.store.modes()
        return {
            "ok": not self.invalid,
            "version": VERSION,
            "tools": len(self.reg.typed),
            "policy": {"sha": self.engine.sha, "valid": not self.invalid},
            "mode": {
                "default": m["default"],
                "enforce": sorted(k for k, v in m["domains"].items() if v == "enforce"),
                "principals_enforced": sorted(ALWAYS_ENFORCE),
            },
            "executor": self.exec.state(),
            # voice-hub refuses a hub without voice; route = who runs the adapter's tools now (brain: not live)
            "adapters": {
                a: {**e.state(), "route": "adapter" if e.online() else "brain"} for a, e in self.execs.items()
            },
            "calls": self.calls,
            "since": round(self.since),
            "overhead_ms": {"p50": _pct(list(self.overhead), 0.5), "p95": _pct(list(self.overhead), 0.95)},
            "redaction": {
                "on": self.redact is not None,
                "results": self.redacted["results"],
                "hits": self.redacted["hits"],
                "failed": self.redacted["failed"],
                "last_ts": self.redacted["last"][-1]["ts"] if self.redacted["last"] else None,
            },
        }

    # -- the ASGI app
    def app(self) -> Any:
        from fastmcp import FastMCP
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        from tools.mcp_server import INSTRUCTIONS, RegistryTool, to_mcp

        gw = self

        class HubTool(RegistryTool):
            async def run(self, arguments: dict[str, Any]) -> Any:
                return await gw.mcp_call(self._spec_name, arguments)

        mcp = FastMCP("hub", instructions=INSTRUCTIONS, version=VERSION)
        for spec in self.reg.typed.values():
            mcp.add_tool(to_mcp(spec, HubTool))

        def brain_only(req: Request) -> JSONResponse | None:
            if req.scope.get("hub_caller") != "brain":
                return JSONResponse({"error": "forbidden", "hint": "the brain's token only"}, status_code=403)
            return None

        async def body(req: Request) -> dict[str, Any]:
            try:
                b = await req.json()
            except ValueError:
                return {}
            return b if isinstance(b, dict) else {}

        @mcp.custom_route("/gw/health", methods=["GET"])
        async def r_health(req: Request) -> JSONResponse:
            return JSONResponse(gw.health())

        def executor(req: Request) -> Exec | None:
            who = req.scope.get("hub_caller")
            return gw.exec if who == "brain" else gw.execs.get(str(who))

        @mcp.custom_route("/gw/claim", methods=["POST"])
        async def r_claim(req: Request) -> JSONResponse:
            ex = executor(req)
            if ex is None:
                return JSONResponse(
                    {"error": "forbidden", "hint": "the brain's or an adapter's token"}, status_code=403
                )
            a = await body(req)
            try:
                wait = float(a.get("wait", 20))
            except (TypeError, ValueError):
                wait = 20.0
            return JSONResponse({"job": await ex.claim(wait)})

        @mcp.custom_route("/gw/result", methods=["POST"])
        async def r_result(req: Request) -> JSONResponse:
            ex = executor(req)
            if ex is None:
                return JSONResponse(
                    {"error": "forbidden", "hint": "the brain's or an adapter's token"}, status_code=403
                )
            a = await body(req)
            r = a.get("result")
            if not isinstance(a.get("id"), str) or not isinstance(r, dict):
                return JSONResponse({"error": "bad_request", "hint": "id + result"}, status_code=400)
            return JSONResponse({"ok": ex.done(a["id"], r)})

        @mcp.custom_route("/gw/policy", methods=["GET"])
        async def r_policy(req: Request) -> JSONResponse:
            if (no := brain_only(req)) is not None:
                return no
            try:
                days = float(req.query_params.get("days", "30"))
            except ValueError:
                days = 30.0
            rep = gw.store.report(days)
            rep["policy"] = {"sha": gw.engine.sha, "valid": not gw.invalid, "errors": gw.invalid[:5]}
            rep["redaction"] = {"last": list(gw.redacted["last"])}  # which secrets were masked (names only)
            return JSONResponse(rep)

        @mcp.custom_route("/gw/policy/mode", methods=["POST"])
        async def r_mode(req: Request) -> JSONResponse:
            if (no := brain_only(req)) is not None:
                return no
            a = await body(req)
            dom, mode, why = a.get("domain"), a.get("mode"), str(a.get("why") or "")
            if mode not in MODES or not isinstance(dom, str) or (dom != "all" and dom not in gw.reg.domains):
                return JSONResponse({"error": "bad_request", "hint": "domain (or all) + mode log|enforce"}, 400)
            if mode == "enforce" and not a.get("force"):
                rep = gw.store.report()["domains"]
                names = sorted({s.domain for s in gw.reg.typed.values()}) if dom == "all" else [dom]
                short = [n for n in names if not (rep.get(n) or {}).get("ready")]
                if short:
                    return JSONResponse(
                        {
                            "error": "gate",
                            "hint": f"not ready: {', '.join(short)} (≥ {GATE_CALLS} calls and 0 "
                            "unexpected denies each; `tool policy` shows them; force overrides)",
                        },
                        status_code=409,
                    )
            return JSONResponse({"modes": gw.store.set_mode(dom, mode, why or ("forced" if a.get("force") else ""))})

        @mcp.custom_route("/gw/policy/ack", methods=["POST"])
        async def r_ack(req: Request) -> JSONResponse:
            if (no := brain_only(req)) is not None:
                return no
            a = await body(req)
            ids = [i for i in (a.get("ids") or []) if isinstance(i, int)]
            why = str(a.get("why") or "")
            if not ids or not why:
                return JSONResponse({"error": "bad_request", "hint": "ids + why"}, status_code=400)
            return JSONResponse({"acked": gw.store.ack(ids, why)})

        @mcp.custom_route("/gw/policy/check", methods=["POST"])
        async def r_check(req: Request) -> JSONResponse:
            if (no := brain_only(req)) is not None:
                return no
            a = await body(req)
            spec = gw.reg.get(str(a.get("tool") or ""))
            kind = a.get("principal")
            if spec is None or kind not in ("brain", "subagent", "binding", "reflex", "ci", "voice", "phone"):
                return JSONResponse({"error": "bad_request", "hint": "tool + principal"}, status_code=400)
            tainted = {x for x in (a.get("tainted") or []) if isinstance(x, str)}
            d = gw.engine.decide(
                Principal(kind, agent_id="check"), spec, approved=bool(a.get("approved")), tainted=tainted
            )
            return JSONResponse(dict(d.as_dict(), tool=spec.name, mode=gw.store.mode(spec.domain)))

        inner = mcp.http_app(path="/mcp", stateless_http=True, json_response=True, transport="http")
        return BearerAuth(inner, self)


def _trace(meta: dict[str, Any] | None) -> str | None:
    tp = str((meta or {}).get("traceparent") or "")
    m = re.fullmatch(r"[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}", tp.strip().lower())
    return m[1] if m else None


class BearerAuth:
    """ASGI wrapper: every HTTP path but /gw/health needs a hub token; the caller's name goes into the scope."""

    def __init__(self, app: Any, gw: Gateway) -> None:
        self.app, self.gw = app, gw

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") not in OPEN_PATHS:
            hdr = ""
            for k, v in scope.get("headers") or []:
                if k == b"authorization":
                    hdr = v.decode("latin-1")
            who = self.gw.caller(hdr)
            if who is None:
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", b"%d" % len(body))],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            scope["hub_caller"] = who
        await self.app(scope, receive, send)


# ------------------------------------------------------------------------------------------------ in hubd's loop


class Running:
    def __init__(self, gw: Gateway, server: Any, task: asyncio.Task, port: int) -> None:
        self.gw, self.server, self.task, self.port = gw, server, task, port

    def brief(self) -> dict[str, Any]:
        if self.task.done():
            exc = None if self.task.cancelled() else self.task.exception()
            return {"state": "down", "why": type(exc).__name__ if exc else "stopped", "port": self.port}
        h = self.gw.health()
        return {
            "state": "ok" if h["ok"] else "policy invalid",
            "port": self.port,
            "tools": h["tools"],
            "policy": h["policy"]["sha"],
            "enforce": h["mode"]["enforce"],
            "executor": h["executor"]["online"],
            "redaction": h["redaction"]["on"],
            "redacted": h["redaction"]["results"],
        }

    async def stop(self) -> None:
        self.server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.task, 8)


async def start(
    tokens: dict[str, bytes],
    *,
    port: int = PORT,
    bind: str = "0.0.0.0",
    state: Path | None = None,
    registry: Registry | None = None,
    redact: Redact | None = None,
) -> Running:
    """Serve the gateway in the running event loop (hubd's): returns once the socket listens. redact:
    hubredact.Redactor.obj (every result passes it; None = no hub-held secrets to mask)."""
    import uvicorn

    gw = Gateway(tokens, state, registry, redact)
    app = gw.app()
    # proto TCP: asyncio turns Nagle off on accepted connections only for such sockets; with Nagle on, every
    # keep-alive response (headers and body are two writes) waits ~40 ms for the peer's delayed ACK
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.bind((bind, port))
    sock.set_inheritable(True)
    config = uvicorn.Config(
        app,
        log_config=None,
        log_level="warning",
        access_log=False,
        lifespan="on",
        timeout_keep_alive=30,
        timeout_graceful_shutdown=3,
    )

    class Quiet(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # hubd owns SIGTERM/SIGINT
            yield

    server = Quiet(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    return Running(gw, server, task, sock.getsockname()[1])


__all__ = ["Exec", "Gateway", "Running", "Store", "error_text", "start", "state_dir"]
