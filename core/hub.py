"""core.hub: the brain's way into the hub store. Stdlib only (core.http); no database driver here.

Stores: the events ledger, the outbox, approvals and the Telegram transport (tg.*, outbox.*, approval.* events,
deliveries, bindings; the adapter runs in the hub) all live on the hub.
  backend(store) -> "hub" | "sqlite"          tg_hub() -> bool   (backend("tg") == "hub")
  "sqlite" only where no hub is configured (no HUB_URL and no hub token file: test runs with a temp HOME): the
  in-container SQLite store stands in for it.

Client: HUB_URL (default http://claude-hub:8700; tests must set it), the brain's token (HUB_TOKEN_FILE, default
~/.secrets/hub-token: read here, never printed), HUB_SCHEMA (a t_* test schema; unset = live).
  call(method, path, body=None, timeout=5.0) -> dict      raises HubDown (unreachable, 5xx, timeout), Conflict (409),
                                                         Refused (any other 4xx: code + hint)
  new_rid()                                              a request id: the hub answers a repeated rid from its record

Spool (hub down -> spool, ordered replay): typed writes (emit, update, outbox, receipt) that could not reach the hub
wait in ~/.local/state/assistant/hub-spool.db and replay in order; while it holds rows, new writes queue behind them.
  write(op, body)  -> dict | None     None = spooled;  drain(timeout) -> (replayed, left)

Transport (the hub's /tg/* API):
  upload(path) -> "hubfile:out/<name>"       a local file into the hub's tgfiles (the adapter sends it from there)
  receipt(event_id, ts, how, message_id)     a delivery receipt (+ ack); spooled when the hub is down
  tg_q(name, **params) -> list[dict]         one reviewed transport read (POST /tg/q)
  tg_meta(prefix) -> {k: v}                  the adapter's meta counters on the hub (GET /tg/meta)
  tg_journal(since, until=None) -> [entry]   the hub's owner rows as 0.1.22 inbound-journal entries (the hub's adapter
                                             writes no inbound journal: sleepin, menu-usage, usage, webagent read this)

Ledger units of work: ledger(profile, ...) -> a LedgerConnection, an in-memory SQLite database with the events
store's DDL, filled from the hub's snapshot. The ledger code runs on it unchanged; close() (or commit()) sends its
changes to the hub in one atomic apply (every update carries the old row; a row that moved meanwhile = Conflict,
nothing written). New deliveries rows are not ledger rows (the /tg/deliveries API writes them): they are dropped with
a log line. Snapshot or apply failures surface as sqlite3.OperationalError, like a locked store.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from core import clock, db, locks, paths

__all__ = [
    "STORES",
    "TRANSPORT",
    "Conflict",
    "HubDown",
    "HubError",
    "JOURNAL_KINDS",
    "LedgerConnection",
    "Refused",
    "backend",
    "call",
    "drain",
    "is_transport",
    "journal_entry",
    "ledger",
    "new_rid",
    "spool_depth",
    "tg_hub",
    "tg_journal",
    "tg_meta",
    "tg_q",
    "unit",
    "upload",
    "upload_files",
    "receipt",
    "write",
]

STORES = ("events", "outbox", "approvals", "tg")
TRANSPORT = ("tg.", "outbox.", "approval.")  # the Telegram transport's rows (the tg-adapter's)
DEFAULT_URL = "http://claude-hub:8700"
LEDGER_TABLES = ("events", "beats", "wake_rules", "meta")
SPOOL_OPS = {
    "emit": "/events/emit",
    "update": "/events/update",
    "outbox": "/outbox",
    "receipt": "/tg/deliveries/receipt",
}
HUBFILE = "hubfile:"
UPLOAD_MAX = 50 << 20  # the Bot API's upload limit


class HubError(Exception):
    pass


class HubDown(HubError):
    """The hub did not answer (unreachable, 5xx, timeout): writes spool, reads fail open."""


class Conflict(HubError):
    """409: the rows moved since the snapshot; run the unit again."""


class Refused(HubError):
    def __init__(self, code: int, err: str, hint: str = "") -> None:
        super().__init__(f"{code} {err}" + (f": {hint}" if hint else ""))
        self.code, self.err, self.hint = code, err, hint


# ------------------------------------------------------------------------------------------------ store


def backend(store: str) -> str:
    """ "hub"; "sqlite" only where no hub is configured (no HUB_URL, no hub token file)."""
    if store not in STORES:
        raise ValueError(f"store must be one of {STORES}")
    if os.environ.get("HUB_URL", "").strip():
        return "hub"
    try:
        return "hub" if _token_file().exists() else "sqlite"
    except OSError:
        return "hub"


def _token_file() -> Path:
    p = os.environ.get("HUB_TOKEN_FILE", "")
    return paths.guard(p) if p else paths.home() / ".secrets" / "hub-token"


def tg_hub() -> bool:
    """The Telegram transport runs on the hub."""
    return backend("tg") == "hub"


def is_transport(kind: str | None) -> bool:
    return bool(kind) and str(kind).startswith(TRANSPORT)


# ------------------------------------------------------------------------------------------------ client


def url() -> str:
    u = os.environ.get("HUB_URL", "").rstrip("/")
    if u:
        return u
    if paths.test_mode():
        raise HubDown("ASSISTANT_TEST=1: set HUB_URL (tests never reach the real hub)")
    return DEFAULT_URL


def _token() -> str:
    try:
        t = _token_file().read_text(encoding="utf-8").strip()
    except OSError:
        raise HubDown("no hub token") from None
    if not t:
        raise HubDown("empty hub token")
    return t


def new_rid() -> str:
    import uuid  # late: the hook imports this module on every prompt

    return "b-" + uuid.uuid4().hex


def call(method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 5.0) -> dict[str, Any]:
    import urllib.error  # late: keep the per-prompt hook's imports small while the hub is off

    from core import http

    headers = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}
    schema = os.environ.get("HUB_SCHEMA", "")
    if schema:
        headers["X-Hub-Schema"] = schema
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    try:
        with http.open(url() + path, data=data, headers=headers, method=method, timeout=timeout) as r:
            got = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            info = json.loads(e.read() or b"{}")
        except ValueError:
            info = {}
        e.close()
        info = info if isinstance(info, dict) else {}
        err, hint = str(info.get("error") or "error"), str(info.get("hint") or "")
        if e.code == 409:
            raise Conflict(hint or err) from None
        if e.code >= 500:
            raise HubDown(f"{e.code} {err}") from None
        raise Refused(e.code, err, hint) from None
    except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError: an answer that is not JSON
        raise HubDown(type(getattr(e, "reason", e)).__name__) from None
    return got if isinstance(got, dict) else {"value": got}


# ------------------------------------------------------------------------------------------------ spool


def _spool_path() -> str:
    return os.fspath(paths.state("hub-spool.db"))


def _spool() -> sqlite3.Connection:
    old = os.umask(0o077)
    try:
        os.makedirs(os.path.dirname(_spool_path()), mode=0o700, exist_ok=True)
        con = sqlite3.connect(_spool_path(), timeout=5, isolation_level=None)
    finally:
        os.umask(old)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS spool(id INTEGER PRIMARY KEY, ts REAL NOT NULL, op TEXT NOT NULL,"
        " body TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, last_error TEXT)"
    )
    return con


def spool_depth() -> int:
    if not os.path.exists(_spool_path()):
        return 0
    con = _spool()
    try:
        return int(con.execute("SELECT COUNT(*) FROM spool").fetchone()[0])
    finally:
        con.close()


def _spool_put(op: str, body: dict[str, Any], why: str) -> None:
    con = _spool()
    try:
        con.execute(
            "INSERT INTO spool(ts, op, body, last_error) VALUES (?, ?, ?, ?)",
            (clock.now(), op, json.dumps(body, ensure_ascii=False), why[:200]),
        )
    finally:
        con.close()


def _log(line: str) -> None:
    try:
        from core import events  # late: core.events imports this module

        events.append_private(paths.state("emit-errors.log"), f"{clock.now():.0f} hub {line}"[:400])
    except OSError:
        pass


def drain(timeout: float = 10.0) -> tuple[int, int]:
    """Replay the spool in order (one process at a time). Stops at the first row the hub cannot take yet; a row the
    hub refuses for good (4xx) is logged and dropped so it cannot block the rest. -> (replayed, left)."""
    if not os.path.exists(_spool_path()):
        return 0, 0
    end = time.monotonic() + timeout
    done = 0
    with locks.hold(paths.state("hub-spool.lock"), wait=0) as fd:
        con = _spool()
        try:
            if fd is None:  # another process drains
                return 0, int(con.execute("SELECT COUNT(*) FROM spool").fetchone()[0])
            for sid, op, body in con.execute("SELECT id, op, body FROM spool ORDER BY id").fetchall():
                if time.monotonic() > end:
                    break
                b = json.loads(body)
                try:
                    b = upload_files(b) if op == "outbox" else b
                    call("POST", SPOOL_OPS[op], b, timeout=max(1.0, min(5.0, end - time.monotonic())))
                except (HubDown, Conflict) as e:
                    con.execute("UPDATE spool SET tries = tries + 1, last_error=? WHERE id=?", (str(e)[:200], sid))
                    break
                except Refused as e:
                    _log(f"spool row {sid} ({op}) refused for good: {e}")
                con.execute("DELETE FROM spool WHERE id=?", (sid,))
                done += 1
            left = int(con.execute("SELECT COUNT(*) FROM spool").fetchone()[0])
        finally:
            con.close()
    return done, left


def write(op: str, body: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any] | None:
    """A typed ledger write with a request id: straight to the hub when the spool is empty and the hub answers;
    else into the spool behind the rows already there (None)."""
    body = {**body, "rid": body.get("rid") or new_rid()}
    if spool_depth():
        drain(timeout)
        if spool_depth():
            _spool_put(op, body, "behind the spool")
            return None
    try:
        return call("POST", SPOOL_OPS[op], upload_files(body) if op == "outbox" else body, timeout=timeout)
    except (HubDown, Conflict) as e:  # spooled as given: local paths upload again at the replay (same ref)
        _spool_put(op, body, str(e))
        _log(f"{op} spooled: {e}")
        return None


# ------------------------------------------------------------------------------------------------ ledger units of work

_AUTO = re.compile(r"\(id INTEGER PRIMARY KEY,")


def _memory_ddl() -> list[str]:
    out = []
    for stmt in db.ddl_statements(db.DDL) + db.ddl_statements(db.DDL_V2):
        if stmt.startswith("ALTER TABLE"):
            out.append(stmt)
            continue
        out.append(_AUTO.sub("(id INTEGER PRIMARY KEY AUTOINCREMENT,", stmt, count=1))
    return out


def _pk(table: str) -> str:
    return "k" if table == "meta" else "event_id" if table == "deliveries" else "id"


class LedgerConnection(sqlite3.Connection):
    """The in-memory ledger of one unit of work. close() sends its changes (once); discard() drops them."""

    _session: dict[str, Any] | None = None

    def commit_hub(self) -> dict[str, int]:
        s = self._session
        if s is None or s.get("done"):
            return {}
        s["done"] = True
        return _commit(self, s)

    def discard(self) -> None:
        if self._session is not None:
            self._session["done"] = True

    def close(self) -> None:
        try:
            if self._session is not None and not self._session.get("done"):
                try:
                    self.commit_hub()
                except Conflict as e:
                    raise sqlite3.OperationalError(f"hub: the ledger changed meanwhile ({e}); run it again") from None
                except HubError as e:
                    raise sqlite3.OperationalError(f"hub: {e}") from None
        finally:
            super().close()


def _insert(con: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = list(row)
    con.execute(
        f"INSERT OR REPLACE INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()],
    )


def ledger(
    profile: str = "full",
    *,
    names: Iterable[str] | None = None,
    ids: Iterable[int] | None = None,
    reserve: dict[str, int] | None = None,
    beats_days: float = 40.0,
    local: bool = False,
    timeout: float = 5.0,
) -> LedgerConnection:
    """A unit of work over the hub ledger (see the module doc). profile: hook | ingest | full | ids. local: unused
    (every row lives on the hub; inbox.writer still passes local=False)."""
    del local
    now = clock.now()
    body: dict[str, Any] = {"profile": profile, "reserve": reserve or {}}
    if profile == "ingest":
        body["names"] = sorted(set(names or []))
    if profile == "ids":
        body["ids"] = [int(i) for i in ids or []]
    if profile == "full":
        body["beats_since"] = now - beats_days * 86400
    try:
        snap = call("POST", "/ledger/snapshot", body, timeout=timeout)
    except HubError as e:
        raise sqlite3.OperationalError(f"hub: {e}") from None
    con = sqlite3.connect(":memory:", factory=LedgerConnection, isolation_level=None, check_same_thread=False)
    assert isinstance(con, LedgerConnection)
    con.row_factory = sqlite3.Row
    for stmt in _memory_ddl():
        con.execute(stmt)
    shadow: dict[tuple[str, Any], dict[str, Any]] = {}
    for t in LEDGER_TABLES:
        for r in (snap.get("rows") or {}).get(t) or []:
            _insert(con, t, r)
    for t in (*LEDGER_TABLES, "deliveries"):
        for r in con.execute(f"SELECT * FROM {t}"):
            d = dict(r)
            shadow[(t, d[_pk(t)])] = d
    for t, (start, _k) in (snap.get("reserved") or {}).items():
        con.execute("INSERT OR IGNORE INTO sqlite_sequence(name, seq) VALUES (?, 0)", (t,))
        con.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (int(start) - 1, t))
    con._session = {"shadow": shadow, "reserved": snap.get("reserved") or {}, "timeout": timeout}
    return con


def _changes(con: sqlite3.Connection, shadow: dict) -> tuple[list[dict], int]:
    """(hub changes, new deliveries rows): deliveries rows are no ledger rows on the hub (the /tg/deliveries API writes
    them), so new ones are only counted for _commit's log line."""
    hub_ch: list[dict] = []
    dropped = 0
    seen = set()
    for t in (*LEDGER_TABLES, "deliveries"):
        pk = _pk(t)
        for r in con.execute(f"SELECT * FROM {t}"):
            d = dict(r)
            key = (t, d[pk])
            seen.add(key)
            if key in shadow:
                old = shadow[key]
                diff = {k: v for k, v in d.items() if old.get(k) != v or type(old.get(k)) is not type(v)}
                diff = {k: v for k, v in diff.items() if not (isinstance(v, (int, float)) and old.get(k) == v)}
                if diff:
                    hub_ch.append({"t": t, "op": "upd", "pk": d[pk], "set": diff, "old": old})
            elif t == "deliveries":
                dropped += 1
            else:
                hub_ch.append({"t": t, "op": "ins", "pk": d[pk], "row": d})
    for key, old in shadow.items():
        if key not in seen:
            hub_ch.append({"t": key[0], "op": "del", "pk": key[1], "old": old})
    return hub_ch, dropped


