"""inbox.metrics: M2/M3 from the ledger, the old-path proxy and the beat baselines. Read-only.

  compute(con, since, until, *, traces_db, hbgate_log, beats=False) -> dict

M2 (agenda path; needs beats rows, i.e. the registered hook):
  listed_first  share of events listed in the first agenda after they landed (acked before that beat: not counted)
  listed_2      share listed within the first two agendas
  hook_fail     beats rows with ok=0 / all beats rows; plus traces beat turns without a beats row within 120 s
  latency       emit -> first listed, p50/p95 minutes per delivery class (now, beat)
M3:
  restarts      per restart beat: events pending before it that its agenda did not list (want: none)
  stale_new     events still status new whose last change is older than 3 h (want 0)
  gone_no_ack   notes deleted without an ack in the window (want 0 once the brain acks)
Old-path proxy (ingest + traces.db beat starts, read-only): for notes that landed in the window, was the note
still there at the first beat after it landed (else it was deleted before any beat saw it), and the minutes from
landing to that beat. Ingest runs every minute, so a note that lived < 1 min is not counted at all.
--beats: the beat baselines from traces.db: beat turns, tokens per beat (mean/p50/p95, idle vs notes), API calls per
beat, context per call, cache-read share, cost per beat and per day, tokens per day, compactions (from the meta
scan), owner first-reply p50/p95, hb-gate wake/skip counts.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics as st
from collections.abc import Sequence
from datetime import datetime, tzinfo
from typing import Any

from core import paths

__all__ = ["compute", "pct", "render"]

BEAT_TRIGGERS = ("heartbeat", "inbox")


def pct(xs: Sequence[float], q: float) -> float | None:
    """Inclusive percentile (statistics.quantiles method='inclusive'); None for no data."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _share(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _ro(path: str) -> sqlite3.Connection | None:
    if not path or not os.path.exists(path):
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _beats(con: sqlite3.Connection, since: float, until: float) -> list[dict[str, Any]]:
    out = []
    for r in con.execute("SELECT * FROM beats WHERE ts >= ? AND ts <= ? ORDER BY ts, id", (since, until)):
        d = dict(r)
        try:
            d["listed_set"] = set(json.loads(d.get("listed") or "[]"))
        except ValueError:
            d["listed_set"] = set()
        out.append(d)
    return out


def _traces_beats(tc: sqlite3.Connection | None, since: float, until: float) -> list[float]:
    if tc is None:
        return []
    try:
        rows = tc.execute(
            "SELECT start_ts FROM turns WHERE start_ts >= ? AND start_ts <= ? AND (trigger IN"
            " ('heartbeat','inbox') OR start_kind='restart') ORDER BY start_ts",
            (since, until),
        )
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def m2(con: sqlite3.Connection, since: float, until: float, tc: sqlite3.Connection | None) -> dict[str, Any]:
    beats = _beats(con, since, until + 86400)
    ok_beats = [b for b in beats if b["ok"]]
    in_win = [b for b in beats if b["ts"] <= until]
    evs = [dict(r) for r in con.execute("SELECT * FROM events WHERE ts >= ? AND ts <= ?", (since, until))]
    first_n = second_n = denom = pending = 0
    lat: dict[str, list[float]] = {"now": [], "beat": []}
    for e in evs:
        after = [b for b in ok_beats if b["ts"] > e["ts"]]
        if not after:
            pending += 1
            continue
        if e.get("acked_ts") and e["acked_ts"] <= after[0]["ts"]:
            continue
        denom += 1
        if e["id"] in after[0]["listed_set"]:
            first_n += 1
            second_n += 1
        elif len(after) > 1 and e["id"] in after[1]["listed_set"]:
            second_n += 1
        if e.get("first_listed_ts") and e.get("delivery") in lat:
            lat[e["delivery"]].append((e["first_listed_ts"] - e["ts"]) / 60)
    fails = sum(1 for b in in_win if not b["ok"])
    tb = _traces_beats(tc, since, until)
    unmatched = sum(1 for t in tb if not any(abs(b["ts"] - t) <= 120 for b in in_win)) if in_win or tb else 0
    return {
        "events": len(evs),
        "counted": denom,
        "pending": pending,
        "listed_first": _share(first_n, denom),
        "listed_2": _share(second_n, denom),
        "beats": len(in_win),
        "hook_fail": _share(fails, len(in_win)),
        "traces_beats": len(tb),
        "traces_beats_without_agenda": unmatched if tb and in_win else None,
        "latency_min": {k: {"n": len(v), "p50": _r(pct(v, 0.5)), "p95": _r(pct(v, 0.95))} for k, v in lat.items()},
    }


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


