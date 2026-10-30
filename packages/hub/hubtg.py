"""hubtg: the Telegram transport on the hub. hubd imports it and adds routes() to its table; every
SQL here runs through hubstore's transactions (one per request; rid = idempotent writes). Nothing here decides
what a row means: the adapter (tg caller) writes what Telegram sent, the brain's bridge and scripts read and ack.

tg caller (the adapter in claude-hub-tg):
  POST /tg/rows              {rows:[{ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key,
                             status, acked_ts, ack_by, binding_receipt}], rid} -> {ids} (same order). Kinds tg.*,
                             outbox.*, approval.*; a known idem_key answers its id and changes nothing; binding_receipt
                             also writes the deliveries row (how 'binding'). Wakes /tg/inbound/poll.
  POST /tg/rows/patch        {idem_key, payload_patch, status?, rid} -> {id}: top-level payload merge, updated_ts
  POST /tg/meta              {set:{k: v}, rid} -> {ok, n}: keys dropped:* offset:* compat_reads:* tg.*
  POST /tg/jobs              {kind: bind|transcribe|scan, args, timeout_s ≤ 300, rid} -> {id, state}; 'unclaimed'
                             at once when no companion polled /tg/jobs/claim within 40 s
  POST /tg/jobs/<id>/wait    {wait ≤ 25} -> {id, kind, state, result?, error?}; queued 5 s -> unclaimed; running past
                             timeout_s + 10 s -> expired
  GET  /tg/presence?after=V&wait=S -> {version, state}: at once when version > after
brain caller:
  POST /tg/inbound/poll      {after, limit ≤ 500, wait ≤ 25} -> {rows: InRow[]} (the bridge's rowsAfter)
  POST /tg/inbound/unreceipted {since_ts, max_id} -> {rows}  · GET /tg/inbound/<id> -> {row} · GET /tg/inbound/max
  GET  /tg/latest-delivered?chat=C&since_ts=T -> {row: {id, trace_id} | null}
  POST /tg/deliveries/sent {event_id, ts?, rid} -> {tries} · POST /tg/deliveries/receipts {ids} -> {receipts}
  GET  /tg/deliveries/seen?since_ts=T -> {seen}
  POST /tg/deliveries/receipt {event_id, ts, how?, kinds?, message_id?, rid} -> {ok, result, first} (receipts.py)
  POST /tg/bridge-event      {kind: bridge.*, subject, payload, labels, delivery, idem_key, trace_id?, rid} -> {id|null}
  POST /tg/jobs/claim        {kinds, wait ≤ 25} -> {job | null} · POST /tg/jobs/<id>/result {ok, result, error?, rid}
  POST /tg/presence          {state} -> {version}
  POST /tg/q                 {name, params} -> {rows, truncated}: the brain-side readers' queries (QUERIES)
  POST /tg/bindings/upsert   {rows, rid} -> {ids} · POST /tg/bindings/retire {id | kind+key, rid} -> {n}
  POST /tg/prune-text        {before_ts, keep_keys, now?, limit?, rid?} -> {n, more} (tgmetrics' 30-day null-out)
both:
  GET  /tg/bindings          -> {version, rows}: every binding but kind 'card' (the adapter keeps card binds itself)
  GET  /tg/meta?prefix=P     -> {meta}: the keys above
  GET  /tg/files/<in|out>/<name> (raw bytes) · POST /tg/files (raw body ≤ 50 MB, X-File-Name) -> {ref, size, sha256}
Files live in HUB_TGFILES (default /srv/tgfiles; shared with claude-hub-tg): in/ = the adapter's downloads, out/ =
uploads (named <sha256[:32]>-<safe basename>, same bytes = same ref). A ref is "hubfile:in/<name>" or
"hubfile:out/<name>". Without the folder (before the owner's tg paste) file calls answer 503 "files dir missing".
Files and jobs older than 7 days / 1 day are pruned hourly. Presence and the companion's last poll are in memory
(a restart counts as a fresh poll: 40 s of grace). Long polls cap at 25 s; hubd gives them OP_TIMEOUT + 27 s.
The log and /health hold ids, counts and states only: never text, payloads or file contents.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import time
import urllib.parse
from typing import Any

import hubstore as hs

CHANNEL_KINDS = (
    "tg.message",
    "tg.button",
    "tg.reaction",
    "tg.poll_answer",
    "tg.stop",
    "tg.bound",
    "approval.decided",
    "bridge.handoff",
)  # fmt: skip  (= telegram-plus bridgedb.ts CHANNEL_KINDS)
TG_KIND = re.compile(r"^(tg|outbox|approval)\.[a-z0-9][a-z0-9._-]*$")
BRIDGE_KIND = re.compile(r"^bridge\.[a-z0-9][a-z0-9._-]*$")
STATUSES = ("new", "listed", "acked", "dismissed", "gone")
META_PREFIXES = ("dropped:", "offset:", "compat_reads:", "tg.")
# the adapter's counters name their reason in words ("dropped:bound busy"): any printable text after the prefix
META_KEY = re.compile(r"^(dropped:|offset:|compat_reads:|tg\.)[^\x00-\x1f\x7f]{1,120}$")
JOB_KINDS = ("bind", "transcribe", "scan")
BIND_KINDS = ("command", "button", "prefix")
LONG_MAX = 25.0
UNCLAIMED_S, COMPANION_SEEN_S, EXPIRE_GRACE = 5.0, 40.0, 10.0
CLAIM_RECHECK_S = 0.5
with contextlib.suppress(KeyError, ValueError):  # tests: HUB_TG_TIMES="unclaimed,seen,grace" (seconds)
    UNCLAIMED_S, COMPANION_SEEN_S, EXPIRE_GRACE = (float(x) for x in os.environ["HUB_TG_TIMES"].split(","))
JOB_KEEP = 86_400
FILES_MAX = 50 << 20
FILES_KEEP = 7 * 86_400
TMP_KEEP = 3_600
ROWS_MAX = 500
PAYLOAD_MAX = 256 << 10
OPAQUE_MAX = 1 << 20
PRESENCE_MAX = 64 << 10
Q_MAX = 20_000
FILE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,119}$")
INROW = (
    "SELECT e.id, e.ts, e.kind, e.payload, e.trace_id, e.status, d.tries, d.receipt_ts"
    " FROM events e LEFT JOIN deliveries d ON d.event_id = e.id"
)


class NotFound(Exception):
    pass


class Wake:
    """Wake every waiter at once (a fresh event per round, so a wait never misses a fire between query and wait)."""

    def __init__(self) -> None:
        self.ev: asyncio.Event | None = None

    def current(self) -> asyncio.Event:
        if self.ev is None:
            self.ev = asyncio.Event()
        return self.ev

    def fire(self) -> None:
        if self.ev is not None:
            self.ev.set()
        self.ev = None


async def _sleep_on(ev: asyncio.Event, secs: float) -> None:
    if secs <= 0:
        return
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(ev.wait(), secs)


class State:
    def __init__(self) -> None:
        self.rows = Wake()
        self.jobs = Wake()
        self.job: dict[int, Wake] = {}
        self.started = time.time()
        self.seen: dict[str, float] = {}  # schema -> the companion's last claim poll
        self.presence: dict[str, tuple[int, Any]] = {}
        self.pwake: dict[str, Wake] = {}

    def job_wake(self, jid: int) -> Wake:
        w = self.job.get(jid)
        if w is None:
            if len(self.job) > 5000:
                self.job.clear()
            w = self.job[jid] = Wake()
        return w

    def companion_age(self, schema: str) -> float:
        return time.time() - self.seen.get(schema, self.started)


S = State()


# ------------------------------------------------------------------------------------------------ helpers


def _num(v: Any, name: str, default: float | None = None) -> float:
    if v is None and default is not None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise hs.BadRequest(f"{name}: a number")
    return float(v)


def _int(v: Any, name: str, default: int | None = None) -> int:
    if v is None and default is not None:
        return default
    if v is None or isinstance(v, bool):
        raise hs.BadRequest(f"{name}: an integer")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise hs.BadRequest(f"{name}: an integer") from None


def _wait(v: Any) -> float:
    return max(0.0, min(_num(v, "wait", 0.0), LONG_MAX))


def _text_json(v: Any, name: str, want: type, limit: int = PAYLOAD_MAX) -> str:
    """JSON text of an object/list, given as the value or as its text (SQLite rows carry text)."""
    if isinstance(v, str):
        try:
            got = json.loads(v)
        except ValueError:
            raise hs.BadRequest(f"{name}: JSON text") from None
        if not isinstance(got, want):
            raise hs.BadRequest(f"{name}: a JSON {want.__name__}")
        out = v
    elif isinstance(v, want):
        out = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    elif v is None:
        out = "{}" if want is dict else "[]"
    else:
        raise hs.BadRequest(f"{name}: a JSON {want.__name__}")
    if len(out) > limit:
        raise hs.BadRequest(f"{name}: over {limit} bytes")
    return out


def _obj(text: Any) -> dict[str, Any]:
    try:
        v = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _str(v: Any, name: str, n: int, *, required: bool = False) -> str | None:
    if v is None or v == "":
        if required:
            raise hs.BadRequest(f"{name}: required")
        return None
    if not isinstance(v, str):
        raise hs.BadRequest(f"{name}: a string")
    if len(v) > n:
        raise hs.BadRequest(f"{name}: at most {n} characters")
    return v


def _kinds(v: Any, default: tuple[str, ...]) -> list[str]:
    if v is None:
        return list(default)
    if (
        not isinstance(v, list)
        or not v
        or len(v) > 50
        or not all(isinstance(k, str) and hs.KIND_RE.match(k) for k in v)
    ):
        raise hs.BadRequest("kinds: a list of 1-50 kind names")
    return v


def _like_prefix(p: str) -> str:
    return p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ------------------------------------------------------------------------------------------------ tg: rows, meta


def _tg_row(i: int, r: Any) -> dict[str, Any]:
    if not isinstance(r, dict):
        raise hs.BadRequest(f"rows[{i}]: an object")
    kind = r.get("kind")
    if not isinstance(kind, str) or not TG_KIND.match(kind):
        raise hs.BadRequest(f"rows[{i}].kind: tg.*, outbox.* or approval.* only")
    delivery = r.get("delivery") or "now"
    if delivery not in hs.CLASSES:
        raise hs.BadRequest(f"rows[{i}].delivery: one of {hs.CLASSES}")
    status = r.get("status") or "new"
    if status not in STATUSES:
        raise hs.BadRequest(f"rows[{i}].status: one of {STATUSES} (pending rows stay with the adapter)")
    acked = r.get("acked_ts")
    return {
        "ts": _num(r.get("ts"), f"rows[{i}].ts"),
        "source": _str(r.get("source"), f"rows[{i}].source", 64) or "tg-adapter",
        "kind": kind,
        "subject": str(r.get("subject") or "")[:500],
        "next": str(r.get("next") or "")[:500],
        "payload": _text_json(r.get("payload"), f"rows[{i}].payload", dict),
        "labels": _text_json(r.get("labels"), f"rows[{i}].labels", list, 4096),
        "delivery": delivery,
        "trace_id": _str(r.get("trace_id"), f"rows[{i}].trace_id", 64),
        "idem_key": _str(r.get("idem_key"), f"rows[{i}].idem_key", 300, required=True),
        "status": status,
        "acked_ts": None if acked is None else _num(acked, f"rows[{i}].acked_ts"),
        "ack_by": _str(r.get("ack_by"), f"rows[{i}].ack_by", 40),
        "binding_receipt": bool(r.get("binding_receipt")),
    }


async def _insert_rows(c: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = []
    for r in rows:
        got = await hs.one(
            c,
            "INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, status,"
            " acked_ts, ack_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (idem_key) DO NOTHING RETURNING id",
            (r["ts"], r["source"], r["kind"], r["subject"], r["next"], r["payload"], r["labels"], r["delivery"],
             r["trace_id"], r["idem_key"], r["status"], r["acked_ts"], r["ack_by"]),
        )  # fmt: skip
        if got is None:
            got = await hs.one(c, "SELECT id FROM events WHERE idem_key=%s", (r["idem_key"],))
            if got is None:
                raise hs.Conflict("a concurrent insert of the same idem_key: retry")
        if r["binding_receipt"]:
            await hs.run(
                c,
                "INSERT INTO deliveries(event_id, sent_ts, tries, receipt_ts, how) VALUES (%s, %s, 0, %s, 'binding')"
                " ON CONFLICT (event_id) DO NOTHING",
                (got["id"], r["ts"], r["ts"]),
            )
        ids.append(got["id"])
    return {"ids": ids}


async def r_rows(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    rows = a.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > ROWS_MAX:
        raise hs.BadRequest(f"rows: a list of 1-{ROWS_MAX}")
    norm = [_tg_row(i, r) for i, r in enumerate(rows)]
    async with h.store.tx(s) as c:
        res = await hs.once(c, a.get("rid"), who, "tg.rows", lambda: _insert_rows(c, norm))
    S.rows.fire()
    return res


async def r_rows_patch(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    key = _str(a.get("idem_key"), "idem_key", 300, required=True)
    patch = a.get("payload_patch") or {}
    if not isinstance(patch, dict):
        raise hs.BadRequest("payload_patch: an object")
    status = a.get("status")
    if status is not None and status not in STATUSES:
        raise hs.BadRequest(f"status: one of {STATUSES}")

    async def do() -> dict[str, Any]:
        r = await hs.one(c, "SELECT id, kind, payload FROM events WHERE idem_key=%s FOR UPDATE", (key,))
        if r is None:
            raise NotFound("no row with that idem_key (forward it first)")
        if not TG_KIND.match(r["kind"]):
            raise hs.Forbidden("not a transport row")
        p = _obj(r["payload"])
        p.update(patch)
        text = json.dumps(p, ensure_ascii=False, separators=(",", ":"))
        if len(text) > PAYLOAD_MAX:
            raise hs.BadRequest("payload over the size cap")
        if status is None:
            await hs.run(c, "UPDATE events SET payload=%s, updated_ts=%s WHERE id=%s", (text, time.time(), r["id"]))
        else:
            await hs.run(
                c,
                "UPDATE events SET payload=%s, status=%s, updated_ts=%s WHERE id=%s",
                (text, status, time.time(), r["id"]),
            )
        return {"id": r["id"]}

    async with h.store.tx(s) as c:
        res = await hs.once(c, a.get("rid"), who, "tg.patch", do)
    S.rows.fire()
    return res


async def r_meta_set(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    got = a.get("set")
    if not isinstance(got, dict) or not got or len(got) > 200:
        raise hs.BadRequest("set: an object of 1-200 keys")
    vals = []
    for k, v in got.items():
        if not META_KEY.match(k):
            raise hs.BadRequest(f"meta key {k[:40]!r}: dropped:*, offset:*, compat_reads:* or tg.* only")
        t = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        if len(t) > 4096:
            raise hs.BadRequest(f"meta {k[:40]}: over 4096 bytes")
        vals.append((k, t))

    async def do() -> dict[str, Any]:
        for k, t in vals:
            await hs.run(
                c, "INSERT INTO meta(k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (k, t)
            )
        return {"ok": True, "n": len(vals)}

    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "tg.meta", do)


async def r_meta_get(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    p = str(q.get("prefix") or "")
    if not p.startswith(META_PREFIXES):
        raise hs.BadRequest(f"prefix: starts with one of {META_PREFIXES}")
    async with h.store.tx(s) as c:
        got = await hs.rows(
            c, "SELECT k, v FROM meta WHERE k LIKE %s ESCAPE '\\' ORDER BY k LIMIT 2000", (_like_prefix(p),)
        )
    return {"meta": {r["k"]: r["v"] for r in got}}


# ------------------------------------------------------------------------------------------------ bindings


async def _bindings(c: Any) -> dict[str, Any]:
    got = await hs.rows(c, "SELECT * FROM bindings WHERE kind != 'card' ORDER BY id")
    v = hashlib.sha256(json.dumps(got, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"version": v, "rows": got}


async def r_bindings(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await _bindings(c)


def _binding(i: int, r: Any) -> tuple[Any, ...]:
    if not isinstance(r, dict):
        raise hs.BadRequest(f"rows[{i}]: an object")
    kind = r.get("kind")
    if kind not in BIND_KINDS:
        raise hs.BadRequest(f"rows[{i}].kind: one of {BIND_KINDS}")
    on_error = r.get("on_error") or "reply"
    if on_error not in ("reply", "forward"):
        raise hs.BadRequest(f"rows[{i}].on_error: reply or forward")
    exp = r.get("expires")
    return (
        kind,
        _str(r.get("key"), f"rows[{i}].key", 64, required=True),
        _str(r.get("tool"), f"rows[{i}].tool", 64, required=True),
        _text_json(r.get("args"), f"rows[{i}].args", list, 4096),
        1 if r.get("note") else 0,
        on_error,
        (_str(r.get("why"), f"rows[{i}].why", 300, required=True) or "").strip(),
        _num(r.get("created"), f"rows[{i}].created", time.time()),
        None if exp is None else _num(exp, f"rows[{i}].expires"),
    )


async def r_bind_upsert(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    rows = a.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        raise hs.BadRequest("rows: a list of 1-500")
    vals = [_binding(i, r) for i, r in enumerate(rows)]

    async def do() -> dict[str, Any]:
        ids = []
        for v in vals:
            got = await hs.one(
                c,
                "INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1) ON CONFLICT (kind, key) DO UPDATE SET"
                " tool = EXCLUDED.tool, args = EXCLUDED.args, note = EXCLUDED.note, on_error = EXCLUDED.on_error,"
                " why = EXCLUDED.why, created = EXCLUDED.created, expires = EXCLUDED.expires, active = 1 RETURNING id",
                v,
            )
            ids.append(got["id"] if got else None)
        return {"ids": ids}

    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "tg.bind", do)


async def r_bind_retire(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    async def do() -> dict[str, Any]:
        if a.get("id") is not None:
            n = await hs.run(c, "UPDATE bindings SET active = 0 WHERE id = %s AND active = 1", (_int(a["id"], "id"),))
        else:
            kind = a.get("kind")
            if kind not in BIND_KINDS + ("card",):
                raise hs.BadRequest("id, or kind + key")
            key = _str(a.get("key"), "key", 64, required=True)
            n = await hs.run(
                c, "UPDATE bindings SET active = 0 WHERE kind = %s AND key = %s AND active = 1", (kind, key)
            )
        return {"n": n}

    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "tg.retire", do)


# ------------------------------------------------------------------------------------------------ bridge (brain)


async def r_poll(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    after = _int(a.get("after"), "after", 0)
    limit = max(1, min(_int(a.get("limit"), "limit", 200), ROWS_MAX))
    end = time.monotonic() + _wait(a.get("wait"))
    while True:
        ev = S.rows.current()
        async with h.store.tx(s) as c:
            got = await hs.rows(
                c,
                INROW + " WHERE e.id > %s AND e.delivery = 'now' AND e.kind = ANY(%s) ORDER BY e.id LIMIT %s",
                (after, list(CHANNEL_KINDS), limit),
            )
        left = end - time.monotonic()
        if got or left <= 0:
            return {"rows": got}
        await _sleep_on(ev, left)


async def r_unreceipted(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        got = await hs.rows(
            c,
            INROW + " WHERE e.id <= %s AND e.ts >= %s AND e.delivery = 'now' AND e.kind = ANY(%s)"
            " AND e.status NOT IN ('acked', 'dismissed', 'gone') AND d.receipt_ts IS NULL ORDER BY e.id LIMIT 5000",
            (_int(a.get("max_id"), "max_id"), _num(a.get("since_ts"), "since_ts"), list(CHANNEL_KINDS)),
        )
    return {"rows": got}


async def r_inrow(h: Any, who: str, s: str, a: dict, q: dict, eid: str) -> Any:
    async with h.store.tx(s) as c:
        r = await hs.one(
            c, INROW + " WHERE e.id = %s AND e.delivery = 'now' AND e.kind = ANY(%s)", (int(eid), list(CHANNEL_KINDS))
        )
    return {"row": r}


async def r_max(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        r = await hs.one(c, "SELECT COALESCE(MAX(id), 0) AS m FROM events")
    return {"id": int((r or {}).get("m") or 0)}


async def r_latest(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    chat = str(q.get("chat") or "")
    if not chat:
        raise hs.BadRequest("chat: required")
    try:
        since = float(q.get("since_ts") or 0)
    except ValueError:
        raise hs.BadRequest("since_ts: a number") from None
    async with h.store.tx(s) as c:
        got = await hs.rows(
            c,
            "SELECT e.id, e.trace_id, e.payload FROM events e JOIN deliveries d ON d.event_id = e.id"
            " WHERE d.sent_ts >= %s AND e.kind = ANY(%s) ORDER BY e.id DESC LIMIT 500",
            (since, list(CHANNEL_KINDS)),
        )
    for r in got:
        cid = _obj(r["payload"]).get("chat_id")
        if cid is not None and str(cid) == chat:
            return {"row": {"id": r["id"], "trace_id": r["trace_id"]}}
    return {"row": None}


async def r_sent(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    eid = _int(a.get("event_id"), "event_id")
    ts = _num(a.get("ts"), "ts", time.time())

    async def do() -> dict[str, Any]:
        r = await hs.one(
            c,
            "INSERT INTO deliveries(event_id, sent_ts, tries) VALUES (%s, %s, 1) ON CONFLICT (event_id)"
            " DO UPDATE SET sent_ts = EXCLUDED.sent_ts, tries = deliveries.tries + 1 RETURNING tries",
            (eid, ts),
        )
        return {"tries": int((r or {}).get("tries") or 1)}

    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "tg.sent", do)


async def r_receipts(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    ids = a.get("ids")
    if not isinstance(ids, list) or len(ids) > 1000:
        raise hs.BadRequest("ids: a list of at most 1000")
    want = [_int(i, "ids[]") for i in ids]
    if not want:
        return {"receipts": {}}
    async with h.store.tx(s) as c:
        got = await hs.rows(c, "SELECT event_id, receipt_ts FROM deliveries WHERE event_id = ANY(%s)", (want,))
    return {"receipts": {str(r["event_id"]): r["receipt_ts"] for r in got}}


async def r_seen(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    try:
        since = float(q.get("since_ts") or 0)
    except ValueError:
        raise hs.BadRequest("since_ts: a number") from None
    async with h.store.tx(s) as c:
        r = await hs.one(
            c, "SELECT 1 AS x FROM deliveries WHERE receipt_ts IS NOT NULL AND receipt_ts >= %s LIMIT 1", (since,)
        )
    return {"seen": r is not None}


async def r_receipt(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    """packages/inbox/receipts.py _receipt(): the first receipt wins; an open row without a note gets acked."""
    eid = _int(a.get("event_id"), "event_id")
    ts = _num(a.get("ts"), "ts", time.time())
    how = _str(a.get("how"), "how", 20) or "channel"
    kinds = _kinds(a.get("kinds"), CHANNEL_KINDS)
    mid = a.get("message_id")

    async def do() -> dict[str, Any]:
        r = await hs.one(c, "SELECT kind, status, note_path, payload FROM events WHERE id=%s FOR UPDATE", (eid,))
        if r is None:
            return {"ok": False, "result": "unknown", "first": False}
        if r["kind"] not in kinds:
            return {"ok": False, "result": "kind", "first": False}
        if mid not in (None, ""):
            pmid = _obj(r["payload"]).get("message_id")
            if pmid is not None and str(pmid) != str(mid):
                return {"ok": False, "result": "mismatch", "first": False}
        prev = await hs.one(c, "SELECT receipt_ts FROM deliveries WHERE event_id=%s", (eid,))
        await hs.run(
            c,
            "INSERT INTO deliveries(event_id, receipt_ts, how) VALUES (%s, %s, %s) ON CONFLICT (event_id) DO UPDATE"
            " SET receipt_ts = COALESCE(deliveries.receipt_ts, EXCLUDED.receipt_ts),"
            " how = COALESCE(deliveries.how, EXCLUDED.how)",
            (eid, ts, how),
        )
        if r["status"] in hs.OPEN and not r["note_path"]:
            await hs.run(
                c,
                "UPDATE events SET status='acked', acked_ts=%s, ack_by='channel'"
                " WHERE id=%s AND status IN ('new','listed')",
                (ts, eid),
            )
        return {"ok": True, "result": "ok", "first": prev is None or prev["receipt_ts"] is None}

    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "tg.receipt", do)


async def r_bridge_event(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    kind = a.get("kind")
    if not isinstance(kind, str) or not BRIDGE_KIND.match(kind):
        raise hs.BadRequest("kind: bridge.* only")
    delivery = a.get("delivery") or "now"
    if delivery not in hs.CLASSES:
        raise hs.BadRequest(f"delivery: one of {hs.CLASSES}")
    row = (
        time.time(), kind, str(a.get("subject") or "")[:500], _text_json(a.get("payload"), "payload", dict),
        _text_json(a.get("labels"), "labels", list, 4096), delivery, _str(a.get("trace_id"), "trace_id", 64),
        _str(a.get("idem_key"), "idem_key", 300, required=True),
    )  # fmt: skip

    async def do() -> dict[str, Any]:
        r = await hs.one(
            c,
            "INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, status)"
            " VALUES (%s, 'tg-bridge', %s, %s, '', %s, %s, %s, %s, %s, 'new') ON CONFLICT (idem_key) DO NOTHING"
            " RETURNING id",
            row,
        )
        return {"id": r["id"] if r else None}

    async with h.store.tx(s) as c:
        res = await hs.once(c, a.get("rid"), who, "tg.bridge", do)
    S.rows.fire()
    return res


# ------------------------------------------------------------------------------------------------ named reads (brain)


def _span(p: dict) -> tuple[float, float]:
    return _num(p.get("since"), "since"), _num(p.get("until"), "until")


QUERIES: dict[str, Any] = {
    # receipts.py _receipt (the read; the write is /tg/deliveries/receipt)
    "event_brief": lambda p: (
        "SELECT id, kind, status, note_path, payload FROM events WHERE id=%s",
        (_int(p.get("id"), "id"),),
    ),
    # receipts.undelivered / brain-watch pending(): open owner rows with no receipt, oldest first
    "undelivered": lambda p: (
        "SELECT e.id, e.kind, e.ts, e.status FROM events e LEFT JOIN deliveries d ON d.event_id = e.id"
        " WHERE e.kind = ANY(%s) AND e.delivery = 'now' AND e.status IN ('new','listed') AND e.ts <= %s"
        " AND d.receipt_ts IS NULL ORDER BY e.ts, e.id",
        (_kinds(p.get("kinds"), CHANNEL_KINDS), _num(p.get("before_ts"), "before_ts")),
    ),
    # brain-watch received_unanswered(): a receipt but no outbox row answering it
    "received_unanswered": lambda p: (
        "SELECT e.id, e.kind, e.ts FROM events e JOIN deliveries d ON d.event_id = e.id"
        " WHERE e.kind = ANY(%s) AND d.receipt_ts IS NOT NULL AND e.ts > %s AND e.ts <= %s"
        " AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.reply_to_event = e.id) ORDER BY e.ts",
        (
            _kinds(p.get("kinds"), CHANNEL_KINDS),
            _num(p.get("after_ts"), "after_ts"),
            _num(p.get("before_ts"), "before_ts"),
        ),
    ),
    # tgmetrics _rows (T2/T3)
    "channel_rows": lambda p: (
        "SELECT e.id, e.kind, e.ts, d.receipt_ts, d.how FROM events e LEFT JOIN deliveries d ON d.event_id = e.id"
        " WHERE e.kind = ANY(%s) AND e.delivery = 'now' AND e.ts >= %s AND e.ts < %s ORDER BY e.id",
        (_kinds(p.get("kinds"), CHANNEL_KINDS), *_span(p)),
    ),
    # tgmetrics _t1 (tg.button ack_ms), _t2 / _t6 (tg.bound)
    "kind_payloads": lambda p: (
        "SELECT id, payload FROM events WHERE kind = %s AND ts >= %s AND ts < %s ORDER BY id",
        (_kinds([p.get("kind")], ())[0], *_span(p)),
    ),
    # tgmetrics _t2 (update ids per bot)
    "source_payloads": lambda p: (
        "SELECT id, payload FROM events WHERE source = %s AND ts >= %s AND ts < %s ORDER BY id",
        (_str(p.get("source"), "source", 64, required=True), *_span(p)),
    ),
    # tgmetrics _t4
    "outbox_live": lambda p: (
        "SELECT id, ts, bot, chat, op, body, status, sent_ts FROM outbox WHERE mode = 'live' AND ts >= %s AND ts < %s"
        " ORDER BY id",
        _span(p),
    ),
    # tgmetrics _t4 (outbox.failed listed)
    "kind_count": lambda p: (
        "SELECT COUNT(*) AS n FROM events WHERE kind = %s AND ts >= %s AND ts < %s",
        (_kinds([p.get("kind")], ())[0], *_span(p)),
    ),
    # tg-bind list --all / export / import (filters client-side, as the SQLite code's WHERE clauses)
    "bindings_all": lambda p: ("SELECT * FROM bindings ORDER BY id", ()),
}  # fmt: skip


async def r_q(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    name = a.get("name")
    fn = QUERIES.get(name) if isinstance(name, str) else None
    if fn is None:
        raise hs.BadRequest(f"name: one of {sorted(QUERIES)}")
    p = a.get("params") or {}
    if not isinstance(p, dict):
        raise hs.BadRequest("params: an object")
    sql_text, params = fn(p)
    async with h.store.tx(s) as c:
        got = await hs.rows(c, sql_text + f" LIMIT {Q_MAX + 1}" if "COUNT(*)" not in sql_text else sql_text, params)
    return {"rows": got[:Q_MAX], "truncated": len(got) > Q_MAX}


async def r_prune_text(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    """tgmetrics.prune_text on the hub: his text out of owner Telegram rows older than before_ts (subject and next
    emptied, payload reduced to keep_keys plus _nulled). Idempotent; `more` = call again."""
    before = _num(a.get("before_ts"), "before_ts")
    keep = a.get("keep_keys") or []
    if not isinstance(keep, list) or not all(isinstance(k, str) for k in keep):
        raise hs.BadRequest("keep_keys: a list of names")
    now = _num(a.get("now"), "now", time.time())
    limit = max(1, min(_int(a.get("limit"), "limit", 2000), 5000))
    async with h.store.tx(s) as c:
        got = await hs.rows(
            c,
            "SELECT id, payload FROM events WHERE ts < %s"
            " AND (source IN ('tg-adapter', 'tg-bridge', 'voice-adapter', 'phone-adapter')"
            " OR kind LIKE 'tg.%%' OR kind IN ('bridge.handoff', 'approval.decided'))"
            " AND payload NOT LIKE '%%\"_nulled\"%%' ORDER BY id LIMIT %s",
            (before, limit),
        )
        for r in got:
            p = {k: v for k, v in _obj(r["payload"]).items() if k in keep}
            p["_nulled"] = round(now)
            await hs.run(
                c,
                "UPDATE events SET subject='', next='', payload=%s WHERE id=%s",
                (json.dumps(p, ensure_ascii=False, separators=(",", ":")), r["id"]),
            )
    return {"n": len(got), "more": len(got) == limit}


# ------------------------------------------------------------------------------------------------ companion jobs


def _job_out(r: dict[str, Any]) -> dict[str, Any]:
    out = {"id": r["id"], "kind": r["kind"], "state": r["state"]}
    if r.get("result") is not None:
        out["result"] = _obj(r["result"])
    if r.get("error"):
        out["error"] = r["error"]
    return out


async def r_job_new(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    kind = a.get("kind")
    if kind not in JOB_KINDS:
        raise hs.BadRequest(f"kind: one of {JOB_KINDS}")
    args = _text_json(a.get("args"), "args", dict, OPAQUE_MAX)
    tmo = _int(a.get("timeout_s"), "timeout_s", 60)
    if not 1 <= tmo <= 300:
        raise hs.BadRequest("timeout_s: 1-300")
    state = "queued" if S.companion_age(s) <= COMPANION_SEEN_S else "unclaimed"

    async def do() -> dict[str, Any]:
        now = time.time()
        r = await hs.one(
            c,
            "INSERT INTO tg_jobs(kind, args, timeout_s, state, created, done_ts) VALUES (%s, %s, %s, %s, %s, %s)"
            " RETURNING id, state",
            (kind, args, tmo, state, now, None if state == "queued" else now),
        )
        if r is None:
            raise RuntimeError("tg_jobs: the insert returned no row")
        return {"id": r["id"], "state": r["state"]}

    async with h.store.tx(s) as c:
        res = await hs.once(c, a.get("rid"), who, "tg.job", do)
    if res.get("state") == "queued":
        S.jobs.fire()
    return res


async def _settle(c: Any, r: dict[str, Any], now: float) -> dict[str, Any]:
    """Time-based states: queued past UNCLAIMED_S -> unclaimed; running past timeout + grace -> expired."""
    if r["state"] == "queued" and now - r["created"] > UNCLAIMED_S:
        await hs.run(
            c, "UPDATE tg_jobs SET state='unclaimed', done_ts=%s WHERE id=%s AND state='queued'", (now, r["id"])
        )
        r = {**r, "state": "unclaimed"}
    elif r["state"] == "running" and now > (r["claimed_ts"] or now) + r["timeout_s"] + EXPIRE_GRACE:
        await hs.run(
            c, "UPDATE tg_jobs SET state='expired', done_ts=%s WHERE id=%s AND state='running'", (now, r["id"])
        )
        r = {**r, "state": "expired"}
    return r


async def r_job_wait(h: Any, who: str, s: str, a: dict, q: dict, jid: str) -> Any:
    end = time.monotonic() + _wait(a.get("wait"))
    w = S.job_wake(int(jid))
    while True:
        ev = w.current()
        now = time.time()
        async with h.store.tx(s) as c:
            r = await hs.one(c, "SELECT * FROM tg_jobs WHERE id=%s FOR UPDATE", (int(jid),))
            if r is None:
                raise NotFound(f"job {jid}")
            r = await _settle(c, r, now)
        left = end - time.monotonic()
        if r["state"] not in ("queued", "running") or left <= 0:
            return _job_out(r)
        nxt = (
            r["created"] + UNCLAIMED_S
            if r["state"] == "queued"
            else (r["claimed_ts"] or now) + r["timeout_s"] + EXPIRE_GRACE
        )
        await _sleep_on(ev, min(left, max(0.05, nxt - now + 0.05)))


async def r_job_claim(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    kinds = a.get("kinds") or list(JOB_KINDS)
    if not isinstance(kinds, list) or not kinds or not all(k in JOB_KINDS for k in kinds):
        raise hs.BadRequest(f"kinds: a list of {JOB_KINDS}")
    end = time.monotonic() + _wait(a.get("wait"))
    while True:
        S.seen[s] = time.time()
        ev = S.jobs.current()
        now = time.time()
        async with h.store.tx(s) as c:
            r = await hs.one(
                c,
                "UPDATE tg_jobs SET state='running', claimed_ts=%s WHERE id = (SELECT id FROM tg_jobs WHERE"
                " state='queued' AND kind = ANY(%s) AND created >= %s ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)"
                " RETURNING id, kind, args, timeout_s, created",
                (now, kinds, now - UNCLAIMED_S),
            )
        if r is not None:
            S.job_wake(r["id"]).fire()
            S.seen[s] = time.time()
            return {"job": {**r, "args": _obj(r["args"])}}
        left = end - time.monotonic()
        if left <= 0:
            S.seen[s] = time.time()
            return {"job": None}
        # A queued row the adapter's wait holds (its FOR UPDATE) is skipped above, and its fire came before this round:
        # look again within CLAIM_RECHECK_S instead of sleeping the whole long poll on it.
        await _sleep_on(ev, min(left, CLAIM_RECHECK_S))


async def r_job_result(h: Any, who: str, s: str, a: dict, q: dict, jid: str) -> Any:
    ok = bool(a.get("ok"))
    result = _text_json(a.get("result"), "result", dict, OPAQUE_MAX)
    err = _str(a.get("error"), "error", 500)

    async def do() -> dict[str, Any]:
        n = await hs.run(
            c,
            "UPDATE tg_jobs SET state=%s, result=%s, error=%s, done_ts=%s WHERE id=%s AND state='running'",
            ("done" if ok else "failed", result, err, time.time(), int(jid)),
        )
        if n:
            return {"ok": True}
        r = await hs.one(c, "SELECT state FROM tg_jobs WHERE id=%s", (int(jid),))
        if r is None:
            raise NotFound(f"job {jid}")
        return {"ok": False, "state": r["state"]}

    async with h.store.tx(s) as c:
        res = await hs.once(c, a.get("rid"), who, "tg.result", do)
    S.job_wake(int(jid)).fire()
    return res


# ------------------------------------------------------------------------------------------------ presence


async def r_presence_set(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    st = a.get("state")
    if not isinstance(st, dict) or len(json.dumps(st)) > PRESENCE_MAX:
        raise hs.BadRequest(f"state: an object under {PRESENCE_MAX} bytes")
    prev = S.presence.get(s, (0, None))[0]
    v = max(prev + 1, int(time.time() * 1000))
    S.presence[s] = (v, st)
    S.pwake.setdefault(s, Wake()).fire()
    return {"version": v}


async def r_presence_get(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    try:
        after = int(q.get("after") or 0)
        wait = max(0.0, min(float(q.get("wait") or 0), LONG_MAX))
    except ValueError:
        raise hs.BadRequest("after, wait: numbers") from None
    end = time.monotonic() + wait
    while True:
        ev = S.pwake.setdefault(s, Wake()).current()
        v, st = S.presence.get(s, (0, None))
        left = end - time.monotonic()
        if v > after or left <= 0:
            return {"version": v, "state": st}
        await _sleep_on(ev, left)


# ------------------------------------------------------------------------------------------------ files (raw HTTP)


def files_dir() -> str:
    return os.environ.get("HUB_TGFILES", "/srv/tgfiles")


def _safe_name(raw: str) -> str:
    base = os.path.basename(urllib.parse.unquote(raw or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip("._-")[:80]
    return base or "file"


async def _send(w: Any, status: int, obj: Any, keep: bool) -> None:
    data = json.dumps(obj, separators=(",", ":")).encode()
    head = (
        f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\nConnection: {'keep-alive' if keep else 'close'}\r\n\r\n"
    )
    w.write(head.encode() + data)
    await w.drain()


async def files_raw(method: str, target: str, headers: dict[str, str], reader: Any, w: Any, who: str | None) -> bool:
    """GET /tg/files/<in|out>/<name> and POST /tg/files, streamed. -> keep the connection alive?"""
    path = urllib.parse.urlsplit(target).path
    try:
        n = int(headers.get("content-length") or 0)
    except ValueError:
        n = -1
    keep = headers.get("connection", "").lower() != "close" and n == 0
    if who is None:
        await _send(w, 401, {"error": "unauthorized"}, keep)
        return keep
    root = files_dir()
    if method == "GET":
        m = re.fullmatch(r"/tg/files/(in|out)/([^/]+)", path)
        if m is None or not FILE_NAME.match(m.group(2)):
            await _send(w, 400, {"error": "bad_request", "hint": "GET /tg/files/<in|out>/<name>"}, keep)
            return keep
        if not os.path.isdir(root):
            await _send(w, 503, {"error": "files_unavailable", "hint": "files dir missing"}, keep)
            return keep
        p = os.path.join(root, m.group(1), m.group(2))
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            await _send(w, 404, {"error": "not_found"}, keep)
            return keep
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            await _send(w, 404, {"error": "not_found"}, keep)
            return keep
        with os.fdopen(fd, "rb") as f:
            size = st.st_size
            w.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {size}\r\n"
                f"Connection: {'keep-alive' if keep else 'close'}\r\n\r\n".encode()
            )
            while chunk := f.read(256 << 10):
                w.write(chunk)
                await w.drain()
        await w.drain()
        return keep
    if method != "POST" or path != "/tg/files":
        await _send(w, 404, {"error": "not_found"}, False)
        return False
    if n <= 0 or n > FILES_MAX:
        await _send(w, 413 if n > FILES_MAX else 400, {"error": "too_large" if n > FILES_MAX else "bad_request",
                    "hint": f"Content-Length 1-{FILES_MAX}"}, False)  # fmt: skip
        return False
    if not os.path.isdir(root):
        await _send(w, 503, {"error": "files_unavailable", "hint": "files dir missing"}, False)
        return False
    out = os.path.join(root, "out")
    os.makedirs(out, mode=0o700, exist_ok=True)
    tmp = os.path.join(out, f".tmp-{secrets.token_hex(8)}")
    h = hashlib.sha256()
    got = 0
    try:
        with open(tmp, "xb") as f:
            while got < n:
                chunk = await asyncio.wait_for(reader.read(min(256 << 10, n - got)), 30)
                if not chunk:
                    raise ConnectionError("short body")
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
        sha = h.hexdigest()
        name = f"{sha[:32]}-{_safe_name(headers.get('x-file-name', ''))}"
        dest = os.path.join(out, name)
        if os.path.exists(dest):
            os.unlink(tmp)
            os.utime(dest)
        else:
            os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    keep = headers.get("connection", "").lower() != "close"
    await _send(w, 200, {"ref": f"hubfile:out/{name}", "size": got, "sha256": sha}, keep)
    return keep


def prune_files(now: float | None = None) -> int:
    now = time.time() if now is None else now
    root, n = files_dir(), 0
    for sub in ("in", "out"):
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for e in os.scandir(d):
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                age = now - e.stat(follow_symlinks=False).st_mtime
                if age > FILES_KEEP or (e.name.startswith(".tmp-") and age > TMP_KEEP):
                    os.unlink(e.path)
                    n += 1
            except OSError:
                continue
    return n


async def housekeeping(hub: Any, stopping: asyncio.Event, every: float = 3600.0) -> None:
    """Hourly: old files (7 d) and jobs (1 d) out. Never fatal."""
    while not stopping.is_set():
        try:
            n = await asyncio.get_running_loop().run_in_executor(None, prune_files)
            jobs = 0
            if hub.store is not None:
                async with hub.store.tx(hs.LIVE) as c:
                    jobs = await hs.run(c, "DELETE FROM tg_jobs WHERE created < %s", (time.time() - JOB_KEEP,))
            if n or jobs:
                print(json.dumps({"event": "tg_prune", "files": n, "jobs": jobs}), flush=True)
        except Exception as e:  # the store may be down: next hour
            print(json.dumps({"event": "tg_prune_failed", "error": type(e).__name__}), flush=True)
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(stopping.wait(), every)


def brief() -> dict[str, Any]:
    """/health's "tg": states and counts only."""
    v = S.presence.get(hs.LIVE, (0, None))[0]
    return {
        "files": "ok" if os.path.isdir(files_dir()) else "missing",
        "companion_age_s": round(S.companion_age(hs.LIVE), 1) if hs.LIVE in S.seen else None,
        "presence": bool(v),
    }


