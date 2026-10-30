"""hubstore: the hub's Postgres store: the events ledger, the outbox and the approvals. Runs only in
the hub container (psycopg 3, vendored in the release); the brain reaches it through hubd's API, never directly.

One schema per store instance: "hub" (live) or "t_<name>" (tests). Every write runs in one transaction; a request id
(rid) makes a write idempotent: the first answer is kept in `requests` and a retry gets it again, never a second write.

Ledger (events, beats, wake_rules, meta) — the brain owns it as it owns the SQLite store today:
  emit / update                typed writes, the core.events semantics (idem keys, note occurrences, payload merges)
  snapshot(profile, ...)       the rows a brain-side unit of work reads (hook | ingest | full) + reserved id blocks
  apply(changes)               that unit of work's row changes, all or nothing; every update and delete carries the
                               row's old values and fails (Conflict -> 409, the client re-runs) when the row moved
Outbox (core.tg rows; the tg caller consumes): enqueue, get, withdraw (queued -> skipped), and for the consumer queued,
  claim (queued -> sending, atomic: the same condition as withdraw, so a row is either withdrawn or claimed, never
  both), set, fail (+ its outbox.failed event in the same transaction), dupes, interrupted, consumers.
Approvals: request mints the nonce here (the brain sends only the action's sha256), consume checks and uses it once
  here; decide and card are the tg caller's (his tap), never the brain's.
Admin: import (one-time copy, ids kept), dump, counts, drop_schema (t_* only).
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import psycopg  # pyright: ignore[reportMissingImports]  (vendored in the release, not in the dev venvs)
from psycopg import sql  # pyright: ignore[reportMissingImports]
from psycopg.rows import dict_row  # pyright: ignore[reportMissingImports]

SCHEMA_RE = re.compile(r"^(hub|t_[a-z0-9_]{1,40})$")
LIVE = "hub"
RID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")
KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
REF_RE = re.compile(r"^[\w:.@/+-]{1,80}$")
OPEN = ("new", "listed")
CLASSES = ("now", "beat", "digest", "mute")
OUTBOX_STATES = ("queued", "sending", "sent", "failed", "skipped")
REQUESTS_KEEP = 7 * 86_400
APPROVALS_KEEP = 30 * 86_400
RESERVE_MAX = 1000

T, F, N = "t", "f", "i"  # column types: text, double precision, integer/bigint
COLS: dict[str, dict[str, str]] = {
    "events": {
        "id": N, "ts": F, "source": T, "kind": T, "subject": T, "next": T, "payload": T, "labels": T, "delivery": T,
        "trace_id": T, "idem_key": T, "note_path": T, "status": T, "updated_ts": F, "first_beat": N,
        "first_listed_ts": F, "acked_ts": F, "ack_by": T, "ack_note": T,
    },
    "beats": {"id": N, "ts": F, "source": T, "ok": N, "ms": N, "chars": N, "listed": T},
    "wake_rules": {"id": N, "created": F, "kind_glob": T, "klass": T, "until": F, "why": T, "active": N},
    "meta": {"k": T, "v": T},
    "outbox": {
        "id": N, "ts": F, "bot": T, "chat": T, "op": T, "body": T, "idem_key": T, "urgent": N, "not_before": F,
        "mode": T, "status": T, "attempts": N, "last_error": T, "sent_ts": F, "tg_message_id": N, "trace_id": T,
        "reply_to_event": N, "files": T, "result": T,
    },
    "deliveries": {"event_id": N, "sent_ts": F, "tries": N, "receipt_ts": F, "how": T},
    "bindings": {
        "id": N, "kind": T, "key": T, "tool": T, "args": T, "note": N, "on_error": T, "why": T, "created": F,
        "expires": F, "active": N,
    },
    "approvals": {
        "nonce": T, "action_sha": T, "summary": T, "ref": T, "chat": T, "message_id": N, "created": F, "expires": F,
        "status": T, "decided_ts": F, "decided_by": T, "decided_via": T, "used_ts": F, "used_by": T, "refused": N,
        "card": T,
    },
}  # fmt: skip
PK = {
    "events": "id", "beats": "id", "wake_rules": "id", "meta": "k", "outbox": "id", "deliveries": "event_id",
    "bindings": "id", "approvals": "nonce",
}  # fmt: skip
LEDGER = ("events", "beats", "wake_rules", "meta")
IDENTITY = ("events", "beats", "wake_rules", "outbox", "bindings")
SET_OK = ("status", "attempts", "not_before", "last_error", "sent_ts", "tg_message_id", "result", "files")


class BadRequest(Exception):
    """400: the request itself is wrong."""


class Conflict(Exception):
    """409: the rows moved since the snapshot (or a duplicate request ran at the same time): re-run."""


class Forbidden(Exception):
    """403: this caller may not do that."""


def sql_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (
        os.environ.get("HUB_SQL_DIR", ""),
        os.path.join(here, "pg"),
        os.path.join(here, "..", "..", "schemas", "pg"),
    ):
        if d and os.path.isdir(d):
            return d
    raise RuntimeError("no SQL migrations directory (HUB_SQL_DIR, app/pg, schemas/pg)")


def migrations() -> list[tuple[str, str]]:
    d = sql_dir()
    return [(n, open(os.path.join(d, n), encoding="utf-8").read()) for n in sorted(os.listdir(d)) if n.endswith(".sql")]


def coerce(table: str, col: str, v: Any) -> Any:
    typ = COLS[table].get(col)
    if typ is None:
        raise BadRequest(f"{table}: no column {col!r}")
    if v is None:
        return None
    try:
        if typ == T:
            return (
                v
                if isinstance(v, str)
                else json.dumps(v, ensure_ascii=False)
                if isinstance(v, (dict, list))
                else str(v)
            )
        if typ == F:
            return float(v)
        if isinstance(v, bool):
            return int(v)
        return int(v) if not isinstance(v, float) or v.is_integer() else v
    except (TypeError, ValueError):
        raise BadRequest(f"{table}.{col}: bad value") from None


def ident(name: str) -> sql.Identifier:
    return sql.Identifier(name)


def _now(a: dict[str, Any]) -> float:
    v = a.get("now")
    return float(v) if isinstance(v, (int, float)) else time.time()


def _one_line(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------------------------------------ connections


class Store:
    """A small pool of autocommit connections; tx(schema) = one transaction with search_path set to the schema."""

    def __init__(self, conninfo: dict[str, Any], size: int = 4) -> None:
        import asyncio

        self.kw = conninfo
        self.idle: list[psycopg.AsyncConnection] = []
        self.sem = asyncio.Semaphore(size)
        self.mlock = asyncio.Lock()
        self.ready: set[str] = set()
        self.writes = 0

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(autocommit=True, connect_timeout=3, **self.kw)

    @contextlib.asynccontextmanager
    async def conn(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.sem:
            c = None
            while self.idle and c is None:
                c = self.idle.pop()
                if c.closed or c.broken:
                    c = None
            if c is None:
                c = await self._connect()
            try:
                yield c
            finally:
                idle = psycopg.pq.TransactionStatus.IDLE
                if not c.closed and not c.broken and c.info.transaction_status == idle:
                    self.idle.append(c)
                else:
                    with contextlib.suppress(Exception):
                        await c.close()

    async def close(self) -> None:
        while self.idle:
            with contextlib.suppress(Exception):
                await self.idle.pop().close()

    async def ensure(self, schema: str) -> None:
        if schema in self.ready:
            return
        if not SCHEMA_RE.match(schema):
            raise BadRequest("bad schema name")
        async with self.mlock:
            if schema in self.ready:
                return
            async with self.conn() as c, c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("hub-migrate:" + schema,))
                await c.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(ident(schema)))
                await c.execute(sql.SQL("SET LOCAL search_path TO {}").format(ident(schema)))
                await c.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,"
                    " applied DOUBLE PRECISION NOT NULL)"
                )
                cur = await c.execute("SELECT version FROM schema_migrations")
                have = {r[0] for r in await cur.fetchall()}
                for name, text in migrations():
                    if name not in have:
                        await c.execute(text.encode())  # no parameters: several statements in one go
                        await c.execute("INSERT INTO schema_migrations VALUES (%s, %s)", (name, time.time()))
                await c.execute("DELETE FROM requests WHERE ts < %s", (time.time() - REQUESTS_KEEP,))
            self.ready.add(schema)

    @contextlib.asynccontextmanager
    async def tx(self, schema: str) -> AsyncIterator[psycopg.AsyncConnection]:
        await self.ensure(schema)
        async with self.conn() as c, c.transaction():
            await c.execute(sql.SQL("SET LOCAL search_path TO {}").format(ident(schema)))
            yield c

    async def migrated(self, schema: str) -> list[str]:
        async with self.tx(schema) as c:
            cur = await c.execute("SELECT version FROM schema_migrations ORDER BY version")
            return [r[0] for r in await cur.fetchall()]

    async def drop_schema(self, schema: str) -> bool:
        if not schema.startswith("t_") or not SCHEMA_RE.match(schema):
            raise Forbidden("only test schemas (t_*) can be dropped")
        async with self.conn() as c, c.transaction():
            await c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(ident(schema)))
        self.ready.discard(schema)
        return True


async def rows(c: psycopg.AsyncConnection, q: Any, params: Any = ()) -> list[dict[str, Any]]:
    cur = c.cursor(row_factory=dict_row)
    await cur.execute(q, params)
    return list(await cur.fetchall()) if cur.description else []


async def one(c: psycopg.AsyncConnection, q: Any, params: Any = ()) -> dict[str, Any] | None:
    r = await rows(c, q, params)
    return r[0] if r else None


async def run(c: psycopg.AsyncConnection, q: Any, params: Any = ()) -> int:
    cur = await c.execute(q, params)
    return cur.rowcount


async def once(
    c: psycopg.AsyncConnection, rid: Any, caller: str, op: str, fn: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    """Run a write once per request id: a repeated rid answers with the stored result and writes nothing."""
    if rid is not None:
        if not isinstance(rid, str) or not RID_RE.match(rid):
            raise BadRequest("rid: 8-80 of letters, digits and ._:-")
        hit = await one(c, "SELECT result, op FROM requests WHERE rid=%s", (rid,))
        if hit is not None:
            if hit["op"] != op:
                raise BadRequest(f"rid already used for {hit['op']}")
            return {**json.loads(hit["result"]), "replayed": True}
    res = await fn()
    if rid is not None:
        await run(
            c,
            "INSERT INTO requests(rid, ts, caller, op, result) VALUES (%s, %s, %s, %s, %s)",
            (rid, time.time(), caller, op, json.dumps(res, ensure_ascii=False)),
        )
    return res


# ------------------------------------------------------------------------------------------------ ledger: typed writes


async def emit(c: psycopg.AsyncConnection, a: dict[str, Any]) -> dict[str, Any]:
    """core.events.emit's row: a known idem key is a dup, unless its event is closed and its note is gone (the
    client says so: note_gone), which makes the old row's key "<key>#<id>" and records a new occurrence."""
    kind = a.get("kind")
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        raise BadRequest("kind: lowercase letters, digits, . _ -")
    delivery = a.get("delivery") or "beat"
    if delivery not in CLASSES:
        raise BadRequest(f"delivery must be one of {CLASSES}")
    key = a.get("idem_key")
    if key is not None and (not isinstance(key, str) or not key or len(key) > 300):
        raise BadRequest("idem_key: 1-300 characters")
    payload = a.get("payload") or {}
    labels = a.get("labels") or []
    if not isinstance(payload, dict) or not isinstance(labels, list):
        raise BadRequest("payload must be an object, labels a list")
    row = (
        float(a["ts"]) if isinstance(a.get("ts"), (int, float)) else time.time(),
        str(a.get("source") or "emit"),
        kind,
        str(a.get("subject") or "")[:500],
        str(a.get("next") or "")[:500],
        json.dumps(payload, ensure_ascii=False),
        json.dumps(sorted({str(x) for x in labels}), ensure_ascii=False),
        delivery,
        a.get("trace_id"),
        key,
        a.get("note_path"),
    )
    ins = (
        "INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, note_path)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    if key is None:
        r = await one(c, ins + " RETURNING id", row)
        return {"id": r["id"] if r else None, "dup": False}
    hit = await one(c, "SELECT id, status FROM events WHERE idem_key=%s FOR UPDATE", (key,))
    if hit is not None:
        if a.get("note_gone") and hit["status"] not in OPEN:
            await run(c, "UPDATE events SET idem_key = idem_key || '#' || id::text WHERE id=%s", (hit["id"],))
        else:
            return {"id": hit["id"], "dup": True}
    r = await one(c, ins + " ON CONFLICT (idem_key) DO NOTHING RETURNING id", row)
    if r is None:  # a concurrent emit of the same key won
        hit = await one(c, "SELECT id FROM events WHERE idem_key=%s", (key,))
        return {"id": hit["id"] if hit else None, "dup": True}
    return {"id": r["id"], "dup": False}


async def update(c: psycopg.AsyncConnection, a: dict[str, Any]) -> dict[str, Any]:
    """core.events.update: payload keys merged, labels added, subject/next replaced, status back to new."""
    key = a.get("idem_key")
    if not isinstance(key, str) or not key:
        raise BadRequest("idem_key needed")
    r = await one(c, "SELECT id, status, payload, labels FROM events WHERE idem_key=%s FOR UPDATE", (key,))
    if r is None:
        return {"id": None, "status": "missing"}
    if r["status"] not in OPEN:
        return {"id": r["id"], "status": "closed"}
    try:
        pl = json.loads(r["payload"] or "{}")
        old = json.loads(r["labels"] or "[]")
    except ValueError:
        pl, old = {}, []
    pl = pl if isinstance(pl, dict) else {}
    pl.update(a.get("payload") or {})
    if a.get("note_fp") is not None:
        pl["note"] = a["note_fp"]
    lab = sorted(set(old if isinstance(old, list) else []) | {str(x) for x in (a.get("labels") or [])})
    sets = ["payload=%s", "labels=%s", "updated_ts=%s", "status='new'"]
    args: list[Any] = [
        json.dumps(pl, ensure_ascii=False),
        json.dumps(lab, ensure_ascii=False),
        float(a["ts"]) if isinstance(a.get("ts"), (int, float)) else time.time(),
    ]
    for f in ("subject", "next"):
        if a.get(f) is not None:
            sets.append(f"{f}=%s")
            args.append(str(a[f])[:500])
    await run(c, f"UPDATE events SET {', '.join(sets)} WHERE id=%s", (*args, r["id"]))
    return {"id": r["id"], "status": "updated"}


# ------------------------------------------------------------------------------------------------ ledger: units of work

_EVENTS_WHERE = {
    "hook": "status IN ('new','listed') OR (status='gone' AND updated_ts >"
    " COALESCE((SELECT MAX(ts) FROM beats WHERE ok=1), 0))",
    "ingest": "status IN ('new','listed') OR idem_key = ANY(%s)",
    "full": "TRUE",
    "ids": "id = ANY(%s)",
}
_BEATS_EDGE = (
    "id = (SELECT MAX(id) FROM beats) OR id = (SELECT id FROM beats WHERE ok=1 ORDER BY ts DESC, id DESC LIMIT 1)"
)


async def snapshot(c: psycopg.AsyncConnection, a: dict[str, Any]) -> dict[str, Any]:
    profile = a.get("profile") or "full"
    if profile not in _EVENTS_WHERE:
        raise BadRequest(f"profile must be one of {sorted(_EVENTS_WHERE)}")
    out: dict[str, list[dict[str, Any]]] = {}
    if profile == "ingest":
        names = [str(x) for x in (a.get("names") or [])][:20000]
        out["events"] = await rows(c, f"SELECT * FROM events WHERE {_EVENTS_WHERE['ingest']} ORDER BY id", (names,))
    elif profile == "ids":
        ids = [int(x) for x in (a.get("ids") or [])][:5000]
        out["events"] = await rows(c, "SELECT * FROM events WHERE id = ANY(%s) ORDER BY id", (ids,))
    else:
        out["events"] = await rows(c, f"SELECT * FROM events WHERE {_EVENTS_WHERE[profile]} ORDER BY id")
    if profile in ("hook", "full"):
        since = float(a.get("beats_since") or 0) if profile == "full" else None
        bq = f"SELECT * FROM beats WHERE {_BEATS_EDGE}" + (" OR ts >= %s" if since is not None else "") + " ORDER BY id"
        out["beats"] = await rows(c, bq, (since,) if since is not None else ())
        out["wake_rules"] = await rows(c, "SELECT * FROM wake_rules ORDER BY id")
    out["meta"] = await rows(c, "SELECT * FROM meta ORDER BY k")
    reserved: dict[str, list[int]] = {}
    for t, k in sorted((a.get("reserve") or {}).items()):
        if t not in ("events", "beats", "wake_rules") or not isinstance(k, int) or not 0 < k <= RESERVE_MAX:
            raise BadRequest("reserve: {events|beats|wake_rules: 1-1000}")
        # The lock keeps inserts (whose identity default is a nextval) out until COMMIT, so the block is ours alone.
        await c.execute(sql.SQL("LOCK TABLE {} IN EXCLUSIVE MODE").format(ident(t)))
        seq = (await one(c, "SELECT pg_get_serial_sequence(%s, 'id') AS s", (t,)) or {})["s"]
        n = (await one(c, "SELECT nextval(%s::regclass) AS n", (seq,)) or {})["n"]
        await c.execute("SELECT setval(%s::regclass, %s, true)", (seq, n + k - 1))
        reserved[t] = [int(n), k]
    return {"rows": out, "reserved": reserved, "ts": time.time()}


def _cond(table: str, old: dict[str, Any]) -> tuple[Any, list[Any]]:  # a psycopg Composable
    parts, args = [], []
    for col, v in sorted(old.items()):
        parts.append(sql.SQL("{} IS NOT DISTINCT FROM %s").format(ident(col)))
        args.append(coerce(table, col, v))
    return sql.SQL(" AND ").join(parts) if parts else sql.SQL("TRUE"), args


async def apply(c: psycopg.AsyncConnection, a: dict[str, Any]) -> dict[str, Any]:
    """A unit of work's changes: [{t, op: ins|upd|del, pk, row|set, old}]. Ledger tables only. Updates and deletes
    match the row's old values (all of them): a row someone else changed meanwhile is a Conflict. meta rows are
    caches: last writer wins."""
    changes = a.get("changes")
    if not isinstance(changes, list) or len(changes) > 5000:
        raise BadRequest("changes: a list of at most 5000")
    n = {"ins": 0, "upd": 0, "del": 0}
    for ch in changes:
        t, op = ch.get("t"), ch.get("op")
        if t not in LEDGER or op not in n:
            raise BadRequest("change: t in events|beats|wake_rules|meta, op in ins|upd|del")
        pk = PK[t]
        if op == "ins":
            row = {k: coerce(t, k, v) for k, v in (ch.get("row") or {}).items()}
            if row.get(pk) is None:
                raise BadRequest(f"{t}: an insert needs its {pk}")
            q = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                ident(t), sql.SQL(", ").join(map(ident, row)), sql.SQL(", ").join(sql.Placeholder() * len(row))
            )
            if t == "meta":
                q = q + sql.SQL(" ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v")
            try:
                async with c.transaction():  # a savepoint: a duplicate key is a Conflict, not a broken transaction
                    await c.execute(q, list(row.values()))
            except psycopg.errors.UniqueViolation:
                raise Conflict(f"{t} {row.get(pk)}: exists already") from None
        elif op == "upd":
            sets = {k: coerce(t, k, v) for k, v in (ch.get("set") or {}).items() if k != pk}
            if not sets:
                continue
            cond, cargs = (sql.SQL("TRUE"), []) if t == "meta" else _cond(t, ch.get("old") or {})
            q = sql.SQL("UPDATE {} SET {} WHERE {} = %s AND {}").format(
                ident(t),
                sql.SQL(", ").join(sql.SQL("{} = %s").format(ident(k)) for k in sets),
                ident(pk),
                cond,
            )
            try:
                async with c.transaction():
                    got = await run(c, q, [*sets.values(), coerce(t, pk, ch.get("pk")), *cargs])
            except psycopg.errors.UniqueViolation:
                raise Conflict(f"{t} {ch.get('pk')}: the change collides with another row") from None
            if got != 1 and t != "meta":
                raise Conflict(f"{t} {ch.get('pk')}: changed since the snapshot")
        else:
            cond, cargs = _cond(t, ch.get("old") or {})
            q = sql.SQL("DELETE FROM {} WHERE {} = %s AND {}").format(ident(t), ident(pk), cond)
            if await run(c, q, [coerce(t, pk, ch.get("pk")), *cargs]) != 1:
                raise Conflict(f"{t} {ch.get('pk')}: changed since the snapshot")
        n[op] += 1
    return n


