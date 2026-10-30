"""Calendar helpers: typed tools over the token-free calendar side of the harness. The Google Calendar itself stays
the claude.ai calendar tools (list/create/update events); these read and feed what scripts use:
calcache (the cached next 21 days: events, busy windows, towns; import after list_events), tripclock (his local
time while away) and plans-watch (dated plans from his chats and mails not yet in the calendar or reminders).
Time phrases: reminders_when. Env: TOOLS_CALCACHE_BIN, TOOLS_TRIPCLOCK_BIN, TOOLS_PLANSWATCH_BIN.
"""

from __future__ import annotations

import os
import re
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from tools.clirun import bin_path, first_line, home, parse_json, run
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "calcache (CLI, managed): the cache scripts read (train-watch, flight-watch, plans-watch, night plans), "
    "refreshed by the daily calcache-refresh note: list_events for 21 days → save the JSON → `calendar_import` "
    "(or `calcache import FILE --consume`). Titles and towns only: descriptions and streets are never cached. "
    "tripclock: his zone (home outside trips). plans-watch (cron */5): ~/inbox/vault-plans-*.md; every item ends "
    "with `calendar_plan_done` (= `plans-watch done KEY --as …`)."
)

REGISTRY.add_domain(
    Domain(
        name="calendar",
        title="Calendar helpers",
        summary="The calendar cache scripts read (events, busy windows, towns; import after list_events), his local"
        " time while travelling, and the dated plans from his chats and mails that still need handling.",
        feature="gateway.calendar",
        notes=NOTES,
    )
)

DATE = r"^(\d{4}-\d\d-\d\d|today|tomorrow|\+\d{1,2})$"


def _calcache() -> str:
    return bin_path("TOOLS_CALCACHE_BIN", "calcache")


def _fail(rc: int, out: str, err: str, what: str) -> ToolError:
    msg = first_line(err, out, default=f"{what}: exit {rc}")
    if what.startswith("calcache") and rc == 3:
        return ToolError("unavailable", msg, "no calendar cache: list_events 21 days → save the JSON → calendar_import")
    if rc == 2:
        return ToolError("invalid_args", msg)
    if rc == 1 and "unknown key" in msg:
        return ToolError("not_found", msg, "calendar_plans lists the open keys")
    return ToolError("failed", msg)


def _json(exe: str, argv: list[str], what: str, ok: tuple[int, ...] = (0,), timeout: float = 30.0) -> Any:
    rc, out, err = run(exe, argv, timeout=timeout)
    if rc not in ok or not out:
        raise _fail(rc, out, err, what)
    return parse_json(out, what)


# ------------------------------------------------------------------ models


class CalEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    start: str
    end: str | None = None
    all_day: bool = False
    title: str
    town: str | None = None
    free: bool = Field(False, description="marked free (his concerts often are; still busy)")
    flags: dict[str, Any] | None = Field(
        default=None, description="travel, busy_evening, away_overnight, abroad, id_check"
    )
    train: dict[str, Any] | None = None
    flight: list[dict[str, Any]] | None = None

    def line(self) -> str:
        span = (
            f"{self.start[:10]} all day"
            if self.all_day
            else f"{self.start[:16].replace('T', ' ')}–{(self.end or '')[11:16]}"
        )
        return f"{span} {self.title}" + (f" · {self.town}" if self.town else "") + (" [free]" if self.free else "")


class EventsIn(ToolInput):
    days: int = Field(7, ge=1, le=21, description="from today on")


class EventsOut(ToolOutput):
    count: int
    events: list[CalEvent]
    note: str | None = Field(default=None, description="calcache's warning, e.g. a stale cache")

    def render(self, fmt: Format = "concise") -> str:
        lines = [e.line() for e in self.events] or ["no events"]
        return "\n".join(lines + ([f"note: {self.note}"] if self.note else []))


class DaysIn(ToolInput):
    date: str = Field("today", pattern=DATE, description="YYYY-MM-DD, today, tomorrow or +N")
    days: int = Field(1, ge=1, le=21)


