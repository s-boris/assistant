"""The typed tool registry: one Python source for the tools the brain calls.

A typed tool is a function from a Pydantic input model to a Pydantic output model, registered with
@tool(NAME, effect=..., labels_out=..., approval=..., rate=...). From the registry come the FastMCP server `hub`
(tools.mcp_server), the `tool` CLI (tools.cli), the per-domain docs and schemas/tools.toml (tools.gen).
Binding tools (an argv the Telegram adapter runs for a command or a tap) live here too, as data.

Effects, lowest first: read (looks only) · home (changes his own state: reminders, lists, lights) · send_owner
(contacts only him) · send_external (anyone else) · spend (money) · irreversible (no undo).
"""

from __future__ import annotations

import inspect
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

EFFECTS = ("read", "home", "send_owner", "send_external", "spend", "irreversible")
APPROVALS = ("none", "owner")
# Coarse labels on what a tool returns (the gateway's Cedar policy reads them):
# owner_text = his own words (reminder texts, list items) · private = vault-filtered answers · counts = numbers only.
LABELS = ("owner_text", "private", "counts")
ERROR_CODES = (
    "invalid_args",  # the input failed validation, or the backend refused it as malformed
    "not_found",  # no such tool, reminder, item ...
    "conflict",  # the change clashes with the current state
    "unavailable",  # the backend is missing or down
    "timeout",  # the backend took too long
    "rate_limited",  # over the tool's rate
    "approval_required",  # needs his ✅ (an approvals nonce) first
    "policy_denied",  # the hub's Cedar policy refused this caller this tool (enforced domains only)
    "failed",  # the backend reported an error
    "internal",  # a bug in the tool or the gateway
)
NAME_RE = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9_-]*)*")
RATE_RE = re.compile(r"(\d+)/(s|min|h)")
RATE_UNIT = {"s": 1.0, "min": 60.0, "h": 3600.0}
Format = Literal["concise", "detailed"]


