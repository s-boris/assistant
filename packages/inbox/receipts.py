"""inbox.receipts: delivery proof for owner Telegram events. Called by the agenda hook. FAILS OPEN.

A channel prompt (`<channel source="plugin:telegram…" … event_id="N">`) that reaches the main session marks event N
delivered: deliveries.receipt_ts (how=channel, first receipt kept) and the event acked (ack_by channel), in one short
transaction. Only rows of the owner-delivery kinds (RECEIPT_KINDS) count; when the tag and the row both carry a
message_id they must agree (a forged tag inside a message's text can't ack another event). Several tags per prompt
are fine (the bridge may batch). Tags without event_id append one line per event to
~/.local/state/assistant/receipts-baseline.jsonl (hook time, message time, lag, message_id, event_type; no chat, user
or text), the T3 baseline.

  channel_tags(prompt) -> [attrs]          the opening tags' attributes (only for a prompt that is channel events)
  record(prompt, now=None) -> dict         receipts + baseline lines; {"receipts": [...], "baseline": n, "skipped": n}
  undelivered(con, now, min_age=300)       open delivery-now RECEIPT_KINDS rows with no receipt, older than min_age
  has_deliveries(con)                      the deliveries table exists (schema 2)
  mark(con, eid, now, how="transcript")    a receipt from other proof (brain-watch's transcript check)
Hub mode (the rows live on the hub): record() writes each receipt through core.hub.receipt (the hub applies the
same kind and message_id checks and the same ack; a hub that is down or slow gets it from the spool, so the hook still
fails open); mark_hub() and undelivered_hub() are mark() and undelivered() over the hub.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Any

from core import clock, db, events, paths

__all__ = [
    "BASELINE_MAX",
    "RECEIPT_KINDS",
    "channel_tags",
    "has_deliveries",
    "is_owner_row",
    "is_receipt_kind",
    "mark",
    "mark_hub",
    "record",
    "undelivered",
    "undelivered_hub",
]

# Owner events that reach the brain through the channel: the bridge's CHANNEL_KINDS (telegram-plus 0.2.0
# bridgedb.ts), delivered when the row's delivery is 'now' (tg.bound only with note on or on error).
RECEIPT_KINDS = (
    "tg.message",
    "tg.button",
    "tg.reaction",
    "tg.poll_answer",
    "tg.stop",
    "tg.bound",
    "approval.decided",
    "bridge.handoff",
)
BASELINE_MAX = 512 * 1024
_SYSREM = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_OPEN = re.compile(r'<channel((?:\s+[\w-]+="[^"]*")+)\s*>')
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def is_receipt_kind(kind: str) -> bool:
    return kind in RECEIPT_KINDS


def is_owner_row(kind: str) -> bool:
    """A row that may hold his Telegram text (tg.*, hand-offs, approval answers): never an ordinary agenda line."""
    return kind.startswith("tg.") or kind in RECEIPT_KINDS


def channel_tags(prompt: str) -> list[dict[str, str]]:
    """Attributes of each opening <channel …> tag from Telegram, for a prompt that starts with one (after system
    reminders); [] for anything else (a heartbeat, a typed message that merely quotes a tag)."""
    s = prompt or ""
    if "<channel" not in s[:4000]:
        return []
    s = _SYSREM.sub("", s).lstrip()
    if not s.startswith("<channel"):
        return []
    out = []
    for m in _OPEN.finditer(s):
        a = dict(_ATTR.findall(m.group(1)))
        src = a.get("source", "")
        if "telegram" in src or src == "":
            out.append(a)
    return out


def _iso(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def has_deliveries(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deliveries'").fetchone() is not None


def _receipt(con: sqlite3.Connection, eid: int, tag: dict[str, str], now: float, how: str = "channel") -> str:
    r = con.execute("SELECT kind, status, note_path, payload FROM events WHERE id=?", (eid,)).fetchone()
    if r is None:
        return "unknown"
    if not is_receipt_kind(r["kind"]):
        return "kind"
    mid = tag.get("message_id")
    if mid:
        try:
            pmid = (json.loads(r["payload"] or "{}") or {}).get("message_id")
        except ValueError:
            pmid = None
        if pmid is not None and str(pmid) != str(mid):
            return "mismatch"
    con.execute(
        "INSERT INTO deliveries(event_id, receipt_ts, how) VALUES(?, ?, ?) ON CONFLICT(event_id) DO UPDATE"
        " SET receipt_ts=COALESCE(receipt_ts, excluded.receipt_ts), how=COALESCE(how, excluded.how)",
        (eid, now, how),
    )
    if r["status"] in events.OPEN and not r["note_path"]:
        con.execute(
            "UPDATE events SET status='acked', acked_ts=?, ack_by='channel' WHERE id=? AND status IN ('new','listed')",
            (now, eid),
        )
    return "ok"


def _baseline(tags: list[dict[str, str]], now: float) -> int:
    p = os.fspath(paths.state("receipts-baseline.jsonl"))
    n = 0
    for a in tags:
        ts = _iso(a.get("ts", ""))
        line = {
            "hook_ts": round(now, 3),
            "msg_ts": ts,
            "lag_s": round(now - ts, 3) if ts else None,
            "message_id": a.get("message_id") or None,
            "event_type": a.get("event_type") or "message",
            **({"redelivered": 1} if a.get("redelivered") else {}),
            **({"via": a["via"]} if a.get("via") in ("voice", "app") else {}),
        }
        events.append_private(p, json.dumps(line, separators=(",", ":")))
        n += 1
    try:
        if n and os.path.getsize(p) > BASELINE_MAX:
            with open(p, encoding="utf-8") as f:
                keep = f.readlines()[-2000:]
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(p + ".tmp", p)
    except OSError:
        pass
    return n


def record(prompt: str, now: float | None = None, timeout: float = 0.3) -> dict[str, Any]:
    """Receipts for the prompt's tagged events, baseline lines for untagged ones. Raises on store errors (the hook
    catches and logs); prints nothing."""
    tags = channel_tags(prompt)
    res: dict[str, Any] = {"receipts": [], "baseline": 0, "skipped": 0}
    if not tags:
        return res
    now = clock.now() if now is None else now
    tagged: list[tuple[int, dict[str, str]]] = []
    plain: list[dict[str, str]] = []
    for a in tags:
        v = a.get("event_id", "")
        if v.isdigit():
            tagged.append((int(v), a))
        else:
            plain.append(a)
    if plain:
        res["baseline"] = _baseline(plain, now)
    if tagged and _hub_tg():
        for eid, a in tagged:
            st = _receipt_hub(eid, a.get("message_id") or None, now, "channel", timeout)
            if st == "ok":
                res["receipts"].append(eid)
            elif st == "spooled":
                res["receipts"].append(eid)
                res["spooled"] = res.get("spooled", 0) + 1
            else:
                res["skipped"] += 1
        return res
    if tagged:
        con = db.connect(timeout=timeout)
        try:
            with db.tx(con):
                for eid, a in tagged:
                    st = _receipt(con, eid, a, now)
                    if st == "ok":
                        res["receipts"].append(eid)
                    else:
                        res["skipped"] += 1
        finally:
            con.close()
    return res


def mark(con: sqlite3.Connection, eid: int, now: float, how: str = "transcript") -> str:
    """A receipt from other proof than the prompt hook (brain-watch: the event's line in the transcript): "ok", or why
    not ("unknown", "kind"). The caller holds the transaction."""
    return _receipt(con, eid, {}, now, how)


def undelivered(con: sqlite3.Connection, now: float, min_age: float = 300) -> list[dict[str, Any]]:
    """Open owner-delivery rows (delivery 'now', as the bridge selects them) older than min_age with no receipt (no
    deliveries row with receipt_ts), oldest first. Ids, kinds and times only. Without the deliveries table (an old
    store read-only): every such row."""
    q = ",".join("?" * len(RECEIPT_KINDS))
    if has_deliveries(con):
        sql = (
            f"SELECT e.id, e.kind, e.ts, e.status FROM events e LEFT JOIN deliveries d ON d.event_id=e.id"
            f" WHERE e.kind IN ({q}) AND e.delivery='now' AND e.status IN ('new','listed') AND e.ts <= ?"
            f" AND d.receipt_ts IS NULL"
            f" ORDER BY e.ts, e.id"
        )
    else:
        sql = (
            f"SELECT id, kind, ts, status FROM events WHERE kind IN ({q}) AND delivery='now'"
            f" AND status IN ('new','listed') AND ts <= ?"
            f" ORDER BY ts, id"
        )
    return [dict(r) for r in con.execute(sql, (*RECEIPT_KINDS, now - min_age))]


# ------------------------------------------------------------------------------------------------ hub


def _hub_tg() -> bool:
    from core import hub

    return hub.tg_hub()


def _receipt_hub(eid: int, message_id: str | None, now: float, how: str, timeout: float = 0.3) -> str:
    """ "ok", "spooled" or why not ("unknown", "kind", "mismatch", …) from the hub's answer."""
    from core import hub

    got = hub.receipt(eid, now, how, message_id=message_id, kinds=RECEIPT_KINDS, timeout=timeout)
    if got is None:
        return "spooled"
    if got.get("ok"):
        return "ok"
    return str(got.get("result") or "refused")


def mark_hub(eid: int, now: float, how: str = "transcript") -> str:
    """mark() over the hub (hub mode): "ok", "spooled", or why not."""
    return _receipt_hub(eid, None, now, how, timeout=2.0)


def undelivered_hub(now: float, min_age: float = 300) -> list[dict[str, Any]]:
    """undelivered() over the hub (hub mode): the same rows (id, kind, ts, status), oldest first. Raises HubError."""
    from core import hub

    rows = hub.tg_q("undelivered", kinds=list(RECEIPT_KINDS), before_ts=now - min_age, timeout=3.0)
    return [{"id": int(r["id"]), "kind": r["kind"], "ts": float(r["ts"]), "status": r.get("status")} for r in rows]
