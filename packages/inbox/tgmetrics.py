"""inbox.tgmetrics: the Telegram metrics T1–T8 (`events metrics --tg`) from the events store and a few logs. Read-only,
except prune_text (the 30-day text rule) which `--record` runs.

  compute(con, since, until, *, now, logs) -> dict    T1..T8, each {"ok": bool|None, ...}; None = no data yet
  prune_text(con, now, days=30) -> int                 null his text in owner Telegram rows older than `days`
  render(res, tz) -> str

T1  tap -> ack: tg.button payload.ack_ms, p95 < 1 s and p99 < 2 s
T2  dropped owner inbound: delivery-now RECEIPT_KINDS rows with no receipt (channel, transcript or binding) within
    10 min, plus tg.bound rows whose outcome is not ok; and the update_id gap per bot vs the adapter's meta
    dropped:* counters (all-time counters: a gap they cannot explain is reported as unexplained)
T3  row -> receipt p50 <= 3 s, p95 <= 60 s (deliveries); plus the 0.1.22 baseline (receipts-baseline.jsonl lag)
T4  outbox (mode live): enqueue -> sent p95 <= 1.5 s; failed <= 0.5 % and each one an outbox.failed event;
    duplicates = two sent rows with the same bot, chat, op and body within 10 s (want 0)
T5  brain restarts for Telegram reasons (restart-wake.log reasons) and deploys (lands) in the window
T6  bound taps (tg.bound): count, ok count, result p95 from payload.ms (brain turns: from traces, later)
T7  tg-adapter ensure starts per day (ensure.log; want <= 2); getUpdates gaps need an adapter history (n/a)
T8  approval violations in approvals.db: used after expiry, used without approval; n/a while it is missing
Hub mode: compute() runs the same queries on an in-memory mirror of the hub's transport rows in the window (the
hub's "window" read: transport events, their deliveries, the live outbox rows, the adapter's dropped:* meta), T8 asks
the hub's approvals audit while approvals live there, and prune_text() asks the hub to null the same columns.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, tzinfo
from typing import Any

from . import receipts
from .metrics import pct

__all__ = ["KEEP_KEYS", "compute", "prune_text", "render"]

DROP_AFTER = 600
DUP_WINDOW = 10
TG_RESTART = re.compile(r"telegram|tg-adapter|tg-bridge|channel|poller|inbound", re.I)
KEEP_KEYS = frozenset(
    {
        "update_id",
        "bot",
        "message_id",
        "chat_id",
        "event_type",
        "ack_ms",
        "ack_ok",
        "ack_error",
        "mode",
        "via",
        "handled",
        "redelivered",
        "ok",
        "outcome",
        "ms",
        "event_id",
        "attachment_kind",
        "duration",
    }
)


def _pl(raw: Any) -> dict[str, Any]:
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _t1(con: sqlite3.Connection, since: float, until: float) -> dict[str, Any]:
    ms = [
        _num(_pl(r[0]).get("ack_ms"))
        for r in con.execute("SELECT payload FROM events WHERE kind='tg.button' AND ts>=? AND ts<?", (since, until))
    ]
    xs = [x for x in ms if x is not None]
    p95, p99 = pct(xs, 0.95), pct(xs, 0.99)
    ok = None if not xs else bool(p95 is not None and p95 < 1000 and p99 is not None and p99 < 2000)
    return {"ok": ok, "n": len(xs), "p95_ms": _r(p95, 0), "p99_ms": _r(p99, 0)}


def _rows(con: sqlite3.Connection, since: float, until: float) -> list[dict[str, Any]]:
    q = ",".join("?" * len(receipts.RECEIPT_KINDS))
    if receipts.has_deliveries(con):
        sql = (
            f"SELECT e.id, e.kind, e.ts, d.receipt_ts, d.how FROM events e LEFT JOIN deliveries d ON d.event_id=e.id"
            f" WHERE e.kind IN ({q}) AND e.delivery='now' AND e.ts>=? AND e.ts<?"
        )
    else:
        sql = (
            f"SELECT id, kind, ts, NULL AS receipt_ts, NULL AS how FROM events WHERE kind IN ({q}) AND delivery='now'"
            f" AND ts>=? AND ts<?"
        )
    return [dict(r) for r in con.execute(sql, (*receipts.RECEIPT_KINDS, since, until))]


def _t2(con: sqlite3.Connection, rows: list[dict[str, Any]], since: float, until: float, now: float) -> dict[str, Any]:
    mature = [r for r in rows if r["ts"] <= min(until, now) - DROP_AFTER]
    dropped = [r["id"] for r in mature if r["receipt_ts"] is None or r["receipt_ts"] - r["ts"] > DROP_AFTER]
    bound_bad = []
    for r in con.execute("SELECT id, payload FROM events WHERE kind='tg.bound' AND ts>=? AND ts<?", (since, until)):
        p = _pl(r[1])
        if p.get("ok") is False or (p.get("outcome") not in (None, "ok")):
            bound_bad.append(r[0])
    per_bot: dict[str, set[int]] = defaultdict(set)
    for r in con.execute(
        "SELECT payload FROM events WHERE source='tg-adapter' AND ts>=? AND ts<?", (since, until)
    ).fetchall():
        p = _pl(r[0])
        uid = p.get("update_id")
        if isinstance(uid, int):
            per_bot[str(p.get("bot") or "?")].add(uid)
    missing = sum((max(u) - min(u) + 1) - len(u) for u in per_bot.values() if u)
    counters = 0
    for r in con.execute("SELECT v FROM meta WHERE k LIKE 'dropped:%'"):
        try:
            counters += int(json.loads(r[0]))
        except (TypeError, ValueError):
            pass
    unexplained = max(0, missing - counters)
    ok = None if not mature and not bound_bad and not per_bot else (not dropped and not bound_bad and not unexplained)
    return {
        "ok": ok,
        "checked": len(mature),
        "dropped": len(dropped),
        "dropped_ids": dropped[:20],
        "bound_not_ok": len(bound_bad),
        "update_gap": missing,
        "dropped_counters": counters,
        "gap_unexplained": unexplained,
    }


def _baseline(path: str, since: float, until: float) -> dict[str, Any]:
    lags = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                h, lag = _num(d.get("hook_ts")), _num(d.get("lag_s"))
                if h is not None and lag is not None and since <= h < until:
                    lags.append(lag)
    except OSError:
        pass
    return {"n": len(lags), "p50_s": _r(pct(lags, 0.5), 1), "p95_s": _r(pct(lags, 0.95), 1)}


def _t3(rows: list[dict[str, Any]], baseline_path: str, since: float, until: float) -> dict[str, Any]:
    lags = [r["receipt_ts"] - r["ts"] for r in rows if r["receipt_ts"] is not None]
    p50, p95 = pct(lags, 0.5), pct(lags, 0.95)
    hows: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["receipt_ts"] is not None:
            hows[r["how"] or "?"] += 1
    ok = None if not lags else bool(p50 is not None and p50 <= 3 and p95 is not None and p95 <= 60)
    return {
        "ok": ok,
        "n": len(lags),
        "p50_s": _r(p50, 2),
        "p95_s": _r(p95, 2),
        "how": dict(hows),
        "baseline_0122": _baseline(baseline_path, since, until),
    }


def _t4(con: sqlite3.Connection, since: float, until: float) -> dict[str, Any]:
    rows = con.execute(
        "SELECT id, ts, bot, chat, op, body, status, sent_ts FROM outbox WHERE mode='live' AND ts>=? AND ts<?",
        (since, until),
    ).fetchall()
    sent = [r for r in rows if r["status"] == "sent" and r["sent_ts"] is not None]
    failed = [r for r in rows if r["status"] == "failed"]
    p95 = pct([r["sent_ts"] - r["ts"] for r in sent], 0.95)
    share = len(failed) / (len(sent) + len(failed)) if (sent or failed) else None
    listed = con.execute(
        "SELECT COUNT(*) FROM events WHERE kind='outbox.failed' AND ts>=? AND ts<?", (since, until)
    ).fetchone()[0]
    groups: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    for r in sent:
        groups[(r["bot"], r["chat"], r["op"], r["body"])].append(r["sent_ts"])
    dups = 0
    for ts in groups.values():
        ts.sort()
        dups += sum(1 for a, b in zip(ts, ts[1:], strict=False) if b - a <= DUP_WINDOW)
    ok = (
        None
        if not rows
        else bool((p95 is None or p95 <= 1.5) and (share or 0) <= 0.005 and dups == 0 and listed >= len(failed))
    )
    return {
        "ok": ok,
        "n": len(rows),
        "sent": len(sent),
        "p95_s": _r(p95, 2),
        "failed": len(failed),
        "failed_share": _r(share, 4),
        "outbox_failed_events": listed,
        "duplicates": dups,
    }


def _t5(restart_log: str, deploys_log: str, since: float, until: float, tz: tzinfo) -> dict[str, Any]:
    total = tg = 0
    try:
        with open(restart_log, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    t = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()
                except ValueError:
                    continue
                if since <= t < until and "after restart" in line:
                    total += 1
                    tg += bool(TG_RESTART.search(line[19:].split("after restart", 1)[-1]))
    except OSError:
        pass
    lands = 0
    try:
        with open(deploys_log, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("op") == "land" and d.get("ok") and since <= (_num(d.get("ts")) or 0) < until:
                    lands += 1
    except OSError:
        pass
    return {"ok": tg == 0, "restarts": total, "telegram_restarts": tg, "lands": lands}


def _t6(con: sqlite3.Connection, since: float, until: float) -> dict[str, Any]:
    ms, ok_n, n = [], 0, 0
    for r in con.execute("SELECT payload FROM events WHERE kind='tg.bound' AND ts>=? AND ts<?", (since, until)):
        p = _pl(r[0])
        n += 1
        ok_n += p.get("ok") is True or p.get("outcome") == "ok"
        x = _num(p.get("ms"))
        if x is not None:
            ms.append(x)
    return {"ok": None, "n": n, "ok_n": ok_n, "result_p95_ms": _r(pct(ms, 0.95), 0), "brain_turns": None}


def _t7(ensure_log: str, since: float, until: float, tz: tzinfo) -> dict[str, Any]:
    per_day: dict[str, int] = defaultdict(int)
    try:
        with open(ensure_log, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split(None, 2)
                if len(parts) < 2 or parts[1] != "start":
                    continue
                t = _num(_float(parts[0]))
                if t is not None and since <= t < until:
                    per_day[datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d")] += 1
    except OSError:
        pass
    mx = max(per_day.values()) if per_day else 0
    return {"ok": None if not per_day else mx <= 2, "starts": sum(per_day.values()), "max_per_day": mx, "gaps": None}


def _float(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


# = approvals.AUDIT_SQL (packages/approvals; a test keeps the two equal): used without his yes, used or decided at or
# after the expiry.
T8_SQL = (
    "SELECT COUNT(*) FROM approvals WHERE (used_ts IS NOT NULL AND (used_ts >= expires OR status != 'approved'"
    " OR decided_ts IS NULL)) OR (decided_ts IS NOT NULL AND decided_ts >= expires)"
)


def _t8(path: str) -> dict[str, Any]:
    if not path or not os.path.exists(path):
        return {"ok": None, "violations": None, "why": "approvals.db missing (P7)"}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            n = c.execute(T8_SQL).fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error as e:
        return {"ok": None, "violations": None, "why": f"{type(e).__name__}: {e}"[:120]}
    return {"ok": n == 0, "violations": n}


def _hub_tg() -> bool:
    from core import hub

    return hub.tg_hub()


def hub_mirror(since: float, until: float) -> sqlite3.Connection:
    """An in-memory store with the hub's transport rows of [since, until): the queries above run on it as they
    run on events.db. Raises core.hub.HubError when the hub does not answer."""
    from core import hub

    def q(name: str, **p: Any) -> list[dict[str, Any]]:
        return hub.tg_q(name, since=since, until=until, timeout=30.0, **p)

    ev: dict[int, dict[str, Any]] = {}

    def put(i: Any, **cols: Any) -> None:  # a row the queries above see in the window (ts: its own when known)
        base = {"id": int(i), "ts": since, "kind": "", "source": "", "payload": "{}", "delivery": "", "status": "new"}
        ev.setdefault(int(i), base).update(cols)

    dels = []
    for r in q("channel_rows", kinds=list(receipts.RECEIPT_KINDS)):
        put(r["id"], kind=r["kind"], ts=float(r["ts"]), delivery="now")
        if r.get("receipt_ts") is not None or r.get("how"):
            dels.append((int(r["id"]), r.get("receipt_ts"), r.get("how")))
    for kind in ("tg.button", "tg.bound"):
        for r in q("kind_payloads", kind=kind):
            put(r["id"], kind=kind, payload=r.get("payload") or "{}")
    for r in q("source_payloads", source="tg-adapter"):
        if int(r["id"]) in ev and ev[int(r["id"])]["payload"] != "{}":
            ev[int(r["id"])]["source"] = "tg-adapter"  # its payload is in already (one row, one payload)
        else:
            put(r["id"], source="tg-adapter", payload=r.get("payload") or "{}")
    failed = q("kind_count", kind="outbox.failed")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    for st in hub._memory_ddl():
        con.execute(st)
    for r in ev.values():
        con.execute(
            "INSERT INTO events(id, ts, source, kind, payload, delivery, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["ts"], r["source"], r["kind"], r["payload"], r["delivery"], r["status"]),
        )
    for n in range(int((failed[0] if failed else {}).get("n") or 0)):  # counted only: stand-ins in the window
        con.execute("INSERT INTO events(id, ts, source, kind) VALUES (?, ?, 'hub', 'outbox.failed')", (-1 - n, since))
    con.executemany("INSERT OR REPLACE INTO deliveries(event_id, receipt_ts, how) VALUES (?, ?, ?)", dels)
    for r in q("outbox_live"):
        con.execute(
            "INSERT INTO outbox(id, ts, bot, chat, op, body, status, sent_ts, mode)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'live')",
            (r["id"], r["ts"], r["bot"], r["chat"], r["op"], r["body"], r["status"], r.get("sent_ts")),
        )
    for k, v in hub.tg_meta("dropped:").items():
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, v))
    return con


def _t8_hub() -> dict[str, Any]:
    from core import hub

    try:
        a = hub.call("GET", "/approvals/audit", timeout=5.0)
    except hub.HubError as e:
        return {"ok": None, "violations": None, "why": f"hub: {e}"[:120]}
    return {"ok": a.get("violations") == 0, "violations": a.get("violations")}


def compute(
    con: sqlite3.Connection, since: float, until: float, *, now: float, logs: dict[str, str], tz: tzinfo
) -> dict[str, Any]:
    """T1..T8 from con (events.db), or from the hub's rows in hub mode (con is then not read)."""
    if _hub_tg():
        from core import hub

        mirror = hub_mirror(since, until)
        try:
            res = _compute(mirror, since, until, now=now, logs=logs, tz=tz)
        finally:
            mirror.close()
        if hub.backend("approvals") == "hub":
            res["T8"] = _t8_hub()
        res["store"] = "hub"
        return res
    return _compute(con, since, until, now=now, logs=logs, tz=tz)