class ToolError(Exception):
    """A typed failure: code (one of ERROR_CODES), a one-line message and a hint (the next step)."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        if code not in ERROR_CODES:
            code = "internal"
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


class ToolInput(BaseModel):
    """Base of every input model: unknown fields are refused; `format` picks the output size."""

    model_config = ConfigDict(extra="forbid")
    format: Format = Field("concise", description="concise (default): the short answer; detailed: every field")


@dataclass(frozen=True)
class JobTimes:
    """An async backend job behind a tool's answer, for the gateway's job log: its id, state (queued |
    running = not answered yet, the call logs code "pending"; done | failed | ...), created and ended (epoch s, None
    while unknown), elapsed_s (the backend's own count) and started (this call submitted the job; a pickup only
    fills in the times of a job the log already has)."""

    job: str
    state: str
    created: float | None = None
    ended: float | None = None
    elapsed_s: float | None = None
    started: bool = False


class ToolOutput(BaseModel):
    """Base of every output model. DETAIL names the fields only the detailed form carries."""

    model_config = ConfigDict(extra="forbid")
    DETAIL: ClassVar[tuple[str, ...]] = ()

    def job_times(self) -> JobTimes | None:
        """The async job this answer reports on (vault jobs), else None. Not part of the output."""
        return None

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude_none=True)
        if fmt == "concise":
            for k in self.DETAIL:
                d.pop(k, None)
        return d

    def render(self, fmt: Format = "concise") -> str:
        """The answer as text (what the CLI prints and the MCP text block carries). Domains override it."""
        return ", ".join(f"{k}={v}" for k, v in self.data(fmt).items())


@dataclass(frozen=True)
class Domain:
    name: str
    title: str
    summary: str
    feature: str  # usage feature of its gateway calls
    notes: str = ""  # the CLI and cron side, rendered under the typed tools in the docs


@dataclass(frozen=True)
class Spec:
    """One typed tool."""

    name: str
    domain: str
    fn: Callable[[Any], ToolOutput] = field(repr=False)
    input: type[ToolInput]
    output: type[ToolOutput]
    effect: str
    labels_out: tuple[str, ...]
    approval: str
    rate: str | None
    destructive: bool
    idempotent: bool
    timeout_s: float
    examples: tuple[dict[str, Any], ...]
    doc: str

    @property
    def mcp_name(self) -> str:
        return mcp_name(self.name)

    @property
    def summary(self) -> str:
        return self.doc.strip().splitlines()[0] if self.doc.strip() else self.name

    @property
    def rate_limit(self) -> tuple[int, float] | None:
        return parse_rate(self.rate)


@dataclass(frozen=True)
class ArgvTool:
    """A binding tool: a command line the Telegram adapter runs for a command or a tap; no typed in/out."""

    name: str
    argv: tuple[str, ...]
    effect: str
    description: str
    timeout_s: int | None = None
    output: str | None = None  # text (default) | picture
    text_arg: bool | None = None
    note: bool | None = None
    menu: str | None = None  # the /commands menu line of a command bound to it; none = not in the menu
    toast: str | None = None  # the toast of a bound tap; none = "✓ <button text>"


def mcp_name(name: str) -> str:
    """The MCP (and Claude API) form of a tool name: [a-z0-9_] only, so reminders.add -> reminders_add."""
    return re.sub(r"[^a-z0-9_]", "_", name)


def parse_rate(rate: str | None) -> tuple[int, float] | None:
    if not rate:
        return None
    m = RATE_RE.fullmatch(rate)
    if not m or int(m[1]) < 1:
        raise ValueError(f"rate {rate!r}: want N/s, N/min or N/h")
    return int(m[1]), RATE_UNIT[m[2]]


class Registry:
    def __init__(self) -> None:
        self.domains: dict[str, Domain] = {}
        self.typed: dict[str, Spec] = {}
        self.argv: dict[str, ArgvTool] = {}

    def add_domain(self, d: Domain) -> Domain:
        if d.name in self.domains and self.domains[d.name] != d:
            raise ValueError(f"domain {d.name} registered twice")
        self.domains[d.name] = d
        return d

    def add(self, spec: Spec) -> Spec:
        if not NAME_RE.fullmatch(spec.name):
            raise ValueError(f"tool name {spec.name!r}: lower case, dots between parts")
        if spec.domain not in self.domains:
            raise ValueError(f"{spec.name}: unknown domain {spec.domain!r}")
        if spec.effect not in EFFECTS or spec.approval not in APPROVALS:
            raise ValueError(f"{spec.name}: effect {spec.effect!r} / approval {spec.approval!r}")
        if any(lb not in LABELS for lb in spec.labels_out):
            raise ValueError(f"{spec.name}: labels_out {spec.labels_out!r} (known: {', '.join(LABELS)})")
        parse_rate(spec.rate)
        self._unique(spec.name)
        self.typed[spec.name] = spec
        return spec

    def add_argv(self, t: ArgvTool) -> ArgvTool:
        if not NAME_RE.fullmatch(t.name) or t.effect not in EFFECTS or not t.argv:
            raise ValueError(f"binding tool {t.name!r}: name, effect or argv")
        if t.output not in (None, "text", "picture") or (t.timeout_s is not None and not 1 <= t.timeout_s <= 300):
            raise ValueError(f"binding tool {t.name!r}: output or timeout_s")
        self._unique(t.name)
        self.argv[t.name] = t
        return t

    def _unique(self, name: str) -> None:
        taken = {mcp_name(n) for n in (*self.typed, *self.argv)}
        if name in self.typed or name in self.argv or mcp_name(name) in taken:
            raise ValueError(f"tool {name!r} registered twice (or clashes after {mcp_name(name)!r})")

    def get(self, name: str) -> Spec | None:
        """A typed tool by registry name (reminders.add) or MCP name (reminders_add)."""
        if name in self.typed:
            return self.typed[name]
        return next((s for s in self.typed.values() if s.mcp_name == name), None)

    def in_domain(self, domain: str) -> list[Spec]:
        return [s for s in self.typed.values() if s.domain == domain]


REGISTRY = Registry()


def tool(
    name: str,
    *,
    effect: str,
    labels_out: tuple[str, ...] = (),
    approval: str = "none",
    rate: str | None = None,
    destructive: bool = False,
    idempotent: bool = False,
    timeout_s: float = 30.0,
    examples: tuple[dict[str, Any], ...] = (),
    registry: Registry | None = None,
) -> Callable[[Callable[[Any], ToolOutput]], Callable[[Any], ToolOutput]]:
    """Register fn(args: SomeInput) -> SomeOutput as the typed tool NAME (domain = NAME's first part).
    The docstring is the tool description: first line = summary, the rest = when to use it."""

    def deco(fn: Callable[[Any], ToolOutput]) -> Callable[[Any], ToolOutput]:
        hints = typing.get_type_hints(fn)
        params = list(inspect.signature(fn).parameters)
        if len(params) != 1:
            raise TypeError(f"{name}: a tool takes exactly one argument (its input model)")
        tin, tout = hints.get(params[0]), hints.get("return")
        if not (isinstance(tin, type) and issubclass(tin, ToolInput)):
            raise TypeError(f"{name}: the argument must be annotated with a ToolInput subclass")
        if not (isinstance(tout, type) and issubclass(tout, ToolOutput)):
            raise TypeError(f"{name}: the return must be annotated with a ToolOutput subclass")
        req = [
            k for k in getattr(tout, "DETAIL", ()) if k not in tout.model_fields or tout.model_fields[k].is_required()
        ]
        if req:
            raise TypeError(f"{name}: DETAIL fields must exist and be optional (concise drops them): {req}")
        for ex in examples:
            tin.model_validate(ex)  # examples are docs and test cases: they must validate
        (registry or REGISTRY).add(
            Spec(
                name=name,
                domain=name.split(".")[0],
                fn=fn,
                input=tin,
                output=tout,
                effect=effect,
                labels_out=tuple(labels_out),
                approval=approval,
                rate=rate,
                destructive=destructive,
                idempotent=idempotent,
                timeout_s=timeout_s,
                examples=tuple(examples),
                doc=inspect.cleandoc(fn.__doc__ or ""),
            )
        )
        return fn

    return deco


def load(registry: Registry | None = None) -> Registry:
    """The registry with every domain module imported (idempotent)."""
    from tools import domains

    domains.load_all()
    return registry or REGISTRY
