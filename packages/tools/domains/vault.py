"""Vault client: typed tools over `vault` (~/bin/vault, the privacy vault's client): answers, facts, plans, the event
feed, mail cards and hits, Telegram digests, his coarse HA flags, counts. tgread = `vault tg`, so `tgread ask` is
vault.ask (source tg) and `tgread summarize` is vault.tg_summarize. Paperless (also through the vault): domain
paperless, same guard.

Privacy boundary (binding): only the vault's structured defaults are tools. Text and content releases (text and
content flags, raw and media requests, tier-2 fields with a reason, downloads, llm-run) are not tools and never
become one: they stay owner-✅ only, through the vault bot (skill vault). Every argv passes guard() right before it
runs: an allowlist of commands and options; values are refused when they start with "-". So a subagent that holds
these tools still only gets answers.
Slow paths (ask, facts, summarize, mail cards) run as vault jobs (`vault --async`): the tool waits up to wait_s
seconds and returns the answer, else {state: running, job}; vault.job picks it up later. Env: TOOLS_VAULT_BIN.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal, TypeGuard

from pydantic import AfterValidator, Field, PrivateAttr, model_validator

from tools.clirun import bin_path, first_line, no_dash, parse_json, run
from tools.core import REGISTRY, Domain, Format, JobTimes, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "`vault` (CLI; skill vault): the same paths by hand, plus what is NOT a tool on purpose: text and content"
    " releases, raw and media requests, tier-2 paperless fields (all need his ✅ in the vault bot), deploys, people,"
    " firefly, index, admin. Slow answers: the tools submit a vault job (`vault --async …`) and wait wait_s (default"
    " 20 s); `running` → `vault_job {id}` later (results kept 30 min). In the main session keep wait_s ≤ 20 (never"
    " block a turn on the vault); a background subagent may wait up to 230."
)

REGISTRY.add_domain(
    Domain(
        name="vault",
        title="Vault client",
        summary="Answers, not text, from his private data in the privacy vault: Telegram and Gmail questions, facts,"
        " dated plans, the event feed, mail cards, Telegram digests, his coarse HA flags. Text and content stay"
        " behind his ✅ in the vault bot (not tools).",
        feature="gateway.vault",
        notes=NOTES,
    )
)

# ------------------------------------------------------------------ guard

# (command words) the tools may run; anything else is a bug and never reaches the vault
COMMANDS = frozenset(
    {
        ("ask", "tg"),
        ("ask", "gmail"),
        ("facts", "tg"),
        ("facts", "gmail"),
        ("plans",),
        ("events",),
        ("ha", "now"),
        ("gmail", "get"),
        ("gmail", "search"),
        ("tg", "summarize"),
        ("status",),
        ("inbox",),
        ("job", "wait"),
        ("job", "list"),  # ids, states and times only (the gateway's job-log sweep)
        ("paperless", "get"),
        ("paperless", "fields"),
        ("paperless", "search"),
        ("paperless", "recent"),
    }
)
# every option a tool may pass (exact strings: argparse prefers an exact match over an abbreviation)
OPTIONS = frozenset(
    {
        "--json",
        "--no-text",
        "--chat",
        "--search",
        "--since",
        "--kind",
        "--account",
        "--retrieval",
        "-n",
        "--days",
        "--max",
        "--source",
        "--deep",
        "--folder",
        "--hours",
        "--unread",
        "--fields",
    }
)
JOB_RE = re.compile(r"[0-9a-f]{16}")


def guard(argv: list[str]) -> list[str]:
    """ARGV unchanged if it is an allowed vault command with allowed options only; else a ToolError (a bug)."""
    a = argv[1:] if argv[:1] == ["--async"] else list(argv)
    cmd = tuple(a[:2]) if tuple(a[:2]) in COMMANDS else tuple(a[:1])
    if cmd not in COMMANDS:
        raise ToolError("internal", f"vault {' '.join(a[:2])}: not a tool path", "a bug: report it")
    for x in a[len(cmd) :]:
        if x.startswith("-") and x not in OPTIONS:
            raise ToolError("internal", f"vault {' '.join(cmd)}: option {x.split('=')[0]} is not a tool path", "a bug")
    return argv


def _bin() -> str:
    return bin_path("TOOLS_VAULT_BIN", "vault")


# core.drive's one drive-flag rule, inlined: the hub release ships packages/tools as app/tools but no core/, so the
# gateway can't import core.drive. tests/tools/test_vault.py holds it to core.drive.verdict (parity test): change
# both together. On AND the answer at most DRIVING_MAX_AGE_MIN (default 60) minutes old; older = unknown.
DRIVE_MAX_AGE_MIN = 60


def _drive_max_age() -> int:
    raw = os.environ.get("DRIVING_MAX_AGE_MIN", "").strip()
    try:
        return int(raw) if raw else DRIVE_MAX_AGE_MIN
    except ValueError:
        return DRIVE_MAX_AGE_MIN


def _num(v: Any) -> TypeGuard[int | float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def drive_verdict(answer: Any) -> tuple[str, int | None]:
    """("yes", minutes | None) | ("no", None) | ("unknown", None), exactly as core.drive.verdict(answer)."""
    if not isinstance(answer, dict):
        return "unknown", None
    d = answer.get("drive")
    if isinstance(d, dict):
        on, mins = d.get("active"), d.get("minutes")
    elif isinstance(d, bool):
        on, mins = d, None
    else:
        return "unknown", None
    if on is None:
        return "unknown", None
    age = answer.get("age_min")
    if _num(age) and age > _drive_max_age():
        return "unknown", None
    if on is True:
        return "yes", int(mins) if _num(mins) else None
    return "no", None


def vault_run(argv: list[str], timeout: float = 90.0) -> tuple[int, str, str]:
    return run(_bin(), guard(argv), timeout=timeout, env={"VAULT_QUIET": "1"})


def vault_fail(rc: int, out: str, err: str, verb: str) -> ToolError:
    msg = first_line(err, out, default=f"vault {verb}: exit {rc}").removeprefix("vault: ")
    if rc == 2:
        return ToolError("invalid_args", msg, f"`tool show` lists the fields; `vault {verb} --help` by hand")
    if rc == 3:
        return ToolError("unavailable", msg, "the vault, its LLM or the source is down: `vault status`")
    if rc == 75:
        return ToolError("rate_limited", msg, "the vault's LLM is busy: retry in a minute")
    if rc == 77:
        return ToolError("unavailable", "vault auth failed", "`vault status`; the token is the owner's")
    if rc == 10:
        return ToolError("approval_required", msg, "a release waits for his ✅ in the vault bot: not a tool path")
    if "no such" in msg or "unknown" in msg:
        return ToolError("not_found", msg)
    return ToolError("failed", msg)


def vault_json(argv: list[str], verb: str, timeout: float = 90.0, ok: tuple[int, ...] = (0,)) -> Any:
    rc, out, err = vault_run(argv, timeout)
    if rc not in ok or not out:
        raise vault_fail(rc, out, err, verb)
    return parse_json(out, f"vault {verb}")


# ------------------------------------------------------------------ fields

Text = Annotated[str, AfterValidator(no_dash)]
SINCE = r"^(\d{1,4}[hdwm]|\d{4}-\d\d-\d\d)$"
REF = r"^(?:[A-Za-z0-9_.-]{1,32}:)?\d{1,12}(?:@[^\s]{1,120})?$"
Kind = Literal[
    "yesno", "when", "who", "deadline", "actionable", "requests", "count", "short", "interests", "digest", "mail_card"
]
WaitS = Annotated[
    int,
    Field(
        ge=0,
        le=230,
        description="seconds to wait for the vault's answer (0 = only start the job); not done by then: "
        "state running + job id for vault_job. Main session: <= 20",
    ),
]


class JobOut(ToolOutput):
    state: Literal["done", "running", "queued"] = Field(description="done = result holds the answer")
    job: str | None = Field(default=None, description="the vault job id (vault_job picks it up; kept 30 min)")
    result: dict[str, Any] | None = Field(default=None, description="the vault's structured answer (no message text)")
    elapsed_s: int | None = None
    position: int | None = Field(default=None, description="queue position while waiting")
    eta_s: float | None = None
    progress: Any = None
    DETAIL: ClassVar[tuple[str, ...]] = ("progress", "elapsed_s")
    # the job's own times for the gateway's job log (vault-answer SLO); never part of the output
    _times: JobTimes | None = PrivateAttr(default=None)

    def job_times(self) -> JobTimes | None:
        return self._times

    def with_times(self, t: JobTimes | None) -> JobOut:
        self._times = t
        return self

    def render(self, fmt: Format = "concise") -> str:
        if self.state != "done":
            eta = f", ETA ~{int(self.eta_s)} s" if self.eta_s is not None else ""
            pos = f", position {self.position}" if self.position else ""
            return f"vault job {self.job} {self.state}{pos}{eta}: vault_job {{id: {self.job}}} later"
        return render_result(self.result or {})


def render_result(r: dict[str, Any]) -> str:
    """A short text form of a vault answer (the structured form carries everything)."""
    kind = r.get("kind")
    if kind == "digest":
        w = r.get("window") or {}
        lines = [f"digest: {w.get('messages', 0)} message(s) in {w.get('chats', 0)} chat(s), {w.get('hours')} h"]
        for x in r.get("needs_you") or []:
            lines.append(f"- needs you: {x.get('chat')} · {x.get('from')} wants {x.get('wants')}")
        for x in r.get("plans") or []:
            lines.append(f"- plan: {x.get('date')} {x.get('start') or ''} {x.get('what')} · {x.get('chat')}")
        if r.get("topics"):
            lines.append("topics: " + ", ".join(f"{k} {v}" for k, v in r["topics"].items()))
        return "\n".join(lines)
    if "answer" in r:
        where = r.get("chat") or r.get("query") or r.get("ref") or ""
        head = f"{kind or 'answer'}{' · ' + str(where) if where else ''} ({r.get('messages', 0)} message(s))"
        return head + "\n" + json.dumps(r.get("answer"), ensure_ascii=False)[:1500]
    return json.dumps(r, ensure_ascii=False)[:1500]


def _epoch(v: Any) -> float | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def job_times(j: dict[str, Any], started: bool = False) -> JobTimes | None:
    """A vault job's id, state and times (created_at / ended_at, elapsed_s) from its JSON (job wait or job list);
    None without an id or state. Timings only."""
    jid, state = j.get("id"), j.get("state")
    if not isinstance(jid, str) or not JOB_RE.fullmatch(jid) or not isinstance(state, str):
        return None
    el = j.get("elapsed_s")
    return JobTimes(
        job=jid,
        state=state,
        created=_epoch(j.get("created_at")),
        ended=_epoch(j.get("ended_at")),
        elapsed_s=float(el) if _num(el) else None,
        started=started,
    )


def _job_view(j: dict[str, Any]) -> JobOut:
    state = j.get("state")
    jid = j.get("id")
    if state == "done":
        res = j.get("result")
        if not isinstance(res, dict):
            res = {"value": res}
        if res.get("text_release") or res.get("request_id"):
            raise ToolError("failed", "the vault answered with a release request", "not a tool path: skill vault")
        return JobOut(state="done", job=jid, result=res, elapsed_s=j.get("elapsed_s")).with_times(job_times(j))
    if state in ("queued", "running"):
        return JobOut(
            state=state,
            job=jid,
            elapsed_s=j.get("elapsed_s"),
            position=j.get("position"),
            eta_s=j.get("eta_s"),
            progress=j.get("progress"),
        ).with_times(job_times(j))
    e = j.get("error") or {}
    msg = (e.get("error") if isinstance(e, dict) else str(e)) or f"job {state}"
    code = e.get("code") if isinstance(e, dict) else None
    if state == "expired" or code == "result_dropped":
        raise ToolError("not_found", f"job {jid}: {msg}"[:300], "run the question again")
    if code == "llm_busy":
        raise ToolError("rate_limited", f"job {jid}: {msg}"[:300], "the vault's LLM is busy: retry in a minute")
    raise ToolError("failed", f"job {jid} {state}: {msg}"[:300])


def job_wait(jid: str, wait_s: int) -> JobOut:
    rc, out, err = vault_run(["job", "wait", jid, "--max", str(wait_s), "--json"], timeout=wait_s + 100)
    if rc in (0, 1, 12) and out.startswith("{"):
        return _job_view(parse_json(out, "vault job wait"))
    raise vault_fail(rc, out, err, "job wait")


def job_submit(argv: list[str], wait_s: int) -> JobOut:
    """`vault --async ARGV`: the job id at once; then wait up to wait_s for the answer. The answer's job times are
    marked started (the gateway's job log keeps this call's tool for the job)."""
    rc, out, err = vault_run(["--async", *argv], timeout=90)
    jid = first_line(out)
    if rc != 0 or not JOB_RE.fullmatch(jid):
        raise vault_fail(rc or 1, out, err, argv[0])
    if wait_s <= 0:
        return JobOut(state="queued", job=jid).with_times(JobTimes(job=jid, state="queued", started=True))
    res = job_wait(jid, wait_s)
    return res.with_times(dataclasses.replace(res.job_times() or JobTimes(job=jid, state=res.state), started=True))


def job_list() -> list[JobTimes]:
    """Every job the vault still holds (`vault job list --json`: kept 30 min after it ends), as times only."""
    d = vault_json(["job", "list", "--json"], "job list", timeout=60)
    if not isinstance(d, list):
        raise ToolError("failed", "vault job list: no JSON list", "run `vault job list --json` by hand")
    return [t for t in (job_times(j) for j in d if isinstance(j, dict)) if t is not None]


def _opts(*pairs: tuple[str, Any]) -> list[str]:
    out: list[str] = []
    for flag, val in pairs:
        if val is None or val is False:
            continue
        out += [flag] if val is True else [flag, str(val)]
    return out


# ------------------------------------------------------------------ models


class AskIn(ToolInput):
    source: Literal["tg", "gmail"] = Field(description="tg = his Telegram chats; gmail = one mail (ref)")
    question: Text | None = Field(default=None, max_length=500, description="the question (none: the kind's default)")
    chat: Text | None = Field(default=None, max_length=120, description="tg: one chat (id, alias or name)")
    search: Text | None = Field(default=None, max_length=200, description="tg: answer over the hits for this query")
    ref: str | None = Field(
        default=None, pattern=REF, description="gmail: the mail's REF (main:1234, from events/search)"
    )
    since: str | None = Field(default=None, pattern=SINCE, description="tg window: 30d (default), 12w, 48h, 2026-09-01")
    kind: Kind | None = Field(
        None,
        description="answer shape: yesno, when, who, deadline, actionable, requests, count, short, interests,"
        " digest (tg, no chat needed), mail_card (gmail)",
    )
    account: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,32}$", description="gmail: account label")
    retrieval: Literal["hybrid", "keyword"] | None = Field(default=None, description="tg: hybrid = keyword + semantic")
    wait_s: WaitS = 20

    @model_validator(mode="after")
    def _shape(self) -> AskIn:
        if self.source == "gmail":
            if not self.ref:
                raise ValueError("gmail needs ref")
            if self.chat or self.search or self.retrieval or self.since:
                raise ValueError("chat, search, since and retrieval are for tg")
        else:
            if self.ref or self.account:
                raise ValueError("ref and account are for gmail")
            if bool(self.chat) == bool(self.search) and self.kind != "digest":
                raise ValueError("tg: give exactly one of chat or search")
        return self


class FactsIn(ToolInput):
    source: Literal["tg", "gmail"]
    chat: Text | None = Field(default=None, max_length=120, description="tg: one chat (id, alias or name)")
    ref: str | None = Field(default=None, pattern=REF, description="gmail: the mail's REF")
    since: str | None = Field(default=None, pattern=SINCE, description="tg window, default 60d")
    n: int | None = Field(default=None, ge=1, le=500, description="tg: newest N messages of the window (default 400)")
    account: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    wait_s: WaitS = 20

    @model_validator(mode="after")
    def _shape(self) -> FactsIn:
        if (self.source == "tg") != bool(self.chat) or (self.source == "gmail") != bool(self.ref):
            raise ValueError("tg needs chat, gmail needs ref (not both)")
        if self.source == "gmail" and (self.since or self.n):
            raise ValueError("since and n are for tg")
        if self.source == "tg" and self.account:
            raise ValueError("account is for gmail")
        return self


class JobIn(ToolInput):
    id: str = Field(pattern=r"^[0-9a-f]{16}$", description="the job id a vault tool returned")
    wait_s: WaitS = 20


class SummarizeIn(ToolInput):
    hours: float | None = Field(default=None, gt=0, le=720, description="the last N hours (default: the vault's, 24)")
    since: str | None = Field(default=None, pattern=SINCE, description="instead of hours: 2d, 2026-10-20")
    chat: Text | None = Field(default=None, max_length=120, description="one chat only")
    unread: bool = Field(False, description="only unread chats")
    wait_s: WaitS = 20

    @model_validator(mode="after")
    def _one(self) -> SummarizeIn:
        if self.hours is not None and self.since:
            raise ValueError("give hours or since, not both")
        return self


class GmailGetIn(ToolInput):
    ref: str = Field(pattern=REF, description="main:1234 or work:1234@Folder (from vault_events / vault_gmail_search)")
    deep: bool = Field(False, description="the full card with dates (reads the mail; slower)")
    account: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    folder: Text | None = Field(default=None, max_length=120)
    wait_s: WaitS = 20


class GmailSearchIn(ToolInput):
    query: Text = Field(min_length=1, max_length=300, description="Gmail search syntax: from:x newer_than:7d …")
    n: int = Field(10, ge=1, le=50)
    account: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]{1,32}$", description="one account (default: all)"
    )
    folder: Text | None = Field(default=None, max_length=120, description="default INBOX")


class ResultOut(ToolOutput):
    result: dict[str, Any] = Field(description="the vault's structured answer (hits, card, counts; no text)")

    def render(self, fmt: Format = "concise") -> str:
        return render_result(self.result)


class PlansIn(ToolInput):
    days: int = Field(14, ge=1, le=60, description="look ahead (the feed holds about 7 days of messages)")


class PlansOut(ToolOutput):
    start: str | None = Field(default=None, description="first day")
    end: str | None = Field(default=None, description="last day")
    plans: list[dict[str, Any]] = Field(description="date, start, what, venue, kind, chat, source, chat_key, ref")

    def render(self, fmt: Format = "concise") -> str:
        if not self.plans:
            return f"no dated plans {self.start} .. {self.end}"
        return "\n".join(
            f"{p.get('date')} {p.get('start') or ''} {p.get('what')}"
            + (f", {p['venue']}" if p.get("venue") else "")
            + f" · {p.get('chat') or '?'} [{p.get('source')} {p.get('ref') or p.get('chat_key') or ''}]"
            for p in self.plans
        )


class EventsIn(ToolInput):
    since: str = Field(
        "latest",
        pattern=r"^(latest|\d{1,12})$",
        description="a cursor: events after it; latest = only the current cursor (a starting point)",
    )
    max: int = Field(100, ge=1, le=500)
    source: Literal["telegram", "gmail", "paperless", "homeassistant"] | None = None


class EventsOut(ToolOutput):
    events: list[dict[str, Any]] = Field(description="triaged + anonymized events (text only for ❗/★ ones)")
    next_cursor: int | str | None = None
    more: bool = False
    reset: bool = False

    def render(self, fmt: Format = "concise") -> str:
        lines = [
            f"{e.get('cursor')} {e.get('source')} {e.get('ts', '')[:16]} {e.get('flag') or ''} "
            f"{e.get('chat') or e.get('from') or ''} {e.get('category') or ''}".strip()
            for e in self.events
        ]
        lines.append(f"next cursor: {self.next_cursor}" + (" (more)" if self.more else ""))
        return "\n".join(lines)


class Empty(ToolInput):
    pass


class HaNowOut(ToolOutput):
    place: dict[str, Any] | None = Field(default=None, description="zone name or away + distance band (+ town), since")
    drive: dict[str, Any] | None = None
    activity: str | None = None
    sleep: str | None = None
    last_sleep: dict[str, Any] | None = None
    alarm: str | None = None
    battery: dict[str, Any] | None = None
    bluetooth: list[Any] | None = Field(default=None, description="categories only")
    wifi: Any = None
    on_call: Any = None
    dnd: Any = None
    signals: list[Any] | None = None
    pending: list[Any] | None = None
    last_update: str | None = None
    age_min: int | None = Field(default=None, description="minutes since the vault heard HA")
    device: str | None = None
    counts: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    DETAIL: ClassVar[tuple[str, ...]] = ("device", "counts", "connection", "pending")

    def render(self, fmt: Format = "concise") -> str:
        p = self.place or {}
        where = p.get("zone") or p.get("kind") or "?"
        if p.get("band"):
            where += f" ({p['band']}{', ' + p['town'] if p.get('town') else ''})"
        # core.drive's one rule (inlined as drive_verdict): on AND the answer at most 60 min old; older = unknown
        state, mins = drive_verdict({"drive": self.drive, "age_min": self.age_min})
        if state == "yes":
            drv = f"driving {mins} min" if mins is not None else "driving"
        else:
            drv = "not driving" if state == "no" else "drive unknown"
        bits = [f"place {where}", drv]
        bits += [f"activity {self.activity}"] if self.activity else []
        bits += [f"sleep {self.sleep}"] if self.sleep else []
        b = self.battery or {}
        bits += [f"battery {b.get('level')}%"] if b.get("level") is not None else []
        return " · ".join(bits) + f" (age {self.age_min} min)"


class CountsOut(ToolOutput):
    counts: dict[str, Any] = Field(description="numbers and states only")

    def render(self, fmt: Format = "concise") -> str:
        return json.dumps(self.counts, ensure_ascii=False)[:1500]


# ------------------------------------------------------------------ tools


@tool(
    "vault.ask",
    effect="read",
    labels_out=("private",),
    rate="120/h",
    timeout_s=330,
    examples=(
        {"source": "tg", "chat": "Alex", "question": "when do we meet?", "kind": "when"},
        {"source": "gmail", "ref": "main:1234", "kind": "deadline"},
    ),
)
def ask(a: AskIn) -> JobOut:
    """An answer from his Telegram chats or one mail, checked in the vault: never message text.
    tg: one chat or a search; gmail: one mail by ref. kind picks the shape (when, yesno, who, deadline ...)."""
    argv = ["ask", a.source]
    if a.source == "gmail":
        argv += [a.ref or ""] + _opts(("--kind", a.kind), ("--account", a.account))
    else:
        argv += _opts(
            ("--chat", a.chat),
            ("--search", a.search),
            ("--since", a.since),
            ("--kind", a.kind),
            ("--retrieval", a.retrieval),
        )
    argv += ["--json"] + ([a.question] if a.question else [])
    return job_submit(argv, a.wait_s)


@tool(
    "vault.facts",
    effect="read",
    labels_out=("private",),
    rate="60/h",
    timeout_s=330,
    examples=({"source": "tg", "chat": "Alex", "since": "30d"}, {"source": "gmail", "ref": "main:1234"}),
)
def facts(a: FactsIn) -> JobOut:
    """Facts of one chat or mail as JSON: events, to-dos, places, decisions, open questions (values, no text).
    Dates are worked out from the message times."""
    if a.source == "tg":
        argv = ["facts", "tg", a.chat or ""] + _opts(("--since", a.since), ("-n", a.n))
    else:
        argv = ["facts", "gmail", a.ref or ""] + _opts(("--account", a.account))
    return job_submit(argv, a.wait_s)


@tool("vault.job", effect="read", labels_out=("private",), rate="120/min", timeout_s=330, examples=({"id": "0" * 16},))
def job(a: JobIn) -> JobOut:
    """The answer of a vault job another vault tool started (state running): waits up to wait_s seconds."""
    return job_wait(a.id, a.wait_s)


@tool(
    "vault.tg_summarize",
    effect="read",
    labels_out=("private",),
    rate="20/h",
    timeout_s=330,
    examples=({"hours": 24}, {"unread": True}),
)
def tg_summarize(a: SummarizeIn) -> JobOut:
    """A digest of his Telegram (= tgread summarize): needs-you items, plans, signals, topics; no message text."""
    argv = ["tg", "summarize", "--no-text", "--json"] + _opts(
        ("--hours", None if a.hours is None else f"{a.hours:g}"),
        ("--since", a.since),
        ("--chat", a.chat),
        ("--unread", a.unread),
    )
    return job_submit(argv, a.wait_s)


@tool(
    "vault.gmail_get",
    effect="read",
    labels_out=("private",),
    rate="120/h",
    timeout_s=330,
    examples=({"ref": "main:1234"}, {"ref": "main:1234", "deep": True}),
)
def gmail_get(a: GmailGetIn) -> JobOut:
    """A mail's card (category, action, deadline, attachments); deep = the full card with dates. Never the text."""
    argv = ["gmail", "get", a.ref, "--no-text", "--json"] + _opts(
        ("--deep", a.deep), ("--account", a.account), ("--folder", a.folder)
    )
    return job_submit(argv, a.wait_s)