def m3(con: sqlite3.Connection, since: float, until: float, now: float) -> dict[str, Any]:
    beats = [b for b in _beats(con, since, until) if b["ok"] and b.get("source") == "restart"]
    evs = [
        dict(r) for r in con.execute("SELECT id, ts, status, updated_ts, acked_ts FROM events WHERE ts <= ?", (until,))
    ]
    restarts = []
    for b in beats:
        t = b["ts"]
        pending = [
            e["id"]
            for e in evs
            if e["ts"] < t
            and not (e["acked_ts"] and e["acked_ts"] < t)
            and not (e["status"] == "gone" and e["updated_ts"] and e["updated_ts"] < t)
        ]
        missing = [i for i in pending if i not in b["listed_set"]]
        restarts.append({"beat": b["id"], "ts": t, "pending": len(pending), "missing": missing[:20], "ok": not missing})
    stale = con.execute(
        "SELECT COUNT(*) FROM events WHERE status='new' AND COALESCE(updated_ts, ts) < ?", (now - 3 * 3600,)
    ).fetchone()[0]
    gone = con.execute(
        "SELECT COUNT(*) FROM events WHERE status='gone' AND updated_ts >= ? AND updated_ts <= ?", (since, until)
    ).fetchone()[0]
    return {
        "restarts": restarts,
        "restarts_ok": all(r["ok"] for r in restarts),
        "stale_new": stale,
        "gone_no_ack": gone,
    }


def writers(con: sqlite3.Connection, since: float, until: float, err_log: str | None = None) -> dict[str, Any]:
    """The emit contract: per note kind that has rows a writer emitted (source not "inbox"), typed rows vs
    rows ingest made from notes; dupes = note names with more than one row of which one is typed; emit errors =
    lines in emit-errors.log in the window (store failures, writer fallbacks, update failures)."""
    rows = con.execute(
        "SELECT kind, source, idem_key FROM events WHERE note_path IS NOT NULL AND ts >= ? AND ts <= ?", (since, until)
    ).fetchall()
    typed_kinds = {r[0] for r in rows if r[1] != "inbox"}
    per: dict[str, dict[str, int]] = {}
    names: dict[str, list[bool]] = {}
    for kind, src, key in rows:
        if kind not in typed_kinds:
            continue
        d = per.setdefault(kind, {"typed": 0, "ingested": 0})
        d["typed" if src != "inbox" else "ingested"] += 1
        names.setdefault(str(key or "").split("#", 1)[0], []).append(src != "inbox")
    dupes = sorted(n for n, t in names.items() if len(t) > 1 and any(t))
    errs: dict[str, int] = {}
    try:
        with open(err_log or paths.state("emit-errors.log"), encoding="utf-8", errors="replace") as f:
            for ln in f:
                parts = ln.split(" ", 2)
                try:
                    t = float(parts[0])
                except (ValueError, IndexError):
                    continue
                if since <= t <= until and len(parts) > 1:
                    errs[parts[1]] = errs.get(parts[1], 0) + 1
    except OSError:
        pass
    return {
        "kinds": per,
        "dupes": len(dupes),
        "dupe_names": dupes[:10],
        "emit_errors": sum(errs.values()),
        "emit_errors_by_kind": errs,
    }


def old_path(con: sqlite3.Connection, since: float, until: float, tc: sqlite3.Connection | None) -> dict[str, Any]:
    tb = _traces_beats(tc, since, until + 86400)
    if tc is None:
        return {"available": False}
    evs = [
        dict(r)
        for r in con.execute(
            "SELECT id, ts, kind, status, updated_ts, acked_ts FROM events"
            " WHERE source='inbox' AND ts >= ? AND ts <= ?",
            (since, until),
        )
    ]
    n = seen = before = pending = 0
    wait: list[float] = []
    by_kind: dict[str, int] = {}
    for e in evs:
        nxt = next((t for t in tb if t > e["ts"]), None)
        if nxt is None:
            pending += 1
            continue
        n += 1
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        end = (
            e["acked_ts"]
            if e["status"] in ("acked", "dismissed")
            else (e["updated_ts"] if e["status"] == "gone" else None)
        )
        if end is not None and end <= nxt:
            before += 1
        else:
            seen += 1
        wait.append((nxt - e["ts"]) / 60)
    top = sorted(by_kind.items(), key=lambda kv: -kv[1])[:6]
    return {
        "available": True,
        "notes": n,
        "pending": pending,
        "seen_by_a_beat": _share(seen, n),
        "deleted_before_first_beat": before,
        "to_first_beat_min": {"p50": _r(pct(wait, 0.5)), "p95": _r(pct(wait, 0.95))},
        "by_kind": dict(top),
    }


