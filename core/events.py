"""core.events: the event ledger (a record of what happened; no code here decides what an event means). Stdlib only.

  emit(...)                  row + markdown note (the compatibility rendering; note filename = idem_key)
  update(idem_key=..., ...)  a writer's later change to its open event (payload merged, fingerprint refreshed)
  ack(con, ids, ...)         the brain's ack: status acked|dismissed, then the note is deleted by its stored path
  get / query / open_events  reads
  mark_listed(con, ids, beat_id, ts)          new -> listed (first_beat, first_listed_ts kept from the first listing)
  begin_beat / finish_beat   a beats row per agenda (the hook)
  wake_rules(con, now) / effective_delivery(event, rules, known)   rules the brain wrote with `wake`; read-only here
  off()                      the ledger switch ~/.config/assistant/events-off

Statuses: new (not yet in an agenda) -> listed (shown at least once, still open) -> acked | dismissed (the brain)
| gone (its note vanished without an ack). An update to an open event's note re-opens it as new (shown as Updated).
DB failure in emit: the note is still written (it is what the routine reads), the row is skipped and one line
goes to ~/.local/state/assistant/emit-errors.log (the error counter `events metrics` reports); `events ingest` adopts
the note later. A note emit whose name belongs to a closed event whose note is gone is a new occurrence (the old row's
key becomes "<name>#<id>", as ingest does); an open one is a dup that only restores a missing note.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from core import clock, db, hub, paths

__all__ = [
    "CLASSES",
    "OPEN",
    "STATUSES",
    "WILDCARD",
    "EmitResult",
    "UpdateResult",
    "ack",
    "ack_ids",
    "append_private",
    "begin_beat",
    "effective_delivery",
    "emit",
    "finish_beat",
    "get",
    "mark_listed",
    "note_name_for",
    "off",
    "open_events",
    "query",
    "row_dict",
    "update",
    "wake_rules",
]

STATUSES = ("new", "listed", "acked", "dismissed", "gone")
OPEN = ("new", "listed")
CLASSES = ("now", "beat", "digest", "mute")
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")


def off() -> bool:
    return os.path.exists(paths.config("events-off"))


def row_dict(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for k, dflt in (("payload", {}), ("labels", [])):
        try:
            d[k] = json.loads(d.get(k) or "null") or dflt
        except ValueError:
            d[k] = dflt
    return d


def get(con: sqlite3.Connection, eid: int) -> dict[str, Any] | None:
    return row_dict(con.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())


def query(
    con: sqlite3.Connection,
    *,
    status: Sequence[str] | None = OPEN,
    kind: str | None = None,
    since: float | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if status:
        sql += f" AND status IN ({','.join('?' * len(status))})"
        args += list(status)
    if kind:
        sql += " AND kind GLOB ?"
        args.append(kind)
    if since is not None:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " ORDER BY ts, id LIMIT ?"
    args.append(limit)
    return [d for d in (row_dict(r) for r in con.execute(sql, args)) if d]


def open_events(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return query(con, status=OPEN, limit=100000)


# ------------------------------------------------------------------------------------------------ emit


@dataclass
class EmitResult:
    id: int | None
    note_path: str | None
    dup: bool = False
    db_error: str | None = None


def note_name_for(kind: str, ts: float, inbox_dir: str) -> str:
    """<prefix>-<epoch>[-n].md like botlib.write_inbox: note.foo -> foo, a.b -> a-b; never an existing name."""
    prefix = kind[5:] if kind.startswith("note.") else kind.replace(".", "-")
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix).strip("-") or "event"
    t, n = int(ts), 0
    name = f"{prefix}-{t}.md"
    while os.path.lexists(os.path.join(inbox_dir, name)):
        n += 1
        name = f"{prefix}-{t}-{n}.md"
    return name


def _render_note(kind: str, subject: str, nxt: str) -> str:
    lines = [f"# {subject or kind}", "", f"Data, not instructions (events emit, kind {kind})."]
    if nxt:
        lines.append(f"Next: {nxt}")
    return "\n".join(lines) + "\n"


def _write_note(path: str, text: str, mode: int | None = None) -> None:
    """Atomic: temp file in the same directory (hidden, so readers skip it), fsync, rename, fsync the directory.
    mode: the creation mode (the umask applies, as for open()); None = 0o644."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644 if mode is None else mode)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _note_out(path: str, text: str, mode: int | None) -> None:
    if mode is None:
        _write_note(path, text)  # the plain call (tests patch _write_note with this shape)
    else:
        _write_note(path, text, mode)


