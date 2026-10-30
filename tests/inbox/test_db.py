"""core.db: DDL contract, pragmas, modes, crash safety."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
from inbox_testlib import PY, REPO, run_events

from core import db


def test_ddl_is_schemas_events_sql_verbatim() -> None:
    assert db.DDL == (REPO / "schemas" / "events.sql").read_text(encoding="utf-8")


def test_ddl_v2_is_schemas_events_v2_sql_verbatim() -> None:
    assert db.DDL_V2 == (REPO / "schemas" / "events-v2.sql").read_text(encoding="utf-8")


def test_tg_adapter_schema_v2_is_the_same_text() -> None:
    text = (REPO / "schemas" / "events-v2.sql").read_text(encoding="utf-8")
    assert (REPO / "adapters" / "tg" / "src" / "schema_v2.sql").read_text(encoding="utf-8") == text


def test_connect_migrates_a_v1_store_once(home: Path) -> None:
    p = home / ".local/state/assistant/events.db"
    p.parent.mkdir(parents=True, mode=0o700)
    old = sqlite3.connect(p, isolation_level=None)
    for stmt in db.ddl_statements():
        old.execute(stmt)
    old.execute("INSERT INTO meta VALUES('schema', '1')")
    old.execute("INSERT INTO outbox(ts, bot, chat, op, body) VALUES (1, 'b', '1', 'send', '{}')")
    old.execute("ALTER TABLE outbox ADD COLUMN files TEXT")  # one v2 column already there (another writer)
    old.close()
    con = db.connect()
    cols = [r[1] for r in con.execute("PRAGMA table_info(outbox)")]
    assert cols[-3:] == ["files", "reply_to_event", "result"]
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"deliveries", "bindings"} <= tables
    assert db.meta_get(con, "schema") == 2
    assert tuple(con.execute("SELECT bot, result FROM outbox").fetchone()) == ("b", None)
    con.close()
    db.connect().close()  # idempotent


def test_ddl_statements() -> None:
    stmts = db.ddl_statements()
    assert len(stmts) == 6
    assert all(sqlite3.complete_statement(s) for s in stmts)
    assert [s.split("(")[0].split()[-1] for s in stmts] == ["events", "events", "beats", "wake_rules", "outbox", "meta"]


def test_connect_creates_private_store_with_pragmas(home: Path) -> None:
    con = db.connect()
    p = home / ".local/state/assistant/events.db"
    assert p.exists()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    for suffix in ("-wal", "-shm"):
        q = Path(str(p) + suffix)
        if q.exists():
            assert stat.S_IMODE(q.stat().st_mode) == 0o600
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "beats", "wake_rules", "outbox", "meta"} <= tables
    assert db.meta_get(con, "schema") == 2
    con.close()
    db.connect().close()  # idempotent


def test_ro_missing_store_raises(home: Path) -> None:
    with pytest.raises(FileNotFoundError):
        db.connect(ro=True)
    assert not (home / ".local/state/assistant/events.db").exists()


def test_test_mode_refuses_the_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    import pwd

    from core import paths

    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("HOME", next((e.pw_dir for e in pwd.getpwall() if e.pw_uid == os.getuid()), "/nonexistent-home"))
    with pytest.raises(paths.RealHomeRefused):
        db.connect()


def test_meta_roundtrip(home: Path) -> None:
    con = db.connect()
    with db.tx(con):
        db.meta_set(con, "x", {"a": [1, 2]})
    assert db.meta_get(con, "x") == {"a": [1, 2]}
    assert db.meta_get(con, "missing", 7) == 7


def test_tx_rolls_back_on_error(home: Path) -> None:
    con = db.connect()
    with pytest.raises(RuntimeError), db.tx(con):
        con.execute("INSERT INTO meta VALUES('k', '1')")
        raise RuntimeError("boom")
    assert db.meta_get(con, "k") is None


def _child(code: str, home: Path) -> subprocess.CompletedProcess[str]:
    prog = f"import os, sys\nsys.path[:0] = [{str(REPO)!r}, {str(REPO / 'packages')!r}]\n" + textwrap.dedent(code)
    return subprocess.run(
        [PY, "-I", "-c", prog], capture_output=True, text=True, timeout=30, env={**os.environ, "HOME": str(home)}
    )


def test_crash_between_insert_and_commit_leaves_nothing(home: Path) -> None:
    db.connect().close()
    r = _child(
        """
from core import db
con = db.connect()
con.execute("BEGIN IMMEDIATE")
con.execute("INSERT INTO events(ts, source, kind) VALUES(1, 't', 'x.y')")
os._exit(9)
""",
        home,
    )
    assert r.returncode == 9, r.stderr
    con = db.connect()
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with db.tx(con):  # the write lock is free again
        con.execute("INSERT INTO events(ts, source, kind) VALUES(1, 't', 'x.y')")


def test_emit_crash_after_note_before_commit_is_adopted_once(home: Path) -> None:
    """The note is written inside the transaction; a crash before COMMIT leaves only the note. Ingest adopts it
    (kind from the registry), and a retried emit with the same note name is a no-op."""
    db.connect().close()
    name = "train-watch-failed-1792602808.md"
    r = _child(
        f"""
from core import events
real = events._write_note
def boom(path, text):
    real(path, text)
    os._exit(7)
events._write_note = boom
events.emit(kind="cron.fail", subject="train-watch failed (cron)", note_name={name!r}, body="# t\\n")
""",
        home,
    )
    assert r.returncode == 7, r.stderr
    note = home / "inbox" / name
    assert note.exists()
    os.utime(note, (note.stat().st_mtime - 10,) * 2)
    con = db.connect()
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert run_events("ingest").returncode == 0
    rows = con.execute("SELECT kind, idem_key, payload FROM events").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("cron.fail", name)]
    assert '"job": "train-watch"' in rows[0][2]
    r2 = run_events("emit", "--kind", "cron.fail", "--subject", "again", "--note", name, "--body-file", "/dev/null")
    assert r2.returncode == 0 and r2.stdout.startswith("dup #"), r2.stdout + r2.stderr
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert note.read_text(encoding="utf-8") == "# t\n"  # a dup emit never rewrites the note
