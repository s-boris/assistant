"""Reminders: typed tools over the `remind` CLI (~/bin/remind, managed in this repo as scripts/remind).

Each tool runs remind's own code path in a subprocess (its lock, store, trip clock, analytics and delivery stay
as they are). Firing: the hub's durable per-slot timers (workflow reminder.due), which call reminders.fire at the
due instant, the hub job `remind hub tick` (a due pass every 5 min) and cron's `remind place-due`; the store stays
the single source of truth, so any mix of triggers fires a slot at most once (hub-sentinel's `remind hub fallback`
while the hub is down included). Env: TOOLS_REMIND_BIN (the binary).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    '`remind` (CLI for scripts and cron): `add "YYYY-MM-DD HH:MM" "text"` (my dated follow-ups) · `add --owner`'
    " (his) · `add --call` (rings him once via `voice-relay call`, then the usual text; any hour) · `add --on"
    " home|leave|zone:NAME|leave:zone:NAME [--every] [--urgent] [--tag T]` (place reminders, cron `remind place-due`;"
    " quiet hours held to 08:00 unless --urgent) · `when PHRASE [--json]` (EN/DE time or place phrases; add, snooze"
    " and move take them too) · `move N|ID WHEN` · `cancel/skip N|ID` (`cancel --tag T|--match TEXT`) · `list"
    " [--ids|--json [--mine]]` · `snooze ID WHEN` · `done ID` (both idempotent; series IDs refused: skip/cancel) ·"
    " `buttons ID` + `press [DATA]` (the `rm:` reminder buttons) · `actions ID` + `act VERB ARG` (phone app) ·"
    " `due` (the due pass: run by the hub timers, `hub tick` and `hub fallback`, never by cron) · `hub"
    " status|sync [--full]|tick|fire ID AT|fallback`. IDs are 8-hex; owner reminders go out via `tg-buttons`, then"
    " `tg`, then a note for me (typed `note.reminder*` event). Managed. Flags, env and internals: `remind --help`."
)

REGISTRY.add_domain(
    Domain(
        name="reminders",
        title="Reminders",
        summary="His reminders (Telegram at the minute, by cron, no tokens) and my own dated notes; by time, "
        "series or place. Times are home wall time; while he is away, phrases are read on his clock.",
        feature="gateway.reminders",
        notes=NOTES,
    )
)

ID_PATTERN = r"^[0-9a-f]{8}$"
PLACE_PATTERN = r"^(home|leave|(leave:)?zone:\S.*)$"
Via = Literal["telegram", "voice", "app"]
STAMP = r"\w{3} (\d{4}-\d\d-\d\d \d\d:\d\d)"


def _bin() -> str:
    return os.environ.get("TOOLS_REMIND_BIN") or os.path.join(os.path.expanduser("~"), "bin", "remind")


def _run(
    *argv: str, via: str | None = None, timeout: float = 30.0, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    env = dict(os.environ, **(env or {}))
    if via:
        env["REMIND_VIA"] = via
    try:
        p = subprocess.run(
            [_bin(), *argv], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env
        )
    except FileNotFoundError as e:
        raise ToolError("unavailable", "remind is not installed", "deploy it: `assistant land`") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError("timeout", f"remind {argv[0]} took over {timeout:.0f} s", "check reminders_list, retry") from e
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _fail(rc: int, out: str, err: str, verb: str) -> ToolError:
    msg = (err or out or f"remind {verb}: exit {rc}").splitlines()[0].removeprefix("remind: ")[:300]
    if "no such reminder" in msg or "no reminder with ID" in msg:
        return ToolError("not_found", msg, "reminders_list shows the IDs")
    if rc == 2 or "bad " in msg:
        return ToolError("invalid_args", msg, "reminders_when checks a time phrase; IDs are 8 hex characters")
    return ToolError("failed", msg)


def _items(mine: bool = False) -> dict[str, Any]:
    rc, out, err = _run("list", "--json", *(["--mine"] if mine else []))
    if rc != 0:
        raise _fail(rc, out, err, "list")
    try:
        return json.loads(out)
    except ValueError as e:
        raise ToolError("failed", "remind list --json: not JSON", "run `remind list` to see why") from e


# ------------------------------------------------------------------ models


class Reminder(BaseModel):
    model_config = ConfigDict(extra="ignore")  # remind may add keys: tolerated

    id: str
    kind: Literal["timed", "series", "place"]
    text: str
    owner: bool = Field(description="his reminder (Telegram); false = a note for me")
    at: str | None = Field(None, description="next time, home wall time YYYY-MM-DD HH:MM")
    local: str | None = Field(None, description="the same time on his clock while he is away")
    every: str | None = None
    until: str | None = None
    on: str | None = Field(None, description="place trigger: home | leave | zone:NAME | leave:zone:NAME")
    where: str | None = None
    every_time: bool | None = None
    held: str | None = Field(None, description="fired in quiet hours: held until this time")
    call: bool = False
    tag: str | None = None

    def line(self) -> str:
        when = f"📍 {self.where or self.on}" if self.kind == "place" else (self.at or "?")
        extra = (f" (every {self.every})" if self.every else "") + (" 📞" if self.call else "")
        return f"{self.id}  {when}  {self.text}{extra}{'' if self.owner else '  [mine]'}"


class Fired(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    text: str
    state: str | None = None
    fired: str | None = None
    to: str | None = None
    on: str | None = None


class WhenIn(ToolInput):
    phrase: str = Field(
        min_length=1,
        max_length=200,
        description="e.g. 'tomorrow 9', 'friday 18:00', 'in 2 hours', "
        "'morgen früh', 'after the concert', '2h before my flight', 'when I get home'",
    )


class WhenOut(ToolOutput):
    kind: Literal["at", "on"] = Field(description="at = a time, on = a place")
    at: str | None = Field(None, description="home wall time YYYY-MM-DD HH:MM")
    on: str | None = None
    why: str | None = Field(None, description="how the phrase was read")
    form: str | None = Field(None, description="exact | day | relative | calendar | place")
    DETAIL: ClassVar[tuple[str, ...]] = ("form",)

    def render(self, fmt: Format = "concise") -> str:
        return (f"--on {self.on}" if self.kind == "on" else f"{self.at}") + (f"  ({self.why})" if self.why else "")


class ListIn(ToolInput):
    mine: bool = Field(False, description="only his reminders (owner + place ones), not my notes")
    query: str | None = Field(None, max_length=100, description="only reminders whose text contains this (any case)")
    recent: bool = Field(False, description="also his reminders fired in the last 36 h (for snooze/done)")
    limit: int = Field(50, ge=1, le=500)


class ListOut(ToolOutput):
    now: str
    count: int = Field(description="matching reminders (before limit)")
    items: list[Reminder]
    recent: list[Fired] | None = None

    def render(self, fmt: Format = "concise") -> str:
        lines = [f"{self.count} reminder(s), now {self.now}"] + [r.line() for r in self.items]
        if self.count > len(self.items):
            lines.append(f"… {self.count - len(self.items)} more (raise limit)")
        for f in self.recent or []:
            lines.append(f"fired {f.fired}: {f.id} {f.text} [{f.state or 'sent'}]")
        return "\n".join(lines)


class AddIn(ToolInput):
    text: str = Field(min_length=1, max_length=1000, description="what to remind (his words, short)")
    owner: bool = Field(description="true = his reminder (Telegram at the time); false = a note for me (inbox)")
    when: str | None = Field(
        None,
        max_length=200,
        description="'YYYY-MM-DD HH:MM' (home wall time) or a phrase "
        "('tomorrow 9', 'friday 18:00', 'in 2 hours', 'after the concert'); a place phrase "
        "('when I get home') becomes `on`",
    )
    on: str | None = Field(
        None,
        pattern=PLACE_PATTERN,
        description="place trigger instead of a time: home | leave | zone:NAME | leave:zone:NAME (his reminders only)",
    )
    every: str | None = Field(
        None,
        max_length=40,
        description="repeat rule (timed only): daily | weekdays | weekends"
        " | weekly[:mon,thu] | monthly[:DAY|last] | yearly | every:Nd | every:Nw",
    )
    until: str | None = Field(None, pattern=r"^\d{4}-\d\d-\d\d$", description="last day of a series (with every)")
    every_time: bool = Field(False, description="place only: fire each time, not just next time")
    urgent: bool = Field(False, description="place only: fire in quiet hours too")
    call: bool = Field(False, description="also ring him at the time (wake or departure call; his only)")
    soft: bool = Field(False, description="my nudge to him: waits out a sleep-in morning")
    tag: str | None = Field(None, pattern=r"^[A-Za-z0-9_.:-]{1,40}$", description="machine key for cancel by tag")
    via: Via | None = Field(None, description="who asked: telegram | voice | app (analytics)")

    @model_validator(mode="after")
    def _shape(self) -> AddIn:
        if (self.when is None) == (self.on is None):
            raise ValueError("give exactly one of when (a time) or on (a place)")
        if self.on is not None and not self.owner:
            raise ValueError("place reminders are his: owner must be true")
        if self.call and not self.owner:
            raise ValueError("call rings him: owner must be true")
        if self.until and not self.every:
            raise ValueError("until needs every")
        if self.on is not None and self.every:
            raise ValueError("place reminders repeat with every_time, not every")
        if self.on is None and (self.every_time or self.urgent):
            raise ValueError("every_time and urgent are for place reminders")
        if self.text.startswith("-"):
            raise ValueError("text must not start with '-'")
        return self


class AddOut(ToolOutput):
    id: str | None = Field(None, description="the new reminder's ID (None if it already fired)")
    kind: Literal["timed", "series", "place"]
    owner: bool
    at: str | None = None
    local: str | None = None
    every: str | None = None
    until: str | None = None
    on: str | None = None
    where: str | None = None
    call: bool = False
    message: str | None = Field(None, description="remind's confirmation line")
    DETAIL: ClassVar[tuple[str, ...]] = ("message",)

    def render(self, fmt: Format = "concise") -> str:
        return f"{self.id or '?'}: {self.message}"


class IdIn(ToolInput):
    id: str = Field(pattern=ID_PATTERN, description="the reminder's 8-hex ID (reminders_list, or the button data)")


class SnoozeIn(IdIn):
    when: str = Field(
        min_length=1,
        max_length=200,
        description="+1h | +Nm | +Nh | +Nd | tonight | tomorrow | next "
        "(a place reminder's next time) | 'YYYY-MM-DD HH:MM' | a phrase",
    )


class MoveIn(IdIn):
    when: str = Field(
        min_length=1,
        max_length=200,
        description="new time: 'YYYY-MM-DD HH:MM', +2h, tomorrow or a phrase ('friday 18:00')",
    )


class ChangeOut(ToolOutput):
    id: str
    at: str | None = Field(None, description="the new time, home wall time")
    message: str


class CancelIn(ToolInput):
    id: str | None = Field(None, pattern=ID_PATTERN, description="one reminder (a series goes whole)")
    tag: str | None = Field(None, pattern=r"^[A-Za-z0-9_.:-]{1,40}$", description="every reminder with this tag")
    match: str | None = Field(
        None, min_length=2, max_length=100, description="every reminder whose text contains this (any case)"
    )

    @model_validator(mode="after")
    def _one(self) -> CancelIn:
        if sum(x is not None for x in (self.id, self.tag, self.match)) != 1:
            raise ValueError("give exactly one of id, tag or match")
        return self


class CancelOut(ToolOutput):
    cancelled: int
    lines: list[str] = Field(description="remind's lines: time + text of each cancelled one")

    def render(self, fmt: Format = "concise") -> str:
        return "\n".join([f"cancelled {self.cancelled}"] + self.lines) if self.cancelled else "no matching reminder"


class PressIn(ToolInput):
    data: str = Field(
        pattern=r"^rm:(s1h|ston|stom|snxt|done):[0-9a-f]{8}$",
        description="a reminder button's callback data, e.g. rm:s1h:1a2b3c4d",
    )


class PressOut(ToolOutput):
    reply: str = Field(description="the one-line answer (no reminder text)")

    def render(self, fmt: Format = "concise") -> str:
        return self.reply


# ------------------------------------------------------------------ tools


@tool(
    "reminders.when",
    effect="read",
    idempotent=True,
    rate="120/min",
    examples=({"phrase": "tomorrow 9"}, {"phrase": "when I get home"}),
)
def when(a: WhenIn) -> WhenOut:
    """Read a natural time or place phrase the way reminders_add will; stores nothing.
    EN/DE, calendar anchors ('after the concert', '2h before my flight') and places ('when I get home')."""
    rc, out, err = _run("when", a.phrase, "--json")
    try:
        d = json.loads(out)
    except ValueError:
        raise _fail(rc, out, err, "when") from None
    if not d.get("ok"):
        raise ToolError(
            "invalid_args",
            str(d.get("error") or "unreadable phrase")[:200],
            "try 'YYYY-MM-DD HH:MM', 'tomorrow 9', 'in 2 hours'",
        )
    return WhenOut(kind=d["kind"], at=d.get("at"), on=d.get("on"), why=d.get("why"), form=d.get("form"))


@tool(
    "reminders.list",
    effect="read",
    labels_out=("owner_text",),
    idempotent=True,
    rate="120/min",
    examples=({}, {"mine": True, "query": "dentist"}),
)
def list_(a: ListIn) -> ListOut:
    """Pending reminders with their IDs (timed, series, place), soonest first.
    mine = only his; query filters the text; recent adds his ones fired in the last 36 h."""
    d = _items(a.mine)
    items = [Reminder.model_validate({k: v for k, v in r.items() if v is not None}) for r in d.get("items", [])]
    if a.query:
        q = a.query.lower()
        items = [r for r in items if q in r.text.lower()]
    recent = [Fired.model_validate({k: v for k, v in f.items() if v is not None}) for f in d.get("recent", [])]
    return ListOut(now=d.get("now", ""), count=len(items), items=items[: a.limit], recent=recent if a.recent else None)


@tool(
    "reminders.add",
    effect="home",
    labels_out=("owner_text",),
    rate="30/min",
    examples=(
        {"text": "call the garage", "owner": True, "when": "tomorrow 9", "via": "telegram"},
        {"text": "water the plants", "owner": True, "when": "2026-11-02 18:00", "every": "weekly"},
        {"text": "take the parcel slip", "owner": True, "on": "leave"},
    ),
)
def add(a: AddIn) -> AddOut:
    """Add a reminder: his (owner=true, Telegram at the minute by cron) or a note for me (owner=false).
    Give when (a time or phrase) or on (a place). every repeats; call also rings him."""
    argv = ["add"] + (["--owner"] if a.owner else []) + (["--call"] if a.call else []) + (["--soft"] if a.soft else [])
    for flag, val in (("--tag", a.tag), ("--via", a.via), ("--every", a.every), ("--until", a.until)):
        if val:
            argv += [flag, val]
    before = {r["id"] for r in _items().get("items", [])}
    if a.on is not None:
        argv += ["--on", a.on] + (["--every"] if a.every_time else []) + (["--urgent"] if a.urgent else []) + [a.text]
    else:
        argv += [a.when or "", a.text]
    rc, out, err = _run(*argv, via=a.via)
    if rc != 0 or not out.startswith("added"):
        raise _fail(rc, out, err, "add")
    m = re.match(r"added location reminder ([0-9a-f]{8})", out)
    rid = m[1] if m else None
    after = _items().get("items", [])
    if rid is None:
        new = [r for r in after if r["id"] not in before and r["text"] == a.text]
        rid = new[-1]["id"] if new else None
    item = next((r for r in after if r["id"] == rid), None) if rid else None
    if item:
        return AddOut(
            id=rid,
            kind=item["kind"],
            owner=item["owner"],
            at=item.get("at"),
            local=item.get("local"),
            every=item.get("every"),
            until=item.get("until"),
            on=item.get("on"),
            where=item.get("where"),
            call=bool(item.get("call")),
            message=out.splitlines()[0],
        )
    at = re.search(rf"reminder for {STAMP}", out)
    kind = "place" if m else ("series" if a.every else "timed")
    return AddOut(
        id=rid,
        kind=kind,
        owner=a.owner or kind == "place",
        at=at[1] if at else None,
        call=a.call,
        message=out.splitlines()[0],
    )


def _change(verb: str, a: SnoozeIn | MoveIn) -> ChangeOut:
    rc, out, err = _run(verb, a.id, a.when)
    if rc != 0:
        raise _fail(rc, out, err, verb)
    at = re.search(rf"(?:snoozed|moved) to {STAMP}", out)
    return ChangeOut(id=a.id, at=at[1] if at else None, message=out.splitlines()[0] if out else verb)


@tool(
    "reminders.snooze",
    effect="home",
    labels_out=("owner_text",),
    idempotent=True,
    rate="30/min",
    examples=({"id": "1a2b3c4d", "when": "+1h"}, {"id": "1a2b3c4d", "when": "tomorrow"}),
)
def snooze(a: SnoozeIn) -> ChangeOut:
    """Snooze a reminder (pending or fired in the last 30 days) by its ID: it comes back at the new time.
    A second snooze moves the same entry again; next = a place reminder's next time there."""
    return _change("snooze", a)