# ------------------------------------------------------------------------------------------------ outbox


async def outbox_enqueue(c: psycopg.AsyncConnection, a: dict[str, Any]) -> dict[str, Any]:
    op, bot, chat = a.get("op"), a.get("bot"), a.get("chat")
    if not all(isinstance(x, str) and x for x in (op, bot, chat)):
        raise BadRequest("op, bot and chat needed")
    body = a.get("body")
    if not isinstance(body, dict):
        raise BadRequest("body must be an object")
    files = a.get("files")
    r = await one(
        c,
        "INSERT INTO outbox(ts, bot, chat, op, body, idem_key, urgent, not_before, mode, trace_id, reply_to_event,"
        " files) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (idem_key) DO NOTHING RETURNING id",
        (
            time.time(),
            bot,
            chat,
            op,
            json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            a.get("idem_key"),
            1 if a.get("urgent") else 0,
            a.get("not_before"),
            str(a.get("mode") or "live"),
            a.get("trace_id"),
            a.get("reply_to_event"),
            json.dumps(files) if files else None,
        ),
    )
    return {"id": r["id"] if r else None}


async def outbox_get(c: psycopg.AsyncConnection, oid: int) -> dict[str, Any] | None:
    return await one(c, "SELECT * FROM outbox WHERE id=%s", (oid,))


