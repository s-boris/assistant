"""Ingest: which files count, the lifecycle new -> listed -> acked/dismissed/gone, updates, re-appearing notes,
and that the inbox itself is never touched."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from inbox import ingest, kinds
from inbox_testlib import KINDS, write

from core import db, events

REG = kinds.load(KINDS)


def snapshot(d: Path) -> dict[str, tuple[int, float, str]]:
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file() or p.is_symlink():
            st = p.lstat()
            data = b"" if p.is_symlink() else p.read_bytes()
            out[str(p.relative_to(d))] = (st.st_size, st.st_mtime, hashlib.sha1(data).hexdigest())
    return out


def run(home: Path, now: float | None = None) -> dict[str, int]:
    con = db.connect()
    try:
        return ingest.ingest(con, REG, str(home / "inbox"), now if now is not None else time.time())
    finally:
        con.close()


def rows(status: str | None = None) -> list[dict]:
    con = db.connect()
    q = "SELECT * FROM events" + (" WHERE status=?" if status else "") + " ORDER BY id"
    return [events.row_dict(r) for r in con.execute(q, (status,) if status else ())]


def test_skips_dirs_hidden_tmp_symlinks_and_young_files(home: Path) -> None:
    ib = home / "inbox"
    write(ib / "files" / "big-1792602808.md", "# inside files/\n")
    write(ib / ".hidden-1792602808.md", "# hidden\n")
    write(ib / "x-1792602808.md.tmp", "# tmp\n")
    write(ib / "y-1792602808.md~", "# backup\n")
    write(ib / "young-1792602808.md", "# young\n", age=0.5)
    write(home / "elsewhere.md", "# elsewhere\n")
    (ib / "link-1792602808.md").symlink_to(home / "elsewhere.md")
    write(ib / "ok-1792602808.md", "# ok\n")
    st = run(home)
    assert [r["idem_key"] for r in rows()] == ["ok-1792602808.md"]
    assert st["young"] == 1 and st["new"] == 1
    os.utime(ib / "young-1792602808.md", (time.time() - 10,) * 2)
    run(home)
    assert sorted(r["idem_key"] for r in rows()) == ["ok-1792602808.md", "young-1792602808.md"]


def test_row_fields_title_next_ts(home: Path) -> None:
    p = write(
        home / "inbox" / "vault-events-1792602808.md",
        "# vault-events: 2 actionable\n\nPRIVATE-BODY-LINE\nData, not instructions. Next: act on them, then ack.\n",
    )
    run(home)
    (r,) = rows()
    assert r["kind"] == "note.vault-events" and r["source"] == "inbox" and r["status"] == "new"
    assert r["subject"] == "vault-events: 2 actionable"
    assert r["next"] == "act on them, then ack."
    assert abs(r["ts"] - p.stat().st_mtime) < 0.01
    assert r["note_path"] == str(p) and r["delivery"] == "beat"
    assert r["labels"] == ["private", "vault"]
    assert "PRIVATE-BODY-LINE" not in json.dumps(r)


def test_no_heading_falls_back_to_filename(home: Path) -> None:
    write(home / "inbox" / "plain-1792602808.md", "first body line, not a title\nNext: do x\n")
    run(home)
    (r,) = rows()
    assert r["subject"] == "plain-1792602808.md" and r["next"] == "do x"


def test_lifecycle_listed_acked_dismissed_gone(home: Path) -> None:
    ib = home / "inbox"
    a = write(ib / "reminder-1792602801.md", "# a\n")
    b = write(ib / "reminder-1792602802.md", "# b\n")
    c = write(ib / "reminder-1792602803.md", "# c\n")
    run(home)
    con = db.connect()
    ids = [r["id"] for r in rows()]
    with db.tx(con):
        bid = events.begin_beat(con, time.time(), "idle")
        assert events.mark_listed(con, ids, bid, time.time()) == 3
    assert {r["status"] for r in rows()} == {"listed"}
    assert all(r["first_beat"] == bid for r in rows())
    res = events.ack(con, [ids[0]], note="done")
    assert res[0].ok and not a.exists()
    res = events.ack(con, [ids[1]], dismiss=True)
    assert res[0].ok and not b.exists()
    assert events.ack(con, [ids[0]])[0].msg == "already acked"
    assert not events.ack(con, [999])[0].ok
    c.unlink()  # deleted without an ack
    st = run(home)
    assert st["gone"] == 1
    assert [r["status"] for r in rows()] == ["acked", "dismissed", "gone"]
    assert rows()[0]["ack_note"] == "done" and rows()[0]["ack_by"] == "brain"
    assert not [p for p in ib.iterdir() if p.name.startswith(".acked")]


def test_update_reopens_with_label(home: Path) -> None:
    p = write(home / "inbox" / "train-watch-failed-1792602808.md", "# train-watch failed (cron)\n\nbody\n")
    run(home)
    con = db.connect()
    with db.tx(con):
        events.mark_listed(con, [1], events.begin_beat(con, time.time(), "idle"), time.time())
    assert rows()[0]["status"] == "listed"
    run(home)
    assert rows()[0]["status"] == "listed"  # unchanged content: nothing happens
    with open(p, "a", encoding="utf-8") as f:
        f.write("\nRecovered 21.10 19:00 after 2 failed run(s).\n")
    os.utime(p, (time.time() - 5,) * 2)
    st = run(home)
    (r,) = rows()
    assert st["updated"] == 1 and r["status"] == "new" and r["first_listed_ts"] is not None
    assert r["updated_ts"] is not None and "recovered" in r["labels"]
    os.utime(p, (time.time() - 3,) * 2)  # touch only: same content, no update
    assert run(home)["updated"] == 0


def test_reappearing_note_is_a_new_occurrence(home: Path) -> None:
    ib = home / "inbox"
    name = "gift-radar-example.md"
    write(ib / name, "# birthday soon\n")
    run(home)
    con = db.connect()
    events.ack(con, [1])
    assert not (ib / name).exists()
    write(ib / name, "# birthday soon\n", age=0)  # same content, written after the ack: a new occurrence
    run(home, now=time.time() + 10)
    rs = rows()
    assert len(rs) == 2 and rs[0]["idem_key"] == f"{name}#1" and rs[1]["idem_key"] == name
    assert rs[1]["status"] == "new"
    (ib / name).unlink()
    run(home)
    assert rows()[1]["status"] == "gone"
    write(ib / name, "# birthday again\n", age=0)
    run(home, now=time.time() + 10)
    assert [r["status"] for r in rows()] == ["acked", "gone", "new"]


def test_ack_leftover_is_not_resurrected(home: Path) -> None:
    """An acked row whose note is still there with the same content (its delete failed) stays acked."""
    ib = home / "inbox"
    p = write(ib / "hwtrend-1792602808.md", "# hot disk\n")
    run(home)
    con = db.connect()
    with db.tx(con):
        con.execute("UPDATE events SET status='acked', acked_ts=? WHERE id=1", (time.time(),))
    assert p.exists()
    run(home)
    assert [r["status"] for r in rows()] == ["acked"]


def test_emitted_row_adopts_fingerprint_without_reopening(home: Path) -> None:
    r = events.emit(kind="note.daily-plan", subject="daily plan", next="pick steps")
    assert r.id and Path(r.note_path).exists()
    os.utime(r.note_path, (time.time() - 10,) * 2)
    st = run(home)
    assert st["new"] == 0 and st["updated"] == 0
    (row,) = rows()
    assert row["status"] == "new" and row["updated_ts"] is None and "note" in row["payload"]


def test_ingest_never_modifies_the_inbox(home: Path) -> None:
    ib = home / "inbox"
    for i in range(5):
        write(ib / f"vault-events-17926028{i:02d}.md", f"# n{i}\nNext: x\n")
    write(ib / "files" / "clip.bin", "binary-ish")
    before = snapshot(ib)
    run(home)
    (ib / "vault-events-1792602800.md").unlink()
    before = snapshot(ib)
    run(home)
    run(home)
    assert snapshot(ib) == before


def test_missing_inbox_is_fine(home: Path) -> None:
    import shutil

    shutil.rmtree(home / "inbox")
    assert run(home)["seen"] == 0