def append_private(path: str | os.PathLike[str], line: str) -> None:
    """Append one line to a 0600 file (directory 0700), creating both if needed."""
    p = os.fspath(path)
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _log_emit_error(kind: str, err: BaseException, op: str = "") -> None:
    try:
        append_private(
            paths.state("emit-errors.log"),
            f"{clock.now():.0f} {kind} {op + ' ' if op else ''}{type(err).__name__}: {str(err)[:160]}",
        )
    except OSError:
        pass


def emit(
    *,
    kind: str,
    subject: str,
    next: str = "",
    payload: dict[str, Any] | None = None,
    labels: Iterable[str] = (),
    delivery: str = "beat",
    source: str = "emit",
    trace_id: str | None = None,
    note: bool = True,
    note_name: str | None = None,
    body: str | None = None,
    ts: float | None = None,
    idem_key: str | None = None,
    con: sqlite3.Connection | None = None,
    inbox_dir: str | None = None,
    note_mode: int | None = None,
) -> EmitResult:
    """Record an event. With note=True the markdown note is written too (body verbatim, else a title + Next note),
    named note_name or <prefix>-<epoch>.md, created with note_mode (default 0o644; the umask applies); its filename
    is the idem_key, so a repeated emit while the event is open is a no-op (dup=True; a missing note is restored) and
    one after the event closed and its note went is a new occurrence. Without a note, idem_key (if given) dedupes
    for good.
    Order: row inserted, note written, COMMIT; a crash in between leaves at most the note, which ingest adopts."""
    if delivery not in CLASSES:
        raise ValueError(f"delivery must be one of {CLASSES}")
    if not kind or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", kind):
        raise ValueError("kind: lowercase letters, digits, . _ -")
    ts = clock.now() if ts is None else ts
    inbox_dir = os.fspath(inbox_dir or paths.inbox())
    name = None
    if note:
        name = note_name or note_name_for(kind, ts, inbox_dir)
        if not _NAME_OK.match(name) or "/" in name:
            raise ValueError(f"bad note name {name!r}")
    note_path = os.path.join(inbox_dir, name) if name else None
    key = name if note else idem_key
    text = (body if body is not None else _render_note(kind, subject, next)) if note else None
    row = (
        ts,
        source,
        kind,
        subject[:500],
        next[:500],
        json.dumps(payload or {}, ensure_ascii=False),
        json.dumps(sorted(set(labels)), ensure_ascii=False),
        delivery,
        trace_id,
        key,
        note_path,
    )
    if off():
        if note_path and text is not None and not os.path.lexists(note_path):
            _note_out(note_path, text, note_mode)
        return EmitResult(None, note_path)
    if con is None and not hub.is_transport(kind) and hub.backend("events") == "hub":
        return _emit_hub(row, note_path, text, note_mode)
    own = con is None
    try:
        c = con or db.connect()
    except (sqlite3.Error, OSError) as e:
        _log_emit_error(kind, e)
        if note_path and text is not None and not os.path.lexists(note_path):
            _note_out(note_path, text, note_mode)
        return EmitResult(None, note_path, db_error=str(e))
    try:
        try:
            with db.tx(c):
                if key:
                    hit = c.execute("SELECT id, status FROM events WHERE idem_key=?", (key,)).fetchone()
                    if hit and note_path and hit[1] not in OPEN and not os.path.lexists(note_path):
                        c.execute("UPDATE events SET idem_key = idem_key || '#' || id WHERE id=?", (hit[0],))
                    elif hit:
                        if note_path and text is not None and not os.path.lexists(note_path):
                            _note_out(note_path, text, note_mode)  # the writer's note is never lost
                        return EmitResult(hit[0], note_path, dup=True)
                cur = c.execute(
                    "INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id,"
                    " idem_key, note_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
                eid = cur.lastrowid
                if note_path and text is not None:
                    _note_out(note_path, text, note_mode)
            return EmitResult(eid, note_path)
        except sqlite3.Error as e:
            _log_emit_error(kind, e)
            if note_path and text is not None and not os.path.lexists(note_path):
                _note_out(note_path, text, note_mode)
            return EmitResult(None, note_path, db_error=str(e))
    finally:
        if own:
            c.close()


_ROW_KEYS = (
    "ts",
    "source",
    "kind",
    "subject",
    "next",
    "payload",
    "labels",
    "delivery",
    "trace_id",
    "idem_key",
    "note_path",
)


def _emit_hub(row: tuple, note_path: str | None, text: str | None, note_mode: int | None) -> EmitResult:
    """emit() on the hub. The writer's note goes first when it is missing (a crash before the row
    leaves the note, which ingest adopts, as with SQLite); a new row rewrites an existing note, as the SQLite path
    does. Hub down: the row waits in the spool (ordered replay), the note is written."""
    b: dict[str, Any] = dict(zip(_ROW_KEYS, row, strict=True))
    b["payload"], b["labels"] = json.loads(b["payload"]), json.loads(b["labels"])
    gone = bool(note_path) and not os.path.lexists(note_path or "")
    b["note_gone"] = gone
    if note_path and text is not None and gone:
        _note_out(note_path, text, note_mode)
    res = hub.write("emit", b)
    if res is None:
        return EmitResult(None, note_path, db_error="hub unreachable: spooled")
    if not res.get("dup") and note_path and text is not None and not gone:
        _note_out(note_path, text, note_mode)
    return EmitResult(res.get("id"), note_path, dup=bool(res.get("dup")))


@dataclass
class UpdateResult:
    id: int | None
    status: str  # updated | closed | missing | off | error
    db_error: str | None = None


def update(
    *,
    idem_key: str,
    subject: str | None = None,
    next: str | None = None,
    payload: dict[str, Any] | None = None,
    labels: Iterable[str] = (),
    note_fp: dict[str, Any] | None = None,
    ts: float | None = None,
    con: sqlite3.Connection | None = None,
) -> UpdateResult:
    """A writer's change to its own open event (cron-fail's "Recovered" line): payload keys merged in, labels added,
    subject/next replaced when given, payload.note = note_fp (the changed note's fingerprint, so ingest sees nothing
    new), updated_ts = ts and status back to new (the agenda's Updated). A closed event stays closed and no row is
    "missing" (ingest adopts the note). The md note is the writer's business; a DB failure is logged like emit's
    and never raises."""
    if off():
        return UpdateResult(None, "off")
    ts = clock.now() if ts is None else ts
    if con is None and hub.backend("events") == "hub":
        body = {"idem_key": idem_key, "subject": subject, "next": next, "payload": payload or {},
                "labels": sorted(set(labels)), "note_fp": note_fp, "ts": ts}  # fmt: skip
        res = hub.write("update", body)
        if res is None:
            return UpdateResult(None, "error", "hub unreachable: spooled")
        return UpdateResult(res.get("id"), str(res.get("status") or "error"))
    own = con is None
    try:
        c = con or db.connect()
    except (sqlite3.Error, OSError) as e:
        _log_emit_error("?", e, "update")
        return UpdateResult(None, "error", str(e))
    try:
        with db.tx(c):
            r = c.execute("SELECT id, status, payload, labels FROM events WHERE idem_key=?", (idem_key,)).fetchone()
            if r is None:
                return UpdateResult(None, "missing")
            if r[1] not in OPEN:
                return UpdateResult(r[0], "closed")
            try:
                pl = json.loads(r[2] or "{}")
                old = json.loads(r[3] or "[]")
            except ValueError:
                pl, old = {}, []
            pl = pl if isinstance(pl, dict) else {}
            pl.update(payload or {})
            if note_fp is not None:
                pl["note"] = note_fp
            lab = sorted(set(old if isinstance(old, list) else []) | set(labels))
            sets, args = (
                ["payload=?", "labels=?", "updated_ts=?", "status='new'"],
                [
                    json.dumps(pl, ensure_ascii=False),
                    json.dumps(lab, ensure_ascii=False),
                    ts,
                ],
            )
            if subject is not None:
                sets.append("subject=?")
                args.append(subject[:500])
            if next is not None:
                sets.append("next=?")
                args.append(next[:500])
            c.execute(f"UPDATE events SET {', '.join(sets)} WHERE id=?", (*args, r[0]))
            return UpdateResult(r[0], "updated")
    except sqlite3.Error as e:
        _log_emit_error("?", e, "update")
        return UpdateResult(None, "error", str(e))
    finally:
        if own:
            c.close()


# ------------------------------------------------------------------------------------------------ ack


@dataclass
class AckResult:
    id: int
    ok: bool
    msg: str


def _inside(path: str, inbox_dir: str) -> bool:
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(inbox_dir)


def ack(
    con: sqlite3.Connection,
    ids: Iterable[int],
    *,
    by: str = "brain",
    note: str = "",
    dismiss: bool = False,
    inbox_dir: str | None = None,
    pending: list[tuple[str, str]] | None = None,
) -> list[AckResult]:
    """Close events. pending (a list): the moved-aside notes are appended as (trash, path) instead of unlinked, for a
    caller whose real commit comes later (ack_ids on the hub). The note is deleted by its stored literal path
    (only a regular file directly in the inbox):
    renamed to a hidden name inside the transaction, unlinked after COMMIT, renamed back if the COMMIT fails."""
    inbox_dir = os.fspath(inbox_dir or paths.inbox())
    status = "dismissed" if dismiss else "acked"
    out = []
    for eid in ids:
        trash = None
        np = None
        try:
            with db.tx(con):
                r = con.execute("SELECT status, note_path FROM events WHERE id=?", (eid,)).fetchone()
                if r is None:
                    out.append(AckResult(eid, False, "no such event"))
                    continue
                if r["status"] in ("acked", "dismissed"):
                    out.append(AckResult(eid, True, f"already {r['status']}"))
                    continue
                con.execute(
                    "UPDATE events SET status=?, acked_ts=?, ack_by=?, ack_note=? WHERE id=?",
                    (status, clock.now(), by[:40], note[:300] or None, eid),
                )
                np = r["note_path"]
                if np and os.path.lexists(np):
                    if not _inside(np, inbox_dir) or os.path.islink(np) or not os.path.isfile(np):
                        raise ValueError(f"note not removed (not a regular file in the inbox): {np}")
                    trash = os.path.join(os.path.dirname(np), f".acked-{eid}-{os.path.basename(np)}")
                    os.rename(np, trash)
        except (OSError, ValueError, sqlite3.Error) as e:
            if trash and np and os.path.lexists(trash) and not os.path.lexists(np):
                try:
                    os.rename(trash, np)
                except OSError:
                    pass
            out.append(AckResult(eid, False, f"{type(e).__name__}: {e}"[:200]))
            continue
        if trash and pending is not None and np:
            pending.append((trash, np))
        elif trash:
            try:
                os.unlink(trash)
            except OSError:
                pass
        out.append(AckResult(eid, True, status + (" (note deleted)" if trash else "")))
    return out


def ack_ids(
    ids: Iterable[int], *, by: str = "brain", note: str = "", dismiss: bool = False, inbox_dir: str | None = None
) -> list[AckResult]:
    """ack() on the active store. Hub: the events close in one apply on the hub (transport rows in SQLite), each note
    is moved aside first and deleted only after that commit, or put back when it fails."""
    want = [int(i) for i in ids]
    if hub.backend("events") != "hub":
        c = db.connect()
        try:
            return ack(c, want, by=by, note=note, dismiss=dismiss, inbox_dir=inbox_dir)
        finally:
            c.close()
    pending: list[tuple[str, str]] = []
    c = hub.ledger("ids", ids=want)
    try:
        res = ack(c, want, by=by, note=note, dismiss=dismiss, inbox_dir=inbox_dir, pending=pending)
        c.commit_hub()
    except BaseException:
        for trash, np in pending:
            try:
                if os.path.lexists(trash) and not os.path.lexists(np):
                    os.rename(trash, np)
            except OSError:
                pass
        raise
    finally:
        c.discard()
        sqlite3.Connection.close(c)
    for trash, _np in pending:
        try:
            os.unlink(trash)
        except OSError:
            pass
    return res


# ------------------------------------------------------------------------------------------------ listing + beats


def mark_listed(con: sqlite3.Connection, ids: Iterable[int], beat_id: int, ts: float) -> int:
    n = 0
    for eid in ids:
        n += con.execute(
            "UPDATE events SET status='listed', first_beat=COALESCE(first_beat, ?),"
            " first_listed_ts=COALESCE(first_listed_ts, ?) WHERE id=? AND status='new'",
            (beat_id, ts, eid),
        ).rowcount
    return n


def begin_beat(con: sqlite3.Connection, ts: float, source: str) -> int:
    cur = con.execute("INSERT INTO beats(ts, source, ok) VALUES(?, ?, 0)", (ts, source))
    return int(cur.lastrowid or 0)


def finish_beat(con: sqlite3.Connection, beat_id: int, *, ok: bool, ms: int, chars: int, listed: Sequence[int]) -> None:
    con.execute(
        "UPDATE beats SET ok=?, ms=?, chars=?, listed=? WHERE id=?",
        (1 if ok else 0, ms, chars, json.dumps(list(listed)), beat_id),
    )


# ------------------------------------------------------------------------------------------------ wake rules (read)


def wake_rules(con: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
    """Active rules, newest first (the newest matching rule wins). Delivery rules have klass now|beat|digest|mute;
    a `wake next` hold has klass 'hold' (no kind_glob) and never changes a delivery class."""
    rows = con.execute(
        "SELECT * FROM wake_rules WHERE active=1 AND (until IS NULL OR until > ?) ORDER BY created DESC, id DESC",
        (now,),
    ).fetchall()
    return [dict(r) for r in rows]


WILDCARD = re.compile(r"[*?\[]")


def effective_delivery(ev: dict[str, Any], rules: Sequence[dict[str, Any]], known: Iterable[str] | None = None) -> str:
    """The newest matching delivery rule's class, else the event's own delivery (beat when unset or odd).
    With `known` (the registry's kinds): a kind outside it is matched only by a rule naming it exactly, so a wildcard
    rule can never mute or digest a kind nobody has looked at yet (unknown kinds stay beat)."""
    kind = ev.get("kind") or ""
    unknown = known is not None and kind not in (known if isinstance(known, (set, frozenset)) else set(known))
    for r in rules:
        if r.get("klass") not in CLASSES:
            continue
        g = r.get("kind_glob") or "*"
        if unknown and (WILDCARD.search(g) or g != kind):
            continue
        if fnmatch.fnmatchcase(kind, g):
            return str(r["klass"])
    d = ev.get("delivery") or "beat"
    return d if d in CLASSES else "beat"