async def outbox_withdraw(c: psycopg.AsyncConnection, oid: int, why: str) -> dict[str, Any]:
    n = await run(
        c,
        "UPDATE outbox SET status='skipped', last_error=%s WHERE id=%s AND status='queued'",
        (f"withdrawn: {why}"[:300], oid),
    )
    return {"ok": n == 1}


async def outbox_queued(c: psycopg.AsyncConnection, caller: str, bots: list[str]) -> dict[str, Any]:
    if not bots or not all(isinstance(b, str) and b for b in bots):
        raise BadRequest("bots needed")
    now = time.time()
    for b in bots:
        await run(
            c,
            "INSERT INTO consumers(bot, caller, seen) VALUES (%s, %s, %s)"
            " ON CONFLICT (bot) DO UPDATE SET caller = EXCLUDED.caller, seen = EXCLUDED.seen",
            (b, caller, now),
        )
    got = await rows(
        c, "SELECT * FROM outbox WHERE status='queued' AND bot = ANY(%s) AND mode='live' ORDER BY id LIMIT 500", (bots,)
    )
    return {"rows": got}


async def outbox_claim(c: psycopg.AsyncConnection, oid: int, attempts: int) -> dict[str, Any]:
    n = await run(c, "UPDATE outbox SET status='sending', attempts=%s WHERE id=%s AND status='queued'", (attempts, oid))
    return {"claimed": n == 1}


