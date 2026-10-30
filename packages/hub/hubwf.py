"""hubwf: durable workflows and schedules inside hubd, on DBOS Transact over the hub's Postgres.

hubd imports this only while the engine is switched on (DBOS, SQLAlchemy and their deps ship in the release's vendor/).
DBOS keeps its system tables in schema HUB_WF_SCHEMA (default "dbos") of the hub database, on its own small
SQLAlchemy pool (POOL); the password comes from hubd's secret loader as a connect argument, never inside a URL.
Workflows are plain (sync) functions on DBOS's executor threads; a step that needs hubd (its store, the gateway) runs
the coroutine on hubd's loop (LOOP, run_coroutine_threadsafe) through HOOKS, which hubd fills. The application
version is pinned (WF_VERSION): a hub release that changes this file still recovers what the previous one left
pending. Bump it only for a change that would replay an old workflow differently (and say so in the commit).

Determinism (ops/lint/wf_determinism.py, a CI test): a workflow body only computes on its arguments and on what its
steps and DBOS calls return; clocks, randomness, files, the network, HOOKS, LOOP and the environment belong in steps.

Workflows the brain may start (NAMES, `POST /wf/start`):
  webtask.approval  the web-task approval wait. args {task, stop, nonce?, ttl (30-3600 s, default 900),
                    notify ["expired"]?}. The deadline is a step's output (recorded once: a restart keeps it). It waits
                    for a message on topic "decision" ({"decision": approved|declined|stopped|replaced|cancelled,
                    "via"?}) in recv chunks of CHUNK s; between chunks it reads the hub-held approval for `nonce` (his
                    tap is decided on the hub). Result
                    {outcome, via, waited_s, task, stop}; an expiry with "expired" in notify emits one ledger event
                    workflow.webtask (idem key wf:<workflow id>).
  webtask.run       a whole web task as one workflow (id webtask:<task>; the brain's webagent starts it at
                    `webagent start`). args {task, kind?, dry?,
                    pending_ttl (900), approved_ttl (600), idle (2700), revive (7200), wait_max (3600), notify
                    ["stale", "expired"]?}. Phases: running (lease: the agent's abx window, steps) -> pending (a stop
                    before the commit: the approval wait, 15 min, reading the hub-held approval between recv
                    chunks like webtask.approval) -> approved -> executing (the browser service passed the one
                    committing action on a consumed token) -> done | failed, or aborted | declined | expired | stale.
                    Messages on topic "task" ({"op": lease|step|wait|stop|decision|commit|done|abort|revive, ...}: ids,
                    counts and states only, never page text) move it; its clock moves the deadlines (a running task
                    goes stale after `idle` s without a message, a wait for his vault ✅ pauses that; stale ends after
                    `revive` s unless revived). Event "phase" = the last step boundary (a restart resumes there: DBOS
                    replays the recorded messages and clock reads). The end writes one ledger row workflow.webtask (the
                    receipt; idem key wf:<workflow id>): delivery beat for an outcome in notify, mute otherwise; dry
                    runs write none. Advisory: webagent's own state machine stays the authority.
  hub.probe         smoke test: waits up to ttl s (1-600, default 30) for one message on topic "ping".
  reminder.due      one pending reminder slot's durable timer. args {rid (8 hex), at (its slot, home wall time
                    "YYYY-MM-DD HH:MM"), ts (the due instant, epoch s: the first instant whose home wall minute is
                    >= at, so DST gaps and repeats read like remind's own due pass)}; id rem:<rid>:<YYYYMMDDHHMM>
                    (":2"-":9" after a cancelled one). It sleeps (DBOS.sleep: durable, a restart keeps the wake time)
                    until ts + REM_PAD, then asks the brain to fire it (tool reminders.fire, caller hub = principal
                    reflex). The brain's remind store decides (single source of truth: fired, gone or
                    held), so a repeat or a stale timer never sends twice. A call that did not reach the brain,
                    or an early wake, is retried every REM_RETRY s, REM_TRIES times; then the brain's sweep has it.
                    Result {rid, at, decision, mode, woke_s, tries}. The brain starts and cancels these (`remind hub`).
  event.window      one event window's clock (a night out, a party, a drive, a guarded sleep-in morning). args
                    {kind (night | party | drive | sleepin), key (the brain's id for it: event-card's night key, the
                    morning's date, ...), start, end (epoch s; end - start <= EVW_SPAN_MAX), every (60-900 s, a
                    multiple of 60; default 60)}; id evw:<kind>:<12 hex of sha256(key)>:<start, UTC YYYYMMDDHHMM>
                    (":2"-":9" after a cancelled one). It sleeps (durably) until start, then once per `every` s, on
                    the minute grid + EVW_PAD, asks the brain for one tick (tool workflow.window {kind, key, at = the
                    slot}, caller hub = principal reflex). The brain decides everything: which registry jobs are due
                    at that minute and whether the window is still open; it starts them. Its answer active (optionally
                    next_s: the next tick it needs, at least `every` away) keeps the clock going; done | off | gone end
                    it; unavailable | error count as a miss and the clock goes on. `end` is a hard cap: the brain's
                    own window check ends it first.
                    Result {kind, key, ticks, misses, outcome (done | off | gone | capped), last}. The brain starts
                    and cancels these (`eventwf sync`).
Scheduled: job.tick(at, context {job, mode, timeout?}) asks the brain to run job `job` through the gateway
(tool workflow.job, caller "hub" = principal reflex: Cedar decides and logs it like any call) on the schedule's queue
(QUEUES: jobs-critical 2 · jobs-fast 4 · jobs-slow 2, so a slow report never delays an every-minute job; hub-jobs,
concurrency 1, for a schedule that names no queue). mode shadow = the tool only says what it
would run; live = it runs it. Schedules are DBOS schedules (cron, the brain's time zone) the brain creates, pauses,
resumes and removes (`hub wf watches`, tool workflow.schedule); the names start with SCHED_PREFIX.
Time limit: a schedule made with `timeout` (1-JOB_TIMEOUT_MAX s: the registry job's limit) passes it to the
tool as timeout_s, so the brain ends the job by then, and the hub waits that long plus grace (job_wait) before the
call counts as lost; one made without it waits JOB_LIMIT and passes nothing.
Catch-up: DBOS starts each schedule from "now" at a launch, so a tick that fell while the engine was down (a hub
restart or deploy, Postgres down, the switch off) never fires. catch_up(), right after a launch, runs each active
schedule's latest missed tick once, late, as the DBOS backfill of that one slot (the scheduler's own workflow id
sched-<name>-<slot>, so a second launch, or the scheduler itself, never runs it twice), when: catch-up is on for it
(context catch_up true|false, the registry's; absent = on for a cadence of a day or longer, CATCHUP_DAILY; never for a
cadence under CATCHUP_HOURLY), the tick is at most min(cadence/2, CATCHUP_MAX) old, nothing of the schedule fired or
ran since the tick, and the tick is its own: the context's `since` (set when its cron, zone or mode was set; a same
replace keeps it) is older, or, for one made before `since` existed, it fired before or has an older run. Older
missed ticks are never run: one catch-up per schedule at most.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID

WF_VERSION = "hubwf-1"
APP = "claude-hub"
POOL = 4
# recv chunk (s) between hub-approval reads; HUB_WF_CHUNK (1-60) for tests. Only a recv timeout: replay-safe to change.
CHUNK = max(1.0, min(60.0, float(os.environ.get("HUB_WF_CHUNK") or 30)))
STEP_TIMEOUT = 15.0
JOB_LIMIT = 600  # a schedule without its own timeout: the tool's own limit (workflow.job timeout_s 600)
JOB_TIMEOUT_MAX = 3600  # the longest job a schedule may wait for (hubwfapi.JOB_TIMEOUT_MAX says the same)
JOB_GRACE = 60.0  # on top of the job's limit: the gateway's 20 s answer grace + the result's way back
JOB_TIMEOUT = JOB_LIMIT + JOB_GRACE  # a schedule without its own timeout waits this long (660 s)
NAMES = ("webtask.approval", "webtask.run", "hub.probe", "reminder.due", "event.window")
DECISIONS = ("approved", "declined", "stopped", "replaced", "cancelled")
HUB_STATES = {"approved": "approved", "used": "approved", "denied": "declined", "cancelled": "cancelled",
              "expired": "expired"}  # fmt: skip
TTL_DEFAULT, TTL_MIN, TTL_MAX = 900, 30, 3600
JOB_QUEUE = "hub-jobs"  # concurrency 1: the queue of a schedule that names no class
# per-class queues (core.schedules.QUEUES); the brain's tool worker has a thread for each slot + spare ones
QUEUES = {JOB_QUEUE: 1, "jobs-critical": 2, "jobs-fast": 4, "jobs-slow": 2}
SCHED_PREFIX = "watch:"  # the watch jobs
SCHED_PREFIXES = (SCHED_PREFIX, "job:")  # job:<name> = any other registry job
JOB_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MODES = ("shadow", "live")
# catch-up (see the docstring): a missed tick runs at most this late (s; grace = min(cadence/2, it));
# HUB_WF_CATCHUP_MAX (1-21600) for tests
CATCHUP_MAX = max(1.0, min(21600.0, float(os.environ.get("HUB_WF_CATCHUP_MAX") or 21600)))
CATCHUP_DAILY = 20 * 3600.0  # a cadence this long or longer (daily or slower; a DST day is 23 h): on by default
CATCHUP_HOURLY = 3000.0  # catch_up true needs ticks this far apart at least (an hour): minute-level ones never
CADENCE_TICKS = 12  # the cadence = the shortest gap among this many ticks from the missed one

# Filled by hubd before launch: the loop its steps run coroutines on, and the hooks they call.
LOOP: asyncio.AbstractEventLoop | None = None
HOOKS: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}


def _on_loop(coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
    loop = LOOP
    if loop is None or loop.is_closed():
        coro.close()
        raise RuntimeError("hubwf: hubd's loop is not set")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


# ------------------------------------------------------------------------------------------------ pure helpers


def webtask_args(a: Any) -> dict[str, Any]:
    """The webtask.approval arguments, checked and normalised (pure: a workflow calls it)."""
    if not isinstance(a, dict):
        raise ValueError("args: an object")
    task = a.get("task")
    if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task):
        raise ValueError("task: the web task id")
    stop = a.get("stop", 1)
    if not isinstance(stop, int) or isinstance(stop, bool) or not 0 <= stop <= 1000:
        raise ValueError("stop: the stop number (0-1000)")
    nonce = a.get("nonce")
    if nonce is not None and (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)):
        raise ValueError("nonce: 32 hex characters or null")
    ttl = a.get("ttl", TTL_DEFAULT)
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not TTL_MIN <= ttl <= TTL_MAX:
        raise ValueError(f"ttl: {TTL_MIN}-{TTL_MAX} seconds")
    notify = a.get("notify") or []
    if not isinstance(notify, list) or any(x not in ("expired",) for x in notify):
        raise ValueError('notify: a list of outcomes to report as ledger events (only "expired")')
    return {"task": task, "stop": stop, "nonce": nonce, "ttl": float(ttl), "notify": list(notify)}


def decision_of(msg: Any) -> tuple[str, str] | None:
    """A decision message -> (outcome, via), else None (pure)."""
    if isinstance(msg, dict) and msg.get("decision") in DECISIONS:
        via = msg.get("via")
        return str(msg["decision"]), (via[:40] if isinstance(via, str) and via else "message")
    return None


# -- webtask.run: the task's state machine, pure (the workflow feeds it messages and clock reads)
RUN_KINDS = ("form", "cancel", "booking", "order", "order-check", "account", "unsubscribe", "other")
RUN_OPS = ("lease", "step", "wait", "stop", "decision", "commit", "done", "abort", "revive")
RUN_END = ("done", "failed", "aborted", "declined", "expired", "stale")
RUN_NOTIFY = ("stale", "expired", "failed")
RUN_CHUNK = 300.0  # recv cap outside an approval wait: bounds how far a replayed recv can overshoot a deadline
RUN_LIMITS = {"pending_ttl": (30, 3600, 900), "approved_ttl": (30, 3600, 600), "idle": (60, 14400, 2700),
              "revive": (0, 14400, 7200), "wait_max": (0, 3600, 3600)}  # fmt: skip
SID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


def webtask_run_args(a: Any) -> dict[str, Any]:
    """The webtask.run arguments, checked and normalised (pure)."""
    if not isinstance(a, dict):
        raise ValueError("args: an object")
    task = a.get("task")
    if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task):
        raise ValueError("task: the web task id")
    kind = a.get("kind", "other")
    if kind not in RUN_KINDS:
        raise ValueError(f"kind: one of {', '.join(RUN_KINDS)}")
    dry = a.get("dry", False)
    if not isinstance(dry, bool):
        raise ValueError("dry: true or false")
    out: dict[str, Any] = {"task": task, "kind": kind, "dry": dry}
    for k, (lo, hi, dflt) in RUN_LIMITS.items():
        v = a.get(k, dflt)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"{k}: {lo}-{hi} seconds")
        out[k] = float(v)
    notify = a.get("notify", ["stale", "expired"])
    if not isinstance(notify, list) or any(x not in RUN_NOTIFY for x in notify):
        raise ValueError(f"notify: a list of outcomes to report as beat events ({', '.join(RUN_NOTIFY)})")
    out["notify"] = [x for x in RUN_NOTIFY if x in notify]
    return out


def _int(v: Any, lo: int = 0, hi: int = 100_000) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def run_init(t0: float) -> dict[str, Any]:
    return {"phase": "running", "steps": 0, "stop": 0, "nonce": None, "lease": None, "last": t0, "wait_until": None,
            "deadline": None, "committed": False, "revived": 0, "outcome": None, "via": ""}  # fmt: skip


def run_view(s: dict[str, Any]) -> dict[str, Any]:
    """The "phase" event: the last step boundary (ids, counts, states and times only)."""
    return {k: s[k] for k in ("phase", "steps", "stop", "lease", "committed", "revived", "deadline", "outcome")}


def run_idle_at(s: dict[str, Any], a: dict[str, Any]) -> float:
    """When a running/executing task goes stale: idle s after the last message, or after his vault ✅ wait ends."""
    return max(s["last"], s["wait_until"] or 0.0) + a["idle"]


def run_due(s: dict[str, Any], a: dict[str, Any]) -> float:
    """The next deadline of the current phase."""
    if s["phase"] in ("running", "executing"):
        return run_idle_at(s, a)
    return float(s["deadline"] or 0.0)


def _end(s: dict[str, Any], outcome: str, via: str) -> dict[str, Any]:
    return {**s, "phase": outcome, "outcome": outcome, "via": via[:40], "deadline": None}


def run_next(s: dict[str, Any], msg: Any, now: float, a: dict[str, Any]) -> dict[str, Any]:
    """One transition (pure): a message on topic "task", or None = the clock (deadlines). Unknown or out-of-phase
    messages change nothing (webagent's state machine is the authority; this mirrors it)."""
    if s["outcome"] is not None:
        return s
    ph = s["phase"]
    if msg is None:
        if ph in ("running", "executing") and now >= run_idle_at(s, a):
            if a["revive"] <= 0 or ph == "executing":
                return _end(s, "stale", "idle")
            return {**s, "phase": "stale", "deadline": now + a["revive"]}
        if ph in ("pending", "approved") and now >= (s["deadline"] or 0):
            return _end(s, "expired", "deadline")
        if ph == "stale" and now >= (s["deadline"] or 0):
            return _end(s, "stale", "revive window over")
        return s
    if not isinstance(msg, dict) or msg.get("op") not in RUN_OPS:
        return s
    op = msg["op"]
    if op == "abort":
        reason = msg.get("reason")
        return _end(s, "aborted", reason if isinstance(reason, str) and reason else "abort")
    if op == "revive":
        return {**s, "phase": "running", "last": now, "wait_until": None, "deadline": None,
                "revived": s["revived"] + 1} if ph == "stale" else s  # fmt: skip
    if ph == "stale":
        return s
    if op == "lease":
        sid = msg.get("sid")
        return {**s, "lease": sid if isinstance(sid, str) and SID_RE.fullmatch(sid) else "?", "last": now}
    if op == "step":
        n = _int(msg.get("n"))
        return {**s, "steps": max(s["steps"], n) if n is not None else s["steps"] + 1, "last": now}
    if op == "wait":
        until = msg.get("until")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now:
            return {**s, "wait_until": min(float(until), now + a["wait_max"])}
        return {**s, "wait_until": None, "last": now}  # the wait is over: the idle clock restarts now
    if op == "stop":
        n = _int(msg.get("stop"), 1)
        if ph not in ("running", "pending") or n is None:
            return s
        nonce = msg.get("nonce")
        nonce = nonce if isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{32}", nonce) else None
        return {**s, "phase": "pending", "stop": n, "nonce": nonce, "deadline": now + a["pending_ttl"], "last": now,
                "wait_until": None}  # fmt: skip
    if op == "decision":
        d = decision_of(msg)
        if d is None or ph != "pending" or _int(msg.get("stop"), 1) not in (None, s["stop"]):
            return s
        return run_decided(s, d[0], d[1], now, a)
    if op == "commit":
        if ph != "approved" or _int(msg.get("stop"), 1) not in (None, s["stop"]):
            return s
        return {**s, "phase": "executing", "committed": True, "last": now, "deadline": None}
    if op == "done":
        ok, cont = msg.get("ok") is not False, msg.get("cont") is True
        committed = s["committed"] or msg.get("committed") is True
        if cont and ok:
            return {**s, "phase": "running", "committed": committed, "last": now, "deadline": None, "nonce": None}
        return _end({**s, "committed": committed}, "done" if ok else "failed", "webagent")
    return s


def run_decided(s: dict[str, Any], decision: str, via: str, now: float, a: dict[str, Any]) -> dict[str, Any]:
    """The pending stop's approval ended (a message from webagent, or the hub-held approval's state)."""
    if decision == "approved":
        return {**s, "phase": "approved", "deadline": now + a["approved_ttl"], "via": via[:40]}
    if decision in ("declined", "stopped"):
        return _end(s, "declined" if decision == "declined" else "aborted", via)
    if decision == "expired":
        return _end(s, "expired", via)
    return s  # replaced / cancelled: the next stop message opens the new wait


def run_receipt(s: dict[str, Any], a: dict[str, Any], wid: str, waited: float) -> dict[str, Any]:
    """The ledger row at the end (pure): ids, counts and the outcome; beat only for an outcome he set in notify."""
    out = s["outcome"]
    beat = out in a["notify"]
    nxt = {"stale": f"webagent sweep, then close the task's tab (webagent status {a['task']})",
           "expired": f"webagent sweep, then close the task's tab (webagent status {a['task']})",
           "failed": f"webagent status {a['task']}: tell him what did not work"}  # fmt: skip
    return {
        "kind": "workflow.webtask",
        "source": "hubwf",
        "subject": f"web task {a['task']}: {out}" + (" (committed)" if s["committed"] else ""),
        "next": nxt.get(out, "") if beat else "",
        "payload": {
            "task": a["task"],
            "kind": a["kind"],
            "outcome": out,
            "via": s["via"],
            "steps": s["steps"],
            "stops": s["stop"],
            "committed": s["committed"],
            "revived": s["revived"],
            "workflow": wid,
            "waited_s": round(waited, 1),
        },  # fmt: skip
        "delivery": "beat" if beat else "mute",
        "idem_key": f"wf:{wid}",
    }


def probe_args(a: Any) -> float:
    ttl = (a or {}).get("ttl", 30) if isinstance(a, dict) else 30
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 1 <= ttl <= 600:
        raise ValueError("ttl: 1-600 seconds")
    return float(ttl)


REM_ID = re.compile(r"^[0-9a-f]{8}$")
REM_AT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
REM_PAD = 1.0  # wake this long after the due instant: the brain's clock reads whole minutes, so it is due by then
REM_TIMEOUT = 300  # reminders.fire's limit: a --call reminder rings (up to 150 s) before its text goes out
REM_TRIES = 6  # fire attempts while the brain can't take the call (or the wake was early); then its sweep has it
# the wait between fire attempts; HUB_WF_REM_RETRY (1-600) for tests. A DBOS.sleep length: replay-safe to change.
REM_RETRY = max(1.0, min(600.0, float(os.environ.get("HUB_WF_REM_RETRY") or 60)))
REM_AGAIN = ("unavailable", "error", "early")  # not a decision of the store: try again after REM_RETRY


def reminder_args(a: Any) -> dict[str, Any]:
    """The reminder.due arguments, checked and normalised (pure: the workflow calls it)."""
    if not isinstance(a, dict):
        raise ValueError("args: an object")
    rid, at, ts = a.get("rid"), a.get("at"), a.get("ts")
    if not isinstance(rid, str) or not REM_ID.match(rid):
        raise ValueError("rid: the reminder's 8-hex ID")
    if not isinstance(at, str) or not REM_AT.match(at):
        raise ValueError('at: its slot, "YYYY-MM-DD HH:MM" (home wall time)')
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not 1e9 < ts < 1e10:
        raise ValueError("ts: the due instant (epoch seconds)")
    return {"rid": rid, "at": at, "ts": float(ts)}


def reminder_wid(rid: str, at: str) -> str:
    """The timer's canonical workflow id: rem:<rid>:<YYYYMMDDHHMM> (pure)."""
    return f"rem:{rid}:{re.sub(r'[^0-9]', '', at)}"


EVW_KINDS = ("night", "party", "drive", "sleepin")
EVW_KEY = re.compile(r"^[A-Za-z0-9@._:+-]{1,120}$")
EVW_SPAN_MAX = 48 * 3600  # a window (incl. a night's recap until the next evening) never spans more than this
EVW_PAD = 2.0  # each tick lands this long after its minute (cron's own lag): the brain reads whole minutes
EVW_TIMEOUT = 60  # workflow.window's limit: the brain starts the due jobs detached and answers at once
EVW_END = ("done", "off", "gone")  # the brain closed the window (or its switch is off, or the window is unknown)
# the grid `every` lives on (s): a minute; HUB_WF_EVW_MIN (1-60) for tests. Only refuses new args: replay-safe.
EVW_MIN = max(1, min(60, int(os.environ.get("HUB_WF_EVW_MIN") or 60)))


def window_args(a: Any) -> dict[str, Any]:
    """The event.window arguments, checked and normalised (pure: the workflow calls it)."""
    if not isinstance(a, dict):
        raise ValueError("args: an object")
    kind, key, start, end, every = a.get("kind"), a.get("key"), a.get("start"), a.get("end"), a.get("every", 60)
    if kind not in EVW_KINDS:
        raise ValueError(f"kind: one of {', '.join(EVW_KINDS)}")
    if not isinstance(key, str) or not EVW_KEY.match(key):
        raise ValueError("key: 1-120 chars of A-Z a-z 0-9 @ . _ : + -")
    ts: list[float] = []
    for k, v in (("start", start), ("end", end)):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 1e9 < v < 1e10:
            raise ValueError(f"{k}: epoch seconds")
        ts.append(float(v))
    t0, t1 = ts
    if not t0 < t1 <= t0 + EVW_SPAN_MAX:
        raise ValueError(f"end: after start, at most {EVW_SPAN_MAX // 3600} h later")
    if isinstance(every, bool) or not isinstance(every, int) or not EVW_MIN <= every <= 900 or every % EVW_MIN:
        raise ValueError(f"every: {EVW_MIN}-900 s, a multiple of {EVW_MIN}")
    return {"kind": kind, "key": key, "start": t0, "end": t1, "every": every}


def window_wid(kind: str, key: str, start: float) -> str:
    """The window's canonical workflow id: evw:<kind>:<12 hex of sha256(key)>:<start minute, UTC> (pure)."""
    import hashlib

    h = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"evw:{kind}:{h}:{datetime.fromtimestamp(start, UTC).strftime('%Y%m%d%H%M')}"


def window_next(now: float, every: int, next_s: Any, end: float) -> float:
    """The next tick's slot (pure): the next `every` boundary after now, or the one the brain asked for (next_s,
    at least `every` away), never past `end` (the last tick lands on the cap itself)."""
    nxt = (now // every + 1) * every
    if isinstance(next_s, (int, float)) and not isinstance(next_s, bool) and next_s > every:
        nxt = max(nxt, ((now + next_s) // every) * every)
    return min(nxt, end)


def check_args(name: str, args: Any) -> None:
    """Refuse bad arguments before a workflow starts (the workflow checks them again: a refusal there is an error)."""
    if name == "webtask.approval":
        webtask_args(args)
    elif name == "webtask.run":
        webtask_run_args(args)
    elif name == "hub.probe":
        probe_args(args)
    elif name == "reminder.due":
        reminder_args(args)
    elif name == "event.window":
        window_args(args)
    else:
        raise ValueError(f"name: one of {', '.join(NAMES)}")


# ------------------------------------------------------------------------------------------------ steps


@DBOS.step(name="hubwf.clock")
def clock() -> float:
    return time.time()


@DBOS.step(name="hubwf.approval_state")
def approval_state(nonce: str) -> str | None:
    """The hub-held approval's state (pending, approved, denied, cancelled, expired, used), or None (no hub store,
    no such approval, or the store is down)."""
    fn = HOOKS.get("approval_state")
    if fn is None:
        return None
    try:
        got = _on_loop(fn(nonce), STEP_TIMEOUT)
    except Exception:
        return None
    return got if isinstance(got, str) else None


@DBOS.step(name="hubwf.emit", retries_allowed=True, max_attempts=4, interval_seconds=5.0, backoff_rate=2.0)
def emit(row: dict[str, Any]) -> dict[str, Any]:
    fn = HOOKS.get("emit")
    if fn is None:
        return {"emitted": False, "why": "no store"}
    got = _on_loop(fn(row), STEP_TIMEOUT)
    return {"emitted": True, "id": (got or {}).get("id"), "dup": bool((got or {}).get("dup"))}


@DBOS.step(name="hubwf.fire_reminder")
def fire_reminder(rid: str, at: str) -> dict[str, Any]:
    """One reminders.fire call through the gateway. Never raises: a call that did not reach the brain answers
    unavailable (or error), and the workflow tries again; anything else is the store's decision."""
    fn = HOOKS.get("gateway")
    if fn is None:
        return {"decision": "unavailable", "why": "the gateway is off"}
    try:
        res = _on_loop(fn("reminders.fire", {"id": rid, "at": at}, timeout_s=REM_TIMEOUT), REM_TIMEOUT + JOB_GRACE)
    except Exception as e:
        return {"decision": "unavailable", "why": type(e).__name__}
    res = res if isinstance(res, dict) else {}
    raw = res.get("data")
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if not res.get("ok"):
        code = str(res.get("code") or "internal")[:40]
        return {"decision": "unavailable" if code in ("unavailable", "timeout") else "error", "why": code}
    out: dict[str, Any] = {
        "decision": str(data.get("decision") or "error")[:24],
        "mode": str(data.get("mode") or "")[:10],
    }
    if isinstance(data.get("late_s"), (int, float)) and not isinstance(data.get("late_s"), bool):
        out["late_s"] = data["late_s"]
    return out


@DBOS.step(name="hubwf.window_tick")
def window_tick(kind: str, key: str, at: int) -> dict[str, Any]:
    """One workflow.window call through the gateway, plus the hub's clock after it (the next sleep starts from
    there). Never raises: a call that did not reach the brain answers unavailable (or error); the clock goes on."""
    fn = HOOKS.get("gateway")
    out: dict[str, Any]
    if fn is None:
        out = {"decision": "unavailable", "why": "the gateway is off"}
    else:
        try:
            res = _on_loop(fn("workflow.window", {"kind": kind, "key": key, "at": at}, timeout_s=EVW_TIMEOUT),
                           EVW_TIMEOUT + JOB_GRACE)  # fmt: skip
        except Exception as e:
            res = {"ok": False, "code": "unavailable", "why": type(e).__name__}
        res = res if isinstance(res, dict) else {}
        raw = res.get("data")
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if not res.get("ok"):
            code = str(res.get("code") or "internal")[:40]
            out = {"decision": "unavailable" if code in ("unavailable", "timeout") else "error", "why": code}
        else:
            out = {"decision": str(data.get("decision") or "error")[:24]}
            ns = data.get("next_s")
            if isinstance(ns, (int, float)) and not isinstance(ns, bool) and 0 < ns <= EVW_SPAN_MAX:
                out["next_s"] = float(ns)
            ran = data.get("ran")
            if isinstance(ran, list):
                out["ran"] = [str(x)[:40] for x in ran[:8]]
    out["now"] = time.time()
    return out


class JobUnavailable(Exception):
    """The brain's executor did not take the call (offline, restarting): the step retries."""


def job_limit(v: Any) -> int | None:
    """A schedule context's timeout: whole seconds 1-JOB_TIMEOUT_MAX, else None (= JOB_LIMIT, nothing passed)."""
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= JOB_TIMEOUT_MAX:
        return None
    return v


def job_wait(limit: int | None) -> float:
    """How long the hub waits for one workflow.job call: the job's limit + JOB_GRACE (no limit: JOB_TIMEOUT)."""
    return float(limit) + JOB_GRACE if limit else JOB_TIMEOUT


def job_meta() -> dict[str, Any]:
    """One trace per job run. The trace id comes from the DBOS workflow id (retries stay in the same trace); the
    all-zero parent makes the workflow.job gateway span the trace's root."""
    try:
        wid = str(DBOS.workflow_id or "")
    except Exception:  # noqa: BLE001  (outside a workflow: no trace)
        wid = ""
    if not wid:
        return {}
    return {"traceparent": f"00-{hashlib.sha256(wid.encode()).hexdigest()[:32]}-{'0' * 16}-01"}


@DBOS.step(name="hubwf.run_job", retries_allowed=True, max_attempts=3, interval_seconds=20.0, backoff_rate=2.0)
def run_job(job: str, mode: str, slot: str, limit: int | None = None) -> dict[str, Any]:
    fn = HOOKS.get("gateway")
    if fn is None:
        raise JobUnavailable("the gateway is off")
    args: dict[str, Any] = {"name": job, "mode": mode, "slot": slot}
    kw: dict[str, Any] = {"meta": m} if (m := job_meta()) else {}
    if limit:
        # the brain ends the job within timeout_s; the gateway waits that + its grace (an old schedule: neither)
        args["timeout_s"] = limit
        res = _on_loop(fn("workflow.job", args, timeout_s=limit, **kw), job_wait(limit))
    else:
        res = _on_loop(fn("workflow.job", args, **kw), JOB_TIMEOUT)
    code = (res or {}).get("code") or "internal"
    if code in ("unavailable", "timeout"):
        raise JobUnavailable(code)
    data = (res or {}).get("data") or {}
    return {"ok": bool((res or {}).get("ok")), "code": code, "exit": data.get("exit"), "secs": data.get("secs"),
            "mode": data.get("mode", mode)}  # fmt: skip


# ------------------------------------------------------------------------------------------------ workflows


@DBOS.workflow(name="webtask.approval")
def webtask_approval(args: dict[str, Any]) -> dict[str, Any]:
    a = webtask_args(args)
    t0 = clock()
    deadline = t0 + a["ttl"]
    DBOS.set_event("waiting", {"until": deadline, "task": a["task"], "stop": a["stop"]})
    outcome, via = "", ""
    now = t0
    while not outcome:
        left = deadline - now
        if left <= 0:
            outcome, via = "expired", "deadline"
            break
        got = decision_of(DBOS.recv("decision", timeout_seconds=min(CHUNK, left)))
        if got is not None:
            outcome, via = got
            break
        if a["nonce"]:
            st = approval_state(a["nonce"])
            if st in HUB_STATES:
                outcome, via = HUB_STATES[st], "hub approval"
                break
        now = clock()
    end = clock()
    res = {"outcome": outcome, "via": via, "waited_s": round(end - t0, 1), "task": a["task"], "stop": a["stop"]}
    DBOS.set_event("outcome", res)
    if outcome == "expired" and "expired" in a["notify"]:
        wid = DBOS.workflow_id or ""
        emit(
            {
                "kind": "workflow.webtask",
                "source": "hubwf",
                "subject": f"web task {a['task']}: his approval card expired unanswered",
                "next": f"webagent sweep, then close the task's tab (webagent status {a['task']})",
                "payload": {"task": a["task"], "stop": a["stop"], "outcome": outcome, "workflow": wid},
                "delivery": "beat",
                "idem_key": f"wf:{wid}",
            }
        )
    return res


@DBOS.workflow(name="webtask.run")
def webtask_run(args: dict[str, Any]) -> dict[str, Any]:
    a = webtask_run_args(args)
    t0 = clock()
    s = run_init(t0)
    DBOS.set_event("phase", run_view(s))
    now = t0
    while s["outcome"] is None:
        left = run_due(s, a) - now
        if left <= 0:
            nxt = run_next(s, None, now, a)
        else:
            msg = DBOS.recv("task", timeout_seconds=min(CHUNK if s["phase"] == "pending" else RUN_CHUNK, left))
            now = clock()
            if msg is not None:
                nxt = run_next(s, msg, now, a)
            else:
                nxt = s
                if s["phase"] == "pending" and s["nonce"]:
                    st = approval_state(s["nonce"])
                    if st in HUB_STATES and HUB_STATES[st] != "cancelled":
                        nxt = run_decided(s, HUB_STATES[st], "hub approval", now, a)
                if nxt is s:
                    nxt = run_next(s, None, now, a)
        if run_view(nxt) != run_view(s):
            DBOS.set_event("phase", run_view(nxt))
        s = nxt
    end = clock()
    wid = DBOS.workflow_id or ""
    res = {"outcome": s["outcome"], "via": s["via"], "task": a["task"], "steps": s["steps"], "stops": s["stop"],
           "committed": s["committed"], "revived": s["revived"], "waited_s": round(end - t0, 1)}  # fmt: skip
    if not a["dry"]:
        emit(run_receipt(s, a, wid, end - t0))
    return res


@DBOS.workflow(name="hub.probe")
def hub_probe(args: dict[str, Any]) -> dict[str, Any]:
    ttl = probe_args(args)
    t0 = clock()
    msg = DBOS.recv("ping", timeout_seconds=ttl)
    end = clock()
    return {"got": msg is not None, "message": msg if isinstance(msg, (str, int, float, bool)) else None,
            "waited_s": round(end - t0, 1)}  # fmt: skip


@DBOS.workflow(name="job.tick")
def job_tick(at: datetime, context: Any) -> dict[str, Any]:
    ctx = context if isinstance(context, dict) else {}
    job, mode = ctx.get("job"), ctx.get("mode")
    if not isinstance(job, str) or not JOB_RE.match(job) or mode not in MODES:
        return {"ok": False, "code": "invalid_args", "job": job, "mode": mode}
    r = run_job(job, mode, at.isoformat(), job_limit(ctx.get("timeout")))
    return {**r, "job": job}


@DBOS.workflow(name="reminder.due")
def reminder_due(args: dict[str, Any]) -> dict[str, Any]:
    a = reminder_args(args)
    DBOS.set_event("waiting", {"rid": a["rid"], "at": a["at"], "until": a["ts"]})
    t0 = clock()
    if a["ts"] + REM_PAD > t0:
        DBOS.sleep(a["ts"] + REM_PAD - t0)
    woke = clock()
    r: dict[str, Any] = {}
    tries = 0
    while tries < REM_TRIES:
        tries += 1
        r = fire_reminder(a["rid"], a["at"])
        if r.get("decision") not in REM_AGAIN:
            break
        if tries < REM_TRIES:
            DBOS.sleep(REM_RETRY)
    res = {"rid": a["rid"], "at": a["at"], "decision": r.get("decision"), "mode": r.get("mode"),
           "woke_s": round(woke - a["ts"], 1), "tries": tries}  # fmt: skip
    if r.get("why"):
        res["why"] = r["why"]
    if "late_s" in r:
        res["late_s"] = r["late_s"]
    DBOS.set_event("outcome", res)
    return res


@DBOS.workflow(name="event.window")
def event_window(args: dict[str, Any]) -> dict[str, Any]:
    a = window_args(args)
    DBOS.set_event("waiting", {"kind": a["kind"], "key": a["key"], "start": a["start"], "end": a["end"]})
    t0 = clock()
    if a["start"] > t0:
        DBOS.sleep(a["start"] - t0 + EVW_PAD)
        t0 = clock()
    ticks = misses = 0
    last: dict[str, Any] = {}
    outcome = "capped"
    slot = (t0 // a["every"]) * a["every"]  # the first tick is for the grid slot it wakes in
    while True:
        r = window_tick(a["kind"], a["key"], int(slot))
        ticks += 1
        dec = r.get("decision")
        last = {"at": int(slot), "decision": dec}
        if r.get("ran"):
            last["ran"] = r["ran"]
        DBOS.set_event("last", last)
        if dec in EVW_END:
            outcome = str(dec)
            break
        if dec not in ("active", "idle"):
            misses += 1
        now = float(r.get("now") or slot)
        if slot >= a["end"] or now >= a["end"]:
            break
        slot = window_next(now, a["every"], r.get("next_s"), a["end"])
        DBOS.sleep(max(1.0, slot + EVW_PAD - now))
    res = {"kind": a["kind"], "key": a["key"], "ticks": ticks, "misses": misses, "outcome": outcome, "last": last}
    DBOS.set_event("outcome", res)
    return res


WORKFLOWS = {
    "webtask.approval": webtask_approval,
    "webtask.run": webtask_run,
    "hub.probe": hub_probe,
    "reminder.due": reminder_due,
    "event.window": event_window,
}


# ------------------------------------------------------------------------------------------------ engine (sync: run
# these on a worker thread, never on hubd's loop: DBOS refuses its sync API inside a running event loop)


def launch(conninfo: dict[str, Any], schema: str, *, sched_poll: float = 30.0, notify_poll: float = 1.0) -> None:
    url = sa.engine.URL.create(
        "postgresql+psycopg",
        username=conninfo.get("user"),
        host=conninfo.get("host"),
        port=conninfo.get("port"),
        database=conninfo.get("dbname"),
    )
    connect: dict[str, Any] = {"application_name": "hubd-dbos", "connect_timeout": 5}
    if conninfo.get("password"):
        connect["password"] = conninfo["password"]
    engine = sa.create_engine(url, connect_args=connect, pool_size=POOL, max_overflow=2, pool_timeout=10,
                              pool_pre_ping=True)  # fmt: skip
    DBOS(
        config={
            "name": APP,
            "system_database_url": url.render_as_string(hide_password=True),
            "system_database_engine": engine,
            "dbos_system_schema": schema,
            "application_version": WF_VERSION,
            "log_level": "WARNING",
            "scheduler_polling_interval_sec": sched_poll,
            "notification_listener_polling_interval_sec": notify_poll,
        }
    )
    DBOS.launch()
    # a job holds one of the brain's executor threads: each queue's concurrency is its share of them (persisted)
    for q, n in QUEUES.items():
        DBOS.register_queue(q, worker_concurrency=n, global_concurrency=n, on_conflict="always_update")


def destroy() -> None:
    DBOS.destroy(workflow_completion_timeout_sec=0)


def _ms(v: Any) -> float | None:
    try:
        return round(int(v) / 1000.0, 3)
    except (TypeError, ValueError):
        return None


def _small(v: Any, n: int = 2000) -> Any:
    """Outputs travel as JSON; anything else (or anything big) as a short type note."""
    import json

    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return f"<{type(v).__name__}>"
    return v if len(s) <= n else f"<{len(s)} bytes>"


def row(st: Any, events: bool = False) -> dict[str, Any]:
    g = lambda k: getattr(st, k, None)  # noqa: E731
    err = g("error")
    out: dict[str, Any] = {
        "id": g("workflow_id"),
        "name": g("name"),
        "status": g("status"),
        "created": _ms(g("created_at")),
        "updated": _ms(g("updated_at")),
        "recoveries": g("recovery_attempts"),
        "queue": g("queue_name"),
        "version": g("app_version"),
        "output": _small(g("output")) if g("status") == "SUCCESS" else None,
        "error": type(err).__name__ if isinstance(err, BaseException) else (str(err)[:200] if err else None),
    }
    if events and out["id"]:
        try:
            out["events"] = {k: _small(v, 600) for k, v in DBOS.get_all_events(out["id"]).items()}
        except Exception:
            out["events"] = {}
    return out


def start(name: str, args: Any, wid: str) -> dict[str, Any]:
    fn = WORKFLOWS.get(name)
    if fn is None:
        raise ValueError(f"name: one of {', '.join(NAMES)}")
    check_args(name, args)
    if name == "reminder.due":  # one timer per slot: the canonical id (a cancelled one's slot takes :2-:9)
        base = reminder_wid(args["rid"], args["at"])
        if wid != base and not re.fullmatch(re.escape(base) + r":[2-9]", wid):
            raise ValueError(f"id: {base} (or {base}:2-9 after a cancelled timer)")
    if name == "event.window":  # one clock per window: the canonical id (a cancelled one's window takes :2-:9)
        base = window_wid(args["kind"], args["key"], float(args["start"]))
        if wid != base and not re.fullmatch(re.escape(base) + r":[2-9]", wid):
            raise ValueError(f"id: {base} (or {base}:2-9 after a cancelled window)")
    with SetWorkflowID(wid):
        h = DBOS.start_workflow(fn, args)
    st = h.get_status()
    return row(st)


def status(wid: str) -> dict[str, Any] | None:
    st = DBOS.get_workflow_status(wid)
    return row(st, events=True) if st is not None else None


def listing(name: str | None, state: str | None, limit: int, prefix: str | None) -> list[dict[str, Any]]:
    kw: dict[str, Any] = {"limit": limit, "sort_desc": True, "load_input": False, "load_output": True}
    if name:
        kw["name"] = name
    if state:
        kw["status"] = state
    if prefix:
        kw["workflow_id_prefix"] = prefix
    return [row(s) for s in DBOS.list_workflows(**kw)]


def send(wid: str, topic: str, message: Any, key: str | None) -> None:
    if DBOS.get_workflow_status(wid) is None:
        raise LookupError(f"no workflow {wid}")
    DBOS.send(wid, message, topic, idempotency_key=key)


def cancel(wid: str) -> dict[str, Any]:
    if DBOS.get_workflow_status(wid) is None:
        raise LookupError(f"no workflow {wid}")
    DBOS.cancel_workflow(wid)
    st = DBOS.get_workflow_status(wid)
    return row(st)


def sched_row(s: Any, runs: int = 0) -> dict[str, Any]:  # a DBOS WorkflowSchedule (a TypedDict)
    got = s.get("context")
    ctx: dict[str, Any] = got if isinstance(got, dict) else {}
    out = {
        "name": s.get("schedule_name"),
        "job": ctx.get("job"),
        "mode": ctx.get("mode"),
        "cron": s.get("schedule"),
        "tz": s.get("cron_timezone"),
        "status": s.get("status"),
        "last_fired": s.get("last_fired_at"),
        "queue": s.get("queue_name"),
        "timeout": job_limit(ctx.get("timeout")) or JOB_LIMIT,
        "catch_up": sched_catch_up(s.get("schedule"), s.get("cron_timezone"), ctx),
    }
    if runs:
        try:
            rs = DBOS.list_workflows(schedule_name=out["name"], limit=runs, sort_desc=True, load_input=False)
            out["runs"] = [{"id": getattr(r, "workflow_id", None), "status": getattr(r, "status", None),
                            "at": _ms(getattr(r, "created_at", None)),
                            "output": _small(getattr(r, "output", None), 300)} for r in rs]  # fmt: skip
        except Exception:
            out["runs"] = []
    return out


def schedules(runs: int = 0) -> list[dict[str, Any]]:
    out = []
    for pre in SCHED_PREFIXES:
        out += [sched_row(s, runs) for s in DBOS.list_schedules(schedule_name_prefix=pre)]
    return out


def schedule_set(
    name: str,
    job: str,
    cron: str,
    tz: str | None,
    mode: str,
    queue: str | None = None,
    timeout: int | None = None,
    catch_up: bool | None = None,
) -> dict[str, Any]:
    pre = next((p for p in SCHED_PREFIXES if name.startswith(p)), None)
    if pre is None or not JOB_RE.match(name[len(pre) :]):
        raise ValueError(f"name: {' | '.join(p + '<job>' for p in SCHED_PREFIXES)}")
    if not JOB_RE.match(job) or mode not in MODES:
        raise ValueError(f"job: [a-z0-9-] · mode: {' | '.join(MODES)}")
    if queue is not None and queue not in QUEUES:
        raise ValueError(f"queue: {' | '.join(QUEUES)}")
    if timeout is not None and job_limit(timeout) is None:
        raise ValueError(f"timeout: whole seconds 1-{JOB_TIMEOUT_MAX}")
    if catch_up is not None and not isinstance(catch_up, bool):
        raise ValueError("catch_up: true or false")
    ctx: dict[str, Any] = {"job": job, "mode": mode}
    if timeout is not None:
        ctx["timeout"] = timeout
    if catch_up is not None:
        ctx["catch_up"] = catch_up
    # since: when this cron, zone and mode were set (catch-up runs no tick from before it); a same replace keeps it
    cur = DBOS.get_schedule(name)
    got = cur.get("context") if cur is not None else None
    old: dict[str, Any] = got if isinstance(got, dict) else {}
    same = cur is not None and (cur.get("schedule"), cur.get("cron_timezone"), old.get("mode")) == (cron, tz, mode)
    if not same:
        ctx["since"] = round(time.time())
    elif isinstance(old.get("since"), int) and not isinstance(old.get("since"), bool):
        ctx["since"] = old["since"]
    entry: dict[str, Any] = {
        "schedule_name": name,
        "workflow_fn": job_tick,
        "schedule": cron,
        "context": ctx,
        "queue_name": queue or JOB_QUEUE,
    }
    if tz:
        entry["cron_timezone"] = tz
    DBOS.apply_schedules([entry])  # type: ignore[list-item]
    got = DBOS.get_schedule(name)
    return sched_row(got) if got else {"name": name}


def schedule_op(name: str, op: str) -> dict[str, Any]:
    if DBOS.get_schedule(name) is None:
        raise LookupError(f"no schedule {name}")
    if op == "pause":
        DBOS.pause_schedule(name)
    elif op == "resume":
        DBOS.resume_schedule(name)
    elif op == "delete":
        DBOS.delete_schedule(name)
        return {"name": name, "status": "DELETED"}
    elif op == "trigger":
        h = DBOS.trigger_schedule(name)
        return {"name": name, "status": "TRIGGERED", "run": h.get_workflow_id()}
    else:
        raise ValueError("op: pause | resume | delete | trigger")
    got = DBOS.get_schedule(name)
    return sched_row(got) if got else {"name": name}


# ------------------------------------------------------------------------------------------------ catch-up


def catch_up_on(ctx: dict[str, Any], gap: float) -> bool:
    """Catch-up for a schedule with this context and cadence (s): its catch_up, else on for daily or slower (pure)."""
    v = ctx.get("catch_up")
    if isinstance(v, bool):
        return v and gap >= CATCHUP_HOURLY
    return gap >= CATCHUP_DAILY


def catch_up_grace(gap: float) -> float:
    """How late a missed tick may still run (s): min(cadence/2, CATCHUP_MAX) (pure)."""
    return min(gap / 2.0, CATCHUP_MAX)


def catch_up_verdict(
    tick: float, gap: float, now: float, ctx: dict[str, Any], last: float | None, newest: float | None, exists: bool
) -> str:
    """What a launch does about a schedule's latest tick (pure; epoch seconds). last = the slot it last fired,
    newest = when its newest run was made, exists = the scheduler's workflow for that tick exists.
      off    catch-up is off for it          fired  it fired at or after the tick: nothing was missed
      ran    a run of it was made at or after the tick (that slot's own, a trigger): nothing to run again
      new    the tick is not its own: its cron, zone or mode was set after it (`since`), or, made before `since`
             was kept, nothing shows it existed then (never fired, no older run)
      late   missed longer ago than its grace          run    missed within its grace: run that tick, once"""
    if not catch_up_on(ctx, gap):
        return "off"
    if last is not None and last >= tick:
        return "fired"
    if exists or (newest is not None and newest >= tick):
        return "ran"
    since = ctx.get("since")
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        if since >= tick:
            return "new"
    elif last is None and newest is None:
        return "new"
    if now - tick > catch_up_grace(gap):
        return "late"
    return "run"


def cron_ticks(cron: str, tz: str | None, now: float) -> tuple[datetime, float]:
    """A schedule's latest tick before now (in its zone, as the scheduler writes it) and its cadence: the shortest
    gap (s) among CADENCE_TICKS ticks from there. Raises on a cron DBOS would not take either."""
    from croniter import croniter  # noqa: PLC0415  (DBOS's own cron library, vendored with it)

    z = ZoneInfo(tz) if tz else UTC
    prev = croniter(cron, datetime.fromtimestamp(now, z), second_at_beginning=True).get_prev(datetime)
    it = croniter(cron, prev, second_at_beginning=True)
    ts = [prev.timestamp()] + [it.get_next(datetime).timestamp() for _ in range(CADENCE_TICKS)]
    return prev, min(b - a for a, b in zip(ts, ts[1:], strict=False))


def sched_catch_up(cron: Any, tz: Any, ctx: dict[str, Any]) -> bool | None:
    """A schedule's effective catch-up (its row shows it); None when the cron can't be read."""
    try:
        _, gap = cron_ticks(str(cron), tz if isinstance(tz, str) and tz else None, time.time())
    except Exception:
        return None
    return catch_up_on(ctx, gap)


def _iso_ts(v: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(v)).timestamp() if v else None
    except ValueError:
        return None


def catch_up_one(s: Any, now: float) -> dict[str, Any]:
    """One schedule's catch-up at a launch: {name, verdict[, tick, late_s, id]}; verdict run = its tick enqueued."""
    name = str(s.get("schedule_name"))
    got = s.get("context")
    ctx: dict[str, Any] = got if isinstance(got, dict) else {}
    if s.get("status", "ACTIVE") != "ACTIVE":
        return {"name": name, "verdict": "paused"}
    tz = s.get("cron_timezone")
    tick, gap = cron_ticks(str(s.get("schedule")), tz if isinstance(tz, str) and tz else None, now)
    out: dict[str, Any] = {"name": name, "tick": tick.isoformat(), "late_s": round(now - tick.timestamp(), 1)}
    if not catch_up_on(ctx, gap):
        return {**out, "verdict": "off"}
    last = _iso_ts(s.get("last_fired_at"))
    newest: float | None = None
    exists = False
    if last is None or last < tick.timestamp():
        exists = DBOS.get_workflow_status(f"sched-{name}-{tick.isoformat()}") is not None
        rs = DBOS.list_workflows(schedule_name=name, limit=1, sort_desc=True, load_input=False, load_output=False)
        newest = _ms(getattr(rs[0], "created_at", None)) if rs else None
    verdict = catch_up_verdict(tick.timestamp(), gap, now, ctx, last, newest, exists)
    if verdict == "run":
        # the scheduler's own slot (its workflow id): start and end are exclusive, so exactly that tick
        hs = DBOS.backfill_schedule(name, tick - timedelta(seconds=1), tick + timedelta(seconds=1))
        out["id"] = hs[0].get_workflow_id() if hs else None
    return {**out, "verdict": verdict}


def catch_up(now: float | None = None) -> list[dict[str, Any]]:
    """Right after a launch: each schedule's verdict (catch_up_one); a failing one is verdict error, never raised."""
    t = time.time() if now is None else now
    out: list[dict[str, Any]] = []
    for pre in SCHED_PREFIXES:
        for s in DBOS.list_schedules(schedule_name_prefix=pre):
            try:
                out.append(catch_up_one(s, t))
            except Exception as e:
                out.append({"name": s.get("schedule_name"), "verdict": "error", "why": type(e).__name__})
    return out


def counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for s in ("PENDING", "ENQUEUED", "ERROR"):
        try:
            out[s.lower()] = len(DBOS.list_workflows(status=s, limit=500, load_input=False, load_output=False))
        except Exception:
            out[s.lower()] = -1
    return out
