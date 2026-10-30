"""core.db: the events store (SQLite, WAL, synchronous=FULL). Stdlib only.

  connect(path=None, *, timeout=5.0, ro=False)   open (and create) the store; default core.paths.state("events.db")
  tx(con)                                         BEGIN IMMEDIATE ... COMMIT (ROLLBACK on any error)
  meta_get(con, k, default=None) / meta_set(con, k, v)   small JSON values in the meta table
  DDL, DDL_V2, SCHEMA_VERSION
  migrate(con)                                    apply DDL v2 once (connect does it; read-only connections never)

The DDL below is schemas/events.sql verbatim (a test diffs the two); it maps 1:1 to the hub's Postgres store.
Files are created under umask 077: the directory 0700, the database and its -wal/-shm 0600. Connections run
in autocommit mode (isolation_level=None): every write goes through tx(), so a writer holds the lock only briefly and
a crash before COMMIT leaves nothing behind.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from core import paths

__all__ = [
    "DDL",
    "DDL_V2",
    "SCHEMA_VERSION",
    "connect",
    "ddl_statements",
    "default_path",
    "ledger",
    "meta_get",
    "meta_set",
    "migrate",
    "tx",
]

SCHEMA_VERSION = 2

DDL = (  # schemas/events.sql verbatim (long lines split here only to fit the source width)
    "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, "
    "kind TEXT NOT NULL,\n"
    "  subject TEXT NOT NULL DEFAULT '', next TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT "
    "'{}',\n"
    "  labels TEXT NOT NULL DEFAULT '[]', delivery TEXT NOT NULL DEFAULT 'beat', trace_id TEXT, idem_key "
    "TEXT UNIQUE,\n"
    "  note_path TEXT, status TEXT NOT NULL DEFAULT 'new',  -- new|listed|acked|dismissed|gone\n"
    "  updated_ts REAL, first_beat INTEGER, first_listed_ts REAL, acked_ts REAL, ack_by TEXT, ack_note "
    "TEXT);\n"
    "CREATE INDEX IF NOT EXISTS events_status ON events(status, ts);\n"
    "CREATE TABLE IF NOT EXISTS beats(id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT, ok INTEGER, "
    "ms INTEGER, chars INTEGER, listed TEXT);\n"
    "CREATE TABLE IF NOT EXISTS wake_rules(id INTEGER PRIMARY KEY, created REAL NOT NULL, kind_glob "
    "TEXT, klass TEXT NOT NULL, until REAL, why TEXT, active INTEGER NOT NULL DEFAULT 1);\n"
    "CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, ts REAL NOT NULL, bot TEXT NOT NULL, chat "
    "TEXT NOT NULL, op TEXT NOT NULL,\n"
    "  body TEXT NOT NULL, idem_key TEXT UNIQUE, urgent INTEGER NOT NULL DEFAULT 0, not_before REAL, "
    "mode TEXT NOT NULL DEFAULT 'live',\n"
    "  status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, "
    "sent_ts REAL, tg_message_id INTEGER, trace_id TEXT);\n"
    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);\n"
    "-- pragmas: journal_mode=WAL, synchronous=FULL, busy_timeout=5000; umask 077\n"
)


DDL_V2 = (  # schemas/events-v2.sql verbatim (schema 2: deliveries, bindings, three outbox columns; additive)
    "-- DDL v2 (v2 Phase 1 plan §3): forward-only and additive on top of events.sql (schema 1).\n"
    "-- core.db applies it once per store (meta schema -> 2) and adds the outbox columns only where missing\n"
    "-- (the telegram-plus 0.2.0 bridge adds the same deliveries table and outbox columns when missing).\n"
    "-- Kept apart from events.sql, the Phase 0 contract the tg-adapter's own schema.sql is compared with.\n"
    "CREATE TABLE IF NOT EXISTS deliveries(event_id INTEGER PRIMARY KEY, sent_ts REAL,\n"
    "  tries INTEGER NOT NULL DEFAULT 0, receipt_ts REAL, how TEXT);  -- how: channel|transcript|binding\n"
    "CREATE TABLE IF NOT EXISTS bindings(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,\n"
    "  tool TEXT NOT NULL, args TEXT NOT NULL DEFAULT '[]', note INTEGER NOT NULL DEFAULT 0,\n"
    "  on_error TEXT NOT NULL DEFAULT 'reply', why TEXT NOT NULL, created REAL NOT NULL, expires REAL,\n"
    "  active INTEGER NOT NULL DEFAULT 1, UNIQUE(kind, key));  -- kind: command|button|prefix|card\n"
    "ALTER TABLE outbox ADD COLUMN reply_to_event INTEGER;\n"
    "ALTER TABLE outbox ADD COLUMN files TEXT;\n"
    "ALTER TABLE outbox ADD COLUMN result TEXT;  -- the adapter's JSON outcome of a row (bridge 0.2.0)\n"
)


def default_path() -> Path:
    return paths.state("events.db")


def _chmod600(p: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            if os.stat(p + suffix).st_mode & 0o077:
                os.chmod(p + suffix, 0o600)


def connect(
    path: str | os.PathLike[str] | None = None, *, timeout: float = 5.0, ro: bool = False
) -> sqlite3.Connection:
    """Open the store. ro=True opens read-only and never creates anything (FileNotFoundError if missing).
    `timeout` (seconds) is also the busy_timeout; the default 5 s matches the contract."""
    p = os.fspath(paths.guard(path if path is not None else default_path()))
    busy = max(0, int(timeout * 1000))
    if ro:
        if not os.path.exists(p):
            raise FileNotFoundError(p)
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=timeout, isolation_level=None)
        con.execute(f"PRAGMA busy_timeout={busy}")
        con.row_factory = sqlite3.Row
        return con
    old = os.umask(0o077)
    try:
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        con = sqlite3.connect(p, timeout=timeout, isolation_level=None)
        con.execute(f"PRAGMA busy_timeout={busy}")
        if con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.row_factory = sqlite3.Row
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() is None:
            with tx(con):
                for stmt in ddl_statements():
                    con.execute(stmt)
                con.execute("INSERT OR IGNORE INTO meta VALUES('schema', '1')")
        migrate(con)
    finally:
        os.umask(old)
    _chmod600(p)
    return con


def ledger(*, ro: bool = False, timeout: float = 5.0, **kw: Any) -> sqlite3.Connection:
    """The events ledger for one unit of work: an in-memory copy from the hub whose close() sends its changes
    (core.hub.ledger: profile, names, ids, reserve); this SQLite store in a test run without a hub."""
    from core import hub  # late: core.hub imports this module

    if hub.backend("events") == "hub":
        return hub.ledger(timeout=timeout, **kw)
    return connect(ro=ro, timeout=timeout)


def _schema(con: sqlite3.Connection) -> int:
    try:
        return int(meta_get(con, "schema", 1) or 1)
    except (TypeError, ValueError):
        return 1


def migrate(con: sqlite3.Connection) -> int:
    """Bring the store to SCHEMA_VERSION (forward-only, additive). Cheap when current: one meta read. Two processes
    migrating at once serialise on BEGIN IMMEDIATE and the second finds the work done. Returns the schema."""
    if _schema(con) >= SCHEMA_VERSION:
        return _schema(con)
    with tx(con):
        if _schema(con) < 2:
            have = {r[1] for r in con.execute("PRAGMA table_info(outbox)")}
            for stmt in ddl_statements(DDL_V2):
                m = re.search(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", stmt)
                if m and m.group(1) == "outbox" and m.group(2) in have:
                    continue
                con.execute(stmt)
            meta_set(con, "schema", 2)
    return _schema(con)


def ddl_statements(ddl: str = DDL) -> list[str]:
    """The DDL as single statements (executescript() would COMMIT our open transaction first)."""
    out, buf = [], ""
    for line in ddl.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    return out


@contextlib.contextmanager
def tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction: BEGIN IMMEDIATE takes the write lock up front (waits up to busy_timeout)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def meta_get(con: sqlite3.Connection, k: str, default: Any = None) -> Any:
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    if row is None or row[0] is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def meta_set(con: sqlite3.Connection, k: str, v: Any) -> None:
    con.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, json.dumps(v, ensure_ascii=False, separators=(",", ":"))),
    )
