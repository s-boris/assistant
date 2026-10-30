"""The gateway: the one way a typed tool runs, for the MCP server and the CLI alike.

call(NAME, ARGS) validates ARGS against the tool's input model, checks its rate and approval, runs it, and logs
one row per call: ~/.local/state/tools/gateway.db (ts, tool, surface, trace id + its source, ms, code, actor; never
arguments or results) and one usage event (feature = the domain's, attrs via=gateway, tool, surface, trace, code).
A call whose answer is an async job still queued or running (a vault tool at its wait_s) logs code "pending", not
"ok" (the caller's wait is not the answer time). Such a job also gets a row in the job log (table jobs: job id,
the tool that started it, state, created / ended epoch s, duration s; timings only): the starting call inserts it, a
vault.job pickup fills in its end, and sweep_jobs (`tool jobs sweep`, run by `slo --cache` every 15 min while a row is
open) reads the end from the vault's job list, which keeps a job 30 min after it ends. The vault-answer SLO reads it.
The trace id comes from the MCP request's _meta.traceparent, else $TRACEPARENT (CLI calls only), else a new one.
The long-lived surfaces (the hub worker, the stdio MCP server) never read their environment's TRACEPARENT: it is the
one of the Bash call that started them, and every call they serve would join that one stale trace
(drop_inherited_trace() clears it when they start). Each call also
sends one span ("gateway.<tool>": tool, domain, surface, code, trace source, actor, times; never arguments or
results) to the local OTLP receiver (`traces otel serve`, which forwards to Phoenix), parented to the caller's span
from the traceparent, so a tool call joins the Claude Code trace that made it (core.otlp; ASSISTANT_OTLP=off stops it).
Direct mode per domain: a file ~/.config/tools/direct-<domain> (or direct-all) puts
that domain in "direct" mode: its calls answer `unavailable` with a hint to use the CLI, and the MCP server leaves
its tools out at the next start. `tool direct [DOMAIN on|off]` flips it; no restart needed for the refusal.
Env: TOOLS_STATE (state dir) · TOOLS_CONFIG (config dir: the direct switches) · TOOLS_APPROVALS_BIN (approvals CLI) ·
USAGE=off (no usage events).
"""

from __future__ import annotations

import math
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tools.core import REGISTRY, Format, JobTimes, Registry, Spec, ToolError, load

TRACEPARENT_RE = re.compile(r"[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")
# the traceparent of the gateway span a tool function runs under (set only while spec.fn runs), so a tool
# that starts a process (workflow.job) can hand it on and that process's spans join the same trace.
CURRENT_TP: ContextVar[str | None] = ContextVar("gateway_current_tp", default=None)
# Surfaces whose process is one call (`tool call`): its environment's TRACEPARENT is its caller's. Not "hub" or "mcp".
ENV_TRACE_SURFACES = frozenset({"cli"})
TRACE_ENV = ("TRACEPARENT", "TRACESTATE")
EXIT = {"invalid_args": 2, "approval_required": 4, "rate_limited": 5, "policy_denied": 6, "internal": 1}  # others: 3
RESERVED = ("_caller", "_approval")  # hub stamp + approval nonce: never part of a tool's input
KEEP_DAYS = 90
PENDING_STATES = frozenset({"queued", "running"})  # an async job not answered yet: the call logs code "pending"
OK_CODES = frozenset({"ok", "pending"})  # no error (spans, stats)
JOB_LOST_S = 3600  # an open job row the vault's list no longer holds (expired unseen, or a restart) after this: lost


def _home() -> Path:
    try:
        from core import paths  # the harness core (repo checkout, or ~/bin/lib/core when deployed)

        return paths.home()
    except ImportError:
        return Path.home()


def state_dir() -> Path:
    return Path(os.environ.get("TOOLS_STATE") or _home() / ".local" / "state" / "tools")


def config_dir() -> Path:
    return Path(os.environ.get("TOOLS_CONFIG") or _home() / ".config" / "tools")


def direct_domains() -> set[str]:
    """Domains switched to direct (CLI) mode: {"all"} or names, from the direct-* files in the config dir."""
    try:
        return {p.name[len("direct-") :] for p in config_dir().glob("direct-*") if p.is_file()}
    except OSError:
        return set()


