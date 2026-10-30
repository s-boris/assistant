"""inbox.ingest: mirror ~/inbox into the event ledger. Read-only on the inbox: it never writes, moves or deletes a note.

  ingest(con, reg, inbox_dir, now, min_age=2.0) -> stats   one pass, one write transaction

Per note (idem_key = its filename):
  no row               -> new row: kind from the registry (else note.<prefix>, cron.fail for NAME-failed-*), ts =
                          the note's mtime, subject = title, next = Next line, payload.note = fingerprint
  open row, same sha1  -> nothing (a row emit wrote without a fingerprint adopts it silently)
  open row, new sha1   -> updated: title/Next/labels refreshed, updated_ts = mtime, status back to new (Updated)
  acked/dismissed row  -> same content, not written after the ack: a leftover, nothing; else a new occurrence
  gone row             -> the note came back: a new occurrence (new row)
A new occurrence moves the old row's idem_key to "<name>#<id>" so the key stays unique. Open rows whose note is no
longer in the inbox become gone (deleted without an ack). Notes still being written (< min_age s) wait a run.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from core import db
from core.events import OPEN

from . import notes
from .kinds import Registry

__all__ = ["ingest"]


def _insert(con: sqlite3.Connection, info: notes.NoteInfo, reg: Registry) -> None:
    m = reg.classify(info.name)
    payload: dict[str, Any] = {"note": info.fingerprint()}
    if m.extras:
        payload["match"] = m.extras
    con.execute(
        "INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, idem_key, note_path)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            info.mtime,
            "inbox",
            m.kind,
            info.title,
            info.next,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(reg.labels_for(m.kind, info.text)),
            reg.delivery(m.kind),
            info.name,
            info.path,
        ),
    )


def ingest(con: sqlite3.Connection, reg: Registry, inbox_dir: str, now: float, min_age: float = 2.0) -> dict[str, int]:
    inbox_dir = os.path.abspath(inbox_dir)
    stable, young = notes.scan(inbox_dir, now, min_age)
    infos: list[notes.NoteInfo] = []
    stats = {
        "seen": 0,
        "new": 0,
        "updated": 0,
        "reopened": 0,
        "gone": 0,
        "unchanged": 0,
        "young": len(young),
        "errors": 0,
    }
    for p in stable:  # read outside the write lock
        try:
            infos.append(notes.read_note(p))
        except FileNotFoundError:
            continue
        except OSError:
            stats["errors"] += 1
    present = {i.name for i in infos} | young
    stats["seen"] = len(infos)
    with db.tx(con):
        for info in infos:
            r = con.execute(
                "SELECT id, status, payload, labels, kind, acked_ts FROM events WHERE idem_key=?", (info.name,)
            ).fetchone()
            if r is None:
                _insert(con, info, reg)
                stats["new"] += 1
                continue
            try:
                payload = json.loads(r["payload"] or "{}") or {}
            except ValueError:
                payload = {}
            old = payload.get("note") if isinstance(payload.get("note"), dict) else None
            if r["status"] in OPEN:
                if old is None:  # written by emit: adopt the fingerprint, no status change
                    payload["note"] = info.fingerprint()
                    con.execute(
                        "UPDATE events SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), r["id"])
                    )
                    stats["unchanged"] += 1
                elif old.get("sha1") == info.sha1:
                    stats["unchanged"] += 1
                else:
                    payload["note"] = info.fingerprint()
                    try:
                        labels = set(json.loads(r["labels"] or "[]"))
                    except ValueError:
                        labels = set()
                    labels |= set(reg.labels_for(r["kind"], info.text))
                    con.execute(
                        "UPDATE events SET subject=?, next=?, payload=?, labels=?, updated_ts=?, status='new'"
                        " WHERE id=?",
                        (
                            info.title,
                            info.next,
                            json.dumps(payload, ensure_ascii=False),
                            json.dumps(sorted(labels)),
                            info.mtime,
                            r["id"],
                        ),
                    )
                    stats["updated"] += 1
                continue
            if (
                r["status"] in ("acked", "dismissed")
                and old is not None
                and old.get("sha1") == info.sha1
                and info.mtime <= float(r["acked_ts"] or 0)
            ):
                stats["unchanged"] += 1  # the ack's delete did not happen: never resurrect it
                continue
            con.execute("UPDATE events SET idem_key=idem_key || '#' || id WHERE id=?", (r["id"],))
            _insert(con, info, reg)
            stats["reopened"] += 1
        rows = con.execute(
            "SELECT id, note_path FROM events WHERE status IN ('new','listed') AND note_path IS NOT NULL"
        ).fetchall()
        for r in rows:
            np = r["note_path"]
            if os.path.dirname(os.path.abspath(np)) != inbox_dir or os.path.basename(np) in present:
                continue
            if os.path.lexists(np):  # skipped this run (e.g. a symlink): not gone
                continue
            con.execute("UPDATE events SET status='gone', updated_ts=? WHERE id=?", (now, r["id"]))
            stats["gone"] += 1
        db.meta_set(con, "ingest", {"ts": now, **stats})
    return stats
