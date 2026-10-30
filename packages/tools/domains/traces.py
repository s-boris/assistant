"""Traces: typed READ tools over my own traces: `traces report --json` (traces.db from the transcripts: cost by
trigger, owner latency, tool errors, hook blocks, slowest turns, costliest agents), the gateway's call log
(per tool: calls, error codes, p95) and Phoenix (the hub's trace store, fed by `traces otel serve`): its status,
one trace as a span tree, the slowest traces. Numbers, ids, names and categories only: no message text, tool input
or output exists in either store.

Boundaries: read-only; Phoenix down = `unavailable` (traces.db answers `traces.report`). Env: TOOLS_TRACES_BIN.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from tools import gateway
from tools.clirun import bin_path, first_line, run_json
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "`traces` (CLI, managed): `traces ingest` (cron */5) fills traces.db from the transcripts, `traces otel up` "
    "(cron */5) keeps the local OTLP receiver running; it stores Claude Code's own numbers in traces.db and "
    "forwards allowlisted spans to Phoenix (`traces phoenix status`; off: touch ~/.config/traces/phoenix-off). "
    "`traces brief` is the /traces answer, `traces agents` the subagent table, `traces phoenix joined` the share "
    "of his turns with one joined trace."
)

REGISTRY.add_domain(
    Domain(
        name="traces",
        title="Traces",
        summary="My own traces, read-only: cost by trigger, his reply latency, failing tools, hook blocks, slowest"
        " turns and costliest agents (traces.db), gateway calls per tool, and Phoenix's span trees and slowest traces.",
        feature="gateway.traces",
        notes=NOTES,
    )
)


def _fail(rc: int, out: str, err: str, what: str) -> ToolError:
    msg = first_line(err, out, default=f"{what}: exit {rc}")
    if rc == 4:
        return ToolError("unavailable", msg, "Phoenix is down or unreachable: traces.report still answers")
    if rc == 3:
        return ToolError("not_found", msg, "traces.slow lists trace ids; Phoenix keeps 30 days")
    if rc == 2:
        return ToolError("invalid_args", msg)
    return ToolError("failed", msg, f"run `{what}` by hand to see why")


def _traces(argv: list[str], timeout: float = 60) -> Any:
    _, d = run_json(bin_path("TOOLS_TRACES_BIN", "traces"), argv, "traces", timeout=timeout, fail=_fail)
    return d


def _ms(v: Any) -> str:
    if not isinstance(v, int | float):
        return "?"
    return f"{v / 1000:.1f} s" if v < 120_000 else f"{v / 60000:.1f} min"


# ------------------------------------------------------------------ models


class ReportIn(ToolInput):
    days: float = Field(default=1.0, ge=0.04, le=30, description="window in days back from now")


class HoursIn(ToolInput):
    hours: int = Field(default=24, ge=1, le=720, description="window in hours back from now")


class SlowIn(HoursIn):
    top: int = Field(default=5, ge=1, le=20, description="how many traces")


class TraceIn(ToolInput):
    trace_id: str = Field(
        pattern=r"^(?:[0-9a-f]{32}|last)$", description='a 32-hex trace id (traces.slow), or "last" = newest turn'
    )
    limit: int = Field(default=200, ge=1, le=1000, description="max spans returned (detailed)")


class ReportOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("slow_turns", "agents", "tool_counts", "latency", "gateway_tools")
    days: float
    cost: dict[str, Any] = Field(description="list-price USD: main, agents, total, by trigger (turns, cost)")
    owner: dict[str, Any] = Field(description="his turns: count, first reply p50/p95 ms, turn p50/p95 ms")
    errors: dict[str, Any] = Field(description="tool calls, errors, rate, hook blocks, top error categories")
    gateway: dict[str, Any] = Field(description="typed tool calls: total, errors by code, slowest tools by p95")
    slowest: list[dict[str, Any]] = Field(description="the 3 slowest turns (id, trigger, kind, ms, tools, cost)")
    costliest_agents: list[dict[str, Any]] = Field(description="the 3 costliest subagent runs (title, cost, ms)")
    slow_turns: list[dict[str, Any]] | None = None
    agents: list[dict[str, Any]] | None = None
    tool_counts: dict[str, int] | None = None
    latency: dict[str, Any] | None = None
    gateway_tools: dict[str, Any] | None = None

    def render(self, fmt: Format = "concise") -> str:
        c, o, e, g = self.cost, self.owner, self.errors, self.gateway
        out = [
            f"{self.days:g} d: ${c.get('total', 0):.2f} list (main ${c.get('main', 0):.2f}, agents"
            f" ${c.get('agents', 0):.2f}); by trigger "
            + ", ".join(f"{k} ${v.get('cost', 0):.2f}" for k, v in (c.get("by_trigger") or {}).items()),
            f"his turns {o.get('turns')}: first reply p50 {_ms(o.get('first_p50'))}, p95 {_ms(o.get('first_p95'))}",
            f"tool calls {e.get('tools')}, failed {e.get('errors')} ({100 * (e.get('rate') or 0):.1f} %), hook blocks"
            f" {e.get('hook_blocks')}; top: " + ", ".join(f"{k} {v}" for k, v in (e.get("top") or {}).items()),
            f"gateway: {g.get('calls')} calls, errors {g.get('errors') or {}}",
        ]
        out += [
            f"slow: {t.get('trigger')}/{t.get('start_kind')} {_ms(t.get('dur_ms'))}, {t.get('tools')} tools,"
            f" ${t.get('cost') or 0:.2f}"
            for t in self.slowest
        ]
        out += [
            f"agent: {a.get('description')} ${a.get('cost') or 0:.2f} {_ms(a.get('dur_ms'))}"
            for a in self.costliest_agents
        ]
        return "\n".join(out)


class PhoenixOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("kept_out",)
    url: str
    project: str
    health: str | None = None
    forwarding: dict[str, Any] = Field(description="on/off, spans in, forwarded, lost, failures, last error")
    retention: dict[str, Any] | None = Field(default=None, description="policy name, max days, sweep cron")
    traces: int | None = Field(default=None, description="traces in the window (1000 = at least)")
    joined: dict[str, Any] | None = Field(
        default=None, description="his turns with a Phoenix trace (share), gateway calls inside Claude Code traces"
    )
    kept_out: dict[str, int] | None = Field(default=None, description="attribute keys the allowlist dropped")

    def render(self, fmt: Format = "concise") -> str:
        f, r, j = self.forwarding, self.retention or {}, self.joined or {}
        out = [
            f"Phoenix {self.health or '?'} · project {self.project} · forwarding {'off' if f.get('off') else 'on'}:"
            f" {f.get('received', 0)} spans in, {f.get('forwarded', 0)} forwarded, {f.get('lost', 0)} lost"
            + (f" (last error {f.get('last_err')})" if f.get("last_err") else ""),
            f"retention {r.get('policy', '?')} {r.get('max_days', '?')} d · traces {self.traces}",
        ]
        if j:
            out.append(
                f"his turns traced {j.get('owner_turns_traced')}/{j.get('owner_turns')} · gateway calls in Claude Code"
                f" traces {j.get('gateway_calls_in_cc_traces')}/{j.get('gateway_calls')}"
            )
        return "\n".join(out)


class TraceOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("spans",)
    summary: dict[str, Any] = Field(
        description="trace id, root, start, ms, spans, LLM/tool/gateway calls, agents, errors, tokens, slowest tools"
    )
    spans: list[dict[str, Any]] | None = Field(
        default=None, description="span rows: ids, parent, name, ms, tool, model, agent, gateway code, status"
    )

    def render(self, fmt: Format = "concise") -> str:
        s = self.summary
        head = (
            f"trace {s.get('trace_id')}: {s.get('root')} {_ms(s.get('ms'))}, {s.get('spans')} spans"
            f" ({s.get('llm_calls')} LLM, {s.get('tool_calls')} tools, {s.get('gateway_calls')} gateway,"
            f" {s.get('agents')} agents, {s.get('errors')} errors)"
        )
        slow = ", ".join(f"{x.get('tool')} {_ms(x.get('ms'))}" for x in s.get("slowest_tools") or [])
        lines = [head] + ([f"slowest: {slow}"] if slow else [])
        if fmt == "detailed":
            lines += [
                f"{r.get('name')} {_ms(r.get('ms'))} {r.get('tool') or r.get('model') or ''}" for r in self.spans or []
            ]
        return "\n".join(lines)


class SlowOut(ToolOutput):
    hours: int
    traces: list[dict[str, Any]] = Field(description="per trace: the TraceOut summary")

    def render(self, fmt: Format = "concise") -> str:
        if not self.traces:
            return f"no traces in {self.hours} h"
        return "\n".join(
            f"{_ms(t.get('ms'))} {t.get('root')} · {t.get('spans')} spans, {t.get('gateway_calls')} gateway ·"
            f" {t.get('trace_id')}"
            + (f" · slowest {t['slowest_tools'][0].get('tool')}" if t.get("slowest_tools") else "")
            for t in self.traces
        )


# ------------------------------------------------------------------ tools


@tool(
    "traces.report",
    effect="read",
    labels_out=("counts",),
    rate="20/min",
    idempotent=True,
    timeout_s=60,
    examples=({}, {"days": 3}),
)
def report(a: ReportIn) -> ReportOut:
    """My cost, his reply latency, failing tools, hook blocks, slowest turns and costliest agents, plus the gateway's
    calls per tool. From traces.db (works without Phoenix). For "why was that slow", "what costs the most", "which
    tools fail", the daily self-review and metric checks (G2 first reply, G3 failures)."""
    d = _traces(["report", "--days", f"{a.days:g}", "--json"])
    if not isinstance(d, dict):
        raise ToolError("failed", "traces report: no JSON object", "run `traces report --json` by hand")
    st = gateway.stats(a.days)
    errs: dict[str, int] = {}
    for t in st["tools"].values():
        for k, v in (t.get("errors") or {}).items():
            errs[k] = errs.get(k, 0) + v
    p95 = sorted(((n, t.get("p95_ms") or 0) for n, t in st["tools"].items()), key=lambda x: -x[1])[:3]
    top_err = dict(sorted((d.get("error_counts") or {}).items(), key=lambda kv: -kv[1])[:5])
    return ReportOut(
        days=a.days,
        cost={
            "main": round(d.get("main_cost") or 0, 2),
            "agents": round(d.get("agent_cost") or 0, 2),
            "total": round(d.get("total_cost") or 0, 2),
            "by_trigger": {
                k: {"turns": v.get("turns"), "cost": round((v.get("main_cost") or 0) + (v.get("agent_cost") or 0), 2)}
                for k, v in (d.get("by_trigger") or {}).items()
                if isinstance(v, dict)
            },
        },
        owner={
            "turns": d.get("owner_turns"),
            "first_p50": d.get("owner_first_p50"),
            "first_p95": d.get("owner_first_p95"),
            "dur_p50": d.get("owner_dur_p50"),
            "dur_p95": d.get("owner_dur_p95"),
        },
        errors={
            "tools": d.get("tools"),
            "errors": d.get("errors"),
            "rate": d.get("error_rate"),
            "hook_blocks": d.get("hook_blocks"),
            "top": top_err,
        },
        gateway={
            "calls": sum(t.get("calls", 0) for t in st["tools"].values()),
            "errors": errs,
            "slowest_p95": dict(p95),
            "trace_src": st.get("trace_src"),
        },
        slowest=(d.get("slow_turns") or [])[:3],
        costliest_agents=[
            {k: x.get(k) for k in ("description", "effort", "cost", "dur_ms", "status")}
            for x in (d.get("top_agents") or [])[:3]
        ],
        slow_turns=d.get("slow_turns") or [],
        agents=d.get("top_agents") or [],
        tool_counts=d.get("tool_counts") or {},
        latency=d.get("latency") or {},
        gateway_tools=st["tools"],
    )