@tool(
    "vault.gmail_search",
    effect="read",
    labels_out=("private",),
    rate="60/h",
    timeout_s=120,
    examples=({"query": "from:example.com newer_than:30d"},),
)
def gmail_search(a: GmailSearchIn) -> ResultOut:
    """Mail hits for a Gmail search (refs, dates, senders, categories; no text): refs for vault_gmail_get / ask."""
    argv = ["gmail", "search", a.query, "-n", str(a.n), "--no-text", "--json"] + _opts(
        ("--account", a.account), ("--folder", a.folder)
    )
    d = vault_json(argv, "gmail search", timeout=110)
    return ResultOut(result=d if isinstance(d, dict) else {"hits": d})


@tool("vault.plans", effect="read", labels_out=("private",), rate="60/min", examples=({}, {"days": 7}))
def plans(a: PlansIn) -> PlansOut:
    """Dated plans from his chats and mails (the vault's event feed): calendar suggestions, not yet checked
    against the calendar (plans-watch does that: calendar_plans)."""
    d = vault_json(["plans", "--days", str(a.days), "--json"], "plans", timeout=70)
    return PlansOut(start=d.get("from"), end=d.get("to"), plans=list(d.get("plans") or []))


@tool("vault.events", effect="read", labels_out=("private",), rate="60/min", examples=({"since": "1200", "max": 50},))
def events(a: EventsIn) -> EventsOut:
    """The vault's live event feed after a cursor: new messages and mails, triaged + anonymized, HA flags.
    A pure read: the cron readers keep their own cursors."""
    argv = ["events", "--since", a.since, "--max", str(a.max), "--json"] + _opts(("--source", a.source))
    d = vault_json(argv, "events", timeout=70)
    return EventsOut(
        events=list(d.get("events") or []),
        next_cursor=d.get("next_cursor"),
        more=bool(d.get("more")),
        reset=bool(d.get("reset")),
    )