def _commit(con: sqlite3.Connection, s: dict[str, Any]) -> dict[str, int]:
    hub_ch, dropped = _changes(con, s["shadow"])
    out = {"hub": 0, "local": 0}
    if dropped:
        _log(f"{dropped} new deliveries row(s) of a ledger unit not applied (the /tg/deliveries API writes them)")
    if hub_ch:
        call("POST", "/ledger/apply", {"changes": hub_ch, "rid": new_rid()}, timeout=s["timeout"])
        out["hub"] = len(hub_ch)
    return out


def unit(fn: Callable[[LedgerConnection], Any], *, tries: int = 3, **kw: Any) -> Any:
    """Run fn on a fresh ledger(**kw) and commit; on a Conflict, run it again (up to `tries` times)."""
    last: Exception | None = None
    for _ in range(max(1, tries)):
        con = ledger(**kw)
        try:
            res = fn(con)
            con.commit_hub()
            return res
        except Conflict as e:
            last = e
        finally:
            con.discard()
            sqlite3.Connection.close(con)
    raise sqlite3.OperationalError(f"hub: the ledger kept changing ({last}); run it again")


# ------------------------------------------------------------------------------------------------ transport
# The Telegram transport lives on the hub: the adapter runs in claude-hub-tg; the brain's readers and
# writers (receipts, brain-watch, tgmetrics, tg-bind, core.tg) use these calls. Ids, kinds, times and counts travel;
# nothing here logs a row's text.

