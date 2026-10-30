"""inbox.writer: the emit contract for script writers — the typed event row AND the identical md note.

  note(kind, name, body, *, source, payload=None, mode=None, subject=None, inbox_dir=None) -> EmitResult
        the note is the writer's text, byte for byte, under the writer's file name (= idem_key) and creation mode;
        the row gets the kind (None: the registry's kind for the name, as ingest), the registry's delivery and
        labels (label_lines on the text, as ingest), title and Next line derived from the text like ingest
        (`subject` is used only when the text has no "#" title) and the writer's typed payload
        ({"type": kind, "v": 1, ...}; schemas/payloads/<kind>.schema.json)
  new_note(prefix, body, *, source, kind=None, payload=None, mode=None, subject=None, inbox_dir=None) -> EmitResult
        note() under a fresh name <prefix>-<epoch>[-n].md, as botlib.write_inbox names notes (never an existing
        file; a same-named open row whose note holds another writer's text moves on to the next name)
  changed(name, *, payload=None, inbox_dir=None) -> UpdateResult
        the writer changed its note in place (cron-fail's "Recovered" line): merged payload, labels from the new
        text, the note's new fingerprint, status back to new (Updated); no row / closed row = nothing (ingest adopts)

A DB failure never loses the note: core.events writes it anyway and logs one line to
~/.local/state/assistant/emit-errors.log (the error counter `events metrics` reports). Anything else that breaks
here propagates, so the writer falls back to its old note code and counts that itself (one line in the same log).
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from core import clock, db, events, paths

from . import kinds, notes

__all__ = ["changed", "new_note", "note"]


def note(
    kind: str | None,
    name: str,
    body: str,
    *,
    source: str,
    payload: dict[str, Any] | None = None,
    mode: int | None = None,
    subject: str | None = None,
    inbox_dir: str | None = None,
    ts: float | None = None,
) -> events.EmitResult:
    reg = kinds.load()
    kind = kind or reg.classify(name).kind  # None: the kind ingest would give this note name
    title, nxt = notes.parse(body, name)
    if subject and title == notes.clean(name, 160):
        title = notes.clean(subject, 160)
    pl: dict[str, Any] = {"type": kind, "v": 1}
    pl.update(payload or {})
    return events.emit(
        kind=kind,
        subject=title,
        next=nxt,
        payload=pl,
        labels=reg.labels_for(kind, body),
        delivery=reg.delivery(kind),
        source=source,
        note_name=name,
        body=body,
        note_mode=mode,
        ts=ts,
        inbox_dir=inbox_dir,
    )


def _holds(path: str, body: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read() == body
    except (OSError, UnicodeDecodeError):
        return False


def new_note(
    prefix: str,
    body: str,
    *,
    source: str,
    kind: str | None = None,
    payload: dict[str, Any] | None = None,
    mode: int | None = None,
    subject: str | None = None,
    inbox_dir: str | None = None,
    ts: float | None = None,
) -> events.EmitResult:
    if not prefix or "/" in prefix or prefix.startswith("."):
        raise ValueError(f"bad note prefix {prefix!r}")
    d = os.fspath(inbox_dir or paths.inbox())
    t = int(clock.now() if ts is None else ts)
    for n in range(1000):
        name = f"{prefix}-{t}.md" if n == 0 else f"{prefix}-{t}-{n}.md"
        path = os.path.join(d, name)
        if os.path.lexists(path):
            continue
        r = note(kind, name, body, source=source, payload=payload, mode=mode, subject=subject, inbox_dir=d, ts=ts)
        if not r.dup or _holds(path, body):  # a dup that restored our text is fine; another writer's note: next name
            return r
    raise FileExistsError(f"no free note name for {prefix}-{t} in {d}")


def changed(
    name: str,
    *,
    payload: dict[str, Any] | None = None,
    inbox_dir: str | None = None,
    ts: float | None = None,
) -> events.UpdateResult:
    if events.off():
        return events.UpdateResult(None, "off")
    path = os.path.join(os.fspath(inbox_dir or paths.inbox()), name)
    try:
        info: notes.NoteInfo | None = notes.read_note(path)
    except OSError:
        info = None
    from core import hub

    if hub.backend("events") == "hub":  # the kind from a small hub snapshot, then the typed update (spools)
        kind = None
        try:
            c = hub.ledger("ingest", names=[name], local=False, timeout=3.0)
            c.discard()
            r = c.execute("SELECT kind FROM events WHERE idem_key=?", (name,)).fetchone()
            kind = r[0] if r is not None else None
            sqlite3.Connection.close(c)
        except sqlite3.Error:
            pass
        labels = kinds.load().labels_for(kind, info.text) if (kind and info is not None) else []
        return events.update(
            idem_key=name, payload=payload, labels=labels, note_fp=info.fingerprint() if info else None, ts=ts
        )
    try:
        con = db.connect()
    except (sqlite3.Error, OSError) as e:
        events._log_emit_error(name, e, "update")
        return events.UpdateResult(None, "error", str(e))
    try:
        r = con.execute("SELECT kind FROM events WHERE idem_key=?", (name,)).fetchone()
        labels = kinds.load().labels_for(r[0], info.text) if (r is not None and info is not None) else []
        return events.update(
            idem_key=name,
            payload=payload,
            labels=labels,
            note_fp=info.fingerprint() if info is not None else None,
            ts=ts,
            con=con,
        )
    except sqlite3.Error as e:
        events._log_emit_error(name, e, "update")
        return events.UpdateResult(None, "error", str(e))
    finally:
        con.close()