@tool("vault.ha_now", effect="read", labels_out=("private",), rate="120/min", idempotent=True, examples=({},))
def ha_now(a: Empty) -> HaNowOut:
    """His current coarse flags from the vault's HA feed: zone or away + distance band (+ town), drive, activity,
    sleep, alarm, battery, and their age. Never coordinates or streets."""
    d = vault_json(["ha", "now", "--json"], "ha now", timeout=40)
    keep = set(HaNowOut.model_fields)
    return HaNowOut.model_validate({k: v for k, v in d.items() if k in keep})


@tool("vault.status", effect="read", labels_out=("counts",), rate="60/min", idempotent=True, examples=({},))
def status(a: Empty) -> CountsOut:
    """The vault's own state: LLM, Telegram login and sync, sources (no data)."""
    return CountsOut(counts=vault_json(["status", "--json"], "status", timeout=40))


@tool("vault.inbox", effect="read", labels_out=("counts",), rate="60/min", idempotent=True, examples=({},))
def inbox(a: Empty) -> CountsOut:
    """Inbox zero, counts only: needs-you (now/today/week, by source), FYI, newsletters, drafts ready.
    The list itself lives only in his vault bot (/inbox there)."""
    return CountsOut(counts=vault_json(["inbox", "--json"], "inbox", timeout=70))