LONG = frozenset({"r_poll", "r_job_wait", "r_job_claim", "r_presence_get"})
N_ID = r"(\d{1,18})"


def routes(
    brain: frozenset[str], tg: frozenset[str], both: frozenset[str]
) -> list[tuple[str, str, Any, frozenset[str]]]:
    return [
        ("POST", r"/tg/rows", r_rows, tg),
        ("POST", r"/tg/rows/patch", r_rows_patch, tg),
        ("POST", r"/tg/meta", r_meta_set, tg),
        ("GET", r"/tg/meta", r_meta_get, both),
        ("GET", r"/tg/bindings", r_bindings, both),
        ("POST", r"/tg/bindings/upsert", r_bind_upsert, brain),
        ("POST", r"/tg/bindings/retire", r_bind_retire, brain),
        ("POST", r"/tg/jobs", r_job_new, tg),
        ("POST", rf"/tg/jobs/{N_ID}/wait", r_job_wait, tg),
        ("POST", r"/tg/jobs/claim", r_job_claim, brain),
        ("POST", rf"/tg/jobs/{N_ID}/result", r_job_result, brain),
        ("POST", r"/tg/presence", r_presence_set, brain),
        ("GET", r"/tg/presence", r_presence_get, tg),
        ("POST", r"/tg/inbound/poll", r_poll, brain),
        ("POST", r"/tg/inbound/unreceipted", r_unreceipted, brain),
        ("GET", r"/tg/inbound/max", r_max, brain),
        ("GET", rf"/tg/inbound/{N_ID}", r_inrow, brain),
        ("GET", r"/tg/latest-delivered", r_latest, brain),
        ("POST", r"/tg/deliveries/sent", r_sent, brain),
        ("POST", r"/tg/deliveries/receipts", r_receipts, brain),
        ("GET", r"/tg/deliveries/seen", r_seen, brain),
        ("POST", r"/tg/deliveries/receipt", r_receipt, brain),
        ("POST", r"/tg/bridge-event", r_bridge_event, brain),
        ("POST", r"/tg/q", r_q, brain),
        ("POST", r"/tg/prune-text", r_prune_text, brain),
    ]