def _hbgate(path: str, since: float, until: float, tz: tzinfo) -> dict[str, int] | None:
    if not path or not os.path.exists(path):
        return None
    c = {"wake": 0, "skip": 0}
    rx = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (wake|skip) ")
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = rx.match(line)
            if not m:
                continue
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()
            if since <= t <= until:
                c[m.group(2)] += 1
    return c


def beats_baseline(
    con: sqlite3.Connection, tc: sqlite3.Connection | None, since: float, until: float, tz: tzinfo, hbgate_log: str
) -> dict[str, Any]:
    days = max((until - since) / 86400, 1e-9)
    out: dict[str, Any] = {"days": round(days, 2)}
    if tc is not None:
        rows = [
            dict(r)
            for r in tc.execute(
                "SELECT trigger, start_kind, api_calls, input, output, cache_read, cache_w5, cache_w1h, cost FROM turns"
                " WHERE start_ts >= ? AND start_ts < ? AND trigger IN ('heartbeat','inbox')",
                (since, until),
            )
        ]

        def tok(r: dict[str, Any]) -> int:
            return sum(int(r.get(k) or 0) for k in ("input", "output", "cache_read", "cache_w5", "cache_w1h"))

        def ctx(r: dict[str, Any]) -> int:
            return sum(int(r.get(k) or 0) for k in ("input", "cache_read", "cache_w5", "cache_w1h"))

        def stats(rs: list[dict[str, Any]]) -> dict[str, Any]:
            t = [tok(r) for r in rs]
            return {
                "n": len(rs),
                "mean": _r(st.mean(t) if t else None, 0),
                "p50": _r(pct(t, 0.5), 0),
                "p95": _r(pct(t, 0.95), 0),
                "calls": _r(st.mean([int(r.get("api_calls") or 0) for r in rs]) if rs else None, 2),
            }

        idle = [r for r in rows if r["trigger"] == "heartbeat"]
        notes = [r for r in rows if r["trigger"] == "inbox"]
        per_call = [ctx(r) / r["api_calls"] for r in rows if r.get("api_calls")]
        allctx = sum(ctx(r) for r in rows)
        cost = sum(float(r.get("cost") or 0) for r in rows)
        out.update(
            {
                "beat_turns": len(rows),
                "per_day": _r(len(rows) / days, 1),
                "tokens": stats(rows),
                "idle": stats(idle),
                "notes": stats(notes),
                "context_per_call": _r(st.mean(per_call) if per_call else None, 0),
                "cache_read_share": _r(
                    sum(int(r.get("cache_read") or 0) for r in rows) / allctx if allctx else None, 4
                ),
                "cost_per_beat": _r(cost / len(rows) if rows else None, 4),
                "cost_per_day": _r(cost / days, 2),
                "tokens_per_day": _r(sum(tok(r) for r in rows) / days, 0),
            }
        )
        fr = [
            r[0] / 1000
            for r in tc.execute(
                "SELECT first_reply_ms FROM turns WHERE start_ts >= ? AND start_ts < ?"
                " AND trigger='owner' AND first_reply_ms IS NOT NULL",
                (since, until),
            )
        ]
        out["owner_first_reply_s"] = {"n": len(fr), "p50": _r(pct(fr, 0.5)), "p95": _r(pct(fr, 0.95))}
    try:
        comps = [
            c
            for c in (
                json.loads((con.execute("SELECT v FROM meta WHERE k='compactions'").fetchone() or ["[]"])[0]) or []
            )
            if since <= c.get("ts", 0) < until
        ]
    except (ValueError, sqlite3.Error):
        comps = []
    posts = [c["post"] for c in comps if isinstance(c.get("post"), int)]
    out["compactions"] = {
        "n": len(comps),
        "per_day": _r(len(comps) / days, 1),
        "pre_p50": _r(pct([c["pre"] for c in comps if isinstance(c.get("pre"), int)], 0.5), 0),
        "secs_p50": _r(pct([c["ms"] / 1000 for c in comps if isinstance(c.get("ms"), int)], 0.5)),
        "post_min": min(posts) if posts else None,
        "post_max": max(posts) if posts else None,
    }
    out["hb_gate"] = _hbgate(hbgate_log, since, until, tz)
    return out


