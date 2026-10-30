"""loop.signals: what the daily review reads. Counts, ids, names and numbers only: never message text, tool input
or output (the sources already keep it out; this module only copies fields it names).

  collect(days=1) -> {"traces", "beats", "usage", "evals", "denials", "reflexes", "corrections", "health", "ci",
                      "cost", "errors"}
"health" = the self-review's step (h) setup-health findings (loop.health latest.json, <= 26 h old; None when stale).
"ci" = shared CI blockers in 24 h (core.ciblock's first-seen/cleared log): count, blocked minutes, the longest.
"cost" = cost per agent run, 24 h and 7 d: subagent cost / runs (`traces agents`); target C2 in targets.json (the $3.40
baseline is per run). Cost per landed change (/ ok lands in deploys.jsonl,
records from 21.10) rides along as an info value: no target, no finding.
Each source is one command with a timeout; a failing source is listed in "errors" and the rest goes on.
"""

from __future__ import annotations

import re
from typing import Any

from . import common

HOOK_RE = re.compile(r"^(?P<tool>[^:]+):hook:(?P<hook>[\w.-]+)$")
USAGE_FLAGS = ("broken", "slow", "friction")


def traces(days: int = 1) -> tuple[dict[str, Any] | None, str | None]:
    d, err = common.run_json([common.bin_path("traces"), "report", "--days", str(days), "--json"], timeout=120)
    if not isinstance(d, dict):
        return None, err or "traces: unexpected answer"
    counts = {str(k): int(v) for k, v in (d.get("error_counts") or {}).items() if isinstance(v, (int, float))}
    hooks: dict[str, int] = {}
    for k, v in counts.items():
        m = HOOK_RE.match(k)
        if m:
            hooks[m["hook"]] = hooks.get(m["hook"], 0) + v
    hb = (d.get("by_trigger") or {}).get("heartbeat") or {}
    return {
        "days": days,
        "owner_turns": d.get("owner_turns"),
        "owner_first_p50_s": _s(d.get("owner_first_p50")),
        "owner_first_p95_s": _s(d.get("owner_first_p95")),
        "tools": d.get("tools"),
        "errors": d.get("errors"),
        "error_rate_pct": round(100 * float(d.get("error_rate") or 0), 2),
        "error_top": sorted(((k, v) for k, v in counts.items() if not HOOK_RE.match(k)), key=lambda x: -x[1])[:5],
        "hook_blocks": int(d.get("hook_blocks") or sum(hooks.values())),
        "hooks": dict(sorted(hooks.items(), key=lambda x: -x[1])),
        "heartbeat_cost": round(float(hb.get("main_cost") or 0) + float(hb.get("agent_cost") or 0), 2),
    }, None


def _s(ms: Any) -> float | None:
    return round(float(ms) / 1000, 1) if isinstance(ms, (int, float)) else None


def beats(days: int = 1) -> tuple[dict[str, Any] | None, str | None]:
    d, err = common.run_json([common.bin_path("events"), "metrics", "--beats", "--days", str(days), "--json"])
    b = d.get("beats") if isinstance(d, dict) else None
    if not isinstance(b, dict):
        return None, err or "events metrics: no beats block"
    tok = b.get("tokens") or {}
    return {
        "beat_turns": b.get("beat_turns"),
        "tokens_mean": tok.get("mean"),
        "tokens_p95": tok.get("p95"),
        "tokens_per_day": b.get("tokens_per_day"),
        "context_per_call": b.get("context_per_call"),
        "cost_per_day": b.get("cost_per_day"),
    }, None


def usage(days: int = 30) -> tuple[dict[str, Any] | None, str | None]:
    d, err = common.run_json([common.bin_path("product"), "report", "--days", str(days), "--json"], timeout=120)
    if not isinstance(d, dict) or not isinstance(d.get("features"), list):
        return None, err or "product report: unexpected answer"
    flagged = []
    for f in d["features"]:
        if not isinstance(f, dict) or not f.get("owner_facing"):
            continue
        fl = [x for x in (f.get("flags") or []) if x in USAGE_FLAGS]
        if not fl:
            continue
        lat = f.get("latency_ms") or {}
        flagged.append(
            {
                "id": f.get("id"),
                "name": f.get("name") or f.get("id"),
                "area": f.get("area"),
                "flags": fl,
                "uses": int(f.get("uses") or 0),
                "errors": int(f.get("errors") or 0),
                "owner_errors": int(f.get("owner_errors") or 0),
                "complaints": int(f.get("complaints") or 0),
                "retries": int(f.get("retries") or 0),
                "latency_p50_ms": lat.get("p50") if isinstance(lat, dict) else None,
                "metric": str(f.get("metric") or "")[:200],
            }
        )
    meta = d.get("meta") or {}
    return {
        "days": days,
        "owner_uses": meta.get("owner_uses"),
        "features": len(d["features"]),
        "flagged": flagged,
    }, None


