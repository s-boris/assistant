"""loop.propose: change candidates from the signals, metric first. A pre-sort for the brain, never a decision.

  candidates(sig, targets) -> [Candidate dicts], best first
  pick(cands, state, ledger, limit=3) -> the new ones to propose (not open, not cooling down, not ledger-silent)

A candidate: key (stable across days: the same gap keeps one key), metric {id, name, now, target, unit, source}
(the number the change must move), title, evidence (counts), change (where to look first; the builder decides
the actual change), feature (catalogue id, when the gap belongs to one), score (gap ratio x weight).
Rules (each one gap against targets.json): evals newly failing (G4), unexpected gateway denies (G3p), reflex
misfires (R1), tokens per beat (G1), hook blocks (G3h, one per hook), tool-call failures (G3), first reply p95
(G2), owner-facing features flagged broken / friction / slow by `product report` (U1-U3), setup-health anomalies of
severity >= 2 from the self-review's step (h) (H1, one per anomaly: the metric against its 7-day baseline), shared
CI blockers (C1), cost per agent run over 7 d (C2).
Never proposed here: retiring a feature (the weekly product review asks him), anything the ledger declined.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from . import common

COOLDOWN_DAYS = 14
STOP = {
    "the",
    "and",
    "for",
    "with",
    "his",
    "my",
    "of",
    "to",
    "a",
    "an",
    "in",
    "on",
    "bot",
    "skill",
    "app",
    "bin",
    "feature",
    "features",
    "command",
    "commands",
    "menu",
    "fix",
    "smooth",
    "speed",
}


def targets() -> dict[str, Any]:
    d = json.loads(Path(__file__).with_name("targets.json").read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _metric(t: dict[str, Any], mid: str, now: Any, **over: Any) -> dict[str, Any]:
    m = dict(t[mid])
    m.update(over)
    m["id"] = mid
    m["now"] = now
    return m


def _ratio(now: float | None, target: float) -> float:
    if now is None:
        return 0.0
    if target <= 0:
        return 1.0 + min(4.0, float(now))
    return float(now) / float(target)


def candidates(sig: dict[str, Any], t: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    t = t or targets()
    out: list[dict[str, Any]] = []
    fmt = common.fmt_num

    ev = sig.get("evals") or {}
    nf = [str(x) for x in (ev.get("new_fail") or [])]
    if nf:
        out.append(
            {
                "key": "g4:new-fail:" + ",".join(sorted(nf))[:80],
                "metric": _metric(t, "G4", len(nf)),
                "title": f"Fix {len(nf)} eval case(s) that newly fail",
                "evidence": [f"run {ev.get('run_id')}: {ev.get('line')}"[:300], "newly failing: " + ", ".join(nf[:8])],
                "change": "evals report --failures: fix the behaviour (skill, prompt, tool) or the change that broke"
                " it;"
                " rerun evals run --changed",
                "score": 6.0 + len(nf) / 10,
            }
        )

    dn = sig.get("denials") or {}
    for dom, v in sorted((dn.get("domains") or {}).items()):
        u = int(v.get("unexpected") or 0)
        if u > 0:
            tools = sorted({str(x.get("tool")) for x in dn.get("recent") or [] if not x.get("acked")})[:5]
            out.append(
                {
                    "key": f"g3p:{dom}",
                    "metric": _metric(t, "G3p", u, name=f"unexpected gateway denies in {dom}"),
                    "title": f"Resolve unexpected policy denies in {dom}",
                    "evidence": [
                        f"{u} unexpected of {v.get('denies')} denies in {v.get('calls')} calls (mode {v.get('mode')})",
                        "tools: " + (", ".join(tools) or "?"),
                    ],
                    "change": "tool policy: fix policy.cedar or the tool's effect/labels when the deny was wrong; ack"
                    " the"
                    " expected ones (tool policy ack ID --why)",
                    "score": 5.0 + min(1.0, u / 10),
                }
            )

    rx = sig.get("reflexes") or {}
    for name, n in sorted((rx.get("misfires_by_name") or {}).items()):
        if n > 0:
            out.append(
                {
                    "key": f"r1:{name}",
                    "metric": _metric(t, "R1", n, name=f"misfires of reflex {name} in 7 d"),
                    "title": f"Revise or retire reflex {name}",
                    "evidence": [f"{n} misfire(s) in 7 d", f"reflex log {name}"],
                    "change": f"reflex log {name}: revise the rule (and its eval case) or reflex rm {name} --why ...",
                    "score": 5.0 + min(1.0, n / 10),
                }
            )

    b = sig.get("beats") or {}
    if b.get("tokens_mean") is not None and b["tokens_mean"] > t["G1"]["target"]:
        r = _ratio(b["tokens_mean"], t["G1"]["target"])
        out.append(
            {
                "key": "g1:beat-tokens",
                "metric": _metric(t, "G1", b["tokens_mean"]),
                "title": "Cut tokens per beat turn",
                "evidence": [
                    f"{b.get('beat_turns')} beat turns: mean {fmt(b['tokens_mean'], 'tokens')}, p95"
                    f" {fmt(b.get('tokens_p95'), 'tokens')}; {fmt(b.get('tokens_per_day'), 'tokens')}/day"
                    f" (target {fmt(t['G1d']['target'], 'tokens')})",
                    f"context per call {fmt(b.get('context_per_call'), 'tokens')}",
                ],
                "change": "events metrics --beats: which beat sources cost most (notes vs idle); shrink what a beat"
                " reads or batch note handling; re-measure after 20 beats",
                "score": min(5.0, r) * 0.9,
            }
        )

    ci = sig.get("ci") or {}
    if ci.get("minutes") is not None and ci["minutes"] > t["C1"]["target"]:
        lg = ci.get("longest") or {}
        out.append(
            {
                "key": "c1:ci-blocked",
                "metric": _metric(t, "C1", ci["minutes"]),
                "title": "Cut the time every land was blocked by a shared CI blocker",
                "evidence": [
                    str(ci.get("line") or "")[:300],
                    f"longest: {lg.get('key') or '?'} ({round(float(lg.get('secs') or 0) / 60)} min);"
                    f" {ci.get('open') or 0} still open",
                ],
                "change": "assistant ci-status: fix the longest blocker at its root (a suite red on main, the hub"
                " runner's cap or availability) and act on ci.blocked events when they arrive, not when a helper"
                " reports",
                "score": min(5.0, _ratio(ci["minutes"], t["C1"]["target"])) * 1.1,
            }
        )

    co = sig.get("cost") or {}
    wk = co.get("7d") or {}
    if wk.get("per_run") is not None and "C2" in t and wk["per_run"] > t["C2"]["target"]:
        day = co.get("24h") or {}
        out.append(
            {
                "key": "c2:cost-per-run",
                "metric": _metric(t, "C2", wk["per_run"]),
                "title": "Bring the cost per agent run back to pre-v2",
                "evidence": [
                    str(co.get("line") or "")[:300],
                    f"7 d: {wk.get('runs')} agent runs, ${wk.get('cost')}, {wk.get('lands')} lands; 24 h:"
                    f" {day.get('runs')} runs, ${day.get('cost')}, {day.get('lands')} lands",
                ],
                "change": "traces agents --since 'YYYY-MM-DD': the costliest runs and their effort; lower effort where"
                " eval scores hold, fewer re-lands (gate reds), smaller briefs; re-measure after 3 days",
                "score": min(5.0, _ratio(wk["per_run"], t["C2"]["target"])) * 1.0,
            }
        )

    tr = sig.get("traces") or {}
    days = max(1, int(tr.get("days") or 1))
    for hook, n in list((tr.get("hooks") or {}).items())[:3]:
        per_day = n / days
        if per_day > t["G3h"]["target"]:
            out.append(
                {
                    "key": f"g3h:{hook}",
                    "metric": _metric(t, "G3h", round(per_day, 1), name=f"{hook} blocks per day"),
                    "title": f"Stop tripping the {hook} hook",
                    "evidence": [f"{n} blocks in {days} d (all hooks {tr.get('hook_blocks')})"],
                    "change": f"traces: which callers {hook} blocks (tool and category only); fix the skill or script"
                    " that keeps trying it. Never weaken the guard: a rule change needs its tested copy and his OK",
                    "score": min(5.0, _ratio(per_day, t["G3h"]["target"])) * 0.8,
                }
            )
    rate = tr.get("error_rate_pct")
    if rate is not None and rate > t["G3"]["target"]:
        top = ", ".join(f"{k} {v}" for k, v in tr.get("error_top") or [])
        out.append(
            {
                "key": "g3:tool-failures",
                "metric": _metric(t, "G3", rate),
                "title": "Lower the tool-call failure rate",
                "evidence": [f"{tr.get('errors')} of {tr.get('tools')} tool calls failed", f"top: {top or '?'}"],
                "change": "traces report: take the biggest category that is not a plain Bash exit and fix its"
                " caller (timeouts, edit mismatches, browser denials)",
                "score": min(5.0, _ratio(rate, t["G3"]["target"])) * 0.7,
            }
        )
    p95 = tr.get("owner_first_p95_s")
    if p95 is not None and p95 > t["G2"]["target"]:
        out.append(
            {
                "key": "g2:first-reply-p95",
                "metric": _metric(t, "G2", p95),
                "title": "Answer him faster (first reply p95)",
                "evidence": [
                    f"{tr.get('owner_turns')} owner turns: first reply p50 {fmt(tr.get('owner_first_p50_s'), 's')},"
                    f" p95 {fmt(p95, 's')}"
                ],
                "change": "traces report: the slowest owner turns and what ran before the first reply; acknowledge"
                " first (reaction or draft), move slow lookups to the background",
                "score": min(5.0, _ratio(p95, t["G2"]["target"])) * 1.2,
            }
        )

    for f in (sig.get("usage") or {}).get("flagged") or []:
        fid, name = str(f.get("id")), str(f.get("name") or f.get("id"))
        weight = 1.0 + math.log10(1 + int(f.get("uses") or 0)) / 2
        if "broken" in f["flags"] and (f["errors"] >= 5 or f["owner_errors"] > 0):
            out.append(
                {
                    "key": f"u1:{fid}",
                    "metric": _metric(t, "U1", f["errors"], name=f"errors of {fid} (30 d)"),
                    "title": f"Fix {name}",
                    "feature": fid,
                    "evidence": [
                        f"{f['errors']} errors ({f['owner_errors']} in his turns), {f['uses']} uses in 30 d",
                        f"catalogue metric: {f.get('metric') or '-'}",
                    ],
                    "change": f"product report --feature {fid}: find the failing path and fix it; keep its"
                    " instrumentation",
                    "score": (1.5 + math.log10(1 + f["errors"])) * weight,
                }
            )
        if "friction" in f["flags"]:
            n = int(f["complaints"]) + int(f["retries"])
            out.append(
                {
                    "key": f"u2:{fid}",
                    "metric": _metric(t, "U2", n, name=f"complaints + retries of {fid} (30 d)"),
                    "title": f"Smooth {name}",
                    "feature": fid,
                    "evidence": [
                        f"{f['complaints']} complaints, {f['retries']} retries, {f['uses']} uses in 30 d",
                        f"catalogue metric: {f.get('metric') or '-'}",
                    ],
                    "change": f"product report --feature {fid} --by channel: where he retries or complains; fix that"
                    " step",
                    "score": (1.0 + math.log10(1 + n)) * weight,
                }
            )
        if "slow" in f["flags"] and f.get("latency_p50_ms"):
            s = round(float(f["latency_p50_ms"]) / 1000, 1)
            out.append(
                {
                    "key": f"u3:{fid}",
                    "metric": _metric(t, "U3", s, name=f"latency p50 of {fid}"),
                    "title": f"Speed up {name}",
                    "feature": fid,
                    "evidence": [
                        f"latency p50 {fmt(s, 's')}, {f['uses']} uses in 30 d",
                        f"catalogue metric: {f.get('metric') or '-'}",
                    ],
                    "change": f"product report --feature {fid}; traces phoenix slow: the slowest step on its path",
                    "score": 0.8 * weight,
                }
            )
    for f in (sig.get("health") or {}).get("findings") or []:
        sev = int(f.get("sev") or 0)
        if sev < 2 or not f.get("key"):
            continue
        now, base = f.get("now"), f.get("base")
        tgt = base if isinstance(base, (int, float)) else 0
        up = isinstance(now, (int, float)) and now < tgt
        msg = str(f.get("msg") or "")
        out.append(
            {
                "key": f"h1:{f['key']}",
                "metric": _metric(
                    t, "H1", now, name=f"{f.get('source')}: {f.get('metric')}", target=tgt, dir="up" if up else "down"
                ),
                "title": f"Setup health ({f.get('source')}): " + re.split(r"[;(·]", msg)[0].strip()[:70],
                "evidence": [msg[:300]] + ([f"Phoenix trace: {f['link']}"] if f.get("link") else []),
                "change": str(f.get("fix") or "")[:300],
                "score": 3.5 + 0.5 * sev,
            }
        )
    out.sort(key=lambda c: (-c["score"], c["key"]))
    return out


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 3 and w not in STOP}


def ledger_hit(c: dict[str, Any], ledger: list[dict[str, Any]]) -> str | None:
    """A negative ledger row (declined, removed, ...) whose thing the candidate names: stay silent on it."""
    hay = _words(" ".join([c.get("title") or "", c.get("feature") or ""]))
    for row in ledger:
        thing = _words(str(row.get("thing") or ""))
        if len(thing) >= 2 and len(thing & hay) >= max(2, math.ceil(len(thing) * 2 / 3)):
            return f"{row.get('date') or ''} {row.get('verb')}: {row.get('thing')}".strip()
    return None


def negative_ledger() -> list[dict[str, Any]]:
    d, _err = common.run_json([common.bin_path("memory"), "ledger", "--negative", "--json"], timeout=30)
    if isinstance(d, dict):
        d = d.get("rows") or d.get("ledger") or []
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def pick(
    cands: list[dict[str, Any]],
    proposals: dict[str, dict[str, Any]],
    ledger: list[dict[str, Any]],
    *,
    now: float,
    limit: int = 3,
    max_open: int = 6,
    today_n: int = 0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """(new candidates to propose, skip reasons). At most `limit` a day (today_n = already proposed today) and
    `max_open` open or accepted at once. Open or accepted keys are not proposed again; a rejected key waits
    COOLDOWN_DAYS unless its metric got >= 50 % worse since the rejection."""
    skipped: list[str] = []
    by_key: dict[str, dict[str, Any]] = {}
    for p in proposals.values():
        k = p.get("key")
        if k and (k not in by_key or p.get("created_ts", 0) > by_key[k].get("created_ts", 0)):
            by_key[k] = p
    open_n = sum(1 for p in proposals.values() if p.get("status") in ("open", "accepted"))
    room = max(0, min(limit - today_n, max_open - open_n))
    out = []
    for c in cands:
        prev = by_key.get(c["key"])
        if prev and prev.get("status") in ("open", "accepted"):
            skipped.append(f"{c['key']}: already {prev['status']} ({prev['id']})")
            continue
        if prev and prev.get("status") == "rejected":
            age_d = (now - float(prev.get("decided_ts") or prev.get("created_ts") or 0)) / 86400
            was = (prev.get("metric") or {}).get("now")
            worse = isinstance(was, (int, float)) and was > 0 and float(c["metric"]["now"] or 0) >= 1.5 * was
            if age_d < COOLDOWN_DAYS and not worse:
                skipped.append(f"{c['key']}: rejected {age_d:.0f} d ago ({prev['id']})")
                continue
        hit = ledger_hit(c, ledger)
        if hit:
            skipped.append(f"{c['key']}: ledger says {hit}")
            continue
        if len(out) >= room:
            skipped.append(f"{c['key']}: over the limit ({limit} a day, {max_open} open)")
            continue
        out.append(c)
    return out, skipped