async def outbox_set(c: psycopg.AsyncConnection, oid: int, fields: dict[str, Any]) -> dict[str, Any]:
    f = {k: coerce("outbox", k, v) for k, v in (fields or {}).items()}
    bad = set(f) - set(SET_OK)
    if bad or not f:
        raise BadRequest(f"set: only {', '.join(SET_OK)}")
    if "status" in f and f["status"] not in OUTBOX_STATES:
        raise BadRequest("bad status")
    q = sql.SQL("UPDATE outbox SET {} WHERE id = %s").format(
        sql.SQL(", ").join(sql.SQL("{} = %s").format(ident(k)) for k in f)
    )
    return {"ok": await run(c, q, [*f.values(), oid]) == 1}


async def _failed_event(c: psycopg.AsyncConnection, row: dict[str, Any], err: str, partial: bool) -> None:
    payload = {
        "outbox_id": row["id"], "bot": row["bot"], "chat": row["chat"], "op": row["op"],
        "urgent": 1 if row["urgent"] else 0, "mode": row["mode"], "error": err[:300], "partial": bool(partial),
    }  # fmt: skip
    await run(
        c,
        "INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, status)"
        " VALUES (%s, 'tg-adapter', 'outbox.failed', %s, %s, %s, %s, %s, %s, %s, 'new')"
        " ON CONFLICT (idem_key) DO NOTHING",
        (
            time.time(),
            f"Telegram {row['op']} failed (outbox #{row['id']}, chat {row['chat']})",
            "tg-adapter status · enqueue again to retry",
            json.dumps(payload),
            json.dumps(["tg", row["mode"], "outbox"]),
            "now" if row["urgent"] else "beat",
            row.get("trace_id"),
            f"outbox.failed:{row['bot']}:{row['id']}",
        ),
    )


