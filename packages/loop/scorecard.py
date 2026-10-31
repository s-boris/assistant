"""loop.scorecard: the continuous scorecard. Speed and health numbers per hour against a rolling 7-day baseline; a
real regression drops ONE inbox note per metric per day (the heartbeat decides what to do with it). No metrics
store of its own: every number is read live from the store that already has it (Phoenix gets the same turns through
`traces`; traces.db stays the local copy).

  metric          what (stat over a trailing window)              source
  reply_p95       first reply to his messages, p95 s (6 h)        traces.db turns: owner trigger, closed turn,
                                                                  first_reply_ms (the loop's G2 = exit E8)
  helper_p95      helper (subagent) run time, p95 s (6 h)         traces.db agents: dur_ms of finished runs, by end
  cost_day        cost per 24 h, USD at list price                 traces.db turns + agents cost (traces report's sum)
  tokens_day      tokens per 24 h                                  traces.db turns + agents: input, output, cache r/w
  land_p95        land time of green lands, p95 s (12 h)           deploys.jsonl op=land: end - land_cmd_start
                                                                  (land_start before 29.10: it covered CI then)
  land_fail       failed share of land attempts (12 h)             deploys.jsonl op=land: ok false (refusals count)
  vault_wait_p95  vault LLM wait, live lane, p95 s (6 h)           `vault metrics --json` llm.wait.live: one sample
                                                                  per run (the vault keeps no history we can window)
  cron_fail       failed share of cron runs (6 h)                  cron-fail's run log (core.cronruns)
  push_p95        phone push delivery, p95 s (24 h)                the brain's mirror of the phone gateway DB
                                                                  (push_log: sent at -> the app's ack, probes left
                                                                  out; = `phone status` push_p95_ms over 24 h). The
                                                                  mirror keeps 8 days: the baseline's first 2 h are
                                                                  missing, and ~3 pushes a day make it no-data often

Regression rule (RULE): at each of the last 3 full hours h, value(h) = the stat over [h - window, h); the baseline =
the same stat over the 7 days before the first checked window (sums scaled to the window; vault: its own 7-day p95).
Regressed when at all 3 hours n >= min_n, value > 1.5 x baseline and value - baseline >= the metric's floor, and the
baseline has n >= min_base. Then one note ~/inbox/scorecard-<epoch>.md per metric per local day. Status per metric:
ok | high (this hour over the bar, not 3 in a row) | regressed | no-data (too few samples: never a made-up number).

`run()` (cron, hourly): one vault sample, compute, the dashboard section ~/.local/state/scorecard/scorecard.json,
spans to Phoenix through the local OTLP receiver (core.otlp: one "scorecard" span with every metric's value / base / n
as numbers, status OK even when a metric regressed (a regression is data, reported by the notes and the
scorecard.*.regressed attributes; an ERROR status would read as a failed job in setup health), one "land" span
per new land row; cron runs already go there as cron.<job>), then the notes.
State ~/.local/state/scorecard/state.json: alerts {metric: day}, the vault samples (9 days), the land span cursor.
`targets(days)`: the loop targets (packages/loop/targets.json: G1 = E7, G2 = E8, G3/G3h/G3p = E13, G4, E9, E10)
measured now through loop.signals, so the 05.11 re-measure reads them here.
Counts and timings only: no message text, branch names or tool input ever leave the sources.
Overrides (tests): SCORECARD_TRACES_DB, SCORECARD_DEPLOYS, SCORECARD_STATE (dir), SCORECARD_VAULT_BIN, CRON_RUNS_DIR,
HUB_PHONE_MIRROR (the mirror file, as hubkit.phonemirror reads it).
"""

from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import core.notes as notes
from core import clock, jsonstate, paths

FACTOR = 1.5
HOURS = 3
BASE_DAYS = 7
SERIES_H = 48
VAULT_KEEP_S = 9 * 86400
FEED_MAX_AGE = 2 * 3600
SERVICE = "scorecard"
RULE = (
    f"p95/share/sum over a trailing window > {FACTOR} x the {BASE_DAYS}-day baseline (and above it by the floor) at "
    f"each of the last {HOURS} full hours, each with n >= min_n; one inbox note per metric per day"
)

