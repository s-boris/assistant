#!/usr/bin/env python3
"""hubapprovals: the hub's approvals store. PROTECTED: a release
that changes it loads only after the owner's ✅ on its digest (hubverify). The brain requests and consumes; only the
tg caller (his taps) decides. hubstore re-exports every name, so hubd keeps calling hs.approval_*.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any

import psycopg  # pyright: ignore[reportMissingImports]  (vendored in the release)
from hubstore import APPROVALS_KEEP, NONCE_RE, REF_RE, BadRequest, _now, _one_line, one, rows, run

# ------------------------------------------------------------------------------------------------ approvals


def _state(r: dict[str, Any], now: float) -> str:
    if r.get("used_ts") is not None:
        return "used"
    if r["status"] == "pending" and now >= r["expires"]:
        return "expired"
    return str(r["status"])


def _info(r: dict[str, Any], now: float, **kw: Any) -> dict[str, Any]:
    keep = ("nonce", "summary", "ref", "expires", "decided_via", "chat", "message_id", "created", "used_by")
    return {**{k: r.get(k) for k in keep}, "state": _state(r, now), **kw}


async def approval_request(c: psycopg.AsyncConnection, a: dict[str, Any]) -> dict[str, Any]:
    sha = a.get("action_sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise BadRequest("action_sha: the sha256 hex of the canonical action")
    summ = _one_line(a.get("summary"), 80)
    if not summ:
        raise BadRequest("the summary is empty")
    ttl = int(a.get("ttl") or 900)
    if not 1 <= ttl <= 86_400:
        raise BadRequest("ttl must be 1-86400 seconds")
    ref = a.get("ref")
    if ref is not None and (not isinstance(ref, str) or not REF_RE.match(ref)):
        raise BadRequest("ref: 1-80 of letters, digits and :.@/+-_")
    now = _now(a)
    nonce = secrets.token_hex(16)
    await run(
        c,
        "INSERT INTO approvals(nonce, action_sha, summary, ref, chat, created, expires) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (nonce, sha, summ, ref, a.get("chat"), now, now + ttl),
    )
    await run(c, "DELETE FROM approvals WHERE created < %s", (now - APPROVALS_KEEP,))
    return {"nonce": nonce, "expires": now + ttl, "summary": summ, "ref": ref}


async def approval_get(c: psycopg.AsyncConnection, nonce: str) -> dict[str, Any] | None:
    if not NONCE_RE.match(nonce or ""):
        return None
    r = await one(c, "SELECT * FROM approvals WHERE nonce=%s", (nonce,))
    return {**r, "state": _state(r, time.time())} if r else None


async def approval_list(c: psycopg.AsyncConnection, every: bool) -> dict[str, Any]:
    now = time.time()
    q = "SELECT * FROM approvals" + ("" if every else " WHERE status='pending' AND expires > %s") + " ORDER BY created"
    return {"rows": await rows(c, q, () if every else (now,))}


async def approval_attach(c: psycopg.AsyncConnection, nonce: str, a: dict[str, Any]) -> dict[str, Any]:
    chat = str(a.get("chat") or "")
    n = await run(
        c,
        "UPDATE approvals SET chat=%s, message_id=%s WHERE nonce=%s AND status='pending' AND (chat IS NULL OR chat=%s)",
        (chat, int(a.get("message_id") or 0), nonce, chat),
    )
    return {"ok": n == 1}


async def approval_consume(c: psycopg.AsyncConnection, nonce: str, a: dict[str, Any]) -> dict[str, Any]:
    """packages/approvals consume(), here: 0 ok (once) · 3 expired · 4 used · 5 unknown, undecided, declined,
    cancelled or another action."""
    now = _now(a)
    if not NONCE_RE.match(nonce or ""):
        return {"code": 5, "info": {"nonce": nonce, "state": "unknown", "why": "not a nonce"}}
    r = await one(c, "SELECT * FROM approvals WHERE nonce=%s FOR UPDATE", (nonce,))
    if r is None:
        return {"code": 5, "info": {"nonce": nonce, "state": "unknown", "why": "no such approval"}}
    if r["used_ts"] is not None:
        return {"code": 4, "info": _info(r, now, why=f"used already ({r['used_by']})")}
    if now >= r["expires"]:
        if r["status"] == "pending":
            await run(c, "UPDATE approvals SET status='expired' WHERE nonce=%s AND status='pending'", (nonce,))
            r["status"] = "expired"
        return {"code": 3, "info": _info(r, now, why="expired")}
    if r["status"] != "approved":
        await run(c, "UPDATE approvals SET refused = refused + 1 WHERE nonce=%s", (nonce,))
        why = {"pending": "not decided yet", "denied": "he declined", "cancelled": "cancelled (replaced card)"}
        return {
            "code": 3 if r["status"] == "expired" else 5,
            "info": _info(r, now, why=why.get(r["status"], r["status"])),
        }
    if a.get("action_sha") != r["action_sha"]:
        await run(c, "UPDATE approvals SET refused = refused + 1 WHERE nonce=%s", (nonce,))
        return {"code": 5, "info": _info(r, now, why="another action: his approval does not cover it")}
    by = _one_line(a.get("by"), 80) or "cli"
    n = await run(
        c,
        "UPDATE approvals SET used_ts=%s, used_by=%s WHERE nonce=%s AND status='approved' AND used_ts IS NULL"
        " AND expires > %s",
        (now, by, nonce, now),
    )
    if n != 1:
        return {"code": 4, "info": _info(r, now, why="used already")}
    r["used_ts"], r["used_by"] = now, by
    return {"code": 0, "info": _info(r, now, why="approved by him", used_by=by)}


async def approval_cancel(c: psycopg.AsyncConnection, nonce: str, a: dict[str, Any]) -> dict[str, Any]:
    n = await run(
        c,
        "UPDATE approvals SET status='cancelled', decided_ts=NULL WHERE nonce=%s AND status='pending' AND expires > %s",
        (nonce, _now(a)),
    )
    return {"ok": n == 1}


async def approval_decide(c: psycopg.AsyncConnection, nonce: str, a: dict[str, Any]) -> dict[str, Any]:
    """His tap, as the tg caller reports it: pending and unexpired -> approved | denied; anything else is refused."""
    d = a.get("decision")
    if d not in ("y", "n"):
        raise BadRequest("decision: y or n")
    by = str(a.get("by") or "")
    if not re.fullmatch(r"-?\d{1,20}", by):
        raise BadRequest("by: his Telegram user id")
    now = _now(a)
    n = await run(
        c,
        "UPDATE approvals SET status=%s, decided_ts=%s, decided_by=%s, decided_via=%s WHERE nonce=%s"
        " AND status='pending' AND expires > %s",
        ("approved" if d == "y" else "denied", now, by, str(a.get("via") or "adapter")[:20], nonce, now),
    )
    r = await one(c, "SELECT * FROM approvals WHERE nonce=%s", (nonce,))
    return {"ok": n == 1, "state": _state(r, now) if r else "unknown"}


async def approval_card(c: psycopg.AsyncConnection, nonce: str, a: dict[str, Any]) -> dict[str, Any]:
    card = a.get("card")
    if card not in ("edited", "failed"):
        raise BadRequest("card: edited or failed")
    n = await run(c, "UPDATE approvals SET card=%s WHERE nonce=%s", (card, nonce))
    return {"ok": n == 1}


async def approval_expire(c: psycopg.AsyncConnection, a: dict[str, Any]) -> dict[str, Any]:
    """Pending past their expiry -> expired; the rows whose cards the tg caller should edit."""
    now = _now(a)
    due = await rows(
        c,
        "SELECT nonce, chat, message_id FROM approvals"
        " WHERE status='pending' AND expires <= %s AND message_id IS NOT NULL"
        " AND card IS NULL ORDER BY expires",
        (now,),
    )
    n = await run(c, "UPDATE approvals SET status='expired' WHERE status='pending' AND expires <= %s", (now,))
    return {"expired": n, "cards": due}


AUDIT_SQL = (
    "SELECT COUNT(*) AS n FROM approvals WHERE (used_ts IS NOT NULL AND (used_ts >= expires OR status != 'approved'"
    " OR decided_ts IS NULL)) OR (decided_ts IS NOT NULL AND decided_ts >= expires)"
)


async def approval_audit(c: psycopg.AsyncConnection) -> dict[str, Any]:
    n = (await one(c, AUDIT_SQL) or {}).get("n", 0)
    s = await one(
        c,
        "SELECT COUNT(*) AS total, COUNT(used_ts) AS used, COALESCE(SUM(refused), 0) AS refused FROM approvals",
    )
    s = s or {}
    return {
        "ok": n == 0,
        "violations": int(n),
        "approvals": int(s.get("total") or 0),
        "used": int(s.get("used") or 0),
        "refused_attempts": int(s.get("refused") or 0),
    }
