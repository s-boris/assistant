"""Web + browser: typed READ tools over the web agent's own CLIs: `webagent` (tasks, the commit classifier, stats),
`abx` (browser sessions) and `securefill` (fill-session and pending-request counts).

Boundaries (binding): no tool clicks, types, navigates, commits, approves or fills. The commit gate stays where it
is: browser actions go through abx / the browser MCP under webagent-guard, and `webagent approve` stays tied to his
Telegram tap (an approvals nonce or the journal), never a tool. securefill tools return states, names and counts:
never a value, never a fingerprint key, never a field list from the vault. Env: TOOLS_WEBAGENT_BIN, TOOLS_ABX_BIN,
TOOLS_SECUREFILL_BIN.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from tools.clirun import bin_path, first_line, no_dash, run, run_json
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "`webagent` / `abx` / `securefill` (CLIs; skill web-agent; tests: the webagent suite here,"
    " ~/opt/agent-browser/tests): the whole task protocol stays there and with the browser subagent: start, step,"
    " stop (the approval card),"
    " approve (his ✅ only), resume, done, abort; abx start/stop and page commands; securefill session start, fill,"
    " commit, tick, mask, shot. None of those is a tool: the commit gate (webagent-guard, abx's built-in gate) and"
    " the vault's ✅ for his data stay the only way to act on a page."
)

REGISTRY.add_domain(
    Domain(
        name="web",
        title="Web agent",
        summary="Read-only views of the web agent: tasks and their approval state, the commit classifier, stats,"
        " browser sessions, secure-fill session counts. No clicks, approvals or fills.",
        feature="gateway.web",
        notes=NOTES,
    )
)

Text = Annotated[str, AfterValidator(no_dash)]
TASK_RE = r"^wa[0-9a-f]{6}$"
ABX_LINE = re.compile(r"^(?P<sid>\S+)\s+task (?P<task>\S+)\s+idle (?P<idle>\d+) min\s+tab (?P<tab>open|gone)$")


def _web(env: str, name: str) -> str:
    return bin_path(env, name)


def _fail(rc: int, out: str, err: str, what: str) -> ToolError:
    msg = first_line(err, out, default=f"{what}: exit {rc}")
    for p in ("webagent: ", "securefill: ", "abx: "):
        msg = msg.removeprefix(p)
    if "no task" in msg or "no session" in msg:
        return ToolError("not_found", msg, "web.tasks lists them")
    if rc == 2:
        return ToolError("invalid_args", msg)
    if rc == 3 and what.startswith("securefill"):
        return ToolError("unavailable", msg, "the vault or its fill API is down: `vault status`")
    return ToolError("failed", msg, f"run `{what}` by hand to see why")


# ------------------------------------------------------------------ models


class TasksIn(ToolInput):
    id: str | None = Field(default=None, pattern=TASK_RE, description="one task (wa + 6 hex)")
    all: bool = Field(default=False, description="also finished tasks (default: open ones)")


class CheckIn(ToolInput):
    label: Text = Field(min_length=1, max_length=200, description="a button or link label, e.g. 'Pay now'")


class StatsIn(ToolInput):
    days: int = Field(30, ge=1, le=180)


class SessionsIn(ToolInput):
    task: str | None = Field(default=None, pattern=TASK_RE, description="only this web task's sessions")


class EmptyIn(ToolInput):
    pass


class FillSessionIn(ToolInput):
    id: str = Field(pattern=TASK_RE, description="the web task id")


class Task(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    state: str
    mode: str | None = None
    kind: str | None = None
    title: str | None = None
    url: str | None = None
    steps: int | None = None
    stops: int | None = None
    pending: dict[str, Any] | None = Field(
        default=None, description="the step waiting for his ✅: label, category, what"
    )
    expires: str | None = None
    created: str | None = None
    result: dict[str, Any] | None = Field(default=None, description="ok, note, committed")
    fill_session: Any = None


def _slim_task(t: dict[str, Any], fmt: Format) -> dict[str, Any]:
    """Concise: no screenshot paths (a file the brain need not open) and no side-effect detail."""
    d = Task.model_validate(t).model_dump(exclude_none=True)
    if fmt == "concise":
        for k in ("pending", "result"):
            if isinstance(d.get(k), dict):
                d[k] = {kk: vv for kk, vv in d[k].items() if kk != "shot"}
    return d


class TasksOut(ToolOutput):
    count: int
    tasks: list[Task]

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        return {"count": self.count, "tasks": [_slim_task(t.model_dump(), fmt) for t in self.tasks]}

    def render(self, fmt: Format = "concise") -> str:
        if not self.tasks:
            return "no web tasks"
        out = []
        for t in self.tasks:
            extra = ""
            if t.state == "pending" and t.pending:
                extra = (
                    f" · waits for his ✅ on {t.pending.get('category')} “{t.pending.get('label')}” until {t.expires}"
                )
            elif t.result:
                extra = (
                    f" · {'ok' if t.result.get('ok') else 'failed'}{', committed' if t.result.get('committed') else ''}"
                )
            out.append(
                f"{t.id} {t.state} {t.mode or ''} {t.kind or ''} · {(t.title or '')[:60]} · {t.steps} steps{extra}"
            )
        return "\n".join(out)


class CheckOut(ToolOutput):
    commit: bool = Field(description="true: a committing click (stop and ask him first)")
    tier: str | None = Field(
        default=None, description="hard (pay/book/order/delete/cancel ...) | soft (continue/submit ...)"
    )
    category: str | None = None
    matched: list[str] = Field(default_factory=list)
    exempt: Any = None

    def render(self, fmt: Format = "concise") -> str:
        if not self.commit:
            return "not committing"
        return f"COMMITTING ({self.tier}, {self.category}): stop before it; webagent stop sends his approval card"


class StatsOut(ToolOutput):
    stats: dict[str, Any] = Field(
        description="tasks, completed, committed, reached_card, approvals, owner_stops, expired, failed, stale, open,"
        " abandon_rate, completion_rate, median_approval_s, guard_blocks"
    )

    def render(self, fmt: Format = "concise") -> str:
        s = self.stats
        return (
            f"web tasks {s.get('days')} d: {s.get('tasks')} started · {s.get('completed')} completed "
            f"({s.get('committed')} committed) · {s.get('reached_card')} reached a card · {s.get('approvals')} approved"
            f" · median approval {s.get('median_approval_s')} s · {s.get('guard_blocks')} guard blocks"
        )


class Session(BaseModel):
    sid: str
    task: str | None = None
    idle_min: int | None = None
    tab: str | None = Field(default=None, description="open | gone")


class SessionsOut(ToolOutput):
    count: int
    sessions: list[Session]

    def render(self, fmt: Format = "concise") -> str:
        if not self.sessions:
            return "no browser sessions"
        return "\n".join(
            f"{s.sid} · task {s.task or '-'}"
            + (f" · idle {s.idle_min} min · tab {s.tab}" if s.idle_min is not None else "")
            for s in self.sessions
        )


class FillStatusOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("site_profiles",)
    sessions: list[Any] = Field(description="live fill sessions: task, site, state, minutes left (no values)")
    pending_requests: list[Any] = Field(description="vault requests waiting for his ✅")
    live_fingerprints: int = Field(description="released values still redacted from browser output (count only)")
    psp_hosts: int | None = None
    site_profiles: list[str] | None = None

    def render(self, fmt: Format = "concise") -> str:
        return (
            f"{len(self.sessions)} fill session(s), {len(self.pending_requests)} request(s) waiting for his ✅,"
            f" {self.live_fingerprints} live redaction fingerprint(s)"
        )


class FillSessionOut(ToolOutput):
    state: dict[str, Any] = Field(
        description="ok, session (state | null), minutes left, fields released so far (names + counts), card used"
    )

    def render(self, fmt: Format = "concise") -> str:
        s = self.state
        if not s.get("session"):
            return "no fill session for this task" + (f" ({s['vault']})" if s.get("vault") else "")
        return ", ".join(f"{k}={v}" for k, v in s.items() if k != "ok")


# ------------------------------------------------------------------ tools


@tool(
    "web.tasks",
    effect="read",
    labels_out=("private",),
    rate="60/min",
    idempotent=True,
    examples=({}, {"all": True}, {"id": "wa0a1b2c"}),
)
def tasks(a: TasksIn) -> TasksOut:
    """Web tasks (open ones by default, all, or one by id) with their state, waiting step and result.
    States: running, pending = his ✅ card is out, approved, done ... For "is my booking through", resuming or
    checking a task."""
    exe = _web("TOOLS_WEBAGENT_BIN", "webagent")
    argv = ["status", "--json"] + ([a.id] if a.id else []) + (["--all"] if a.all and not a.id else [])
    _, d = run_json(exe, argv, "webagent status", fail=_fail)
    rows = [d] if isinstance(d, dict) else [r for r in d or [] if isinstance(r, dict)]
    ts = [Task.model_validate(r) for r in rows]
    return TasksOut(count=len(ts), tasks=ts)


@tool(
    "web.check",
    effect="read",
    rate="120/min",
    idempotent=True,
    examples=({"label": "Pay now"}, {"label": "Next"}),
)
def check(a: CheckIn) -> CheckOut:
    """Is clicking a control with this label a committing step (pay, book, order, delete, cancel, submit)?
    The same classifier the commit gate uses. true = stop before it: only his ✅ on the approval card lets it run."""
    exe = _web("TOOLS_WEBAGENT_BIN", "webagent")
    _, d = run_json(exe, ["check", a.label], "webagent check", ok=(0, 3), fail=_fail)
    return CheckOut.model_validate(d)


@tool("web.stats", effect="read", labels_out=("counts",), rate="30/min", idempotent=True, examples=({}, {"days": 7}))
def stats(a: StatsIn) -> StatsOut:
    """Web agent numbers: tasks, completions, commits, approval cards and approvals, guard blocks.
    Also the median approval time and the abandon rate."""
    exe = _web("TOOLS_WEBAGENT_BIN", "webagent")
    _, d = run_json(exe, ["stats", "--json", "--days", str(a.days)], "webagent stats", fail=_fail)
    return StatsOut(stats=d if isinstance(d, dict) else {"value": d})


@tool("web.sessions", effect="read", rate="60/min", idempotent=True, examples=({}, {"task": "wa0a1b2c"}))
def sessions(a: SessionsIn) -> SessionsOut:
    """Browser sessions (abx: one pinned window + tab per agent) with their web task, idle time and tab state.
    With task: that task's session ids (for resuming it)."""
    exe = _web("TOOLS_ABX_BIN", "abx")
    rc, out, err = run(exe, ["list", "--task", a.task] if a.task else ["list"], timeout=20)
    if a.task and rc == 4:
        return SessionsOut(count=0, sessions=[])
    if rc != 0:
        raise _fail(rc, out, err, "abx list")
    rows: list[Session] = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if a.task:
            rows.append(Session(sid=ln, task=a.task))
            continue
        m = ABX_LINE.match(ln)
        if not m:
            raise ToolError("failed", "abx list: unexpected line", "run `abx list` by hand")
        task = None if m["task"] == "-" else m["task"]
        rows.append(Session(sid=m["sid"], task=task, idle_min=int(m["idle"]), tab=m["tab"]))
    return SessionsOut(count=len(rows), sessions=rows)