# id, label, stat (p95 | sum | share), unit, window h, min_n, min_base, floor (absolute excess over the baseline)
SPECS: tuple[tuple[str, str, str, str, int, int, int, float], ...] = (
    ("reply_p95", "first reply to him", "p95", "s", 6, 5, 20, 10.0),
    ("helper_p95", "helper run time", "p95", "s", 6, 5, 20, 120.0),
    ("cost_day", "cost per day", "sum", "usd", 24, 10, 50, 5.0),
    ("tokens_day", "tokens per day", "sum", "tokens", 24, 10, 50, 5_000_000.0),
    ("land_p95", "land time", "p95", "s", 12, 4, 15, 120.0),
    ("land_fail", "failed lands", "share", "share", 12, 6, 20, 0.15),
    ("vault_wait_p95", "vault LLM wait (live)", "p95", "s", 6, 5, 20, 15.0),
    ("cron_fail", "failed cron runs", "share", "share", 6, 30, 300, 0.02),
    ("push_p95", "phone push delivery", "p95", "s", 24, 3, 10, 10.0),
)
SPEC = {s[0]: s for s in SPECS}


def pct(xs: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..1); None for no values (the slo tiles' definition)."""
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def _env_path(env: str, default: Path) -> Path:
    v = os.environ.get(env, "")
    return paths.guard(v) if v else paths.guard(default)


def state_dir() -> Path:
    return _env_path("SCORECARD_STATE", paths.home() / ".local" / "state" / "scorecard")


def feed_path() -> Path:
    return state_dir() / "scorecard.json"


def _traces_db() -> Path:
    return _env_path("SCORECARD_TRACES_DB", paths.home() / ".local" / "state" / "traces" / "traces.db")


def _deploys() -> Path:
    return _env_path("SCORECARD_DEPLOYS", paths.state("deploys.jsonl"))


def phone_mirror() -> Path:
    return _env_path("HUB_PHONE_MIRROR", paths.home() / ".local" / "state" / "claude-hub" / "phonegw-mirror.db")


def _hour(t: float) -> float:
    lt = datetime.fromtimestamp(t, None)
    return lt.replace(minute=0, second=0, microsecond=0).timestamp()


# ---------------------------------------------------------------- sources: each returns [(ts, x)] in [since, until)
def _traces(sql: str, since: float, until: float) -> list[tuple[float, float]]:
    p = _traces_db()
    if not p.exists():
        raise FileNotFoundError("traces.db")
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(sql, (since, until)).fetchall()
    finally:
        con.close()
    return [(float(t), float(x)) for t, x in rows if t is not None and x is not None]


TOK = "COALESCE(input,0)+COALESCE(output,0)+COALESCE(cache_read,0)+COALESCE(cache_w5,0)+COALESCE(cache_w1h,0)"


def src_reply(since: float, until: float) -> list[tuple[float, float]]:
    return _traces(
        "SELECT start_ts, first_reply_ms / 1000.0 FROM turns WHERE trigger LIKE 'owner%' AND COALESCE(is_open, 0) = 0"
        " AND first_reply_ms IS NOT NULL AND start_ts >= ? AND start_ts < ?",
        since,
        until,
    )


def src_helper(since: float, until: float) -> list[tuple[float, float]]:
    return _traces(
        "SELECT end_ts, dur_ms / 1000.0 FROM agents WHERE file_status = 'final' AND dur_ms IS NOT NULL"
        " AND end_ts >= ? AND end_ts < ?",
        since,
        until,
    )


def _both(col: str, since: float, until: float) -> list[tuple[float, float]]:
    return _traces(
        f"SELECT start_ts, {col} FROM turns WHERE start_ts >= ?1 AND start_ts < ?2"
        f" UNION ALL SELECT start_ts, {col} FROM agents WHERE start_ts >= ?1 AND start_ts < ?2",
        since,
        until,
    )


def src_cost(since: float, until: float) -> list[tuple[float, float]]:
    return _both("COALESCE(cost, 0)", since, until)


def src_tokens(since: float, until: float) -> list[tuple[float, float]]:
    return _both(TOK, since, until)


def land_rows(since: float, until: float) -> list[dict[str, Any]]:
    """op=land rows of deploys.jsonl ending in [since, until): {t, start, ok, reruns} (numbers only)."""
    p = _deploys()
    if not p.exists():
        raise FileNotFoundError("deploys.jsonl")
    out = []
    with p.open(encoding="utf-8", errors="replace") as f:
        for ln in f:
            if '"land"' not in ln:
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("op") != "land":
                continue
            t = r.get("end") or r.get("ts")
            if not isinstance(t, (int, float)) or not since <= t < until:
                continue
            # the whole `assistant land` (CI + land): land_cmd_start since 29.10; before, land_start covered it
            st = r.get("land_cmd_start") if isinstance(r.get("land_cmd_start"), (int, float)) else r.get("land_start")
            rr = r.get("rerun")
            out.append(
                {
                    "t": float(t),
                    "start": float(st) if isinstance(st, (int, float)) and st <= t else None,
                    "ok": bool(r.get("ok")),
                    "reruns": len(rr) if isinstance(rr, dict) else 0,
                }
            )
    out.sort(key=lambda x: x["t"])
    return out


def src_land_p95(since: float, until: float) -> list[tuple[float, float]]:
    return [(r["t"], r["t"] - r["start"]) for r in land_rows(since, until) if r["ok"] and r["start"] is not None]


def src_land_fail(since: float, until: float) -> list[tuple[float, float]]:
    return [(r["t"], 0.0 if r["ok"] else 1.0) for r in land_rows(since, until)]


def src_cron(since: float, until: float) -> list[tuple[float, float]]:
    from core import cronruns

    return [(r["ts"], 0.0 if r["ok"] else 1.0) for r in cronruns.runs(since, until) if r["ts"] < until]


def src_push(since: float, until: float) -> list[tuple[float, float]]:
    """Phone pushes the app acked: (sent at, s to the ack) from the mirror's push_log (at and acked_ms in ms)."""
    p = phone_mirror()
    if not p.exists():
        raise FileNotFoundError("phonegw-mirror.db")
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT at / 1000.0, acked_ms / 1000.0 FROM push_log WHERE COALESCE(level, '') != 'probe'"
            " AND acked_ms IS NOT NULL AND at >= ? AND at < ?",
            (since * 1000, until * 1000),
        ).fetchall()
    finally:
        con.close()
    return [(float(t), float(x)) for t, x in rows if t is not None and x is not None]


