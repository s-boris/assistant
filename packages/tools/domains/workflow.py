"""Workflows: durable workflows and schedules on the hub (DBOS Transact in hubd, on its Postgres).

The brain starts, reads, messages and cancels workflows and lists and edits the watch schedules; all of it is the
hub's /wf API (core.hub, the brain's token). workflow.job is the other direction: a hub schedule asks the brain's
executor to run one watch job here (the jobs: core.watchjobs; cron's environment and log files).
Switches: the hub's engine (`hub wf engine on|off`, off by default); each schedule's mode (`hub wf watches
shadow|live|off`). workflow.window is the event windows' tick: the hub's event.window clock calls it once a minute
while a night, party, drive or sleep-in morning is open; `eventwf tick` decides.
Env: TOOLS_EVENTWF_BIN (the binary).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, ClassVar, Literal

from pydantic import Field

from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "`hub wf` (CLI): `hub wf status`, `hub wf engine on|off`, `hub wf ls`, `hub wf show ID`, `hub wf watches"
    " shadow|live|off [--jobs a,b]` (live refuses while the jobs' cron lines are still in ~/crontab), `hub wf cron`"
    " (which cron lines the watch schedules replace, and the lines the registry does not know). The registry is"
    " core/schedules.toml (`assistant cron check|tag`). Engine off = every tool here answers unavailable."
)
NAMES = ("webtask.approval", "hub.probe")
WID = r"^[A-Za-z0-9][A-Za-z0-9:._+-]{0,127}$"  # a schedule's run ids carry "+hh:mm"
JOB = r"^[a-z0-9][a-z0-9-]{0,39}$"

REGISTRY.add_domain(
    Domain(
        name="workflow",
        title="Workflows",
        summary="Durable workflows and schedules on the hub (DBOS): start, read, message and cancel workflows (web-task"
        " approval waits, probes), list and edit the watch schedules; the executor side of a scheduled watch job.",
        feature="gateway.workflow",
        notes=NOTES,
    )
)


def _hub(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    from core import hub  # the brain's hub client (lazy: the hub's own copy of this module never calls it)

    try:
        return hub.call(method, path, body, timeout=timeout)
    except hub.HubDown as e:
        raise ToolError("unavailable", f"the hub did not answer ({e})", "`hub status`") from None
    except hub.Refused as e:
        code = getattr(e, "code", 0)
        err, hint = getattr(e, "err", "") or "refused", getattr(e, "hint", "") or ""
        if code == 404:
            raise ToolError("not_found", hint or err) from None
        if code == 400:
            raise ToolError("invalid_args", hint or err) from None
        if code == 503:
            raise ToolError("unavailable", hint or err, "`hub wf engine on` (the main session decides)") from None
        raise ToolError("failed", f"{err}: {hint}"[:300]) from None
    except hub.Conflict as e:
        raise ToolError("conflict", str(e)[:300], "retry") from None


class Run(ToolOutput):
    id: str | None = None
    name: str | None = None
    status: str | None = Field(default=None, description="PENDING (running or waiting), ENQUEUED, SUCCESS, ERROR,"
                               " CANCELLED")  # fmt: skip
    created: float | None = None
    updated: float | None = None
    output: Any = None
    error: str | None = None
    recoveries: int | None = Field(default=None, description="restarts it was recovered from")
    events: dict[str, Any] | None = None
    DETAIL: ClassVar[tuple[str, ...]] = ("created", "updated", "recoveries", "events")

    def line(self) -> str:
        out = f"{self.id} {self.name} {self.status}"
        if self.output is not None:
            out += f" → {self.output}"
        if self.error:
            out += f" ({self.error})"
        return out

    def render(self, fmt: Format = "concise") -> str:
        s = self.line()
        if fmt == "detailed" and self.events:
            s += "\n" + "\n".join(f"  {k}: {v}" for k, v in self.events.items())
        return s


def _run(d: dict[str, Any]) -> Run:
    keep = {k: d.get(k) for k in Run.model_fields if k in d}
    return Run(**keep)


# ------------------------------------------------------------------------------------------------ start/status/send


class StartIn(ToolInput):
    name: Literal["webtask.approval", "hub.probe"] = Field(description="which workflow")
    args: dict[str, Any] = Field(default_factory=dict, description="its arguments (webtask.approval: task, stop,"
                                 " nonce, ttl, notify; hub.probe: ttl)")  # fmt: skip
    id: str | None = Field(default=None, pattern=WID, description="workflow id; the same id again = the same run")


@tool("workflow.start", effect="home", rate="30/min", timeout_s=30,
      examples=({"name": "hub.probe", "args": {"ttl": 30}},))  # fmt: skip
def start(a: StartIn) -> Run:
    """Start a durable workflow on the hub; it survives hub and brain restarts. webtask.approval = a web task's
    approval wait (webagent starts it); hub.probe = a smoke test that waits for one "ping"."""
    body: dict[str, Any] = {"name": a.name, "args": a.args}
    if a.id:
        body["id"] = a.id
    return _run(_hub("POST", "/wf/start", body))


class StatusIn(ToolInput):
    id: str | None = Field(default=None, pattern=WID, description="one workflow (with its events); none = a list")
    name: Literal["webtask.approval", "hub.probe", "job.tick"] | None = None
    status: Literal["PENDING", "ENQUEUED", "SUCCESS", "ERROR", "CANCELLED"] | None = None
    limit: int = Field(default=20, ge=1, le=100)


class StatusOut(ToolOutput):
    runs: list[Run] = Field(default_factory=list)

    def render(self, fmt: Format = "concise") -> str:
        if not self.runs:
            return "no workflows"
        return "\n".join(r.render(fmt) for r in self.runs)

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        return {"runs": [r.data(fmt) for r in self.runs]}


@tool("workflow.status", effect="read", rate="60/min", idempotent=True, timeout_s=30,
      examples=({}, {"name": "webtask.approval", "status": "PENDING"}))  # fmt: skip
def status(a: StatusIn) -> StatusOut:
    """Read workflows on the hub: one by id (with its events, e.g. a web task's waiting.until and outcome) or the
    newest ones, filtered by name and status. For "is the approval wait still running", "did the watches run"."""
    from urllib.parse import quote, urlencode

    if a.id:
        return StatusOut(runs=[_run(_hub("GET", f"/wf/{quote(a.id, safe=':._+-')}"))])
    q = {k: v for k, v in (("name", a.name), ("status", a.status), ("limit", str(a.limit))) if v}
    return StatusOut(runs=[_run(r) for r in _hub("GET", "/wf?" + urlencode(q)).get("rows") or []])


class SendIn(ToolInput):
    id: str = Field(pattern=WID)
    topic: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,39}$", description='e.g. "decision" (webtask.approval), "ping"')
    message: dict[str, Any] | str | int | bool | None = Field(default=None)
    key: str | None = Field(default=None, pattern=WID, description="idempotency key: a resend with it lands once")


class SendOut(ToolOutput):
    sent: bool
    id: str
    topic: str

    def render(self, fmt: Format = "concise") -> str:
        return f"sent to {self.id} on {self.topic}"


@tool("workflow.send", effect="home", rate="60/min", timeout_s=30,
      examples=({"id": "webtask:abc123:1", "topic": "decision", "message": {"decision": "approved"}},))  # fmt: skip
def send(a: SendIn) -> SendOut:
    """Send a message to a waiting workflow (DBOS send: kept until its recv takes it, across restarts).
    webtask.approval reads {"decision": approved|declined|stopped|replaced|cancelled} on topic "decision"."""
    from urllib.parse import quote

    body: dict[str, Any] = {"topic": a.topic, "message": a.message}
    if a.key:
        body["key"] = a.key
    r = _hub("POST", f"/wf/{quote(a.id, safe=':._+-')}/send", body)
    return SendOut(sent=bool(r.get("sent")), id=a.id, topic=a.topic)


class CancelIn(ToolInput):
    id: str = Field(pattern=WID)


@tool("workflow.cancel", effect="home", rate="30/min", timeout_s=30, examples=({"id": "hub-probe-0a1b2c3d4e5f"},))
def cancel(a: CancelIn) -> Run:
    """Cancel a workflow on the hub (it stops at its next step; DBOS keeps the record)."""
    from urllib.parse import quote

    return _run(_hub("POST", f"/wf/{quote(a.id, safe=':._+-')}/cancel", {}))


# ------------------------------------------------------------------------------------------------ schedules


class Sched(ToolOutput):
    name: str | None = None
    job: str | None = None
    mode: str | None = None
    cron: str | None = None
    tz: str | None = None
    status: str | None = None
    last_fired: str | None = None
    queue: str | None = None
    timeout: int | None = None  # the job's limit the hub waits for (s; an older hub: none)
    catch_up: bool | None = None  # a tick missed while the hub was down runs once, late (an older hub: none)
    runs: list[dict[str, Any]] | None = None
    DETAIL: ClassVar[tuple[str, ...]] = ("runs", "tz", "queue", "timeout", "catch_up")

    def render(self, fmt: Format = "concise") -> str:
        s = f"{self.name}: {self.status} {self.mode} '{self.cron}' last {self.last_fired or '-'}"
        if fmt == "detailed" and self.runs:
            s += "\n" + "\n".join(f"  {r.get('id')} {r.get('status')} {r.get('output')}" for r in self.runs)
        return s


class SchedulesIn(ToolInput):
    runs: int = Field(default=0, ge=0, le=20, description="also each schedule's last N runs (detailed shows them)")
    job: str | None = Field(default=None, pattern=JOB, description="show one registry job (its entry + schedule)")
    registry: bool = Field(default=False, description="list the registry (core/schedules.toml), not only schedules")
    cls: Literal["W", "X", "T", "C", "V", "M", "R", "K"] | None = Field(default=None, description="registry class")


class RegJob(ToolOutput):
    name: str
    line: str
    cls: str
    cron: str
    queue: str | None = None
    where: str
    runs_on: str
    target: str
    owner_facing: bool = False
    why: str = ""
    movable: str | None = None
    shown: str | None = None
    schedule: str | None = None  # the hub schedule's status, when there is one
    catch_up: bool = False  # on the hub, a tick missed while it was down runs once, late (registry catch_up)
    DETAIL: ClassVar[tuple[str, ...]] = ("shown", "movable", "catch_up")

    def render(self, fmt: Format = "concise") -> str:
        s = (f"{self.name} [{self.cls}] '{self.cron}' {self.queue or '-'} {self.runs_on}"
             f"{' sched ' + self.schedule if self.schedule else ''} · {self.why}")  # fmt: skip
        if fmt == "detailed":
            s += f"\n  {self.shown} · move: {self.movable or 'ok'} · {self.target}"
        return s


class SchedulesOut(ToolOutput):
    schedules: list[Sched] = Field(default_factory=list)
    jobs: list[RegJob] = Field(default_factory=list)

    def render(self, fmt: Format = "concise") -> str:
        if self.jobs:
            return "\n".join(j.render(fmt) for j in self.jobs)
        return "\n".join(s.render(fmt) for s in self.schedules) or "no watch schedules"

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        out: dict[str, Any] = {"schedules": [s.data(fmt) for s in self.schedules]}
        if self.jobs:
            out["jobs"] = [j.data(fmt) for j in self.jobs]
        return out


def _sched(d: dict[str, Any]) -> Sched:
    lf = d.get("last_fired")
    return Sched(**{k: d.get(k) for k in Sched.model_fields if k in d and k != "last_fired"},
                 last_fired=str(lf) if lf is not None else None)  # fmt: skip


@tool("workflow.schedules", effect="read", rate="30/min", idempotent=True, timeout_s=30,
      examples=({}, {"runs": 3}, {"registry": True, "cls": "W"}, {"job": "train-watch"}))  # fmt: skip
def schedules(a: SchedulesIn) -> SchedulesOut:
    """The hub's job schedules (DBOS) and the schedule registry (core/schedules.toml: every periodic job, its class,
    queue, runner and why). Default: the live schedules (job, mode shadow|live, cron, status, last fire; runs>0 adds
    their newest runs). registry=true (or cls) lists registry jobs with their schedule state; job= shows one."""
    rows = _hub("GET", f"/wf/schedules?runs={a.runs}").get("rows") or []
    out = SchedulesOut(schedules=[_sched(r) for r in rows])
    if a.job or a.registry or a.cls:
        from core import schedules as reg

        r = reg.load()
        by_job = {x.get("job"): x for x in rows}
        es = [r.entry(a.job)] if a.job else [e for e in r.jobs.values() if not a.cls or e.cls == a.cls]
        if a.job and es[0] is None:
            raise ToolError("not_found", f"no registry job {a.job!r}", "workflow.schedules {registry: true}")
        out.jobs = [RegJob(name=e.name, line=e.line, cls=e.cls, cron=e.cron, queue=e.queue_name, where=e.where,
                           runs_on=e.runs_on, target=e.target, owner_facing=e.owner_facing, why=e.why,
                           movable=e.movable(), shown=e.shown(), catch_up=e.catches_up,
                           schedule=(by_job.get(e.name) or {}).get("status")) for e in es if e is not None]  # fmt: skip
        if a.job:
            out.schedules = [x for x in out.schedules if x.job == a.job]
    return out


def sched_name(job: str, cls: str) -> str:
    return f"watch:{job}" if cls == "W" else f"job:{job}"


def _changed(job: str, state: str, why: str, cron: str | None) -> None:
    """Every schedule change shows in the agenda: one schedule.changed row (digest), never fails the change."""
    import json
    import subprocess

    home = os.path.expanduser("~")
    ev = os.environ.get("JOB_EVENTS") or os.path.join(home, "bin", "events")
    payload = {"type": "schedule.changed", "v": 1, "job": job, "state": state, "why": why[:200]}
    if cron:
        payload["cron"] = cron
    try:
        subprocess.run([ev, "emit", "--kind", "schedule.changed", "--subject", f"schedule {job}: {state} ({why[:60]})",
                        "--source", "workflow.schedule", "--delivery", "digest", "--no-note", "--payload",
                        json.dumps(payload)], capture_output=True, timeout=20, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        pass


def _board_live(job: str) -> None:
    """A job went live: `board poke live JOB` checks at once the board items whose done-when probes name
    it (job-live JOB), so the board never still says shadow/pending for it. Best effort: no ~/bin/board (the hub,
    tests) or any failure changes nothing."""
    exe = os.path.join(os.path.expanduser("~"), "bin", "board")
    if not os.access(exe, os.X_OK):
        return
    try:
        subprocess.run([exe, "poke", "live", job], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


class ScheduleIn(ToolInput):
    job: str = Field(pattern=JOB, description="a registry job (core/schedules.toml: watch, train-watch, ...)")
    state: Literal["shadow", "live", "edit", "paused", "active", "absent", "run"] = Field(
        description="shadow/live = add or replace in that mode · edit (new cron, mode kept) · paused · active (resume)"
        " · absent (remove) · run (fire once now)"
    )
    why: str = Field(min_length=3, max_length=200, description="why (shows in the agenda as schedule.changed)")
    cron: str | None = Field(
        default=None, max_length=64, description="override the job's cron (default: its registry cron)"
    )


@tool("workflow.schedule", effect="home", rate="30/min", timeout_s=40,
      examples=({"job": "watch", "state": "live", "why": "watch runs from the hub"},
                {"job": "drive-watch", "state": "paused", "why": "wedding drive day"}))  # fmt: skip
def schedule(a: ScheduleIn) -> Sched:
    """Add, edit, pause, resume, remove or fire one job's schedule on the hub (any registry job that can move; its
    queue and time limit come from the registry). live runs the job for real: only once its cron line is gone (`hub
    wf cron` lists them), or it runs twice. Every change takes a why and shows in the agenda."""
    from core import schedules as reg
    from core import watchjobs

    e = reg.load().entry(a.job)
    if e is None:
        raise ToolError("not_found", f"no registry job {a.job!r}", "workflow.schedules {registry: true}")
    if e.movable() and a.state in ("shadow", "live", "edit"):
        raise ToolError("invalid_args", f"{a.job} can't run from the hub: {e.movable()}")
    if e.timeout > reg.HUB_TIMEOUT_MAX and a.state in ("shadow", "live", "edit"):
        raise ToolError("invalid_args", f"{a.job}: timeout {e.timeout} s is over the hub's {reg.HUB_TIMEOUT_MAX} s")
    name = sched_name(a.job, e.cls)
    if a.state in ("shadow", "live", "edit"):
        mode = a.state
        if a.state == "edit":
            cur = [x for x in _hub("GET", "/wf/schedules").get("rows") or [] if x.get("name") == name]
            if not cur or not a.cron:
                raise ToolError("invalid_args", "edit needs an existing schedule and a cron")
            mode = cur[0].get("mode") or "shadow"
        if mode == "live" and e.timeout > reg.HUB_JOB_LIMIT:
            # a hub release without per-job limits drops `timeout` and gives up after 600 s while the job runs on
            most = _hub("GET", "/wf/engine").get("job_timeout_max") or reg.HUB_JOB_LIMIT
            if not isinstance(most, int) or most < e.timeout:
                hint = "deploy the hub release with per-job limits first (`assistant deploy --target hub`)"
                raise ToolError(
                    "unavailable", f"{a.job} runs up to {e.timeout} s; the hub waits {most} s at most", hint
                )
        body = {"name": name, "job": a.job, "cron": a.cron or e.cron, "tz": watchjobs.local_tz(), "mode": mode,
                "queue": e.queue_name, "timeout": e.timeout}  # fmt: skip
        if e.catch_up is not None:
            body["catch_up"] = e.catch_up  # the registry's; none = the hub's default by cadence
        got = _sched(_hub("POST", "/wf/schedules", body))
    else:
        op = {"paused": "pause", "active": "resume", "absent": "delete", "run": "trigger"}[a.state]
        got = _sched(_hub("POST", f"/wf/schedules/{name}/{op}", {}))
    if a.state != "run":
        _changed(a.job, a.state, a.why, a.cron)
    if a.state == "live":
        _board_live(a.job)
    return got


# ------------------------------------------------------------------------------------------------ the executor side


class JobIn(ToolInput):
    name: str = Field(pattern=JOB, description="the registry job (core/schedules.toml)")
    mode: Literal["shadow", "live"] = Field(default="shadow", description="shadow = only say what would run")
    slot: str | None = Field(default=None, max_length=40, description="the schedule's fire time (for the log)")
    timeout_s: int | None = Field(
        default=None, ge=1, le=3600, description="how long the caller waits (s): the job ends by then at the latest"
    )


class JobOut(ToolOutput):
    job: str
    mode: str
    exit: int | None = None
    secs: float = 0.0
    would_run: str | None = None
    event: str | None = None  # job.failed / job.recovered when this run emitted one

    def render(self, fmt: Format = "concise") -> str:
        if self.mode == "shadow":
            return f"{self.job}: shadow, would run {self.would_run}"
        return f"{self.job}: exit {self.exit} in {self.secs} s" + (f" ({self.event})" if self.event else "")


def _job_env(name: str, home: str) -> dict[str, str]:
    """The job's environment (core.watchjobs.env) plus TRACEPARENT = the workflow.job gateway span, so the job's own
    tool calls and spans join its trace."""
    from core import watchjobs
    from tools.gateway import CURRENT_TP

    env = watchjobs.env(name, home)
    tp = CURRENT_TP.get()
    if tp:
        env["TRACEPARENT"] = tp
    return env


@tool("workflow.job", effect="send_owner", rate="900/h", timeout_s=600, examples=({"name": "watch"},))
def job(a: JobIn) -> JobOut:
    """Run one registry job here as its cron line would (a hub schedule calls this; the brain may too). shadow (the
    default) only says what it would run. A failure streak of `after` runs emits one job.failed event, the next
    good run job.recovered (cron-fail's semantics); each live run of a job with after > 0 also writes cron-fail's
    usage run row (job.<name>), a shadow tick none. Some jobs text him once per event (hence send_owner). The job's
    time limit is its registry timeout, or timeout_s when that is shorter (a hub schedule passes how long it waits)."""
    from core import jobfail, watchjobs

    try:
        j = watchjobs.runnable(a.name)
    except KeyError:
        raise ToolError("not_found", f"no registry job {a.name!r}", "workflow.schedules {registry: true}") from None
    except ValueError as e:
        raise ToolError("invalid_args", f"{a.name}: {e}") from None
    if a.mode == "shadow":
        return JobOut(job=a.name, mode="shadow", would_run=j.shown())
    limit = min(j.timeout_s, a.timeout_s) if a.timeout_s else j.timeout_s
    home = os.path.expanduser("~")
    state = os.path.join(home, ".local", "state")
    err = j.err if j.err not in (None, "out") else (None if j.err == "out" else f"jobs/{a.name}.err")
    for d in j.dirs + tuple(os.path.dirname(x) for x in (j.log, err) if x):
        os.makedirs(os.path.join(state, d), exist_ok=True)
    t0 = time.monotonic()
    log = open(os.path.join(state, j.log), "ab") if j.log else None  # noqa: SIM115
    # stderr: with stdout ("out"), its own file (appended, as `2>>`), or ~/.local/state/jobs/NAME.err (cron-fail's .err)
    efile = open(os.path.join(state, err), "ab" if j.err else "wb") if err else None  # noqa: SIM115
    rc: int | None = None
    oserr: str | None = None
    try:
        out = log if log is not None else subprocess.DEVNULL
        p = subprocess.run(watchjobs.argv(j, home), stdin=subprocess.DEVNULL, stdout=out,
                           stderr=efile if efile is not None else subprocess.STDOUT,
                           env=_job_env(a.name, home), cwd=home, timeout=limit, check=False)  # fmt: skip
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except OSError as e:  # could not start it: a failed run, as the shell's 126/127 under cron-fail
        rc, oserr = (126 if isinstance(e, PermissionError) else 127), type(e).__name__
    finally:
        for f in (log, efile):
            if f is not None:
                f.close()
    secs = time.monotonic() - t0
    # cron-fail's usage run row (job.<name>; spool src hubjob), once per live run; a shadow tick returned above
    jobfail.usage_run(a.name, rc, after=j.after, ok=j.ok, ms=secs * 1000, src="hubjob")
    ev = jobfail.record(a.name, rc, after=j.after, ok=j.ok, cmd=" ".join(j.argv),
                        err=f"~/.local/state/{err}" if err else "")  # fmt: skip
    if oserr:
        raise ToolError("failed", f"{a.name}: {oserr}")
    if rc == 124 and j.argv[:1] != ("timeout",):
        raise ToolError("failed", f"{a.name}: no exit after {limit} s (killed)")
    return JobOut(job=a.name, mode="live", exit=rc, secs=round(secs, 1), event=ev)


class WindowIn(ToolInput):
    kind: Literal["night", "party", "drive", "sleepin"] = Field(description="the window's kind")
    key: str = Field(pattern=r"^[A-Za-z0-9@._:+-]{1,120}$", description="the brain's key for the window")
    at: int = Field(ge=1_000_000_000, le=9_999_999_999, description="the tick's minute slot (epoch s)")


class WindowOut(ToolOutput):
    kind: str
    key: str
    decision: str = Field(description="active | done | gone")
    ran: list[str] = Field(default_factory=list, description="the registry jobs started")
    next_s: float | None = None
    late_s: float | None = None

    def render(self, fmt: Format = "concise") -> str:
        jobs = ", ".join(self.ran) or "nothing due"
        return f"{self.kind} {self.key[:24]}: {self.decision} ({jobs})"


def _eventwf() -> str:
    return os.environ.get("TOOLS_EVENTWF_BIN") or os.path.join(os.path.expanduser("~"), "bin", "eventwf")


@tool(
    "workflow.window",
    effect="send_owner",
    idempotent=True,
    rate="900/h",
    timeout_s=60,
    examples=({"kind": "night", "key": "abc@2026-10-24", "at": 1792000000},),
)
def window(a: WindowIn) -> WindowOut:
    """An event window's tick (the hub's event.window clock, once a minute while it is open): eventwf starts the
    registry jobs due at that minute and says whether the window is still open."""
    try:
        p = subprocess.run([_eventwf(), "tick", a.kind, a.key, "--at", str(a.at), "--json"], capture_output=True,
                           text=True, timeout=50, stdin=subprocess.DEVNULL, check=False)  # fmt: skip
    except FileNotFoundError as e:
        raise ToolError("unavailable", "eventwf is not installed", "deploy it: `assistant land`") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError("timeout", "eventwf tick: no answer in 50 s") from e
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or f"eventwf tick: exit {p.returncode}").strip().splitlines()[0][:300]
        raise ToolError("invalid_args" if p.returncode == 2 else "failed", msg, "run `eventwf status`")
    try:
        d = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except ValueError as e:
        raise ToolError("failed", "eventwf tick: not JSON", "run `eventwf status`") from e

    def num(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    ran = [str(x)[:40] for x in d.get("ran") or [] if isinstance(x, str)][:8]
    return WindowOut(kind=a.kind, key=a.key, decision=str(d.get("decision") or "error")[:24], ran=ran,
                     next_s=num(d.get("next_s")), late_s=num(d.get("late_s")))  # fmt: skip