def is_direct(domain: str, switched: set[str] | None = None) -> bool:
    sw = direct_domains() if switched is None else switched
    return "all" in sw or domain in sw


def set_direct(domain: str, on: bool) -> Path:
    """Switch DOMAIN (or "all") to direct mode (on) or back to the gateway (off); the switch file's path."""
    d = config_dir()
    p = d / f"direct-{domain}"
    if on:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        p.write_text("direct: the domain's typed tools refuse; use its CLI (`tool direct DOMAIN off` undoes it)\n")
    else:
        p.unlink(missing_ok=True)
    return p


def _usage():
    """~/bin/lib/usage.py (stdlib) when present; None in a bare checkout."""
    lib = os.environ.get("ASSISTANT_LIB") or str(_home() / "bin" / "lib")
    if lib not in sys.path:
        sys.path.append(lib)  # appended: the checkout's own core/ and tools/ stay first
    try:
        import usage  # pyright: ignore[reportMissingImports]  (~/bin/lib, not in the repo)

        return usage
    except Exception:
        return None


def trace_ctx(meta: dict[str, Any] | None = None, surface: str = "cli") -> tuple[str, str, str | None]:
    """(trace id, source, parent span id): meta = the MCP request's _meta.traceparent · env = $TRACEPARENT, read
    only on the CLI surface · new = none valid (then no parent)."""
    cands = [("meta", (meta or {}).get("traceparent"))]
    if surface in ENV_TRACE_SURFACES:
        cands.append(("env", os.environ.get("TRACEPARENT")))
    for src, tp in cands:
        m = TRACEPARENT_RE.fullmatch(str(tp or "").strip().lower())
        if m and m[1] != "0" * 32:
            return m[1], src, (m[2] if m[2] != "0" * 16 else None)
    return secrets.token_hex(16), "new", None


def trace_id(meta: dict[str, Any] | None = None, surface: str = "cli") -> tuple[str, str]:
    """(trace id, source): trace_ctx without the parent span."""
    tid, src, _ = trace_ctx(meta, surface)
    return tid, src


def drop_inherited_trace() -> list[str]:
    """For a long-lived process (the hub worker, the stdio MCP server): remove TRACEPARENT/TRACESTATE from its own
    environment, so neither the CLIs its tools start nor its replacement (worker respawn) inherit the trace of the
    Bash call that started it. Returns the names it removed."""
    return [k for k in TRACE_ENV if os.environ.pop(k, None) is not None]


def _span(
    name: str,
    domain: str,
    surface: str,
    tid: str,
    tsrc: str,
    parent: str | None,
    t_start: float,
    code: str,
    actor: str | None,
    span_id: str | None = None,
) -> None:
    """One span per call to the local receiver; never raises, never slows a call by more than the emit timeout."""
    try:
        from core import otlp

        attrs = {
            "gateway.tool": name,
            "gateway.domain": domain,
            "gateway.surface": surface,
            "gateway.code": code,
            "gateway.trace_src": tsrc,
            "gateway.actor": actor,
        }
        sp = otlp.make_span(
            f"gateway.{name}", tid, parent, t_start, time.time(), attrs, error=code not in OK_CODES, span_id=span_id
        )
        otlp.emit([sp], "assistant-gateway")
    except Exception:
        pass


@dataclass
class Result:
    ok: bool
    tool: str
    surface: str
    trace_id: str
    trace_src: str
    ms: int
    data: dict[str, Any] | None = None
    text: str = ""
    error: dict[str, str] | None = None
    known: bool = True  # False: no such tool (exit 2, like a usage error)

    @property
    def code(self) -> str:
        return "ok" if self.ok else (self.error or {}).get("code", "internal")

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else (EXIT.get(self.code, 3) if self.known else 2)

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "tool": self.tool}
        if self.ok:
            out["data"] = self.data
            out["text"] = self.text
        else:
            out["error"] = self.error
        out["trace_id"] = self.trace_id
        out["ms"] = self.ms
        return out

    def error_text(self) -> str:
        e = self.error or {}
        return f"{e.get('code')}: {e.get('message')}" + (f" (hint: {e['hint']})" if e.get("hint") else "")