SOURCES = {
    "reply_p95": src_reply,
    "helper_p95": src_helper,
    "cost_day": src_cost,
    "tokens_day": src_tokens,
    "land_p95": src_land_p95,
    "land_fail": src_land_fail,
    "cron_fail": src_cron,
    "push_p95": src_push,
}


def stat(kind: str, xs: list[float]) -> float | None:
    if not xs:
        return None
    if kind == "p95":
        return pct(xs, 0.95)
    if kind == "share":
        return sum(xs) / len(xs)
    return float(sum(xs))


def window(rows: list[tuple[float, float]], kind: str, t0: float, t1: float) -> tuple[float | None, int]:
    xs = [x for t, x in rows if t0 <= t < t1]
    return stat(kind, xs), len(xs)


def _point(rows: list[tuple[float, float]], kind: str, t: float, w: float) -> dict[str, Any]:
    v, n = window(rows, kind, t - w, t)
    return {"t": t, "value": v, "n": n}


# ---------------------------------------------------------------- the vault: one sample per run
def _vault_bin() -> str:
    return os.environ.get("SCORECARD_VAULT_BIN") or str(paths.bin_dir("vault"))


def vault_wait(days: float) -> tuple[float | None, int] | None:
    """(live-lane LLM wait p95 s, n) over the last `days`; None when the vault doesn't answer."""
    from loop import common

    d, _err = common.run_json([_vault_bin(), "metrics", "--days", f"{days:g}", "--json"], timeout=40)
    w = (((d or {}).get("llm") or {}).get("wait") or {}).get("live") if isinstance(d, dict) else None
    if not isinstance(w, dict):
        return None
    n = int(w.get("n") or 0)
    v = w.get("p95_s")
    return (float(v) if isinstance(v, (int, float)) and n else None), n