async def outbox_fail(c: psycopg.AsyncConnection, oid: int, fields: dict[str, Any], partial: bool) -> dict[str, Any]:
    fields = {**(fields or {}), "status": "failed"}
    err = str(fields.get("last_error") or "failed")
    fields["last_error"] = err
    res = await outbox_set(c, oid, fields)
    row = await outbox_get(c, oid)
    if row is not None:
        await _failed_event(c, row, err, partial)
    return res


async def outbox_interrupted(c: psycopg.AsyncConnection, bots: list[str]) -> dict[str, Any]:
    stale = await rows(c, "SELECT * FROM outbox WHERE status='sending' AND bot = ANY(%s) ORDER BY id", (bots,))
    for r in stale:
        await outbox_fail(c, r["id"], {"last_error": "interrupted while sending (may have been delivered)"}, False)
    return {"failed": len(stale)}


async def outbox_dupes(c: psycopg.AsyncConnection, a: dict[str, Any]) -> dict[str, Any]:
    """The rows the adapter's dedupe compares with (outbox.ts duplicateOf); the adapter compares the texts."""
    got = await rows(
        c,
        "SELECT id, body FROM outbox WHERE bot=%s AND chat=%s AND op=%s AND mode='live' AND id < %s AND ts >= %s"
        " AND status IN ('queued', 'sending', 'sent') ORDER BY id DESC LIMIT 200",
        (a.get("bot"), a.get("chat"), a.get("op"), int(a.get("id") or 0), float(a.get("since") or 0)),
    )
    return {"rows": got}


