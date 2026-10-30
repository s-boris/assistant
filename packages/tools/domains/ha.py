"""Home Assistant: typed tools over `ha` (~/bin/ha), the one safe HA client: privacy-gated reads, allowlisted
writes with a read-back (old -> new), every write logged in ~/.local/state/ha/actions.log.

The tools never talk to HA themselves and never widen ha's rules: one subprocess of `ha` per call, an allowlist of
its subcommands and options (guard()), values that start with "-" refused. Targets ha marks as needing his words
(garage/gate/door covers, switches, buttons, update.install) are not reachable from a tool: ha refuses them and the
tool answers approval_required (his OK, then `ha … --owner-ok "his words"` from Bash). Config, registry and
automation changes stay with ha-apply (admin, his ✅). Env: TOOLS_HA_BIN.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from tools.clirun import bin_path, first_line, no_dash, parse_json, run
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    '`ha` (CLI, managed; skill home-assistant): the same reads and writes by hand, plus `--owner-ok "his words"` '
    "for garage/gate/door covers, switches, buttons and update.install (never a tool), `ha todo` (the shopping "
    "list: `shop`, skill shopping) and `ha device-url`. Exit: 0 ok, 1 HA/connection error, 2 refused/ambiguous, "
    "3 sent but not confirmed (the tools return confirmed=false, never retry blindly). Cleanup, config and "
    "automations: ha-apply / ha-audit (admin, his ✅)."
)

REGISTRY.add_domain(
    Domain(
        name="ha",
        title="Home Assistant",
        summary="His Home Assistant through `ha`: privacy-gated states, history, logbook and automation traces;"
        " allowlisted writes (lights, switches, media, scenes, scripts, vacuum, helpers) with a read-back.",
        feature="gateway.ha",
        notes=NOTES,
    )
)

SUBCOMMANDS = frozenset(
    {"state", "list", "find", "areas", "history", "logbook", "trace", "on", "off", "toggle", "set", "media", "scene"}
    | {"script", "call"}
)
OPTIONS = frozenset(
    {"--json", "--area", "--domain", "--limit", "--since", "--until", "--attrs", "--last", "--dry-run", "--wait"}
    | {"--brightness", "--color-temp", "--color"}
)
ENTITY_ID = r"^[a-z_]+\.[a-z0-9_]+$"
WHEN = r"^(\d{1,4}[mhd]|\d\d:\d\d|\d{4}-\d\d-\d\d([T ]\d\d:\d\d(:\d\d)?([+-]\d\d:\d\d|Z)?)?)$"
Text = Annotated[str, AfterValidator(no_dash)]
Target = Annotated[str, AfterValidator(no_dash), Field(min_length=1, max_length=120)]
Domain_ = Annotated[str, Field(pattern=r"^[a-z_]{2,30}$")]


def guard(argv: list[str]) -> list[str]:
    if not argv or argv[0] not in SUBCOMMANDS:
        raise ToolError("internal", f"ha {argv[:1]}: not a tool path", "a bug: report it")
    for x in argv[1:]:
        if x.startswith("-") and x not in OPTIONS:
            raise ToolError("internal", f"ha {argv[0]}: option {x.split('=')[0]} is not a tool path", "a bug")
    return argv


def _bin() -> str:
    return bin_path("TOOLS_HA_BIN", "ha")


def _fail(rc: int, out: str, err: str, verb: str) -> ToolError:
    msg = first_line(err, out, default=f"ha {verb}: exit {rc}").removeprefix("ha: ")
    if rc == 2:
        if "owner-ok" in msg or "his words" in msg:
            return ToolError(
                "approval_required", msg, 'needs his explicit OK, then `ha … --owner-ok "his words"` from Bash'
            )
        if "ambiguous" in msg or "match" in msg or "no such" in msg or "unknown" in msg:
            return ToolError("invalid_args", msg, "ha_find shows the entity ids; pass one exact id")
        return ToolError("invalid_args", msg, "ha refused it (allowlist): `tool show` and the skill list what works")
    if rc == 1 and any(w in msg for w in ("connect", "timed out", "unreachable", "token", "HTTP 5")):
        return ToolError("unavailable", msg, "HA or its connection is down: retry later")
    return ToolError("failed", msg)


def ha_run(argv: list[str], timeout: float = 45.0) -> tuple[int, str, str]:
    return run(_bin(), guard(argv), timeout=timeout)


def ha_json(argv: list[str], verb: str, timeout: float = 45.0) -> Any:
    rc, out, err = ha_run(argv, timeout)
    if rc != 0 or not out:
        raise _fail(rc, out, err, verb)
    return parse_json(out, f"ha {verb}")


# ------------------------------------------------------------------ read models


class Entity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity_id: str
    name: str | None = None
    area: str | None = None
    state: Any = None
    attributes: dict[str, Any] | None = None
    hidden: Any = Field(default=None, description="details hidden by the privacy gate (count or kinds)")

    def line(self) -> str:
        return (
            f"{self.entity_id}  {self.state}"
            + (f'  "{self.name}"' if self.name else "")
            + (f"  [{self.area}]" if self.area else "")
        )


class StateIn(ToolInput):
    entity: Target = Field(description="an entity id (light.living_room) or a name ('ceiling lamp')")


class StateOut(ToolOutput):
    count: int = Field(description="1, or how many entities matched a name (then: pick one id)")
    entities: list[Entity]

    def render(self, fmt: Format = "concise") -> str:
        if self.count == 1:
            e = self.entities[0]
            attrs = {k: v for k, v in (e.attributes or {}).items() if k not in ("supported_features", "icon")}
            return e.line() + ("\n" + json.dumps(attrs, ensure_ascii=False)[:1200] if attrs else "")
        return f"{self.count} entities match:\n" + "\n".join(e.line() for e in self.entities)


class ListIn(ToolInput):
    domain: Domain_ | None = Field(default=None, description="light, switch, media_player, sensor, vacuum ...")
    area: Text | None = Field(default=None, max_length=60, description="an area name (Living Room, Bedroom ...)")
    limit: int = Field(100, ge=1, le=300)


class FindIn(ToolInput):
    words: Text = Field(min_length=1, max_length=120, description="words of the name, id or area")
    domain: Domain_ | None = None
    limit: int = Field(15, ge=1, le=50)


class EntitiesOut(ToolOutput):
    count: int
    entities: list[Entity]
    DETAIL: ClassVar[tuple[str, ...]] = ()

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        d = super().data(fmt)
        if fmt == "concise":  # the list form: no attribute dumps
            for e in d.get("entities", []):
                e.pop("attributes", None)
        return d

    def render(self, fmt: Format = "concise") -> str:
        return "\n".join([f"{self.count} entities"] + [e.line() for e in self.entities])


class Area(BaseModel):
    name: str
    id: str
    counts: dict[str, int] = Field(description="entities per domain (top 8)")


class AreasOut(ToolOutput):
    areas: list[Area]
    unassigned: int = Field(0, description="entities without an area")

    def render(self, fmt: Format = "concise") -> str:
        return "\n".join(
            [f"{a.name} ({a.id}): " + ", ".join(f"{n} {d}" for d, n in a.counts.items()) for a in self.areas]
            + [f"(no area): {self.unassigned}"]
        )


class HistoryIn(ToolInput):
    entity: Target = Field(description="one entity id or name")
    since: str = Field("24h", pattern=WHEN, description="90m, 24h, 7d, HH:MM or ISO (the recorder keeps ~10 days)")
    until: str | None = Field(default=None, pattern=WHEN)
    attrs: list[Annotated[str, Field(pattern=r"^[a-z0-9_]{1,40}$")]] | None = Field(
        None, max_length=8, description="also these attributes (gated ones stay hidden)"
    )
    limit: int = Field(50, ge=1, le=500)


class HistoryOut(ToolOutput):
    entity_id: str | None = None
    name: str | None = None
    area: str | None = None
    unit: str | None = None
    since: str | None = None
    until: str | None = None
    changes: list[dict[str, Any]] = Field(description="time, state, seconds (+ attrs)")
    earlier_not_shown: int = 0
    attrs_not_seen: list[str] | None = None

    def render(self, fmt: Format = "concise") -> str:
        lines = [f"{self.entity_id}: {len(self.changes)} change(s) {self.since} -> {self.until or 'now'}"]
        lines += [f"  {c.get('time')}  {c.get('state')}  {c.get('seconds')} s" for c in self.changes]
        return "\n".join(lines)


class LogbookIn(ToolInput):
    entity: Target | None = Field(default=None, description="one entity (none: all, hidden ones left out)")
    since: str = Field("24h", pattern=WHEN)
    until: str | None = Field(default=None, pattern=WHEN)
    limit: int = Field(40, ge=1, le=300)


class LogbookOut(ToolOutput):
    entity_id: str | None = None
    since: str | None = None
    until: str | None = None
    entries: list[dict[str, Any]] = Field(description="time, entity_id, name, what, by")
    earlier_not_shown: int = 0
    hidden_entries: int | None = None

    def render(self, fmt: Format = "concise") -> str:
        return "\n".join(
            [f"{len(self.entries)} logbook entries since {self.since}"]
            + [f"  {e.get('time')}  {e.get('name') or e.get('entity_id')}: {e.get('what')}" for e in self.entries]
        )


class TraceIn(ToolInput):
    item: Target = Field(description="an automation or script (id or name)")
    last: int = Field(3, ge=1, le=10)


class TraceOut(ToolOutput):
    entity_id: str | None = None
    name: str | None = None
    stored_runs: int = 0
    runs: list[dict[str, Any]] = Field(description="trigger, conditions passed/failed, steps, errors")

    def render(self, fmt: Format = "concise") -> str:
        lines = [f"{self.entity_id}: {len(self.runs)} of {self.stored_runs} stored runs"]
        for r in self.runs:
            lines.append(f"  {r.get('start') or r.get('when') or ''} {r.get('result') or ''} {r.get('trigger') or ''}")
        return "\n".join(lines)


# ------------------------------------------------------------------ write models

CHANGE_RE = re.compile(r"^(?P<eid>\S+): (?P<rest>.*)$")
MARKS = (
    (" ok (already)", "already"),
    (" ok", "ok"),
    (" (no visible change)", "no_change"),
)


class Change(BaseModel):
    entity_id: str
    old: str | None = None
    new: str | None = None
    status: Literal["ok", "already", "unconfirmed", "no_change", "sent", "dry_run"]


class WriteOut(ToolOutput):
    confirmed: bool = Field(description="every target reached the expected state (read back)")
    dry_run: bool = False
    changes: list[Change]
    notes: list[str] | None = Field(default=None, description="ha's notes (e.g. which entity a name resolved to)")

    def render(self, fmt: Format = "concise") -> str:
        head = "dry run" if self.dry_run else ("ok" if self.confirmed else "sent, NOT confirmed: check before retrying")
        lines = [head] + [
            f"{c.entity_id}: {c.old} -> {c.new} ({c.status})" if c.new is not None else f"{c.entity_id}: {c.old}"
            for c in self.changes
        ]
        return "\n".join(lines + [f"note: {n}" for n in self.notes or []])


def parse_write(rc: int, out: str, err: str, dry: bool) -> WriteOut:
    changes: list[Change] = []
    for ln in out.splitlines():
        if dry:
            m = CHANGE_RE.match(ln.strip())
            if ln.startswith("  ") and m:
                changes.append(Change(entity_id=m["eid"], old=m["rest"], status="dry_run"))
            continue
        m = CHANGE_RE.match(ln)
        if not m or " -> " not in m["rest"]:
            continue
        rest, status = m["rest"], "sent"
        if re.search(r" \(not confirmed after [^)]*\)$", rest):
            rest, status = re.sub(r" \(not confirmed after [^)]*\)$", "", rest), "unconfirmed"
        else:
            for suffix, st in MARKS:
                if rest.endswith(suffix):
                    rest, status = rest[: -len(suffix)], st
                    break
        old, _, new = rest.rpartition(" -> ")
        changes.append(Change(entity_id=m["eid"], old=old, new=new, status=status))  # type: ignore[arg-type]
    notes = [ln.removeprefix("note: ") for ln in err.splitlines() if ln.startswith("note: ")] or None
    return WriteOut(confirmed=rc == 0 and not dry, dry_run=dry, changes=changes, notes=notes)


def _write(argv: list[str], dry_run: bool, wait: float, verb: str) -> WriteOut:
    argv = argv + (["--dry-run"] if dry_run else []) + ["--wait", f"{wait:g}"]
    rc, out, err = ha_run(argv, timeout=wait + 40)
    if rc not in (0, 3):
        raise _fail(rc, out, err, verb)
    return parse_write(rc, out, err, dry_run)


class WriteIn(ToolInput):
    dry_run: bool = Field(False, description="resolve the targets and show them; send nothing")
    wait: float = Field(8.0, ge=0, le=30, description="read-back timeout in seconds")


class SwitchIn(WriteIn):
    action: Literal["on", "off", "toggle"]
    targets: list[Target] | None = Field(
        None, min_length=1, max_length=10, description="one name, or entity ids (several only as ids)"
    )
    area: Text | None = Field(default=None, max_length=60, description="instead of targets: everything in an area")
    domain: Domain_ | None = Field(default=None, description="with area: only this domain (e.g. light)")

    @model_validator(mode="after")
    def _shape(self) -> SwitchIn:
        if (self.targets is None) == (self.area is None):
            raise ValueError("give targets or area")
        if self.domain and not self.area:
            raise ValueError("domain goes with area")
        if self.targets and len(self.targets) > 1 and not all(re.match(ENTITY_ID, t) for t in self.targets):
            raise ValueError("several targets only as entity ids")
        return self


class SetIn(WriteIn):
    target: Target | None = Field(default=None, description="one light (id or name)")
    area: Text | None = Field(default=None, max_length=60, description="instead: all lights in an area")
    brightness: int | None = Field(default=None, ge=0, le=100, description="percent")
    color_temp: int | None = Field(default=None, ge=1500, le=9000, description="kelvin (2700 warm … 6500 cold)")
    color: Annotated[str, Field(pattern=r"^[a-z]{3,20}$")] | None = Field(
        default=None, description="a color name (red …)"
    )

    @model_validator(mode="after")
    def _shape(self) -> SetIn:
        if (self.target is None) == (self.area is None):
            raise ValueError("give target or area")
        if self.brightness is None and self.color_temp is None and self.color is None:
            raise ValueError("give brightness, color_temp or color")
        return self


class MediaIn(WriteIn):
    player: Target = Field(description="a media player (id or name)")
    action: Literal["play", "pause", "stop", "next", "prev", "mute", "unmute", "volume"]
    volume: int | None = Field(default=None, ge=0, le=100, description="with action volume: percent")

    @model_validator(mode="after")
    def _shape(self) -> MediaIn:
        if (self.action == "volume") != (self.volume is not None):
            raise ValueError("volume goes with action volume (and only there)")
        return self


class RunIn(WriteIn):
    kind: Literal["scene", "script"]
    target: Target = Field(description="the scene or script (id or name)")


class CallIn(WriteIn):
    service: str = Field(pattern=r"^[a-z_]+\.[a-z_]+$", description="domain.service on ha's allowlist")
    entity: Target = Field(description="exactly one target entity (id or name) of that domain")
    data: dict[str, Any] | None = Field(default=None, description="service data (no target keys)")

    @model_validator(mode="after")
    def _shape(self) -> CallIn:
        if self.data is not None and len(json.dumps(self.data)) > 2000:
            raise ValueError("data too large")
        return self


# ------------------------------------------------------------------ tools


@tool(
    "ha.state",
    effect="read",
    labels_out=("private",),
    rate="120/min",
    idempotent=True,
    examples=({"entity": "light.living_room"}, {"entity": "ceiling lamp"}),
)
def state(a: StateIn) -> StateOut:
    """One entity in detail (privacy-gated attributes); a name that fits several gives the short list."""
    d = ha_json(["state", a.entity, "--json"], "state")
    rows = d if isinstance(d, list) else [d]
    ents = [Entity.model_validate(r) for r in rows if isinstance(r, dict)]
    return StateOut(count=len(ents), entities=ents)


@tool(
    "ha.list",
    effect="read",
    labels_out=("private",),
    rate="60/min",
    idempotent=True,
    examples=({"domain": "light"}, {"area": "Living Room"}),
)
def list_(a: ListIn) -> EntitiesOut:
    """Entities with state, name and area, by domain and/or area (concise: no attributes)."""
    argv = ["list"] + ([a.domain] if a.domain else []) + (["--area", a.area] if a.area else [])
    d = ha_json(argv + ["--limit", str(a.limit), "--json"], "list")
    ents = [Entity.model_validate(r) for r in d or [] if isinstance(r, dict)]
    return EntitiesOut(count=len(ents), entities=ents)


@tool("ha.find", effect="read", labels_out=("private",), rate="120/min", idempotent=True, examples=({"words": "lamp"},))
def find(a: FindIn) -> EntitiesOut:
    """Fuzzy match on friendly name, entity id and area: the exact ids for the other tools."""
    argv = ["find", a.words] + (["--domain", a.domain] if a.domain else []) + ["--limit", str(a.limit), "--json"]
    rc, out, err = ha_run(argv)
    if rc == 2 and not out.startswith("["):
        return EntitiesOut(count=0, entities=[])
    if rc != 0:
        raise _fail(rc, out, err, "find")
    ents = [Entity.model_validate(r) for r in parse_json(out, "ha find") or [] if isinstance(r, dict)]
    return EntitiesOut(count=len(ents), entities=ents)


AREA_RE = re.compile(r"^(?P<name>.+) \((?P<id>[^()]+)\): (?P<rest>.*)$")


@tool("ha.areas", effect="read", labels_out=("private",), rate="60/min", idempotent=True, examples=({},))
def areas(a: ToolInput) -> AreasOut:
    """Areas with entity counts per domain."""
    rc, out, err = ha_run(["areas"])
    if rc != 0:
        raise _fail(rc, out, err, "areas")
    rows, rest = [], 0
    for ln in out.splitlines():
        if ln.startswith("(no area):"):
            m = re.search(r"(\d+)", ln)
            rest = int(m[1]) if m else 0
            continue
        m = AREA_RE.match(ln)
        if not m:
            continue
        counts = {}
        for part in m["rest"].split(", "):
            n, _, dom = part.partition(" ")
            if n.isdigit() and dom:
                counts[dom] = int(n)
        rows.append(Area(name=m["name"], id=m["id"], counts=counts))
    return AreasOut(areas=rows, unassigned=rest)


@tool(
    "ha.history",
    effect="read",
    labels_out=("private",),
    rate="60/min",
    idempotent=True,
    examples=({"entity": "vacuum.robot", "since": "7d"},),
)
def history(a: HistoryIn) -> HistoryOut:
    """State changes of one entity with durations (recorder ~10 days). Location, network, notification and
    Bluetooth entities stay hidden."""
    argv = ["history", a.entity, "--since", a.since, "--limit", str(a.limit), "--json"]
    argv += (["--until", a.until] if a.until else []) + (["--attrs", ",".join(a.attrs)] if a.attrs else [])
    d = ha_json(argv, "history", timeout=60)
    keep = set(HistoryOut.model_fields)
    return HistoryOut.model_validate({k: v for k, v in d.items() if k in keep})


@tool(
    "ha.logbook",
    effect="read",
    labels_out=("private",),
    rate="60/min",
    idempotent=True,
    examples=({"entity": "automation.away_mode", "since": "7d"},),
)
def logbook(a: LogbookIn) -> LogbookOut:
    """Logbook entries (who or what changed it); hidden entities are left out."""
    argv = ["logbook"] + ([a.entity] if a.entity else []) + ["--since", a.since, "--limit", str(a.limit), "--json"]
    argv += ["--until", a.until] if a.until else []
    d = ha_json(argv, "logbook", timeout=60)
    keep = set(LogbookOut.model_fields)
    return LogbookOut.model_validate({k: v for k, v in d.items() if k in keep})


@tool(
    "ha.trace",
    effect="read",
    labels_out=("private",),
    rate="60/min",
    idempotent=True,
    examples=({"item": "automation.arrival", "last": 3},),
)
def trace(a: TraceIn) -> TraceOut:
    """Why an automation or script did (not) run: its last stored runs, summarised (trigger, conditions, steps,
    errors; never variables or service data)."""
    d = ha_json(["trace", a.item, "--last", str(a.last), "--json"], "trace", timeout=60)
    keep = set(TraceOut.model_fields)
    return TraceOut.model_validate({k: v for k, v in d.items() if k in keep})


@tool(
    "ha.switch",
    effect="home",
    labels_out=("private",),
    rate="30/min",
    timeout_s=75,
    examples=(
        {"action": "off", "targets": ["light.living_room"]},
        {"action": "off", "area": "Living Room", "domain": "light"},
    ),
)
def switch(a: SwitchIn) -> WriteOut:
    """Turn on, off or toggle: one name, entity ids, or an area (+ domain). Allowlisted domains only, read back."""
    argv = [a.action] + (list(a.targets) if a.targets else ["--area", a.area or ""])
    argv += ["--domain", a.domain] if a.domain else []
    return _write(argv, a.dry_run, a.wait, a.action)


@tool(
    "ha.set",
    effect="home",
    labels_out=("private",),
    rate="30/min",
    timeout_s=75,
    examples=({"target": "light.living_room", "brightness": 30, "color_temp": 2700},),
)
def set_(a: SetIn) -> WriteOut:
    """Set a light (or an area's lights): brightness %, color temperature K, color name."""
    argv = ["set"] + ([a.target] if a.target else ["--area", a.area or ""])
    for flag, v in (("--brightness", a.brightness), ("--color-temp", a.color_temp), ("--color", a.color)):
        if v is not None:
            argv += [flag, str(v)]
    return _write(argv, a.dry_run, a.wait, "set")


@tool(
    "ha.media",
    effect="home",
    labels_out=("private",),
    rate="30/min",
    timeout_s=75,
    examples=({"player": "media_player.tv", "action": "pause"},),
)
def media(a: MediaIn) -> WriteOut:
    """Control a media player: play, pause, stop, next, prev, mute, unmute, volume N."""
    argv = ["media", a.player, a.action] + ([str(a.volume)] if a.volume is not None else [])
    return _write(argv, a.dry_run, a.wait, "media")


@tool(
    "ha.run",
    effect="home",
    labels_out=("private",),
    rate="30/min",
    timeout_s=75,
    examples=({"kind": "scene", "target": "Movie"},),
)
def run_(a: RunIn) -> WriteOut:
    """Run a scene or a script (ones that touch locks, alarms or garages are refused by ha)."""
    return _write([a.kind, "run", a.target], a.dry_run, a.wait, a.kind)


@tool(
    "ha.call",
    effect="home",
    labels_out=("private",),
    rate="30/min",
    timeout_s=75,
    examples=(
        {"service": "vacuum.return_to_base", "entity": "vacuum.robot"},
        {"service": "select.select_option", "entity": "select.robot_work_mode", "data": {"option": "vacuum"}},
    ),
)
def call(a: CallIn) -> WriteOut:
    """Any service on ha's allowlist for exactly one entity of that domain (vacuum, cover, climate, fan,
    the writable helpers ...); ha checks the service, the data and the target, then reads back."""
    argv = ["call", a.service, a.entity] + ([json.dumps(a.data, ensure_ascii=False)] if a.data else [])
    return _write(argv, a.dry_run, a.wait, "call")