_FILE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def upload(path: str | os.PathLike[str], *, timeout: float = 60.0) -> str:
    """A local file into the hub's tgfiles (POST /tg/files) -> its ref "hubfile:out/<name>" (same bytes, same ref).
    Refs pass through unchanged. Raises Refused (bad file, too big) or HubDown."""
    p = os.fspath(path)
    if p.startswith(HUBFILE):
        return p
    try:
        size = os.path.getsize(p)
        if size > UPLOAD_MAX:
            raise Refused(413, "too_big", f"{size} bytes is over the {UPLOAD_MAX >> 20} MB Bot API limit")
        with open(p, "rb") as f:
            data = f.read()
    except OSError as e:
        raise Refused(400, "unreadable", f"{type(e).__name__}") from None
    import urllib.error

    from core import http

    name = _FILE_NAME.sub("_", os.path.basename(p))[-100:].strip("._") or "file"
    headers = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/octet-stream", "X-File-Name": name}
    schema = os.environ.get("HUB_SCHEMA", "")
    if schema:
        headers["X-Hub-Schema"] = schema
    try:
        with http.open(url() + "/tg/files", data=data, headers=headers, method="POST", timeout=timeout) as r:
            got = json.load(r)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        if code >= 500:
            raise HubDown(f"{code} upload") from None
        raise Refused(code, "upload refused") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise HubDown(type(getattr(e, "reason", e)).__name__) from None
    ref = got.get("ref") if isinstance(got, dict) else None
    if not isinstance(ref, str) or not ref.startswith(HUBFILE):
        raise HubDown("upload: no ref in the answer")
    return ref


