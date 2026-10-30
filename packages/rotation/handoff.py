"""rotation.handoff: the fresh session's hand-off from durable state, and the promise ledger that proves nothing was
lost. Read-only except where the caller writes the results.

  promises(ctx)          [{key, kind, at, title}]: TODO Due + upcoming items, reminders (and `wake at` timers) due
                         within 48 h, the session-only check-ins (CronCreate) the previous session still had
  text(ctx, rid, ps)     the hand-off note body (titles included: it is the brain's own inbox note)
  snapshot(ps)           the ledger rows kept for `rotate verify` (key, kind, at; no titles)
  verify(snap, rot)      each promise after the rotation: durable | passed | recreated | changed | lost
A promise is kept when it is still in its durable store (TODO.md, remind), its time has passed, or (session-only
check-ins) the new session scheduled the same cron again after reading the hand-off. Seams: ROTATE_REMIND,
ROTATE_BOARD_LINT, ROTATE_AGENT_WATCH, ROTATE_EVENTS.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

from core import paths, todo
from rotation import checks, session

HORIZON_S = 48 * 3600
PROMPT_CUT = 240


def _key(kind: str, ident: str) -> str:
    return f"{kind}:{hashlib.sha256(ident.encode()).hexdigest()[:10]}" if kind == "todo" else f"{kind}:{ident}"


def _hm(ts: float | None, tz: _dt.tzinfo) -> str:
    if not ts:
        return "now"
    return _dt.datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M")


def _remind_items() -> list[dict[str, Any]] | None:
    rc, out = checks._run(checks._cmd("ROTATE_REMIND", ["remind", "list", "--json"]), 20)
    if rc != 0:
        return None
    try:
        d = json.loads(out)
    except ValueError:
        return None
    items = d.get("items") if isinstance(d, dict) else d
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else None


def _remind_at(item: dict[str, Any], tz: _dt.tzinfo) -> float | None:
    at = item.get("at")
    if not isinstance(at, str):
        return None
    try:
        return _dt.datetime.strptime(at[:16], "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()
    except ValueError:
        return None


def todo_promises(now: float) -> list[dict[str, Any]]:
    tz = todo.owner_tz()
    b = todo.read(now=_dt.datetime.fromtimestamp(now, tz), tz=tz)
    out = []
    for item, at in list(b.due) + list(b.upcoming):
        t = todo.title(item, 80)
        out.append(
            {
                "key": _key("todo", t),
                "kind": "todo",
                "at": at.timestamp() if at else None,
                "title": t,
                "where": "Due" if (item, at) in b.due else "upcoming",
            }
        )
    return out


def promises(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    now = ctx["now"]
    tz = todo.owner_tz()
    ps = todo_promises(now)
    items = _remind_items()
    if items is not None:
        for i in items:
            at = _remind_at(i, tz)
            if at is None or at < now - 300 or at > now + HORIZON_S:
                continue
            tag = str(i.get("tag") or "")
            who = "his" if i.get("owner") else ("wake" if tag == "wake" else "mine")
            ps.append(
                {
                    "key": _key("remind", str(i.get("id"))),
                    "kind": "remind",
                    "at": at,
                    "who": who,
                    "title": str(i.get("text") or "")[:80],
                }
            )
    else:
        ps.append({"key": "remind:unreadable", "kind": "error", "at": None, "title": "remind list failed"})
    for c in ctx["crons"]:
        ps.append(
            {
                "key": _key("cron", c["id"]),
                "kind": "cron",
                "at": c.get("next"),
                "cron": c["cron"],
                "recurring": c["recurring"],
                "title": c["prompt"][:PROMPT_CUT],
            }
        )
    return sorted(ps, key=lambda p: (p["at"] is None, p["at"] or 0))


def _board_lines() -> list[str]:
    rc, out = checks._run(checks._cmd("ROTATE_BOARD_LINT", ["board-lint", "handoff"]), 15)
    if rc != 0 or not out.strip():
        return ["(board-lint handoff unavailable: read TODO.md)"]
    lines = out.strip().splitlines()
    return lines[1:] if lines and lines[0].startswith("[handoff]") else lines


def _counts() -> list[str]:
    out = []
    rc, txt = checks._run(checks._cmd("ROTATE_EVENTS", ["events", "agenda", "--json"]), 20)
    if rc == 0:
        try:
            d = json.loads(txt)
            n = {k: len(v) for k, v in d.items() if isinstance(v, list)} if isinstance(d, dict) else {}
            if n:
                out.append("Agenda now: " + ", ".join(f"{k} {v}" for k, v in sorted(n.items()) if v) + ".")
        except ValueError:
            pass
    rc, txt = checks._run(checks._cmd("ROTATE_AGENT_WATCH", ["agent-watch", "status", "--hours", "6"]), 15)
    if rc == 0:
        done = sum(1 for ln in txt.splitlines() if len(ln.split()) > 1 and ln.split()[1] in ("done", "failed"))
        out.append(f"Subagents in the last 6 h: {done} finished, none running (a rotation needs that).")
    return out


def text(ctx: dict[str, Any], rid: str, ps: list[dict[str, Any]]) -> tuple[str, str]:
    """(title, body) of the hand-off note."""
    tz = checks.his_tz()
    s = ctx.get("session") or {}
    last = session.last_owner_message(ctx.get("transcript"))
    title = f"Nightly rotation {_hm(ctx['now'], tz)}: a fresh session"
    body = [
        f"rotation-handoff: {rid}",
        f"The previous session ({str(s.get('sessionId') or '?')[:8]}) ended here; its transcript stays on disk "
        f"(`recall` finds it). His last message: {_hm(last, tz) if last else 'not in the tail'}.",
        "",
        "Promises with a time (open when the previous session ended; keep each):",
    ]
    timed = [p for p in ps if p["kind"] in ("todo", "remind")]
    for n, p in enumerate(timed, 1):
        label = f"TODO {p.get('where', '')}".strip() if p["kind"] == "todo" else f"reminder ({p.get('who')})"
        body.append(f"- [p{n}] {_hm(p['at'], tz)} · {label}: {p['title']}")
    if not timed:
        body.append("- none")
    crons = [p for p in ps if p["kind"] == "cron"]
    body += [
        "",
        "Session check-ins the new session does NOT have (CronCreate is session-only): re-create each with "
        "CronCreate (same cron, same prompt), or `remind add` when it is dated:",
    ]
    for n, p in enumerate(crons, 1):
        kind = "recurring" if p.get("recurring") else f"one-shot {_hm(p['at'], tz)}"
        body.append(f"- [c{n}] {p['key'].split(':', 1)[1]} {kind}, cron `{p['cron']}`: {p['title']}")
    if not crons:
        body.append("- none")
    if any(p["kind"] == "error" for p in ps):
        body.append("- (remind list failed: check `remind list` by hand)")
    body += ["", "Board (as the compaction hand-off shows it):", *_board_lines(), "", *_counts()]
    return title, "\n".join(body)


NEXT = (
    "read this note whole; re-create the check-ins listed (or move dated ones to `remind add`); run the heartbeat "
    "routine; then `rotate verify`; ack this note."
)


def snapshot(ps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: p[k] for k in ("key", "kind", "at", "cron", "recurring") if k in p} for p in ps]


def verify(snap: list[dict[str, Any]], rot: dict[str, Any], now: float) -> dict[str, Any]:
    """Classify each promise after rotation `rot` ({at, id, old_session, new_session?})."""
    since = float(rot.get("at") or 0)
    ags = session.agents()
    s = session.main_session(ags)
    new_sid = str(s.get("sessionId")) if s else ""
    tp = session.transcript(new_sid) if new_sid and new_sid != rot.get("old_session") else None
    read = session.has_marker(tp, f"rotation-handoff: {rot.get('id')}", since) if tp else False
    todo_now = {p["key"] for p in todo_promises(now)}
    items = _remind_items()
    remind_now = {_key("remind", str(i.get("id"))) for i in items} if items is not None else None
    result: dict[str, int] = {"durable": 0, "passed": 0, "recreated": 0, "changed": 0, "lost": 0}
    lost: list[str] = []
    for p in snap:
        k, kind, at = p["key"], p["kind"], p.get("at")
        if kind == "todo":
            st = "durable" if k in todo_now else ("passed" if at and at <= now else "changed")
        elif kind == "remind":
            if remind_now is None:
                st = "lost"  # unknown counts as lost: the metric must not flatter
            else:
                st = "durable" if k in remind_now else ("passed" if at and at <= now else "changed")
        elif kind == "cron":
            if read and session.cron_recreated(tp, str(p.get("cron") or ""), since):
                st = "recreated"
            elif read and at and at <= now:
                st = "passed"
            else:
                st = "lost"
        else:
            continue
        result[st] += 1
        if st == "lost":
            lost.append(k.split(":", 1)[0])
    return {
        "new_session": new_sid[:8] if new_sid else None,
        "rotated": bool(new_sid) and new_sid != rot.get("old_session"),
        "handoff_read": read,
        "promises": len([p for p in snap if p["kind"] in ("todo", "remind", "cron")]),
        **result,
        "lost_kinds": sorted(set(lost)),
    }


def note_name(rid: str) -> str:
    return f"rotation-{rid}.md"


def inbox_note(rid: str):  # -> Path
    return paths.inbox(note_name(rid))