async def consumers(c: psycopg.AsyncConnection) -> dict[str, Any]:
    now = time.time()
    got = await rows(c, "SELECT bot, caller, seen FROM consumers ORDER BY bot")
    return {"consumers": [{**r, "age": round(now - r["seen"], 1)} for r in got]}


# ------------------------------------------------------------------------------------------------ admin


async def import_rows(c: psycopg.AsyncConnection, a: dict[str, Any]) -> dict[str, Any]:
    """Copies rows with their ids; a row whose key (or idem key) exists is skipped (mode skip) or
    replaced (mode replace: refreshes changed rows)."""
    t = a.get("table")
    if t not in COLS:
        raise BadRequest("unknown table")
    mode = a.get("mode") or "skip"
    if mode not in ("skip", "replace"):
        raise BadRequest("mode: skip or replace")
    got = a.get("rows")
    if not isinstance(got, list) or len(got) > 5000:
        raise BadRequest("rows: a list of at most 5000")
    ins = skip = 0
    for raw in got:
        row = {k: coerce(t, k, v) for k, v in raw.items()}
        q = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            ident(t), sql.SQL(", ").join(map(ident, row)), sql.SQL(", ").join(sql.Placeholder() * len(row))
        )
        if mode == "replace":
            upd = [k for k in row if k != PK[t]]
            q = q + sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
                ident(PK[t]), sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(ident(k)) for k in upd)
            )
        else:
            q = q + sql.SQL(" ON CONFLICT DO NOTHING")
        try:
            async with c.transaction():
                n = await run(c, q, list(row.values()))
        except psycopg.errors.UniqueViolation:  # replace: the idem key belongs to another row
            n = 0
        ins += n
        skip += 1 - n
    if t in IDENTITY:
        seq = (await one(c, "SELECT pg_get_serial_sequence(%s, 'id') AS s", (t,)) or {})["s"]
        top = (await one(c, sql.SQL("SELECT COALESCE(MAX(id), 0) AS m FROM {}").format(ident(t))) or {})["m"]
        st = await one(c, sql.SQL("SELECT last_value, is_called FROM {}").format(sql.SQL(seq)))
        nxt = (st["last_value"] + 1) if st and st["is_called"] else (st["last_value"] if st else 1)
        if top >= nxt:
            await c.execute("SELECT setval(%s::regclass, %s, true)", (seq, top))
    return {"inserted": ins, "skipped": skip}