def upload_files(row: dict[str, Any]) -> dict[str, Any]:
    """An outbox request with its local files (the files column and body.files) uploaded: refs instead of paths."""
    out = dict(row)
    if isinstance(out.get("files"), list) and out["files"]:
        out["files"] = [upload(f) for f in out["files"]]
    body = out.get("body")
    if isinstance(body, dict) and isinstance(body.get("files"), list) and body["files"]:
        out["body"] = {**body, "files": [upload(f) for f in body["files"]]}
    return out


def receipt(
    event_id: int,
    ts: float,
    how: str = "channel",
    *,
    message_id: str | None = None,
    kinds: Sequence[str] | None = None,
    timeout: float = 0.3,
) -> dict[str, Any] | None:
    """One delivery receipt on the hub (receipt_ts kept from the first one, the row acked by 'channel' when it was
    new|listed; the hub checks the kind against `kinds` (default: its channel kinds) and, when given, the message id).
    None = spooled (hub down or slow)."""
    body: dict[str, Any] = {"event_id": int(event_id), "ts": ts, "how": how, "rid": new_rid()}
    if message_id:
        body["message_id"] = str(message_id)
    if kinds:
        body["kinds"] = list(kinds)
    return write("receipt", body, timeout=timeout)


def tg_q(name: str, *, timeout: float = 5.0, **params: Any) -> list[dict[str, Any]]:
    """One reviewed transport read (POST /tg/q: event_brief, undelivered, received_unanswered, channel_rows,
    kind_payloads, source_payloads, outbox_live, kind_count, bindings_all; hubd's docstring has their params)."""
    got = call("POST", "/tg/q", {"name": name, "params": params}, timeout=timeout)
    return [r for r in got.get("rows") or [] if isinstance(r, dict)]


