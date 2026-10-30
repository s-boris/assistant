"""Exit-review window (inbox.exitwindow, `slo window`): counts since the start from the existing stores (temp
HOME, fake events/tool bins), the bars, the etas, the weekly cycle and no number for a broken source."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from inbox import exitwindow

REPO = Path(__file__).resolve().parents[2]


def script(p: Path, obj: object, rc: int = 0) -> str:
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(obj)}\nEOF\nexit {rc}\n")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def src(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    now = time.time()
    t0 = now - 2 * 86400
    st = home / ".local" / "state"
    tr = st / "traces" / "traces.db"
    tr.parent.mkdir(parents=True)
    con = sqlite3.connect(tr)
    con.execute("CREATE TABLE turns (start_ts REAL, first_reply_ms INTEGER, trigger TEXT)")
    rows = [(t0 + 60 + i, 1000, "owner") for i in range(40)] + [(t0 - 60, 1000, "owner"), (t0 + 5, 1, "heartbeat")]
    con.executemany("INSERT INTO turns VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    gw = st / "tools" / "gateway.db"
    gw.parent.mkdir(parents=True)
    con = sqlite3.connect(gw)
    con.execute("CREATE TABLE calls (ts REAL, tool TEXT, ms INTEGER)")
    con.executemany("INSERT INTO calls VALUES (?, 'x', 1)", [(t0 + i,) for i in range(250)] + [(t0 - 9,)])
    con.commit()
    con.close()
    bins = tmp_path / "bins"
    bins.mkdir()
    monkeypatch.setenv("SLO_EVENTS_BIN", script(bins / "events", {"beats": {"beat_turns": 30}}))
    sched = {"data": {"schedules": [
        {"name": "cue:weekly-a", "mode": "live", "status": "ACTIVE", "cron": "0 9 * * 1", "last_fired": now - 3600},
        {"name": "cue:weekly-b", "mode": "live", "status": "ACTIVE", "cron": "0 9 * * 0", "last_fired": t0 - 86400},
        {"name": "cue:weekly-off", "mode": "shadow", "status": "ACTIVE", "cron": "0 9 * * 2", "last_fired": None},
        {"name": "watch:x", "mode": "live", "status": "ACTIVE", "cron": "*/5 * * * *", "last_fired": now},
    ]}}  # fmt: skip
    monkeypatch.setenv("SLO_TOOL_BIN", script(bins / "tool", sched))
    return {"now": now, "t0": t0, "bins": bins}


def test_no_window(src: dict) -> None:
    s = exitwindow.status(src["now"])
    assert s["open"] is False and "slo window start" in exitwindow.render(s)


def test_counts_bars_and_eta(src: dict) -> None:
    exitwindow.start(src["t0"], why="test")
    again = exitwindow.start(src["now"])  # already open: kept
    assert again["start"] == src["t0"]
    s = exitwindow.status(src["now"])
    by = {b["id"]: b for b in s["bars"]}
    assert by["beats"]["n"] == 30 and by["beats"]["ok"]
    assert by["owner_turns"]["n"] == 40 and not by["owner_turns"]["ok"]
    assert by["gateway_calls"]["n"] == 250 and by["gateway_calls"]["ok"]
    # 40 turns in 2 days -> 100 at day 5
    assert by["owner_turns"]["eta"] == pytest.approx(src["t0"] + 5 * 86400, abs=1)
    wk = by["weekly_cycle"]
    assert (wk["fired"], wk["weekly"], wk["not_fired"]) == (1, 2, ["cue:weekly-b"]) and not wk["ok"]
    assert s["eta"] == pytest.approx(src["t0"] + 7 * 86400, abs=1) and s["ready"] is False
    assert "his turns" in exitwindow.render(s)
    p = exitwindow.window_path()
    assert p.stat().st_mode & 0o777 == 0o600


def test_week_and_weekly_fired(src: dict) -> None:
    exitwindow.start(src["now"] - 8 * 86400)
    s = exitwindow.status(src["now"])
    by = {b["id"]: b for b in s["bars"]}
    assert by["owner_turns"]["n"] == 41 and by["gateway_calls"]["n"] == 251
    wk = by["weekly_cycle"]
    assert (wk["fired"], wk["weekly"]) == (2, 2) and wk["ok"] is True
    assert s["ready"] is False  # 41 of his turns < 100


def test_broken_source_is_no_number(src: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLO_EVENTS_BIN", script(src["bins"] / "events2", {}, rc=3))
    exitwindow.start(src["t0"])
    s = exitwindow.status(src["now"])
    b = s["bars"][0]
    assert b["n"] is None and b["ok"] is False and b["note"] == "RuntimeError" and s["eta"] is None


def test_cli(src: dict) -> None:
    env = dict(os.environ)
    r = subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "slo"), "window", "start", "--at", str(src["t0"])],
                       capture_output=True, text=True, env=env, check=False)  # fmt: skip
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "slo"), "window", "--json"],
                       capture_output=True, text=True, env=env, check=False)  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["bars"][1]["n"] == 40
    r = subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "slo"), "show", "start"],
                       capture_output=True, text=True, env=env, check=False)  # fmt: skip
    assert r.returncode == 2