def _db() -> sqlite3.Connection:
    d = state_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = d / "gateway.db"
    new = not p.exists()
    c = sqlite3.connect(p, timeout=5)
    if new:
        os.chmod(p, 0o600)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL, tool TEXT NOT NULL, surface TEXT, trace TEXT,"
        " trace_src TEXT, ms INTEGER, code TEXT, actor TEXT)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS calls_tool_ts ON calls (tool, ts)")
    c.execute(
        "CREATE TABLE IF NOT EXISTS jobs (job TEXT PRIMARY KEY, tool TEXT NOT NULL, ts REAL NOT NULL, trace TEXT,"
        " state TEXT NOT NULL, created REAL, ended REAL, s REAL, seen REAL NOT NULL)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS jobs_tool_ended ON jobs (tool, ended)")
    return c


def _rate_ok(spec: Spec) -> bool:
    lim = spec.rate_limit
    if not lim:
        return True
    n, window = lim
    try:
        with _db() as c:
            (seen,) = c.execute(
                "SELECT count(*) FROM calls WHERE tool = ? AND ts > ? AND code NOT IN ('rate_limited', 'invalid_args')",
                (spec.name, time.time() - window),
            ).fetchone()
        return seen < n
    except sqlite3.Error:
        return True  # a broken log never blocks a call