# hub row kind -> the telegram-plus 0.1.22 journal kind (adapters/tg/src/compat.ts JOURNAL_KIND; bridge.handoff = the
# relay's voice/app hand-off, which 0.1.22 journaled as a message with meta.via)
JOURNAL_KINDS: dict[str, str] = {
    "tg.message": "message",
    "tg.button": "button",
    "tg.reaction": "reaction",
    "tg.poll_answer": "poll_answer",
    "tg.stop": "stop",
    "approval.decided": "button",
    "bridge.handoff": "message",
}


def _meta_str(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (list, tuple)):
        return ",".join(_meta_str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)


def journal_entry(row_id: int, kind: str, payload: dict[str, Any], owner_id: str | None) -> dict[str, Any]:
    """One hub row as a 0.1.22 inbound-journal entry, as compat.ts journalEntry writes it (meta = the payload as
    strings minus text, bot and update_id; owner = the payload's user_id is the owner), except that only a message
    a binding answered (handled) becomes kind instant: a bound button stays a button, as the ledger readers saw it."""
    meta = {k: _meta_str(v) for k, v in payload.items() if v is not None and k not in ("text", "bot", "update_id")}
    ts = meta.get("ts", "")
    jkind = "instant" if kind == "tg.message" and meta.get("handled") else JOURNAL_KINDS.get(kind, kind)
    uid = meta.get("user_id", "")
    e: dict[str, Any] = {"id": f"hub:{int(row_id)}", "at": ts, "kind": jkind, "owner": bool(uid) and uid == owner_id}
    e["chat_id"] = meta.get("chat_id", "")
    if meta.get("message_id"):
        e["message_id"] = meta["message_id"]
    if ts:
        e["ts"] = ts
    text = payload.get("text")
    e.update(content="" if text is None else str(text), meta=meta, status="hub")
    return e