# ---------------------------------------------------------------- compute
def _empty(sid: str, why: str) -> dict[str, Any]:
    _, label, kind, unit, wh, min_n, min_base, floor = SPEC[sid]
    return {
        "id": sid, "label": label, "stat": kind, "unit": unit, "window_h": wh, "min_n": min_n, "floor": floor,
        "value": None, "n": 0, "base": None, "base_n": 0, "ratio": None, "status": "no-data", "hours": [],
        "series": [], "note": why,
    }  # fmt: skip


def _judge(m: dict[str, Any], min_base: int) -> None:
    base, floor = m["base"], m["floor"]
    hours = m["hours"]

    def bad(h: dict[str, Any]) -> bool:
        v = h["value"]
        return v is not None and h["n"] >= m["min_n"] and base is not None and v > FACTOR * base and v - base >= floor

    for h in hours:
        h["over"] = bad(h)
    cur = hours[0] if hours else {"value": None, "n": 0}
    m["value"], m["n"] = cur["value"], cur["n"]
    m["ratio"] = round(m["value"] / base, 2) if m["value"] is not None and base else None
    if base is None or m["base_n"] < min_base or cur["value"] is None or cur["n"] < m["min_n"]:
        m["status"] = "no-data"
    elif len(hours) == HOURS and all(h["over"] for h in hours):
        m["status"] = "regressed"
    elif hours[0]["over"]:
        m["status"] = "high"
    else:
        m["status"] = "ok"


def metric(sid: str, now: float, vault: dict[str, Any] | None = None) -> dict[str, Any]:
    _, _label, kind, _unit, wh, _min_n, min_base, _floor = SPEC[sid]
    m = _empty(sid, "")
    w = wh * 3600.0
    h0 = _hour(now)
    checks = [h0 - k * 3600 for k in range(HOURS)]
    b1 = checks[-1] - w
    b0 = b1 - BASE_DAYS * 86400
    if sid == "vault_wait_p95":
        v = vault or {}
        samples = {float(t): (val, n) for t, val, n in v.get("samples") or []}
        base = v.get("base")
        if base:
            m["base"], m["base_n"] = base[0], base[1]
        series_ts = [h0 - k * 3600 for k in range(SERIES_H)]
        for t in series_ts:
            if t in samples:
                m["series"].append({"t": t, "value": samples[t][0], "n": samples[t][1]})
        m["hours"] = [
            {"t": t, "value": samples[t][0] if t in samples else None, "n": samples[t][1] if t in samples else 0}
            for t in checks
        ]
        m["note"] = "vault metrics: the 7-day baseline includes the checked hours"
        _judge(m, min_base)
        return m
    try:
        rows = SOURCES[sid](min(b0, h0 - SERIES_H * 3600 - w), now)
    except (OSError, sqlite3.Error, ValueError) as e:
        return _empty(sid, f"source unreadable: {type(e).__name__}")
    bv, bn = window(rows, kind, b0, b1)
    if kind == "sum" and bv is not None:
        bv = bv * w / (BASE_DAYS * 86400)
    m["base"], m["base_n"] = bv, bn
    m["hours"] = [_point(rows, kind, t, w) for t in checks]
    m["series"] = [_point(rows, kind, h0 - k * 3600, w) for k in range(SERIES_H - 1, -1, -1)]
    _judge(m, min_base)
    return m


def _round(m: dict[str, Any]) -> dict[str, Any]:
    nd = 4 if m["unit"] in ("share", "usd") else 1
    for k in ("value", "base"):
        if isinstance(m.get(k), float):
            m[k] = round(m[k], nd)
    for h in m["hours"] + m["series"]:
        if isinstance(h.get("value"), float):
            h["value"] = round(h["value"], nd)
    return m