def compute(
    con: sqlite3.Connection,
    since: float,
    until: float,
    *,
    now: float,
    traces_db: str,
    hbgate_log: str,
    tz: tzinfo,
    beats: bool = False,
) -> dict[str, Any]:
    tc = _ro(traces_db)
    try:
        res: dict[str, Any] = {
            "since": since,
            "until": until,
            "m2": m2(con, since, until, tc),
            "m3": m3(con, since, until, now),
            "old_path": old_path(con, since, until, tc),
            "writers": writers(con, since, until),
        }
        if beats:
            res["beats"] = beats_baseline(con, tc, since, until, tz, hbgate_log)
        return res
    finally:
        if tc is not None:
            tc.close()


def _f(x: Any, pc: bool = False) -> str:
    if x is None:
        return "–"
    return f"{x * 100:.1f} %" if pc else str(x)


def render(res: dict[str, Any], tz: tzinfo) -> str:
    a = datetime.fromtimestamp(res["since"], tz)
    b = datetime.fromtimestamp(res["until"], tz)
    m2_, m3_, op = res["m2"], res["m3"], res["old_path"]
    L = [f"events metrics {a:%d.%m %H:%M} → {b:%d.%m %H:%M}"]
    lat = m2_["latency_min"]
    L.append(
        f"M2 events {m2_['events']} (counted {m2_['counted']}, pending {m2_['pending']}) · listed first agenda "
        f"{_f(m2_['listed_first'], True)} · within 2 {_f(m2_['listed_2'], True)} · beats {m2_['beats']}, hook "
        f"fail {_f(m2_['hook_fail'], True)} · traces beats {m2_['traces_beats']} (no agenda "
        f"{_f(m2_['traces_beats_without_agenda'])}) · listed after p95: now {_f(lat['now']['p95'])} min, beat "
        f"{_f(lat['beat']['p95'])} min"
    )
    bad = [r for r in m3_["restarts"] if not r["ok"]]
    L.append(
        f"M3 restarts {len(m3_['restarts'])} ({len(bad)} missed something) · new > 3 h {m3_['stale_new']} · "
        f"deleted without ack {m3_['gone_no_ack']}"
    )
    wr = res.get("writers") or {}
    if wr:
        ks = " · ".join(f"{k} {v['typed']}/{v['typed'] + v['ingested']}" for k, v in sorted(wr["kinds"].items()))
        L.append(f"writers typed/all: {ks or 'none yet'} · dupes {wr['dupes']} · emit errors {wr['emit_errors']}")
    if op.get("available"):
        L.append(
            f"old path: notes {op['notes']} (pending {op['pending']}) · still there at the first beat "
            f"{_f(op['seen_by_a_beat'], True)} · deleted before it {op['deleted_before_first_beat']} · to first "
            f"beat p50 {_f(op['to_first_beat_min']['p50'])} / p95 {_f(op['to_first_beat_min']['p95'])} min"
        )
    else:
        L.append("old path: traces.db not available")
    bb = res.get("beats")
    if bb:
        if "beat_turns" in bb:
            t, i, n = bb["tokens"], bb["idle"], bb["notes"]
            L.append(
                f"beats {bb['beat_turns']} ({bb['per_day']}/day) · tokens/beat mean {_k(t['mean'])} p50 "
                f"{_k(t['p50'])} p95 {_k(t['p95'])} (idle {_k(i['mean'])}/{_k(i['p50'])}, notes "
                f"{_k(n['mean'])}/{_k(n['p50'])})"
            )
            L.append(
                f"API calls/beat {t['calls']} (idle {i['calls']}, notes {n['calls']}) · context/call "
                f"{_k(bb['context_per_call'])} · cache reads {_f(bb['cache_read_share'], True)} · cost/beat "
                f"${bb['cost_per_beat']} · ${bb['cost_per_day']}/day · {_k(bb['tokens_per_day'])}/day"
            )
            fr = bb["owner_first_reply_s"]
            L.append(f"owner first reply p50 {fr['p50']} s, p95 {fr['p95']} s (n {fr['n']})")
        c = bb["compactions"]
        L.append(
            f"compactions {c['n']} ({c['per_day']}/day) · pre p50 {_k(c['pre_p50'])} · p50 {c['secs_p50']} s · "
            f"summary {_k(c['post_min'])} → {_k(c['post_max'])}"
        )
        hg = bb.get("hb_gate")
        if hg:
            L.append(f"hb-gate {hg['wake']} wake / {hg['skip']} skip")
    return "\n".join(L)


def _k(x: Any) -> str:
    if x is None:
        return "–"
    x = float(x)
    if x >= 1e6:
        return f"{x / 1e6:.2f}M"
    if x >= 1e3:
        return f"{x / 1e3:.0f}k"
    return f"{x:.0f}"