@tool(
    "reminders.move",
    effect="home",
    labels_out=("owner_text",),
    idempotent=True,
    rate="30/min",
    examples=({"id": "1a2b3c4d", "when": "friday 18:00"},),
)
def move(a: MoveIn) -> ChangeOut:
    """Move a pending one-off reminder to a new time (not a series: cancel or skip that)."""
    return _change("move", a)


@tool(
    "reminders.cancel",
    effect="home",
    labels_out=("owner_text",),
    destructive=True,
    rate="30/min",
    examples=({"id": "1a2b3c4d"}, {"tag": "bill:3"}),
)
def cancel(a: CancelIn) -> CancelOut:
    """Cancel reminders: one by id (a series goes whole), or all with a tag, or all whose text matches."""
    argv = ["cancel", a.id] if a.id else (["cancel", "--tag", a.tag] if a.tag else ["cancel", "--match", a.match or ""])
    rc, out, err = _run(*argv)
    if rc != 0:
        raise _fail(rc, out, err, "cancel")
    lines = [ln.removeprefix("cancelled: ") for ln in out.splitlines() if ln.startswith("cancelled: ")]
    if a.id and not lines:
        raise ToolError("not_found", f"no reminder {a.id}", "reminders_list shows the IDs")
    return CancelOut(cancelled=len(lines), lines=lines)