@tool("web.fill_status", effect="read", labels_out=("counts",), rate="30/min", idempotent=True, examples=({},))
def fill_status(a: EmptyIn) -> FillStatusOut:
    """Secure fill at a glance: live fill sessions, vault requests waiting for his ✅, redaction count.
    Counts and states only: never a value."""
    exe = _web("TOOLS_SECUREFILL_BIN", "securefill")
    _, d = run_json(exe, ["status"], "securefill status", fail=_fail)
    return FillStatusOut(
        sessions=d.get("sessions") or [],
        pending_requests=d.get("pending_requests") or [],
        live_fingerprints=int(d.get("live_fingerprints") or 0),
        psp_hosts=d.get("psp_hosts"),
        site_profiles=d.get("site_profiles"),
    )


@tool(
    "web.fill_session",
    effect="read",
    labels_out=("counts",),
    rate="30/min",
    idempotent=True,
    examples=({"id": "wa0a1b2c"},),
)
def fill_session(a: FillSessionIn) -> FillSessionOut:
    """A web task's fill session: state, minutes left, which of his fields were released and how often.
    Names and counts only. Starting one is `webagent start --data yes` / securefill (his ✅ in the vault bot), not a
    tool."""
    exe = _web("TOOLS_SECUREFILL_BIN", "securefill")
    _, d = run_json(exe, ["session", "status", a.id], "securefill session status", ok=(0, 4, 6), fail=_fail)
    return FillSessionOut(state=d if isinstance(d, dict) else {"value": d})
