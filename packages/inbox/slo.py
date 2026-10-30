"""inbox.slo: the loop SLOs, read-only, from the stores that already time these paths.

  tap_ack        tap -> ack < 1 s (p95)            tg.button ack_ms (events.db, or the hub's rows in hub mode)
  first_reply    first reply p95 < 20 s            traces.db turns: owner turns' first_reply_ms
  event_message  event -> message < 60 s (p95)     outbox rows with reply_to_event: sent_ts - the event's ts
  vault_answer   vault answer < 10 s (p95)         gateway.db job log: vault.ask jobs, created -> ended, finished
                                                   (done) jobs only, by their end time. Beside it (no target): ack =
                                                   every vault.ask call's own time (answer or job id back: the
                                                   caller's wait), pending = calls that came back with the job
                                                   still running (code "pending")
  job_start_lag  job start lag p95 <= 10 s         hub schedule runs: created_at - the tick in the run id
                                                   (dispatch lag; the hub-jobs queue wait is not in it)
  reminder_late  reminder lateness p95 <= 30 s     remind's deliveries.jsonl: sent t - due slot (time reminders)
  joined_traces  joined-trace share >= 95 %        `traces phoenix joined --json` share (his turns with a joined
                                                   trace; it counts >= 1 interaction span, not exactly one)

Each tile: id, label, value, unit, stat, target, op, n, window_days, status ok | miss | no-data, source, note.
No rows or an unreadable source = no-data with value null, never a made-up number. Counts and timings only.
`compute(days)` -> {"at", "window_days", "tiles": [...], "summary": {ok, miss, no_data}}; `write_cache` keeps the last
result in ~/.local/state/slo/tiles.json (0600) for the hub feed (hubkit.feed reads it). `sweep_jobs` (the cache run
calls it first) is the one write: while a job log row is open it runs `tool jobs sweep`, which closes it from the
vault's job list (kept 30 min after a job ends).
Source overrides (tests): SLO_TRACES_DB, SLO_GATEWAY_DB, SLO_REMIND_LOG, SLO_TRACES_BIN, SLO_TOOL_BIN, SLO_CACHE.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from core import clock, paths

# id, label, stat, op, target, unit
SPECS: tuple[tuple[str, str, str, str, float, str], ...] = (
    ("tap_ack", "tap -> ack", "p95", "<", 1.0, "s"),
    ("first_reply", "first reply", "p95", "<", 20.0, "s"),
    ("event_message", "event -> message", "p95", "<", 60.0, "s"),
    ("vault_answer", "vault answer", "p95", "<", 10.0, "s"),
    ("job_start_lag", "job start lag", "p95", "<=", 10.0, "s"),
    ("reminder_late", "reminder lateness", "p95", "<=", 30.0, "s"),
    ("joined_traces", "one trace per request", "share", ">=", 0.95, "share"),
)
FEED_MAX_AGE = 3600.0
RUN_ID_TICK = re.compile(r"-(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)$")


def pct(xs: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..1); None for no values."""
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def _p(env: str, default: Path) -> Path:
    v = os.environ.get(env, "")
    return paths.guard(v) if v else paths.guard(default)


def _ro(p: Path) -> sqlite3.Connection:
    if not p.exists():
        raise FileNotFoundError(str(p))
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _bin(env: str, name: str) -> str:
    return os.environ.get(env) or str(paths.bin_dir(name))


def _json_cmd(argv: list[str], timeout: float = 60) -> Any:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"{Path(argv[0]).name} exit {r.returncode}")
    return json.loads(r.stdout or "null")


# ---------------------------------------------------------------- sources: each returns (values, note)
def _events_con(since: float, until: float) -> tuple[sqlite3.Connection, str]:
    from inbox import tgmetrics

    if tgmetrics._hub_tg():
        return tgmetrics.hub_mirror(since, until), "hub"
    return _ro(paths.state("events.db")), "events.db"


def src_tap_ack(since: float, until: float) -> tuple[list[float], str]:
    con, where = _events_con(since, until)
    try:
        rows = con.execute(
            "SELECT payload FROM events WHERE kind='tg.button' AND ts>=? AND ts<?", (since, until)
        ).fetchall()
    finally:
        con.close()
    out = []
    for (raw,) in rows:
        try:
            v = (json.loads(raw) if isinstance(raw, str) else raw or {}).get("ack_ms")
        except (ValueError, AttributeError):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(v / 1000.0)
    return out, where


def src_event_message(since: float, until: float) -> tuple[list[float], str]:
    con, where = _events_con(since, until)
    try:
        rows = con.execute(
            "SELECT o.sent_ts - e.ts FROM outbox o JOIN events e ON e.id = o.reply_to_event WHERE o.status = 'sent'"
            " AND o.sent_ts IS NOT NULL AND e.ts >= ? AND e.ts < ?",
            (since, until),
        ).fetchall()
    finally:
        con.close()
    return [float(r[0]) for r in rows if r[0] is not None and r[0] >= 0], where