async def dump(c: psycopg.AsyncConnection, t: str, after: Any, limit: int) -> dict[str, Any]:
    if t not in COLS:
        raise BadRequest("unknown table")
    pk = PK[t]
    limit = max(1, min(int(limit or 1000), 5000))
    if after is None:
        q = sql.SQL("SELECT * FROM {} ORDER BY {} LIMIT %s").format(ident(t), ident(pk))
        got = await rows(c, q, (limit,))
    else:
        q = sql.SQL("SELECT * FROM {} WHERE {} > %s ORDER BY {} LIMIT %s").format(ident(t), ident(pk), ident(pk))
        got = await rows(c, q, (coerce(t, pk, after), limit))
    return {"rows": got, "next": got[-1][pk] if len(got) == limit else None}


async def counts(c: psycopg.AsyncConnection) -> dict[str, Any]:
    out = {}
    for t in COLS:
        r = await one(c, sql.SQL("SELECT COUNT(*) AS n FROM {}").format(ident(t)))
        out[t] = int((r or {}).get("n") or 0)
    return out


def glob_match(kind: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(kind, pattern)


# Approvals live in hubapprovals (protected). Imported last: it imports this module's helpers.
from hubapprovals import (  # noqa: E402, F401  (re-exported: hubd calls hs.approval_*)
    AUDIT_SQL,
    _info,
    _state,
    approval_attach,
    approval_audit,
    approval_cancel,
    approval_card,
    approval_consume,
    approval_decide,
    approval_expire,
    approval_get,
    approval_list,
    approval_request,
)