def _compute(
    con: sqlite3.Connection, since: float, until: float, *, now: float, logs: dict[str, str], tz: tzinfo
) -> dict[str, Any]:
    rows = _rows(con, since, until)
    return {
        "since": since,
        "until": until,
        "T1": _t1(con, since, until),
        "T2": _t2(con, rows, since, until, now),
        "T3": _t3(rows, logs.get("baseline", ""), since, until),
        "T4": _t4(con, since, until),
        "T5": _t5(logs.get("restart", ""), logs.get("deploys", ""), since, until, tz),
        "T6": _t6(con, since, until),
        "T7": _t7(logs.get("ensure", ""), since, until, tz),
        "T8": _t8(logs.get("approvals", "")),
    }


def prune_text(con: sqlite3.Connection, now: float, days: float = 30) -> int:
    """His text out of owner Telegram rows older than `days`: subject and next emptied, payload reduced to ids,
    counters and flags (KEEP_KEYS) plus `_nulled`. Idempotent; one transaction."""
    from core import db

    cut = now - days * 86400
    if _hub_tg():  # the rows live on the hub, which nulls the same columns and keeps the same keys
        from core import hub

        n = 0
        for _ in range(50):  # the hub nulls up to 2000 rows a call; `more` = call again
            body = {"before_ts": cut, "keep_keys": sorted(KEEP_KEYS), "now": round(now), "rid": hub.new_rid()}
            got = hub.call("POST", "/tg/prune-text", body, timeout=30.0)
            n += int(got.get("n") or 0)
            if not got.get("more"):
                break
        return n
    n = 0
    with db.tx(con):
        rows = con.execute(
            "SELECT id, payload FROM events WHERE ts < ? AND (source IN ('tg-adapter', 'tg-bridge') OR kind LIKE 'tg.%'"
            " OR kind IN ('bridge.handoff', 'approval.decided')) AND payload NOT LIKE '%\"_nulled\"%'",
            (cut,),
        ).fetchall()
        for r in rows:
            p = _pl(r["payload"])
            keep = {k: v for k, v in p.items() if k in KEEP_KEYS}
            keep["_nulled"] = round(now)
            con.execute(
                "UPDATE events SET subject='', next='', payload=? WHERE id=?",
                (json.dumps(keep, ensure_ascii=False, separators=(",", ":")), r["id"]),
            )
            n += 1
    return n