def src_first_reply(since: float, until: float) -> tuple[list[float], str]:
    con = _ro(_p("SLO_TRACES_DB", paths.home() / ".local" / "state" / "traces" / "traces.db"))
    try:
        rows = con.execute(
            "SELECT first_reply_ms FROM turns WHERE trigger LIKE 'owner%' AND first_reply_ms IS NOT NULL"
            " AND start_ts >= ? AND start_ts < ?",
            (since, until),
        ).fetchall()
    finally:
        con.close()
    return [r[0] / 1000.0 for r in rows], "traces.db"


def _gateway_db() -> Path:
    return _p("SLO_GATEWAY_DB", paths.home() / ".local" / "state" / "tools" / "gateway.db")


def src_vault_answer(since: float, until: float) -> tuple[list[float], str]:
    """The vault job behind each vault.ask, start -> finished, finished jobs only (not the caller's wait_s)."""
    con = _ro(_gateway_db())
    try:
        rows = con.execute(
            "SELECT s FROM jobs WHERE tool = 'vault.ask' AND state = 'done' AND s IS NOT NULL AND ended >= ?"
            " AND ended < ?",
            (since, until),
        ).fetchall()
    finally:
        con.close()
    return [float(r[0]) for r in rows], "gateway.db job log vault.ask (job created -> ended)"


def vault_ack(since: float, until: float) -> dict[str, Any]:
    """Beside vault_answer, no target: ack = each vault.ask call's own time (the caller got the answer or the job
    id back), its p95; pending = calls that came back while the job still ran. Rows from before the "pending" code
    are in ack and count as answered."""
    con = _ro(_gateway_db())
    try:
        rows = con.execute(
            "SELECT ms, code FROM calls WHERE tool = 'vault.ask' AND code IN ('ok', 'pending') AND ms IS NOT NULL"
            " AND ts >= ? AND ts < ?",
            (since, until),
        ).fetchall()
    finally:
        con.close()
    v = pct([r[0] / 1000.0 for r in rows], 0.95)
    return {"n": len(rows), "p95": None if v is None else round(v, 2), "pending": sum(r[1] == "pending" for r in rows)}