@tool(
    "traces.phoenix",
    effect="read",
    labels_out=("counts",),
    rate="20/min",
    idempotent=True,
    timeout_s=60,
    examples=({},),
)
def phoenix(a: HoursIn) -> PhoenixOut:
    """Phoenix (the hub's trace store) as I feed it: health, forwarding counters, retention, traces in the window,
    and how many of his turns have one joined trace. For "are traces landing", "is Phoenix ok"."""
    d = _traces(["phoenix", "status", "--hours", str(a.hours), "--json"])
    f = d.get("forwarder") or {}
    return PhoenixOut(
        url=str(d.get("url")),
        project=str(d.get("project")),
        health=d.get("health"),
        forwarding={
            "off": bool(d.get("off")),
            "core_otlp": d.get("core_otlp"),
            **{k: f.get(k) for k in ("since", "received", "forwarded", "lost", "posts", "failures", "last_err")},
        },
        retention=d.get("retention"),
        traces=d.get("traces"),
        joined=d.get("joined"),
        kept_out=f.get("dropped_keys") or {},
    )


@tool(
    "traces.trace",
    effect="read",
    labels_out=("counts",),
    rate="30/min",
    idempotent=True,
    timeout_s=60,
    examples=({"trace_id": "last"},),
)
def trace(a: TraceIn) -> TraceOut:
    """One trace from Phoenix as a summary (detailed: its span rows): LLM calls, tools, gateway calls, agents,
    errors, tokens and the slowest tools. For "why did that turn take so long", "what did that agent run do"."""
    d = _traces(["phoenix", "trace", a.trace_id, "--json", "--limit", str(a.limit)])
    return TraceOut(summary=d.get("summary") or {}, spans=d.get("spans") or [])


@tool(
    "traces.slow",
    effect="read",
    labels_out=("counts",),
    rate="20/min",
    idempotent=True,
    timeout_s=90,
    examples=({}, {"hours": 72, "top": 10}),
)
def slow(a: SlowIn) -> SlowOut:
    """The slowest traces in Phoenix for the window, each with its span counts and slowest tools.
    Use traces.trace on an id for the tree."""
    d = _traces(["phoenix", "slow", "--hours", str(a.hours), "--top", str(a.top), "--json"], timeout=85)
    return SlowOut(hours=a.hours, traces=d if isinstance(d, list) else [])


__all__ = ["phoenix", "report", "slow", "trace"]