def compute(now: float | None = None, vault: dict[str, Any] | None = None) -> dict[str, Any]:
    now = clock.now() if now is None else now
    ms = [_round(metric(s[0], now, vault)) for s in SPECS]
    summ = {k: sum(1 for m in ms if m["status"] == k) for k in ("ok", "high", "regressed", "no-data")}
    return {
        "at": round(now, 1), "hour": _hour(now), "baseline_days": BASE_DAYS, "factor": FACTOR, "hours": HOURS,
        "rule": RULE, "metrics": ms, "summary": summ,
    }  # fmt: skip


# ---------------------------------------------------------------- state, notes, spans
def load_state() -> dict[str, Any]:
    s = jsonstate.load(state_dir() / "state.json", {})
    return s if isinstance(s, dict) else {}


def save_state(s: dict[str, Any]) -> None:
    jsonstate.save(state_dir() / "state.json", s, mode=0o600, dir_mode=0o700)


def vault_sample(st: dict[str, Any], now: float) -> dict[str, Any]:
    """Take this hour's sample (6 h window) and the 7-day baseline; keep 9 days of samples in the state."""
    wh = SPEC["vault_wait_p95"][4]
    samples = [s for s in st.get("vault") or [] if isinstance(s, list) and len(s) == 3 and s[0] >= now - VAULT_KEEP_S]
    cur = vault_wait(wh / 24)
    if cur is not None:
        h = _hour(now)
        samples = [s for s in samples if s[0] != h] + [[h, cur[0], cur[1]]]
    st["vault"] = samples
    base = vault_wait(BASE_DAYS)
    if base is not None:
        st["vault_base"] = list(base)
    return {"samples": samples, "base": st.get("vault_base") if base is not None else None}


def _fmt(v: Any, unit: str) -> str:
    if v is None:
        return "-"
    if unit == "share":
        return f"{100 * v:.1f} %"
    if unit == "usd":
        return f"${v:.2f}"
    if unit == "tokens":
        return f"{v / 1e6:.1f}M"
    return f"{v:.1f} s"


WHERE = {
    "reply_p95": "traces report --days 1; traces phoenix slow",
    "helper_p95": "traces agents --top 20",
    "cost_day": "traces report --days 2",
    "tokens_day": "traces report --days 2",
    "land_p95": "assistant land log / deploys.jsonl (cifeed)",
    "land_fail": "deploys.jsonl op=land rows (why) / ci-signed",
    "vault_wait_p95": "vault metrics --days 1",
    "cron_fail": "cron-fail / cron-runs",
    "push_p95": "phone status (route, pending, keepalive) / phone pushes; self-review (h) phone",
}


def note_text(m: dict[str, Any]) -> str:
    u = m["unit"]
    hrs = ", ".join(
        f"{datetime.fromtimestamp(h['t'], None):%H:%M} {_fmt(h['value'], u)} (n {h['n']})" for h in m["hours"]
    )
    body = [
        f"{m['label']} ({m['stat']} over {m['window_h']} h) is over {FACTOR} x its {BASE_DAYS}-day baseline for "
        f"{HOURS} hours in a row: now {_fmt(m['value'], u)} vs baseline {_fmt(m['base'], u)} "
        f"(n {m['base_n']}; ratio {m['ratio']}).",
        f"Hours: {hrs}.",
        f"Where to look: {WHERE.get(m['id'], 'scorecard')}. Whole card: `scorecard`.",
        "Numbers only; one note per metric per day.",
    ]
    return notes.compose(
        f"Scorecard regression: {m['label']}",
        body,
        "find the cause in the source above; fix it (or a TODO line) if it hurts him, else note why it is expected."
        " No message to him unless he feels it",
        source="the scorecard",
    )