def sweep_jobs(timeout: float = 120) -> str:
    """Before a cache refresh: while the gateway's job log has an open row (queued / running), `tool jobs sweep`
    closes what the vault's job list has finished. Never raises; a short word for the log."""
    try:
        con = _ro(_gateway_db())
        try:
            (n,) = con.execute("SELECT count(*) FROM jobs WHERE state IN ('queued', 'running')").fetchone()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return "no job log"
    if not n:
        return "nothing open"
    try:
        r = subprocess.run(
            [_bin("SLO_TOOL_BIN", "tool"), "jobs", "sweep", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"sweep failed: {type(e).__name__}"
    return "swept" if r.returncode == 0 else f"sweep exit {r.returncode}"


def _epoch(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _runs(o: Any) -> list[dict[str, Any]]:
    """Every {"id", "at"} run dict under any "runs" list of the workflow.schedules answer."""
    out: list[dict[str, Any]] = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "runs" and isinstance(v, list):
                out += [r for r in v if isinstance(r, dict) and "id" in r]
            else:
                out += _runs(v)
    elif isinstance(o, list):
        for v in o:
            out += _runs(v)
    return out


def src_job_start_lag(since: float, until: float) -> tuple[list[float], str]:
    d = _json_cmd(
        [_bin("SLO_TOOL_BIN", "tool"), "call", "workflow.schedules", '{"runs": 20, "format": "detailed"}', "--json"],
        timeout=60,
    )
    out = []
    for r in _runs(d):
        m = RUN_ID_TICK.search(str(r.get("id") or ""))
        at = _epoch(r.get("at"))
        tick = _epoch(m[1]) if m else None
        if at is None or tick is None or not since <= tick < until:
            continue
        out.append(max(0.0, at - tick))
    return out, "hub schedule runs (dispatch lag)"


def src_reminder_late(since: float, until: float) -> tuple[list[float], str]:
    from core import todo

    p = _p("SLO_REMIND_LOG", paths.home() / ".local" / "state" / "remind" / "deliveries.jsonl")
    tz = todo.owner_tz()
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                t = float(r["t"])
                if r.get("place") or not since <= t < until:
                    continue
                due = datetime.strptime(str(r["at"]), "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            out.append(max(0.0, t - due))
    return out, "remind deliveries.jsonl"


def src_joined(since: float, until: float) -> tuple[dict[str, Any], str]:
    hours = max(1, round((until - since) / 3600))
    d = _json_cmd([_bin("SLO_TRACES_BIN", "traces"), "phoenix", "joined", "--hours", str(hours), "--json"], timeout=90)
    if not isinstance(d, dict):
        raise RuntimeError("traces: no JSON object")
    return d, "traces phoenix joined"


SOURCES: dict[str, Callable[[float, float], tuple[Any, str]]] = {
    "tap_ack": src_tap_ack,
    "first_reply": src_first_reply,
    "event_message": src_event_message,
    "vault_answer": src_vault_answer,
    "job_start_lag": src_job_start_lag,
    "reminder_late": src_reminder_late,
    "joined_traces": src_joined,
}


def _status(value: float | None, op: str, target: float) -> str:
    if value is None:
        return "no-data"
    ok = {"<": value < target, "<=": value <= target, ">=": value >= target}[op]
    return "ok" if ok else "miss"


def tile(sid: str, since: float, until: float, days: float) -> dict[str, Any]:
    spec = next(s for s in SPECS if s[0] == sid)
    _, label, stat, op, target, unit = spec
    t: dict[str, Any] = {"id": sid, "label": label, "stat": stat, "op": op, "target": target, "unit": unit,
                         "window_days": days, "value": None, "n": 0, "source": None, "note": None}  # fmt: skip
    try:
        got, src = SOURCES[sid](since, until)
        t["source"] = src
        if sid == "joined_traces":
            n = got.get("owner_turns")
            share = got.get("share")
            t["n"] = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else 0
            if t["n"] and isinstance(share, (int, float)) and not isinstance(share, bool):
                t["value"] = round(float(share), 3)
            t["note"] = "turns with >= 1 joined interaction span"
        else:
            t["n"] = len(got)
            v = pct(got, 0.95)
            t["value"] = None if v is None else round(v, 2)
    except Exception as e:  # noqa: BLE001  (a source that fails is no-data, with the error type only)
        t["note"] = f"source failed: {type(e).__name__}"
    if sid == "vault_answer":
        try:
            ack = vault_ack(since, until)
        except Exception:  # noqa: BLE001  (the ack series is extra: no note of its own)
            ack = None
        if ack is not None:
            t["ack"] = ack
            if not t["note"]:
                p95 = "-" if ack["p95"] is None else f"{ack['p95']:g} s"
                t["note"] = f"finished jobs only · ack p95 {p95} (n {ack['n']}, {ack['pending']} pending)"
    t["status"] = _status(t["value"], op, target)
    return t


def compute(days: float = 7.0, only: list[str] | None = None) -> dict[str, Any]:
    until = clock.now()
    since = until - days * 86400
    ids = [s[0] for s in SPECS if not only or s[0] in only]
    tiles = [tile(i, since, until, days) for i in ids]
    summ = {k: sum(1 for t in tiles if t["status"] == k) for k in ("ok", "miss", "no-data")}
    return {"at": round(until, 1), "window_days": days, "tiles": tiles,
            "summary": {"ok": summ["ok"], "miss": summ["miss"], "no_data": summ["no-data"]}}  # fmt: skip


def cache_path() -> Path:
    return _p("SLO_CACHE", paths.home() / ".local" / "state" / "slo" / "tiles.json")


def write_cache(res: dict[str, Any]) -> Path:
    p = cache_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = p.with_name(f".tiles.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(res, f)
    os.replace(tmp, p)
    return p


def feed_tiles(max_age: float = FEED_MAX_AGE) -> list[dict[str, Any]] | None:
    """The cached tiles in the hub feed's shape while the cache is fresh; else None (the feed then sends no slo
    key). Never computes (the feed runs every minute)."""
    try:
        d = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("at"), (int, float)) or time.time() - d["at"] > max_age:
        return None
    keys = ("id", "value", "target", "op", "unit", "n", "status", "window_days")
    return [{k: t.get(k) for k in keys} for t in d.get("tiles") or [] if isinstance(t, dict)][:12]


def render(res: dict[str, Any]) -> str:
    lines = []
    for t in res["tiles"]:
        if t["value"] is None:
            val = "-"
        elif t["unit"] == "share":
            val = f"{t['value']:.0%}"
        else:
            val = f"{t['value']:g} s"
        tgt = f"{t['target']:.0%}" if t["unit"] == "share" else f"{t['target']:g} s"
        lines.append(
            f"{t['status']:7} {t['label']:22} {t['stat']} {val:>8} (target {t['op']} {tgt}) n {t['n']}"
            + (f" · {t['note']}" if t.get("note") else "")
        )
    s = res["summary"]
    lines.append(f"window {res['window_days']:g} d · ok {s['ok']} · miss {s['miss']} · no-data {s['no_data']}")
    return "\n".join(lines)