def _approved(spec: Spec, nonce: str | None) -> None:
    """Owner-approval tools run only with a nonce he approved (`approvals`), consumed here once."""
    action = f"tool:{spec.name}"
    hint = f"`approvals new --action {action} --summary …` sends him the ✅ card; pass its nonce as approval"
    if not nonce:
        raise ToolError("approval_required", f"{spec.name} needs his ✅ first", hint)
    exe = os.environ.get("TOOLS_APPROVALS_BIN") or str(_home() / "bin" / "approvals")
    try:
        r = subprocess.run(
            [exe, "consume", nonce, "--action", action, "--by", "gateway"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ToolError("unavailable", f"approvals: {type(e).__name__}", "retry; `approvals status` checks it") from e
    if r.returncode != 0:
        why = {3: "expired", 6: "still pending"}.get(r.returncode, "declined, used or unknown")
        raise ToolError("approval_required", f"approval {why}", hint)


def _log(
    spec_name: str, feature: str, surface: str, tid: str, tsrc: str, ms: int, code: str, actor: str | None = None
) -> str | None:
    """The call's row and usage event; returns the actor (given, else usage's caller_actor) or None."""
    u = _usage()
    try:
        if u is not None:
            actor = actor or u.caller_actor()
            u.log(
                feature,
                "invoke",
                actor=actor,
                ms=ms,
                outcome=code,
                attrs={"via": "gateway", "tool": spec_name, "surface": surface, "trace": tid, "code": code},
                src="gateway",
            )
    except Exception:
        pass
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO calls (ts, tool, surface, trace, trace_src, ms, code, actor) VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), spec_name, surface, tid, tsrc, ms, code, actor),
            )
            if random.random() < 0.01:
                c.execute("DELETE FROM calls WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
    except sqlite3.Error:
        pass
    return actor


def _duration(t: JobTimes) -> float | None:
    """Job start -> finished in s: ended - created (the vault's ISO stamps, 1 s resolution), else its elapsed_s;
    None while the job runs."""
    if t.state in PENDING_STATES:
        return None
    if t.created is not None and t.ended is not None:
        return max(0.0, t.ended - t.created)
    return t.elapsed_s if t.ended is not None or t.state == "done" else None


def _job_row(c: sqlite3.Connection, tool: str | None, t: JobTimes, tid: str | None = None) -> int:
    """Insert (tool given: the call that started the job) or update the job's row; the changed row count."""
    now = time.time()
    vals = (t.state, t.created, t.ended, _duration(t), now, t.job)
    if tool is None:
        return c.execute(
            "UPDATE jobs SET state = ?, created = coalesce(?, created), ended = coalesce(?, ended),"
            " s = coalesce(?, s), seen = ? WHERE job = ?",
            vals,
        ).rowcount
    return c.execute(
        "INSERT INTO jobs (state, created, ended, s, seen, job, tool, ts, trace) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(job) DO UPDATE SET state = excluded.state, created = coalesce(excluded.created, jobs.created),"
        " ended = coalesce(excluded.ended, jobs.ended), s = coalesce(excluded.s, jobs.s), seen = excluded.seen",
        (*vals, tool, now, tid),
    ).rowcount


def _log_job(tool: str, t: JobTimes, tid: str) -> None:
    """The job log row of an answer that reports on a job (never raises: a broken log never fails a call)."""
    try:
        with _db() as c:
            _job_row(c, tool if t.started else None, t, tid)
            if random.random() < 0.01:
                c.execute("DELETE FROM jobs WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
    except sqlite3.Error:
        pass


def open_jobs() -> int:
    """How many job log rows are still queued or running (0 when the log is missing)."""
    try:
        with _db() as c:
            return int(c.execute("SELECT count(*) FROM jobs WHERE state IN ('queued', 'running')").fetchone()[0])
    except sqlite3.Error:
        return 0


def sweep_jobs(lister: Callable[[], list[JobTimes]] | None = None) -> dict[str, int]:
    """Close the open job log rows from the backend's job list (default: the vault's, one `vault job list` call,
    made only while a row is open): state and times as the list has them; an open row the list no longer holds
    after JOB_LOST_S becomes "lost" (never counted as an answer). Counts only; raises when the list fails."""
    out = {"open": 0, "closed": 0, "lost": 0, "still_open": 0}
    with _db() as c:
        rows = c.execute("SELECT job, ts FROM jobs WHERE state IN ('queued', 'running')").fetchall()
    out["open"] = len(rows)
    if not rows:
        return out
    if lister is None:
        from tools.domains import vault  # the only async backend so far

        lister = vault.job_list
    held = {t.job: t for t in lister()}
    now = time.time()
    with _db() as c:
        for jid, ts in rows:
            t = held.get(jid)
            if t is not None:
                _job_row(c, None, t)
                out["closed" if t.state not in PENDING_STATES else "still_open"] += 1
            elif now - ts > JOB_LOST_S:
                c.execute("UPDATE jobs SET state = 'lost', seen = ? WHERE job = ?", (now, jid))
                out["lost"] += 1
            else:
                out["still_open"] += 1
    return out


def jobs(days: float = 7.0) -> dict[str, Any]:
    """The job log per starting tool over DAYS: rows by state, finished-job durations (p50/p95/max s). Counts and
    timings only."""
    since = time.time() - days * 86400
    out: dict[str, Any] = {"days": days, "tools": {}}
    try:
        with _db() as c:
            rows = c.execute("SELECT tool, state, s FROM jobs WHERE ts > ? ORDER BY tool", (since,)).fetchall()
    except sqlite3.Error:
        return out
    for tool, state, dur in rows:
        t = out["tools"].setdefault(tool, {"jobs": 0, "states": {}, "s": []})
        t["jobs"] += 1
        t["states"][state] = t["states"].get(state, 0) + 1
        if state == "done" and dur is not None:
            t["s"].append(float(dur))
    for t in out["tools"].values():
        xs = sorted(t.pop("s"))
        t["done_n"] = len(xs)
        t["p50_s"] = xs[len(xs) // 2] if xs else None
        t["p95_s"] = xs[min(len(xs) - 1, max(0, math.ceil(0.95 * len(xs)) - 1))] if xs else None  # nearest rank
        t["max_s"] = xs[-1] if xs else None
    return out


def call(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    surface: str = "cli",
    meta: dict[str, Any] | None = None,
    approval: str | None = None,
    registry: Registry | None = None,
    actor: str | None = None,
    consume: bool = False,
) -> Result:
    """Run tool NAME (registry or MCP name) with ARGS; never raises. The hub's executor passes actor (its caller's,
    for usage) and consume (the hub's policy allowed the call only because an approval came: consume it here)."""
    t0, wall0 = time.monotonic(), time.time()
    args = {k: v for k, v in (args or {}).items() if k not in RESERVED}
    tid, tsrc, parent = trace_ctx(meta, surface)
    sid = secrets.token_hex(8)
    reg = registry or load()
    spec = reg.get(name)
    data, text, err = None, "", None
    jt: JobTimes | None = None
    feature = reg.domains[spec.domain].feature if spec else "gateway"
    try:
        if spec is None:
            raise ToolError("not_found", f"no tool {name!r}", "`tool ls` lists them")
        if is_direct(spec.domain):
            raise ToolError(
                "unavailable",
                f"{spec.domain} tools are switched to direct mode",
                f"use the CLI (`tool docs {spec.domain}`, section CLI and cron); `tool direct {spec.domain} off` "
                "switches back",
            )
        try:
            inp = spec.input.model_validate(args or {})
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "input"
            raise ToolError("invalid_args", f"{where}: {first.get('msg')}", f"`tool show {spec.name}`") from None
        if not _rate_ok(spec):
            raise ToolError("rate_limited", f"{spec.name}: over {spec.rate}", "wait, or batch the work")
        if spec.approval == "owner" or consume:
            _approved(spec, approval)
        tok = CURRENT_TP.set(f"00-{tid}-{sid}-01")
        try:
            out = spec.fn(inp)
        finally:
            CURRENT_TP.reset(tok)
        if not isinstance(out, spec.output):
            raise ToolError("internal", f"{spec.name} returned {type(out).__name__}", "a bug: report it")
        fmt: Format = "detailed" if getattr(inp, "format", "concise") == "detailed" else "concise"
        data, text = out.data(fmt), out.render(fmt)
        try:
            jt = out.job_times()
        except Exception:  # the job log is bookkeeping: it never fails a call
            jt = None
    except ToolError as e:
        err = e.as_dict()
    except subprocess.TimeoutExpired:
        err = ToolError("timeout", f"{name}: backend timed out", "retry once").as_dict()
    except Exception as e:  # a bug: the type only, never the message (it may carry arguments)
        err = ToolError("internal", f"{name}: {type(e).__name__}", "a bug: report it").as_dict()
    ms = int((time.monotonic() - t0) * 1000)
    shown = spec.name if spec else name[:64]
    res = Result(err is None, shown, surface, tid, tsrc, ms, data, text, err, known=spec is not None)
    code = "pending" if res.ok and jt is not None and jt.state in PENDING_STATES else res.code
    actor = _log(shown, feature, surface, tid, tsrc, ms, code, actor)
    _span(shown, spec.domain if spec else "unknown", surface, tid, tsrc, parent, wall0, code, actor, sid)
    if res.ok and jt is not None:
        _log_job(shown, jt, tid)
    return res


def stats(days: float = 7.0) -> dict[str, Any]:
    """Per tool: calls, errors by code, p50/p95 ms; trace sources (meta / env / new)."""
    since = time.time() - days * 86400
    out: dict[str, Any] = {"days": days, "tools": {}, "trace_src": {}}
    try:
        with _db() as c:
            rows = c.execute(
                "SELECT tool, ms, code, trace_src, surface FROM calls WHERE ts > ? ORDER BY tool", (since,)
            ).fetchall()
    except sqlite3.Error:
        return out
    for tool, ms, code, tsrc, surface in rows:
        t = out["tools"].setdefault(tool, {"calls": 0, "errors": {}, "pending": 0, "ms": [], "surface": {}})
        t["calls"] += 1
        t["ms"].append(ms or 0)
        t["surface"][surface] = t["surface"].get(surface, 0) + 1
        if code == "pending":
            t["pending"] += 1
        elif code != "ok":
            t["errors"][code] = t["errors"].get(code, 0) + 1
        out["trace_src"][tsrc] = out["trace_src"].get(tsrc, 0) + 1
    for t in out["tools"].values():
        ms = sorted(t.pop("ms"))
        t["p50_ms"] = ms[len(ms) // 2]
        t["p95_ms"] = ms[min(len(ms) - 1, int(len(ms) * 0.95))]
    return out


__all__ = [
    "REGISTRY",
    "Result",
    "call",
    "direct_domains",
    "drop_inherited_trace",
    "is_direct",
    "jobs",
    "open_jobs",
    "set_direct",
    "stats",
    "sweep_jobs",
    "trace_ctx",
    "trace_id",
]
