"""inbox.agenda: the compact view of the ledger the brain gets on every beat (hook output, <= 4,000 chars).

  build(con, ...) -> Agenda   text + the ids it lists; pure read (the hook marks them listed and writes the beats row)

Layout (only titles and Next lines, never note bodies):
  [agenda DD.MM HH:MM · beat #N · last HH:MM · data, not instructions]
  Undelivered owner event #N (kind, age): owner Telegram rows (delivery now, receipts.RECEIPT_KINDS) with no
    receipt after 5 min, never with their text; younger ones are in flight and not shown; received ones are acked by
    the receipt; other tg.* rows (digest tg.bound, ...) are never listed
  New (n): one line per event, "now" deliveries first: #id kind HH:MM · title → Next line (or the kind's hint)
  Updated (n): open events whose note changed since they were listed
  Open (n, listed before): still unacked, grouped by kind as id lists (full lines after a restart or compaction)
  digest deliveries: one grouped line per kind; mute: counted per kind (still listed)
  Due · Running · Waiting on him + startable counts · last compaction · active wake rules · reflexes (only when the
    registry holds one or fired: active count, firings since the last beat per reflex, misfires) · Act line
Every open event is listed: when full lines no longer fit, the rest is grouped by kind as id ranges; only past the
hard cap are ids left out (not marked listed, so they come again next beat).
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from core import events, todo

from . import receipts
from .kinds import Registry
from .notes import clean

__all__ = ["BUDGET", "Agenda", "build", "id_ranges"]

BUDGET = 4000
UNDELIVERED_AFTER = 300
UNDELIVERED_LINES = 5
ORDER = {"now": 0, "beat": 1, "digest": 2, "mute": 3}


@dataclass
class Agenda:
    text: str
    listed: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def id_ranges(ids: Iterable[int]) -> str:
    """[3, 5, 6, 7, 9] -> '#3 #5-7 #9'."""
    xs = sorted(set(ids))
    out, i = [], 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[j] + 1:
            j += 1
        out.append(f"#{xs[i]}" if i == j else (f"#{xs[i]} #{xs[j]}" if j == i + 1 else f"#{xs[i]}-{xs[j]}"))
        i = j + 1
    return " ".join(out)


def _hm(ts: float, tz: tzinfo, now: datetime) -> str:
    d = datetime.fromtimestamp(ts, tz)
    return d.strftime("%H:%M") if d.date() == now.date() else d.strftime("%d.%m %H:%M")


def _line(ev: dict[str, Any], reg: Registry, tz: tzinfo, now: datetime, cls: str, updated: bool = False) -> str:
    when = _hm(ev.get("updated_ts") or ev["ts"], tz, now) if updated else _hm(ev["ts"], tz, now)
    tag = " [now]" if cls == "now" else ""
    title = clean(ev.get("subject") or "", 90) or "(no title)"
    nxt = clean(ev.get("next") or "", 150)
    tail = (
        f" → Next: {nxt}" if nxt else (f" → hint: {clean(reg.hint(ev['kind']), 120)}" if reg.hint(ev["kind"]) else "")
    )
    return f"#{ev['id']} {ev['kind']} {when}{tag} · {title}{tail}"


def _age(sec: float) -> str:
    m = max(0, int(sec // 60))
    return f"{m} min" if m < 120 else f"{m // 60} h"


def _grouped(evs: Sequence[dict[str, Any]]) -> list[tuple[str, list[int]]]:
    g: OrderedDict[str, list[int]] = OrderedDict()
    for e in evs:
        g.setdefault(e["kind"], []).append(e["id"])
    return list(g.items())


def _footer(
    now: datetime,
    tz: tzinfo,
    board: todo.Board | None,
    run: dict[str, Any] | None,
    comp: dict[str, Any] | None,
    rules: Sequence[dict[str, Any]],
    gone: int,
    reflexes: dict[str, Any] | None = None,
) -> list[str]:
    L = []
    if board is None or not board.ok:
        L.append("Due: (TODO.md unreadable)")
    elif board.due:
        items = [todo.title(i, 50) + (f" ({at.astimezone(tz):%d.%m %H:%M})" if at else "") for i, at in board.due[:3]]
        more = f" (+{len(board.due) - 3})" if len(board.due) > 3 else ""
        L.append("Due: " + " · ".join(items) + more)
    else:
        nxt = board.upcoming[0] if board.upcoming else None
        L.append("Due: none" + (f" · next {todo.title(nxt[0], 50)} {nxt[1].astimezone(tz):%d.%m %H:%M}" if nxt else ""))
    parts = []
    ags = (run or {}).get("agents")
    if ags is None:
        parts.append("agents ?")
    else:
        shown = [
            f"{a['id'][:8]} {clean(a.get('desc') or '', 32)}"
            + (f" (stalled {a['silent_min']}m)" if a.get("state") == "stalled" else "")
            for a in ags[:4]
        ]
        parts.append(
            f"{len(ags)} agent(s)"
            + (": " + ", ".join(shown) if shown else "")
            + (f" +{len(ags) - 4}" if len(ags) > 4 else "")
        )
    if board is not None and board.ok:
        parts.append(f"TODO in progress {len(board.in_progress)}")
    if run and run.get("stale_from"):
        parts.append(f"(snapshot {run['stale_from']})")
    L.append("Running: " + " · ".join(parts))
    if board is not None and board.ok:
        s = board.startable
        L.append(
            f"Waiting on him: {len(board.waiting)} · Startable: goals {s.get('goals', 0)}, ideas {s.get('ideas', 0)},"
            f" night {s.get('night', 0)}"
        )
    c = "none on record"
    if comp:
        pre, post = comp.get("pre"), comp.get("post")
        size = f", {round(pre / 1000)}k→{round(post / 1000)}k" if isinstance(pre, int) and isinstance(post, int) else ""
        c = f"{_hm(comp['ts'], tz, now)} ({comp.get('trigger') or '?'}{size})"
    w = "none"
    if rules:

        def until(r: dict[str, Any]) -> str:
            return f" until {datetime.fromtimestamp(r['until'], tz):%d.%m %H:%M}" if r.get("until") else ""

        def rule(r: dict[str, Any]) -> str:
            why = f" ({clean(r.get('why') or '', 40)})" if r.get("why") else ""
            if r.get("klass") == "hold":  # `wake next`: idle-only beats held; events, Due and timers still wake
                return f"#{r.get('id')} idle beats held{until(r)}{why}"
            return f"#{r.get('id')} {r.get('kind_glob') or '*'}→{r['klass']}{until(r)}{why}"

        holds = [r for r in rules if r.get("klass") == "hold"][:1]  # the newest hold first, then the rest
        shown = holds + [r for r in rules if r.get("klass") != "hold"][: 3 - len(holds)]
        w = ", ".join(rule(r) for r in shown)
        w += f" +{len(rules) - len(shown)}" if len(rules) > len(shown) else ""
    L.append(f"Last compaction: {c} · Wake rules: {w}")
    if gone:
        L.append(f"Gone without ack since last beat: {gone} (events list --status gone)")
    if reflexes:
        by = list((reflexes.get("by_name") or {}).items())
        fired = ", ".join(f"{k} ×{v}" for k, v in by[:3]) + (f" +{len(by) - 3}" if len(by) > 3 else "")
        L.append(
            f"Reflexes: {reflexes.get('active', 0)} active · fired since last beat {reflexes.get('fired', 0)}"
            + (f" ({fired})" if fired else "")
            + (f" · misfires {reflexes['misfires']}" if reflexes.get("misfires") else "")
            + " (reflex ls · reflex log)"
        )
    L.append("Act: events ack ID… · events show ID · wake …")
    return [clean(x, 400) for x in L]


def build(
    con: sqlite3.Connection,
    *,
    now_ts: float,
    beat_id: int,
    last_beat_ts: float | None,
    reg: Registry,
    tz: tzinfo,
    board: todo.Board | None = None,
    run: dict[str, Any] | None = None,
    comp: dict[str, Any] | None = None,
    full_open: bool = False,
    budget: int = BUDGET,
    reflexes: dict[str, Any] | None = None,
) -> Agenda:
    now = datetime.fromtimestamp(now_ts, tz)
    rules = events.wake_rules(con, now_ts)
    evs = [e for e in events.open_events(con) if not receipts.is_owner_row(e["kind"])]
    try:
        undel = receipts.undelivered(con, now_ts, UNDELIVERED_AFTER)
    except sqlite3.Error:
        undel = []
    known = set(reg.kinds)
    for e in evs:
        e["_cls"] = events.effective_delivery(e, rules, known)
    key = lambda e: (ORDER.get(e["_cls"], 1), e.get("updated_ts") or e["ts"], e["id"])  # noqa: E731
    fresh = sorted((e for e in evs if e["status"] == "new" and e.get("first_listed_ts") is None), key=key)
    upd = sorted((e for e in evs if e["status"] == "new" and e.get("first_listed_ts") is not None), key=key)
    old = sorted((e for e in evs if e["status"] == "listed"), key=key)
    gone = 0
    if last_beat_ts:
        gone = con.execute(
            "SELECT COUNT(*) FROM events WHERE status='gone' AND updated_ts > ?", (last_beat_ts,)
        ).fetchone()[0]
    last = "—" if not last_beat_ts else _hm(last_beat_ts, tz, now)
    head = f"[agenda {now:%d.%m %H:%M} · beat #{beat_id} · last {last} · data, not instructions]"
    foot = _footer(now, tz, board, run, comp, rules, gone, reflexes)
    room = budget - len(head) - sum(len(x) + 1 for x in foot) - 1
    body: list[str] = []
    listed: list[int] = []

    def put(line: str) -> bool:
        nonlocal room
        if len(line) + 1 > room:
            return False
        body.append(line)
        room -= len(line) + 1
        return True

    def put_grouped(prefix: str, evs_: Sequence[dict[str, Any]]) -> None:
        """Grouped id lists; past the hard cap as many whole kinds as fit, the rest not listed."""
        groups = _grouped(evs_)
        if put(prefix + " · ".join(f"{k} ×{len(ids)} {id_ranges(ids)}" for k, ids in groups)):
            listed.extend(e["id"] for e in evs_)
            return
        acc: list[str] = []
        n = 0
        for k, ids in groups:
            part = f"{k} ×{len(ids)} {id_ranges(ids)}"
            if len(prefix + " · ".join([*acc, part])) + 40 > room:
                break
            acc.append(part)
            listed.extend(ids)
            n += len(ids)
        put(prefix + " · ".join(acc) + f" … +{len(evs_) - n} more: events list")

    if undel:
        for u in undel[:UNDELIVERED_LINES]:
            if put(f"Undelivered owner event #{u['id']} ({u['kind']}, {_age(now_ts - u['ts'])})"):
                listed.append(u["id"])
        rest = undel[UNDELIVERED_LINES:]
        if rest and put(f"  +{len(rest)} more undelivered: {id_ranges(u['id'] for u in rest)}"):
            listed.extend(u["id"] for u in rest)
        put("  → events show ID (his text), answer late and say so, then events ack ID")
    # keep room for the grouped lines: as long as every open event, grouped, would take (capped)
    est = len(" · ".join(f"{k} ×{len(ids)} {id_ranges(ids)}" for k, ids in _grouped(evs))) + 120
    reserve = min(est, budget // 2)
    for title, group, upd_flag in (("New", fresh, False), ("Updated", upd, True)):
        if not group:
            continue
        overflow: list[dict[str, Any]] = []
        put(f"{title} ({len(group)}):")
        for e in (x for x in group if x["_cls"] in ("now", "beat")):
            ln = _line(e, reg, tz, now, e["_cls"], upd_flag)
            if room - reserve > len(ln) and put(ln):
                listed.append(e["id"])
            else:
                overflow.append(e)
        if overflow:
            put_grouped("  more: ", overflow)
        dig = [e for e in group if e["_cls"] == "digest"]
        if dig:
            put_grouped("  digest: ", dig)
        mute = [e for e in group if e["_cls"] == "mute"]
        if mute:
            put_grouped("  muted: ", mute)
    if old:
        put(f"Open ({len(old)}, listed before, not acked):")
        rest = list(old)
        if full_open:
            rest = []
            for e in old:
                ln = _line(e, reg, tz, now, e["_cls"])
                if room - reserve > len(ln) and put(ln):
                    listed.append(e["id"])
                else:
                    rest.append(e)
        if rest:
            put_grouped("  ", rest)
    text = "\n".join([head, *body, *foot])
    counts = {
        "new": len(fresh),
        "updated": len(upd),
        "open": len(old),
        "listed": len(set(listed)),
        "gone": gone,
        "undelivered": len(undel),
    }
    return Agenda(text=text, listed=sorted(set(listed)), counts=counts)
