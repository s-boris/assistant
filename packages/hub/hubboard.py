"""hubboard: the board as records: board_items, board_log, board_meta (schemas/pg/004_board.sql).

The brain's `board` CLI (scripts/board) is the only writer; TODO.md and the dashboard render these rows.
  GET  /board[?all=1]      open items + the ones closed in the last 7 days (all=1: every row) and the meta map
  POST /board/apply        {rid, upserts: [item + "version" (0 = new)], meta: {k: v}, log: [{item, op, by, detail}]}:
                           one transaction; an insert of an existing id or an update whose version moved -> 409
                           (the brain reloads and runs again); the rid makes a retry return the first answer
  GET  /board/log[?item=ID&limit=N]   the newest log rows (default 50, at most 500)
"""

from __future__ import annotations

import re
import time
from typing import Any

import hubstore as hs

ID_RE = re.compile(r"^[a-z0-9]{4,16}$")
META_RE = re.compile(r"^[a-z_]{1,40}$")
KINDS = ("item", "raw")
STATES = ("open", "done", "dropped")
KEEP_CLOSED = 7 * 86_400
MAX_UPSERTS, MAX_LOG, MAX_META = 2000, 500, 20
LIMITS = {"section": 80, "line": 8000, "title": 400, "notes": 60_000, "probes": 20_000, "closed_by": 400}
TEXT = ("section", "line", "title", "notes", "probes", "closed_by")
NUM = ("pos", "due_ts", "created", "updated", "closed_ts")
FIELDS = ("kind", *TEXT, *NUM, "state")


def _bad(msg: str) -> hs.BadRequest:
    return hs.BadRequest(msg)


def check_item(u: Any) -> dict[str, Any]:
    """One upsert, validated: every column given (the brain sends whole rows), sizes capped."""
    if not isinstance(u, dict):
        raise _bad("upserts: objects")
    iid = u.get("id")
    if not isinstance(iid, str) or not ID_RE.match(iid):
        raise _bad("id: 4-16 lowercase letters or digits")
    ver = u.get("version")
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 0:
        raise _bad(f"{iid}: version (0 = new)")
    row: dict[str, Any] = {"id": iid, "version": ver}
    for k in TEXT:
        v = u.get(k)
        if v is None and k == "closed_by":
            row[k] = None
            continue
        if not isinstance(v, str) or len(v) > LIMITS[k]:
            raise _bad(f"{iid}: {k}: text up to {LIMITS[k]} characters")
        row[k] = v
    for k in NUM:
        v = u.get(k)
        if v is None and k in ("due_ts", "closed_ts"):
            row[k] = None
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise _bad(f"{iid}: {k}: a number")
        row[k] = float(v)
    if u.get("kind") not in KINDS:
        raise _bad(f"{iid}: kind: {' | '.join(KINDS)}")
    if u.get("state") not in STATES:
        raise _bad(f"{iid}: state: {' | '.join(STATES)}")
    row["kind"], row["state"] = u["kind"], u["state"]
    if not row["section"]:
        raise _bad(f"{iid}: section: not empty")
    return row


async def board_get(c: Any, q: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    if str(q.get("all") or "") in ("1", "true"):
        items = await hs.rows(c, "SELECT * FROM board_items ORDER BY pos, id")
    else:
        items = await hs.rows(
            c,
            "SELECT * FROM board_items WHERE state = 'open' OR closed_ts >= %s ORDER BY pos, id",
            (now - KEEP_CLOSED,),
        )
    meta = {r["k"]: r["v"] for r in await hs.rows(c, "SELECT k, v FROM board_meta")}
    return {"items": items, "meta": meta, "ts": now}


async def board_apply(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    ups, meta, log = a.get("upserts") or [], a.get("meta") or {}, a.get("log") or []
    if not isinstance(ups, list) or len(ups) > MAX_UPSERTS:
        raise _bad(f"upserts: a list of at most {MAX_UPSERTS}")
    if not isinstance(meta, dict) or len(meta) > MAX_META:
        raise _bad(f"meta: an object of at most {MAX_META} keys")
    if not isinstance(log, list) or len(log) > MAX_LOG:
        raise _bad(f"log: a list of at most {MAX_LOG}")
    rows = [check_item(u) for u in ups]
    if len({r["id"] for r in rows}) != len(rows):
        raise _bad("upserts: one row per id")
    now, versions = time.time(), {}
    cols = ", ".join(FIELDS)
    for r in rows:
        vals = [r[k] for k in FIELDS]
        if r["version"] == 0:
            n = await hs.run(
                c,
                f"INSERT INTO board_items(id, {cols}, version) VALUES (%s, {', '.join(['%s'] * len(FIELDS))}, 1)"
                " ON CONFLICT (id) DO NOTHING",
                (r["id"], *vals),
            )
            if n != 1:
                raise hs.Conflict(f"{r['id']}: exists")
            versions[r["id"]] = 1
        else:
            sets = ", ".join(f"{k} = %s" for k in FIELDS)
            n = await hs.run(
                c,
                f"UPDATE board_items SET {sets}, version = version + 1 WHERE id = %s AND version = %s",
                (*vals, r["id"], r["version"]),
            )
            if n != 1:
                raise hs.Conflict(f"{r['id']}: changed since version {r['version']}")
            versions[r["id"]] = r["version"] + 1
    for k, v in meta.items():
        if not isinstance(k, str) or not META_RE.match(k) or not isinstance(v, str) or len(v) > 200_000:
            raise _bad("meta: keys a-z_ (up to 40), text values up to 200000 characters")
        await hs.run(
            c,
            "INSERT INTO board_meta(k, v, updated) VALUES (%s, %s, %s)"
            " ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated = EXCLUDED.updated",
            (k, v, now),
        )
    for e in log:
        if not isinstance(e, dict):
            raise _bad("log: objects")
        item, op, by, detail = e.get("item"), e.get("op"), e.get("by"), e.get("detail", "")
        if item is not None and (not isinstance(item, str) or not ID_RE.match(item)):
            raise _bad("log: item = an item id or null")
        if not all(isinstance(x, str) and 0 < len(x) <= 40 for x in (op, by)) or not isinstance(detail, str):
            raise _bad("log: op and by up to 40 characters, detail text")
        await hs.run(
            c,
            "INSERT INTO board_log(ts, item, op, by_who, detail) VALUES (%s, %s, %s, %s, %s)",
            (now, item, op, by, detail[:2000]),
        )
    return {"versions": versions, "ts": now}


async def board_log(c: Any, q: dict[str, Any]) -> dict[str, Any]:
    try:
        limit = max(1, min(500, int(q.get("limit") or 50)))
    except ValueError:
        raise _bad("limit: a number") from None
    item = q.get("item")
    if item:
        if not ID_RE.match(str(item)):
            raise _bad("item: an item id")
        rows = await hs.rows(c, "SELECT * FROM board_log WHERE item = %s ORDER BY id DESC LIMIT %s", (item, limit))
    else:
        rows = await hs.rows(c, "SELECT * FROM board_log ORDER BY id DESC LIMIT %s", (limit,))
    return {"rows": rows}


async def r_get(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await board_get(c, q)


async def r_apply(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "board.apply", lambda: board_apply(c, a))


async def r_log(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await board_log(c, q)


def routes(brain: frozenset[str]) -> list[tuple[str, str, Any, frozenset[str]]]:
    return [
        ("GET", r"/board", r_get, brain),
        ("POST", r"/board/apply", r_apply, brain),
        ("GET", r"/board/log", r_log, brain),
    ]
