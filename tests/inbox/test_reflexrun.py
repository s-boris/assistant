"""inbox.reflexrun: `events ack` fires the reflexes an acked event matches (v2 exit E6)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from inbox import reflexrun


def _stub(p: Path, body: str) -> str:
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    st = tmp_path / "loop"
    st.mkdir()
    con = sqlite3.connect(st / "reflex.db")
    con.execute(
        "CREATE TABLE firings (id INTEGER PRIMARY KEY, ts REAL, name TEXT, version INTEGER, event TEXT,"
        " outcome TEXT, ms INTEGER, note TEXT)"
    )
    con.execute("INSERT INTO firings (ts, name, event, outcome) VALUES (1, 'hddheat-quiet', '7', 'ok')")
    con.commit()
    con.close()
    log = tmp_path / "calls.log"
    match = json.dumps([{"name": "hddheat-quiet", "tool": "server.health", "args": {}, "eval_case": "c"}])
    _stub(
        tmp_path / "reflex",
        f'echo "$@" >> "{log}"\ncase "$1 $2" in "match note.hddheat") echo \'{match}\';; match*) echo "[]";; esac',
    )
    _stub(tmp_path / "tool", f'echo "$@" >> "{log}"\necho \'{{"ok": true, "text": "array ok · disks 6 · warn 0"}}\'')
    monkeypatch.setenv("LOOP_STATE", str(st))
    monkeypatch.setenv("REFLEX_BIN", str(tmp_path / "reflex"))
    monkeypatch.setenv("TOOL_BIN", str(tmp_path / "tool"))
    return tmp_path


def test_fires_matching_reflex_once_per_event(bins: Path) -> None:
    evs = [
        {"id": 5, "kind": "note.hddheat", "payload": {"max_c": 47}},
        {"id": 6, "kind": "note.board", "payload": "{}"},
        {"id": 7, "kind": "note.hddheat", "payload": {}},  # already fired for #7: not again
    ]
    lines = reflexrun.run(evs)
    assert len(lines) == 1 and lines[0].startswith("#5 reflex hddheat-quiet → server.health ok (")
    assert lines[0].endswith("array ok · disks 6 · warn 0")
    calls = (bins / "calls.log").read_text().splitlines()
    assert any(c.startswith("call server.health {}") for c in calls)
    assert any(c.startswith("fire hddheat-quiet --outcome ok --event 5 --ms ") for c in calls)
    assert not any("--event 7" in c for c in calls)


def test_tool_error_is_logged_as_error_and_ack_goes_on(bins: Path) -> None:
    _stub(bins / "tool", 'echo \'{"ok": false, "error": {"code": "upstream", "message": "timeout"}}\'; exit 1')
    lines = reflexrun.run([{"id": 9, "kind": "note.hddheat", "payload": None}])
    assert lines and " error (" in lines[0] and lines[0].endswith("upstream: timeout")
    assert any("--outcome error --event 9" in c for c in (bins / "calls.log").read_text().splitlines())


def test_off_switch_and_missing_cli(bins: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFLEX_BIN", str(bins / "nope"))
    assert reflexrun.run([{"id": 5, "kind": "note.hddheat", "payload": {}}]) == []
    monkeypatch.setattr(reflexrun, "enabled", lambda: False)
    monkeypatch.setenv("REFLEX_BIN", str(bins / "reflex"))
    assert reflexrun.run([{"id": 5, "kind": "note.hddheat", "payload": {}}]) == []