def _ok(x: Any) -> str:
    return "–" if x is None else ("ok" if x else "MISS")


def _v(x: Any, unit: str = "") -> str:
    return "–" if x is None else f"{x:g}{unit}" if isinstance(x, float) else f"{x}{unit}"


def render(res: dict[str, Any], tz: tzinfo) -> str:
    def hm(t: float) -> str:
        return datetime.fromtimestamp(t, tz).strftime("%d.%m %H:%M")

    t1, t2, t3, t4, t5, t6, t7, t8 = (res[f"T{i}"] for i in range(1, 9))
    b = t3["baseline_0122"]
    L = [
        f"Telegram metrics {hm(res['since'])} → {hm(res['until'])} (– = no data yet)",
        f"T1 tap→ack      {_ok(t1['ok']):4} p95 {_v(t1['p95_ms'], ' ms')} · p99 {_v(t1['p99_ms'], ' ms')}"
        f" (n {t1['n']}; want < 1 s / < 2 s)",
        f"T2 dropped      {_ok(t2['ok']):4} {t2['dropped']} of {t2['checked']} owner rows without a receipt in 10 min"
        f" · bound not ok {t2['bound_not_ok']} · update gap {t2['update_gap']} (dropped counters"
        f" {t2['dropped_counters']}, unexplained {t2['gap_unexplained']})"
        + (f" · ids {' '.join(f'#{i}' for i in t2['dropped_ids'])}" if t2["dropped_ids"] else ""),
        f"T3 row→receipt  {_ok(t3['ok']):4} p50 {_v(t3['p50_s'], ' s')} · p95 {_v(t3['p95_s'], ' s')} (n {t3['n']}"
        + (f"; {', '.join(f'{k} {v}' for k, v in sorted(t3['how'].items()))}" if t3["how"] else "")
        + f"; want ≤ 3 s / ≤ 60 s) · 0.1.22 baseline p50 {_v(b['p50_s'], ' s')} · p95 {_v(b['p95_s'], ' s')}"
        f" (n {b['n']})",
        f"T4 outbox       {_ok(t4['ok']):4} sent p95 {_v(t4['p95_s'], ' s')} · failed {t4['failed']}"
        f" ({_v(t4['failed_share'])}; listed {t4['outbox_failed_events']}) · duplicates {t4['duplicates']}"
        f" (n {t4['n']})",
        f"T5 restarts     {_ok(t5['ok']):4} Telegram {t5['telegram_restarts']} of {t5['restarts']}"
        f" · lands {t5['lands']}",
        f"T6 bound taps   {_ok(t6['ok']):4} {t6['ok_n']} ok of {t6['n']} · result p95 {_v(t6['result_p95_ms'], ' ms')}"
        " · brain turns –",
        f"T7 adapter      {_ok(t7['ok']):4} ensure starts {t7['starts']} (max {t7['max_per_day']}/day; want ≤ 2)"
        " · poll gaps –",
        f"T8 approvals    {_ok(t8['ok']):4} violations {_v(t8['violations'])}"
        + (f" ({t8['why']})" if t8.get("why") else ""),
    ]
    if "pruned" in res:
        L.append(f"text nulled in {res['pruned']} row(s) older than 30 d")
    return "\n".join(L)