def alerts(res: dict[str, Any], st: dict[str, Any], now: float) -> list[Path]:
    """One note per regressed metric per local day; st['alerts'] = {metric: day}."""
    day = datetime.fromtimestamp(now, None).strftime("%Y-%m-%d")
    sent = st.setdefault("alerts", {})
    out = []
    for m in res["metrics"]:
        if m["status"] != "regressed" or sent.get(m["id"]) == day:
            continue
        out.append(notes.write_new(paths.inbox(), "scorecard", note_text(m), now=now))
        sent[m["id"]] = day
    return out


def spans(res: dict[str, Any], st: dict[str, Any], now: float) -> int:
    """The scorecard span + one span per new land row -> the local OTLP receiver (-> Phoenix). Returns spans sent."""
    from core import otlp

    attrs: dict[str, Any] = {}
    for m in res["metrics"]:
        for k in ("value", "base", "n"):
            if isinstance(m.get(k), (int, float)):
                attrs[f"scorecard.{m['id']}.{k}"] = m[k]
        attrs[f"scorecard.{m['id']}.regressed"] = int(m["status"] == "regressed")
    attrs["scorecard.regressed"] = int(res["summary"]["regressed"])
    sent = 0
    # never ERROR for a regression: setup health counts ERROR spans as failed runs of the job
    sc = otlp.make_span(SERVICE, secrets.token_hex(16), start=now - 1, end=now, attrs=attrs)
    if otlp.emit([sc], SERVICE, timeout=5.0):
        sent += 1
    cur = float(st.get("land_cursor") or now - BASE_DAYS * 86400)
    try:
        new = [r for r in land_rows(cur, now + 1) if r["t"] > cur]
    except OSError:
        new = []
    for i in range(0, len(new), 200):
        chunk = new[i : i + 200]
        sp = [
            otlp.make_span(
                "land", secrets.token_hex(16), start=r["start"] if r["start"] is not None else r["t"], end=r["t"],
                attrs={"land.ok": int(r["ok"]), "land.reruns": r["reruns"],
                       "land.secs": round(r["t"] - r["start"], 1) if r["start"] is not None else None},
                error=not r["ok"],
            )
            for r in chunk
        ]  # fmt: skip
        if not otlp.emit(sp, SERVICE, timeout=5.0):
            break
        sent += len(sp)
        st["land_cursor"] = chunk[-1]["t"]
    return sent


def run(now: float | None = None, with_targets: bool = True) -> dict[str, Any]:
    """The hourly cron step: vault sample, compute, feed file, spans, notes. Returns the result (+ run counters)."""
    now = clock.now() if now is None else now
    st = load_state()
    vault = vault_sample(st, now)
    res = compute(now, vault)
    if with_targets:
        res["targets"] = targets(1)
    jsonstate.save(feed_path(), res, mode=0o600, dir_mode=0o700)
    res["spans_sent"] = spans(res, st, now)
    res["notes"] = [notes.home_rel(str(p)) for p in alerts(res, st, now)]
    save_state(st)
    return res


def feed(max_age: float = FEED_MAX_AGE) -> dict[str, Any] | None:
    """The dashboard section: the last run's result while it is under max_age, else None."""
    d = jsonstate.load(feed_path(), None)
    if not isinstance(d, dict) or clock.now() - float(d.get("at") or 0) > max_age:
        return None
    return d


# ---------------------------------------------------------------- the loop / exit targets
EXIT = {"G1": "E7", "G1d": "E7", "G2": "E8", "G3": "E13", "G3h": "E13", "G3p": "E13", "G4": "E14", "E9": "E9",
        "E10": "E10"}  # fmt: skip


def egress_off_hub() -> tuple[int | None, str | None]:
    from loop import common

    d, err = common.run_json([common.bin_path("hub"), "egress", "status", "--json"], timeout=40)
    loc = d.get("local") if isinstance(d, dict) else None
    if not isinstance(loc, dict):
        return None, err or "hub egress status: no local block"
    return sum(1 for v in loc.values() if v != "hub"), None