def tg_journal(
    since: float,
    until: float | None = None,
    *,
    kinds: Iterable[str] | None = None,
    owner_id: str | None = None,
    timeout: float = 3.0,
) -> list[dict[str, Any]]:
    """The hub's Telegram rows stored in [since, until) (until: now + 60 s) as telegram-plus 0.1.22
    inbound-journal entries, oldest first, so the journal's readers (sleepin, menu-usage, usage, webagent) read the
    hub with their own parsers (the hub's adapter writes no inbound journal). kinds: JOURNAL_KINDS' keys (default
    all); owner_id: default core.tg.owner_chat(). One kind_payloads read per kind; the first HubError is raised (a
    caller falls back to the file). Entry ids are "hub:<row id>"."""
    if owner_id is None:
        from core import tg

        owner_id = tg.owner_chat()
    hi = clock.now() + 60.0 if until is None else until
    got: list[tuple[int, dict[str, Any]]] = []
    for k in kinds if kinds is not None else JOURNAL_KINDS:
        if k not in JOURNAL_KINDS:
            raise ValueError(f"kind must be one of {sorted(JOURNAL_KINDS)}")
        for r in tg_q("kind_payloads", kind=k, since=float(since), until=float(hi), timeout=timeout):
            try:
                p = json.loads(r["payload"]) if isinstance(r.get("payload"), str) else r.get("payload")
                rid = int(r["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(p, dict):
                got.append((rid, journal_entry(rid, k, p, owner_id)))
    got.sort(key=lambda x: x[0])
    return [e for _, e in got]


def tg_meta(prefix: str, *, timeout: float = 5.0) -> dict[str, str]:
    """The adapter's meta rows on the hub whose key starts with prefix (dropped:, offset:, compat_reads:, tg.)."""
    import urllib.parse

    got = call("GET", "/tg/meta?" + urllib.parse.urlencode({"prefix": prefix}), timeout=timeout)
    m = got.get("meta")
    return {str(k): str(v) for k, v in m.items()} if isinstance(m, dict) else {}