def evals(hours: int = 26) -> tuple[dict[str, Any] | None, str | None]:
    d, err = common.run_json([common.bin_path("evals"), "summary", "--hours", str(hours), "--json"])
    if not isinstance(d, dict):
        return None, err or "evals summary: unexpected answer"
    if not d.get("run_id"):
        return {"run_id": None, "line": "no eval run in the window"}, None
    keep = ("run_id", "score", "passed", "ran", "regression", "delta", "new_fail", "fixed", "line")
    return {k: d.get(k) for k in keep}, None


def denials(days: int = 1) -> tuple[dict[str, Any] | None, str | None]:
    d, err = common.run_json([common.bin_path("tool"), "policy", "--days", str(days), "--json"], timeout=40)
    if not isinstance(d, dict):
        return None, err or "tool policy: unexpected answer"
    doms = {}
    for name, v in (d.get("domains") or {}).items():
        if isinstance(v, dict):
            doms[name] = {k: v.get(k) for k in ("calls", "denies", "unexpected", "mode", "ready")}
    recent = []
    for x in (d.get("denies") or [])[:20]:
        if isinstance(x, dict):
            recent.append({k: x.get(k) for k in ("id", "tool", "principal", "acked")})
    return {"domains": doms, "recent": recent, "policy_valid": (d.get("policy") or {}).get("valid")}, None


def ci(hours: int = 24) -> tuple[dict[str, Any] | None, str | None]:
    """Shared CI blockers in the last `hours` (core.ciblock's first-seen/cleared log): count, blocked minutes (the
    union), the longest one; keys and numbers only."""
    from core import ciblock

    st = ciblock.stats(hours)
    st["line"] = ciblock.summary_line(st)
    return st, None