@tool("reminders.press", effect="home", idempotent=True, rate="60/min", examples=({"data": "rm:s1h:1a2b3c4d"},))
def press(a: PressIn) -> PressOut:
    """Run a reminder button (rm:s1h|ston|stom|snxt|done:ID): +1 h, tonight 19:00, tomorrow 09:00, next time, done."""
    rc, out, err = _run("press", env={"TELEGRAM_CALLBACK_DATA": a.data})  # the instant form: one line, no text
    if rc != 0 and not out:
        raise _fail(rc, out, err, "press")
    if rc != 0:
        raise ToolError("not_found" if "no reminder" in out else "failed", out[:200], "reminders_list shows the IDs")
    return PressOut(reply=out.splitlines()[0] if out else "ok")


class FireIn(ToolInput):
    id: str = Field(pattern=ID_PATTERN, description="the reminder's ID (8 hex)")
    at: str = Field(pattern=r"^\d{4}-\d\d-\d\d \d\d:\d\d$", description="the slot it is due at, home wall time")


class FireOut(ToolOutput):
    id: str
    at: str
    decision: str = Field(description="fired | gone | held | early (off: remind's test state without REMIND_HUB=1)")
    mode: str = Field(description="live (off: remind's test state)")
    late_s: float | None = None

    def render(self, fmt: Format = "concise") -> str:
        late = f", {self.late_s:g} s after its time" if self.late_s is not None else ""
        return f"{self.id} {self.at}: {self.decision} ({self.mode}{late})"


@tool(
    "reminders.fire",
    effect="send_owner",
    idempotent=True,
    rate="600/h",
    timeout_s=300,
    examples=({"id": "1a2b3c4d", "at": "2026-10-24 09:00"},),
)
def fire(a: FireIn) -> FireOut:
    """The hub timer's call at a reminder's due instant: remind fires it if its store still holds that slot."""
    rc, out, err = _run("hub", "fire", a.id, a.at, timeout=290.0)
    if rc != 0:
        raise _fail(rc, out, err, "hub fire")
    try:
        d = json.loads(out.splitlines()[-1]) if out else {}
    except ValueError as e:
        raise ToolError("failed", "remind hub fire: not JSON", "run `remind hub status`") from e
    late = d.get("late_s")
    late_s = float(late) if isinstance(late, (int, float)) and not isinstance(late, bool) else None
    dec, mode = str(d.get("decision") or "error"), str(d.get("mode") or "off")
    return FireOut(id=a.id, at=a.at, decision=dec, mode=mode, late_s=late_s)