class BusyDay(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: str
    covered: bool = Field(description="inside the cached window")
    busy: list[list[str]] = Field(default_factory=list, description="merged timed windows [HH:MM, HH:MM]")
    evening_busy: bool = False
    away_overnight: bool = False
    all_day: int = 0
    travel: list[str] = Field(default_factory=list)


class BusyOut(ToolOutput):
    days: list[BusyDay]
    note: str | None = None

    def render(self, fmt: Format = "concise") -> str:
        out = []
        for d in self.days:
            if not d.covered:
                out.append(f"{d.date}: not cached")
                continue
            bits = ["busy " + ", ".join(f"{s}–{e}" for s, e in d.busy) if d.busy else "free"]
            bits += ["evening busy"] * d.evening_busy + ["away overnight"] * d.away_overnight
            bits += [f"{d.all_day} all-day"] if d.all_day else []
            out.append(f"{d.date}: " + " · ".join(bits))
        return "\n".join(out + ([f"note: {self.note}"] if self.note else []))


class TownsDay(BaseModel):
    date: str
    covered: bool
    towns: list[str]


class TownsOut(ToolOutput):
    days: list[TownsDay]

    def render(self, fmt: Format = "concise") -> str:
        return "\n".join(f"{d.date}: " + (", ".join(d.towns) or "-" if d.covered else "not cached") for d in self.days)


class CacheOut(ToolOutput):
    cached: bool
    fetched: str | None = None
    hours: float | None = Field(default=None, description="age in hours (refresh daily; stale over 36)")
    events: int | None = None
    window: list[str] | None = None

    def render(self, fmt: Format = "concise") -> str:
        if not self.cached:
            return "no calendar cache"
        return f"fetched {self.fetched} ({self.hours} h ago) · {self.events} events · window {self.window}"


def _safe_file(p: str) -> str:
    rp = os.path.realpath(p)
    roots = [os.path.realpath(home()), "/tmp"]
    if not (os.path.isabs(p) and p.endswith(".json") and any(rp == r or rp.startswith(r + "/") for r in roots)):
        raise ValueError("an absolute .json path under the home dir or /tmp")
    if any(seg in (".secrets", ".ssh") for seg in rp.split("/")):
        raise ValueError("not a secrets path")
    return p


class ImportIn(ToolInput):
    files: list[Annotated[str, AfterValidator(_safe_file)]] = Field(
        min_length=1, max_length=10, description="saved list_events results (JSON), absolute paths"
    )
    days: int = Field(21, ge=1, le=90, description="window from today")
    consume: bool = Field(True, description="delete the input files after a successful import")


class ImportOut(ToolOutput):
    events: int | None = None
    travel: int | None = None
    skipped: int | None = None
    message: str | None = Field(default=None, description="calcache's line")
    DETAIL: ClassVar[tuple[str, ...]] = ("message",)

    def render(self, fmt: Format = "concise") -> str:
        return self.message or f"cached {self.events} events"


class ClockIn(ToolInput):
    at: str | None = Field(
        None,
        pattern=r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d)?([+-]\d\d:\d\d|Z)?|@\d{9,11})$",
        description="another moment (home wall time 'YYYY-MM-DD HH:MM', ISO with offset, @EPOCH); default now",
    )


class ClockOut(ToolOutput):
    local: str = Field(description="his wall clock YYYY-MM-DD HH:MM")
    zone: str
    offset: str | None = None
    home: str | None = Field(default=None, description="the same moment at home")
    home_zone: str | None = None
    away: bool = Field(description="on a trip in another zone")

    def render(self, fmt: Format = "concise") -> str:
        return f"{self.local} {self.zone} ({self.offset})" + (f" · home {self.home}" if self.away else "")


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    date: str
    start: str | None = None
    end: str | None = None
    state: str | None = None
    cat: str | None = Field(default=None, description="sale | deadline | group | proposal | plan")
    kind: str | None = None
    what: str | None = None
    venue: str | None = None
    status: str | None = None
    source: str | None = None
    chat: str | None = None
    ref: str | None = None
    date_note: str | None = Field(default=None, description="check the date (the vault may have read it wrong)")
    mentions: int | None = None


class PlansOut(ToolOutput):
    count: int
    plans: list[Plan]
    DETAIL: ClassVar[tuple[str, ...]] = ()

    def render(self, fmt: Format = "concise") -> str:
        if not self.plans:
            return "nothing open"
        return "\n".join(
            f"{p.key}  {p.date}{' ' + p.start if p.start else ''}  {p.cat or ''}  {p.what or ''}"
            + (f", {p.venue}" if p.venue else "")
            + f" · {p.chat or '?'}"
            + (f"  ⚠ {p.date_note}" if p.date_note else "")
            for p in self.plans
        )


class PlanDoneIn(ToolInput):
    keys: list[Annotated[str, Field(pattern=r"^[0-9a-f]{8}$")]] = Field(min_length=1, max_length=20)
    outcome: Literal["added", "reminded", "asked", "skipped", "exists"] = Field(
        description="added to the calendar · reminder set · asked him · not his / noise · already there"
    )
    why: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,40}$", description="one word")


class PlanDoneOut(ToolOutput):
    acked: int
    message: str

    def render(self, fmt: Format = "concise") -> str:
        return self.message


# ------------------------------------------------------------------ tools


@tool("calendar.events", effect="read", labels_out=("owner_text",), rate="120/min", examples=({"days": 7},))
def events(a: EventsIn) -> EventsOut:
    """Calendar events from the cache (titles, times, towns, travel flags), from today on. Fresh truth for
    changes: the calendar tools (list_events)."""
    rc, out, err = run(_calcache(), ["show", "--days", str(a.days), "--json"], timeout=30)
    if rc != 0 or not out:
        raise _fail(rc, out, err, "calcache show")
    evs = [CalEvent.model_validate(e) for e in parse_json(out, "calcache show") if isinstance(e, dict)]
    return EventsOut(count=len(evs), events=evs, note=first_line(err) or None)