def cost(t: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Cost per agent run over 24 h and 7 d: the subagent runs' cost (`traces agents`, one 7-day call split by start
    time) over their count (C2). Info only: the same cost over the successful lands in deploys.jsonl. Numbers only;
    per_run / per_land None when there was no run / no land."""
    import json
    from datetime import datetime

    from core import paths

    now = common.now()
    t0 = now - 7 * 86400

    def hm(ts: float) -> str:
        return datetime.fromtimestamp(ts, common.tz()).strftime("%Y-%m-%d %H:%M")

    d, err = common.run_json(
        [common.bin_path("traces"), "agents", "--since", hm(t0), "--until", hm(now), "--top", "1000000", "--json"],
        timeout=120,
    )
    if not isinstance(d, list):
        return None, err or "traces agents: unexpected answer"
    lands: list[float] = []
    try:
        with open(paths.home() / ".local" / "state" / "assistant" / "deploys.jsonl", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                    ts = float(r.get("ts") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
                if r.get("op") == "land" and r.get("ok") and t0 <= ts <= now:
                    lands.append(ts)
    except FileNotFoundError:
        pass
    tgt = (t or _targets()).get("C2") or {}
    out: dict[str, Any] = {"target": tgt.get("target"), "baseline": tgt.get("baseline")}
    for key, since in (("24h", now - 86400), ("7d", t0)):
        runs = [a for a in d if isinstance(a, dict) and float(a.get("start_ts") or 0) >= since]
        usd = sum(float(a.get("cost") or 0) for a in runs)
        n = sum(1 for x in lands if x >= since)
        out[key] = {
            "runs": len(runs),
            "cost": round(usd, 2),
            "per_run": round(usd / len(runs), 2) if runs else None,
            "lands": n,
            "per_land": round(usd / n, 2) if n else None,
        }
    out["line"] = cost_line(out)
    return out, None


def _targets() -> dict[str, Any]:
    from . import propose

    return propose.targets()


def cost_line(c: dict[str, Any]) -> str:
    def usd(x: Any, none: str) -> str:
        return f"${x:,.2f}" if isinstance(x, (int, float)) else none

    def pair(k: str, none: str) -> str:
        return f"{usd(c['24h'].get(k), none)} (24 h), {usd(c['7d'].get(k), none)} (7 d)"

    tg = c.get("target")
    return (
        f"Cost per agent run: {pair('per_run', 'n/a (no run)')}, target "
        + (f"≤ ${tg:,.2f}" if isinstance(tg, (int, float)) else "?")
        + (f" ({c['baseline']})" if c.get("baseline") else "")
        + f"; per landed change (info): {pair('per_land', 'n/a (no landed change)')}"
    )


def collect(days: int = 1) -> dict[str, Any]:
    from . import corrections, health, reflex  # late: both import this module's siblings

    out: dict[str, Any] = {"errors": {}}
    for name, fn in (
        ("traces", lambda: traces(days)),
        ("beats", lambda: beats(days)),
        ("usage", lambda: usage(30)),
        ("evals", lambda: evals(26)),
        ("denials", lambda: denials(days)),
        ("reflexes", lambda: (reflex.signal(), None)),
        ("corrections", lambda: (corrections.signal(), None)),
        ("health", lambda: (health.signal(), None)),
        ("ci", lambda: ci(24 * days)),
        ("cost", lambda: cost()),
    ):
        try:
            val, err = fn()
        except Exception as e:  # a broken source never stops the review
            val, err = None, f"{type(e).__name__}: {str(e)[:160]}"
        out[name] = val
        if err:
            out["errors"][name] = err
    return out


def summary_lines(sig: dict[str, Any]) -> list[str]:
    """The report's signal block: one line per source, numbers only."""
    fmt = common.fmt_num
    L = []
    t = sig.get("traces")
    if t:
        hooks = ", ".join(f"{k} {v}" for k, v in list(t["hooks"].items())[:3]) or "none"
        L.append(
            f"traces ({t['days']} d): owner turns {t['owner_turns']},"
            f" first reply p50 {fmt(t['owner_first_p50_s'], 's')}"
            f" p95 {fmt(t['owner_first_p95_s'], 's')}; tool calls {t['tools']}, failed {fmt(t['error_rate_pct'], '%')};"
            f" hook blocks {t['hook_blocks']} ({hooks})"
        )
    b = sig.get("beats")
    if b:
        L.append(
            f"beats: {b['beat_turns']} beat turns, tokens/beat mean {fmt(b['tokens_mean'], 'tokens')}"
            f" p95 {fmt(b['tokens_p95'], 'tokens')}, {fmt(b['tokens_per_day'], 'tokens')}/day"
        )
    u = sig.get("usage")
    if u:
        fl = ", ".join(f"{f['id']} ({'/'.join(f['flags'])})" for f in u["flagged"][:6]) or "none"
        L.append(f"usage (30 d): {u['owner_uses']} owner uses; flagged owner-facing: {fl}")
    e = sig.get("evals")
    if e:
        L.append(f"evals: {e.get('line') or 'no run'}")
    d = sig.get("denials")
    if d:
        unexp = {k: v["unexpected"] for k, v in d["domains"].items() if v.get("unexpected")}
        calls = sum(int(v.get("calls") or 0) for v in d["domains"].values())
        dn = sum(int(v.get("denies") or 0) for v in d["domains"].values())
        L.append(f"gateway: {calls} calls, {dn} denies, unexpected {unexp or 0}")
    r = sig.get("reflexes")
    if r is not None:
        L.append(
            f"reflexes: {r['active']} active, {r['fired_24h']} firings in 24 h, {r['misfires_7d']} misfires in 7 d"
        )
    c = sig.get("corrections")
    if c is not None:
        L.append(
            f"corrections: {c['open']} draft(s) waiting, {c['overdue']} past 24 h, {c['accepted_7d']} accepted in 7 d"
        )
    h = sig.get("health")
    if h is not None:
        sev = {}
        for f in h.get("findings") or []:
            sev[f.get("sev")] = sev.get(f.get("sev"), 0) + 1
        L.append(
            f"setup health (h): {len(h.get('findings') or [])} anomalies (high {sev.get(3, 0)}, med {sev.get(2, 0)},"
            f" low {sev.get(1, 0)})"
        )
    ci_ = sig.get("ci")
    if ci_ is not None:
        L.append(str(ci_.get("line") or "CI blocked: ?") + (f"; {ci_['open']} still open" if ci_.get("open") else ""))
    co = sig.get("cost")
    if co is not None:
        L.append(str(co.get("line") or "Cost per agent run: ?"))
    for k, v in (sig.get("errors") or {}).items():
        L.append(f"(source {k} unavailable: {v})")
    return L
