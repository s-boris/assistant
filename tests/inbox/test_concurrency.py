"""10 concurrent writers (+ 2 concurrent ingesters): every event once, no lock errors, store intact."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from inbox_testlib import PY, REPO

from core import db

WRITER = """
import sys
sys.path[:0] = [{repo!r}, {pkgs!r}]
from core import events
w = int(sys.argv[1])
for i in range(20):
    name = f"load-test-w{{w}}-{{i}}.md"
    r = events.emit(kind="note.load-test", subject=f"w{{w}} e{{i}}", next="ack it", note_name=name)
    assert r.id and not r.db_error, r
    r2 = events.emit(kind="note.load-test", subject="dup", note_name=f"load-test-w{{w}}-{{i}}.md")
    assert r2.dup and r2.id == r.id, r2
print("ok")
"""

INGESTER = """
import sys, time
sys.path[:0] = [{repo!r}, {pkgs!r}]
from core import db
from inbox import ingest, kinds
reg = kinds.load({kinds!r})
for _ in range(15):
    con = db.connect()
    ingest.ingest(con, reg, {inbox!r}, time.time(), min_age=0.0)
    con.close()
print("ok")
"""


def test_ten_concurrent_writers_and_ingest(home: Path) -> None:
    db.connect().close()
    fmt = {
        "repo": str(REPO),
        "pkgs": str(REPO / "packages"),
        "kinds": str(REPO / "schemas" / "kinds.toml"),
        "inbox": str(home / "inbox"),
    }
    procs = [
        subprocess.Popen(
            [PY, "-I", "-c", WRITER.format(**fmt), str(w)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ,
        )
        for w in range(10)
    ]
    procs += [
        subprocess.Popen(
            [PY, "-I", "-c", INGESTER.format(**fmt)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ,
        )
        for _ in range(2)
    ]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0 and out.strip() == "ok", err[-800:]
    con = db.connect()
    assert con.execute("SELECT COUNT(*), COUNT(DISTINCT idem_key) FROM events").fetchone()[:] == (200, 200)
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind != 'note.load-test'").fetchone()[0] == 0
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(list((home / "inbox").glob("load-test-*.md"))) == 200
    assert not (home / ".local/state/assistant/emit-errors.log").exists()