@tool(
    "calendar.busy",
    effect="read",
    labels_out=("owner_text",),
    rate="120/min",
    examples=({"date": "tomorrow"}, {"date": "2026-11-05", "days": 2}),
)
def busy(a: DaysIn) -> BusyOut:
    """Busy windows per day (timed events merged; free-marked ones count), evening busy 18–23, away the night
    after, all-day markers."""
    rc, out, err = run(_calcache(), ["busy", a.date, "--days", str(a.days), "--json"], timeout=30)
    if rc not in (0, 4) or not out:
        raise _fail(rc, out, err, "calcache busy")
    rows = parse_json(out, "calcache busy")
    rows = rows if isinstance(rows, list) else [rows]
    return BusyOut(days=[BusyDay.model_validate(r) for r in rows], note=first_line(err) or None)


@tool("calendar.towns", effect="read", labels_out=("owner_text",), rate="120/min", examples=({"date": "today"},))
def towns(a: DaysIn) -> TownsOut:
    """The towns of each day's events (and train destinations), in order: where he will be."""
    rc, out, err = run(_calcache(), ["towns", a.date, "--days", str(a.days), "--json"], timeout=30)
    if rc not in (0, 4) or not out:
        raise _fail(rc, out, err, "calcache towns")
    rows = parse_json(out, "calcache towns")
    rows = rows if isinstance(rows, list) else [rows]
    return TownsOut(days=[TownsDay.model_validate(r) for r in rows])


@tool("calendar.cache", effect="read", labels_out=("counts",), rate="120/min", idempotent=True, examples=({},))
def cache(a: ToolInput) -> CacheOut:
    """Age and size of the calendar cache (refresh when older than a day)."""
    d = _json(_calcache(), ["age", "--json"], "calcache age", ok=(0, 1))
    return CacheOut.model_validate(d)


@tool(
    "calendar.import",
    effect="home",
    rate="20/h",
    examples=({"files": ["/tmp/calendar-import.json"]},),
)
def import_(a: ImportIn) -> ImportOut:
    """Refresh the cache from saved list_events results (the MCP JSON); consume deletes the inputs after."""
    argv = ["import", *a.files, "--days", str(a.days)] + (["--consume"] if a.consume else [])
    rc, out, err = run(_calcache(), argv, timeout=60)
    if rc != 0:
        raise _fail(rc, out, err, "calcache import")
    m = re.search(r"cached (\d+) events \((\d+) travel, (\d+)", out)
    n = [int(x) for x in m.groups()] if m else [None, None, None]
    return ImportOut(events=n[0], travel=n[1], skipped=n[2], message=first_line(out, default="imported"))


@tool("calendar.clock", effect="read", rate="120/min", idempotent=True, examples=({}, {"at": "2026-11-25 09:00"}))
def clock(a: ClockIn) -> ClockOut:
    """His local time and zone (home outside trips; the trip's zone while away), and the same moment at home."""
    argv = ["now", "--json"] + (["--at", a.at] if a.at else [])
    d = _json(bin_path("TOOLS_TRIPCLOCK_BIN", "tripclock"), argv, "tripclock now")
    keep = set(ClockOut.model_fields)
    return ClockOut.model_validate({k: v for k, v in d.items() if k in keep})


@tool("calendar.plans", effect="read", labels_out=("private",), rate="60/min", idempotent=True, examples=({},))
def plans(a: ToolInput) -> PlansOut:
    """Dated plans from his chats and mails nobody handled yet (plans-watch, after the calendar and reminder
    check): add, remind, ask or skip each, then calendar_plan_done."""
    d = _json(bin_path("TOOLS_PLANSWATCH_BIN", "plans-watch"), ["open", "--json"], "plans-watch open")
    rows = [Plan.model_validate(r) for r in d or [] if isinstance(r, dict)]
    return PlansOut(count=len(rows), plans=rows)


@tool(
    "calendar.plan_done",
    effect="home",
    rate="60/min",
    idempotent=True,
    examples=({"keys": ["1a2b3c4d"], "outcome": "added"},),
)
def plan_done(a: PlanDoneIn) -> PlanDoneOut:
    """Close plans-watch items: added (calendar) · reminded · asked (him) · skipped (not his / noise) · exists."""
    argv = ["done", *a.keys, "--as", a.outcome] + (["--why", a.why] if a.why else [])
    rc, out, err = run(bin_path("TOOLS_PLANSWATCH_BIN", "plans-watch"), argv, timeout=30)
    if rc != 0:
        raise _fail(rc, out, err, "plans-watch done")
    return PlanDoneOut(acked=len(a.keys), message=first_line(out, default="acked"))