def targets(days: float = 1) -> dict[str, Any]:
    """Each loop target with a source here: value now, target, met (None = no data), the exit criterion it carries."""
    from loop import propose, signals

    t = propose.targets()
    d = max(1, int(round(days)))
    tr, e1 = signals.traces(d)
    bt, e2 = signals.beats(d)
    dn, e3 = signals.denials(d)
    ev, e4 = signals.evals(int(d * 24 + 2))
    eg, e5 = egress_off_hub()
    tr, bt, dn, ev = tr or {}, bt or {}, dn or {}, ev or {}
    nf = ev.get("new_fail")
    vals: dict[str, Any] = {
        "G1": bt.get("tokens_mean"),
        "G1d": bt.get("tokens_per_day"),
        "G2": tr.get("owner_first_p95_s"),
        "G3": tr.get("error_rate_pct") if tr else None,
        "G3h": round(tr["hook_blocks"] / d, 1) if isinstance(tr.get("hook_blocks"), int) else None,
        "G3p": sum(int(v.get("unexpected") or 0) for v in (dn.get("domains") or {}).values()) if dn else None,
        "G4": len(nf) if isinstance(nf, list) else nf if isinstance(nf, int) else None,
        "E9": None,
        "E10": eg,
    }
    extra = {
        "G1": {"p95": bt.get("tokens_p95"), "beats": bt.get("beat_turns")},
        "G2": {"p50": tr.get("owner_first_p50_s"), "n": tr.get("owner_turns")},
        "G4": {"score": ev.get("score"), "run": ev.get("run_id")},
        "E9": {"why": "no tool measures the typed share yet"},
        "E10": {"what": "egress handles not on the hub (shadow or local)"},
    }
    rows = []
    for k, v in vals.items():
        if k not in t:
            continue
        tgt, dirn = t[k].get("target"), t[k].get("dir", "down")
        met = None if v is None or tgt is None else (v <= tgt if dirn == "down" else v >= tgt)
        rows.append({"id": k, "exit": EXIT.get(k), "name": t[k].get("name"), "value": v, "target": tgt,
                     "unit": t[k].get("unit"), "dir": dirn, "met": met, "source": t[k].get("source"),
                     **({"extra": extra[k]} if k in extra else {})})  # fmt: skip
    errs = [e for e in (e1, e2, e3, e4, e5) if e]
    return {"days": d, "rows": rows, "errors": errs}


# ---------------------------------------------------------------- render
def render(res: dict[str, Any]) -> str:
    at = datetime.fromtimestamp(res["at"], None)
    s = res["summary"]
    out = [
        f"scorecard {at:%d.%m %H:%M}: {s['ok']} ok · {s['high']} high · {s['regressed']} regressed · "
        f"{s['no-data']} no data (baseline {res['baseline_days']} d, x{res['factor']}, {res['hours']} h in a row)"
    ]
    for m in res["metrics"]:
        u = m["unit"]
        out.append(
            f"  {m['id']:15} {m['status']:9} now {_fmt(m['value'], u):>9} (n {m['n']:>4})  base "
            f"{_fmt(m['base'], u):>9} (n {m['base_n']:>5})  {m['label']}, {m['stat']} {m['window_h']} h"
            + (f"  [{m['note']}]" if m.get("note") and m["status"] == "no-data" else "")
        )
    if res.get("targets"):
        out.append(render_targets(res["targets"]))
    return "\n".join(out)


def render_targets(tg: dict[str, Any]) -> str:
    out = [f"targets ({tg['days']} d): " + " · ".join(
        f"{r['id']}{'/' + r['exit'] if r['exit'] and r['exit'] != r['id'] else ''} "
        f"{'met' if r['met'] else 'miss' if r['met'] is False else 'no-data'}" for r in tg["rows"])]  # fmt: skip
    for r in tg["rows"]:
        out.append(f"  {r['id']:4} {str(r['exit'] or ''):4} {r['name'][:46]:46} now {r['value']!s:>10}  target "
                   f"{r['target']!s:>8} {r['unit'] or ''}")  # fmt: skip
    for e in tg.get("errors") or []:
        out.append(f"  source error: {e}")
    return "\n".join(out)
