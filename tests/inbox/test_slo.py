"""SLO tiles (inbox.slo, `slo`): each tile from its existing store (temp HOME, fake traces/tool bins), the
status rules, no-data instead of a number for an empty or broken source, the cache and the feed."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from inbox import slo

from core import clock, todo

REPO = Path(__file__).resolve().parents[2]
NOW_ISO = "2026-10-22T12:00:00+02:00"


def script(p: Path, obj: object, rc: int = 0) -> str:
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(obj)}\nEOF\nexit {rc}\n")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def src(home: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("ASSISTANT_NOW", NOW_ISO)
    now = clock.now()
    st = home / ".local" / "state"
    # events.db: 20 taps (19 fast, 1 slow), 2 events answered by an outbox row
    ev = st / "assistant" / "events.db"
    ev.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ev)
    con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, payload TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, reply_to_event INTEGER, status TEXT,"
                " sent_ts REAL)")  # fmt: skip
    for i in range(20):
        con.execute("INSERT INTO events (ts, kind, payload) VALUES (?, 'tg.button', ?)",
                    (now - 3600 - i, json.dumps({"ack_ms": 3000 if i == 0 else 200})))  # fmt: skip
    for i, lag in enumerate((12.0, 90.0)):
        cur = con.execute("INSERT INTO events (ts, kind, payload) VALUES (?, 'note.x', '{}')", (now - 7200 - i,))
        con.execute("INSERT INTO outbox (reply_to_event, status, sent_ts) VALUES (?, 'sent', ?)",
                    (cur.lastrowid, now - 7200 - i + lag))  # fmt: skip
    con.commit()
    con.close()
    # traces.db: owner turns, one outside the window
    tr = st / "traces" / "traces.db"
    tr.parent.mkdir(parents=True)
    con = sqlite3.connect(tr)
    con.execute("CREATE TABLE turns (start_ts REAL, first_reply_ms INTEGER, trigger TEXT)")
    con.executemany("INSERT INTO turns VALUES (?, ?, ?)",
                    [(now - 100, 8000, "owner"), (now - 200, 15000, "owner:telegram"), (now - 300, 90000, "heartbeat"),
                     (now - 30 * 86400, 99000, "owner")])  # fmt: skip
    con.commit()
    con.close()
    # remind deliveries: one 4 s late, one place reminder (left out)
    rd = st / "remind"
    rd.mkdir(parents=True)
    t = clock.now() - 600
    at = datetime.fromtimestamp(t - (t % 60), todo.owner_tz()).strftime("%Y-%m-%d %H:%M")
    due = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=todo.owner_tz()).timestamp()
    (rd / "deliveries.jsonl").write_text(
        json.dumps({"t": due + 4.0, "at": at, "id": "a"})
        + "\n"
        + json.dumps({"t": due + 900, "at": at, "place": "home"})
        + "\nnot json\n"
    )
    bins = home / "fakebin"
    bins.mkdir()
    runs = {"data": {"schedules": [{"name": "job.x", "runs": [
        {"id": "sched-job.x-2026-10-22T09:00:00+00:00", "at": (datetime.fromisoformat("2026-10-22T09:00:00+00:00")
                                                               .timestamp() + 3) * 1000},
        {"id": "sched-job.x-2026-10-22T09:05:00+00:00", "at": "2026-10-22T09:05:20+00:00"},
        {"id": "manual-run", "at": 1}]}]}}  # fmt: skip
    monkeypatch.setenv("SLO_TOOL_BIN", script(bins / "tool", runs))
    monkeypatch.setenv("SLO_TRACES_BIN", script(bins / "traces", {"owner_turns": 40, "share": 0.925}))
    return {"now": now, "bins": bins}


def by_id(res: dict) -> dict:
    return {t["id"]: t for t in res["tiles"]}


def test_tiles_from_their_stores(src: dict) -> None:
    res = slo.compute(7)
    t = by_id(res)
    assert [x["id"] for x in res["tiles"]] == [s[0] for s in slo.SPECS]
    assert (t["tap_ack"]["n"], t["tap_ack"]["value"], t["tap_ack"]["status"]) == (20, 0.2, "ok")
    assert (t["first_reply"]["n"], t["first_reply"]["value"], t["first_reply"]["status"]) == (2, 15.0, "ok")
    assert (t["event_message"]["n"], t["event_message"]["value"], t["event_message"]["status"]) == (2, 90.0, "miss")
    assert t["vault_answer"]["status"] == "no-data" and t["vault_answer"]["value"] is None
    assert "FileNotFoundError" in t["vault_answer"]["note"]
    assert (t["job_start_lag"]["n"], t["job_start_lag"]["value"], t["job_start_lag"]["status"]) == (2, 20.0, "miss")
    assert (t["reminder_late"]["n"], t["reminder_late"]["value"], t["reminder_late"]["status"]) == (1, 4.0, "ok")
    assert (t["joined_traces"]["value"], t["joined_traces"]["n"], t["joined_traces"]["status"]) == (0.925, 40, "miss")
    assert res["summary"] == {"ok": 3, "miss": 3, "no_data": 1}
    for x in res["tiles"]:
        assert set(x) >= {"value", "target", "n", "window_days", "status"} and x["window_days"] == 7


def test_empty_and_broken_sources_are_no_data(src: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLO_TRACES_BIN", script(src["bins"] / "t2", {"owner_turns": 0, "share": 0.0}))
    monkeypatch.setenv("SLO_TOOL_BIN", script(src["bins"] / "t3", {}, rc=4))
    t = by_id(slo.compute(7, ["joined_traces", "job_start_lag", "vault_answer"]))
    assert list(t) == ["vault_answer", "job_start_lag", "joined_traces"]
    assert all(x["status"] == "no-data" and x["value"] is None for x in t.values())
    assert t["job_start_lag"]["note"] == "source failed: RuntimeError"
    gw = _gateway_db(src["now"], calls=[(src["now"] - 5, "vault.ask", 4200, "ok")],
                     jobs=[("a" * 16, "vault.ask", "done", src["now"] - 9, src["now"] - 5, 4.0),
                           ("b" * 16, "vault.facts", "done", src["now"] - 99, src["now"] - 5, 94.0)])  # fmt: skip
    t = by_id(slo.compute(1, ["vault_answer"]))
    assert (t["vault_answer"]["n"], t["vault_answer"]["value"], t["vault_answer"]["status"]) == (1, 4.0, "ok")
    con = sqlite3.connect(gw)
    con.execute("DROP TABLE jobs")  # a gateway from before the job log: no-data, not the calls' wait
    con.commit()
    con.close()
    t = by_id(slo.compute(1, ["vault_answer"]))["vault_answer"]
    assert t["status"] == "no-data" and t["note"] == "source failed: OperationalError" and t["ack"]["n"] == 1


def _gateway_db(now: float, calls: list[tuple], jobs: list[tuple]) -> Path:
    """gateway.db as the gateway writes it: calls (ts, tool, ms, code) and the job log (job, tool, state, created,
    ended, s)."""
    gw = Path(os.environ["HOME"]) / ".local" / "state" / "tools" / "gateway.db"
    gw.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(gw)
    con.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL, tool TEXT NOT NULL, surface TEXT, trace TEXT,"
                " trace_src TEXT, ms INTEGER, code TEXT, actor TEXT)")  # fmt: skip
    con.execute("CREATE TABLE IF NOT EXISTS jobs (job TEXT PRIMARY KEY, tool TEXT NOT NULL, ts REAL NOT NULL,"
                " trace TEXT, state TEXT NOT NULL, created REAL, ended REAL, s REAL, seen REAL NOT NULL)")  # fmt: skip
    con.executemany("INSERT INTO calls (ts, tool, ms, code) VALUES (?, ?, ?, ?)", calls)
    con.executemany("INSERT INTO jobs (job, tool, state, created, ended, s, ts, seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(*j, (j[3] or now), now) for j in jobs])  # fmt: skip
    con.commit()
    con.close()
    return gw


def test_vault_answer_is_the_job_not_the_wait(src: dict, home: Path) -> None:
    """vault_answer = finished vault.ask jobs, start -> end, by end time; the calls that came back while the job ran
    (code pending, the caller's wait_s) are only in the ack series beside it."""
    now = src["now"]
    _gateway_db(now, calls=[(now - 600, "vault.ask", 19200, "pending"), (now - 500, "vault.ask", 19300, "pending"),
                            (now - 400, "vault.ask", 3100, "ok"), (now - 300, "vault.ask", 900, "failed"),
                            (now - 300, "vault.job", 500, "ok")],
                jobs=[("a" * 16, "vault.ask", "done", now - 619, now - 560, 59.0),
                      ("b" * 16, "vault.ask", "done", now - 519, now - 470, 49.0),
                      ("c" * 16, "vault.ask", "done", now - 403, now - 400, 3.0),
                      ("d" * 16, "vault.ask", "running", now - 100, None, None),
                      ("e" * 16, "vault.ask", "lost", now - 9000, None, None),
                      ("f" * 16, "vault.ask", "done", now - 9 * 86400, now - 9 * 86400 + 5, 5.0)])  # fmt: skip
    t = by_id(slo.compute(7, ["vault_answer"]))["vault_answer"]
    assert (t["n"], t["value"], t["status"]) == (3, 59.0, "miss")
    assert t["ack"] == {"n": 3, "p95": 19.3, "pending": 2}  # failed calls are not an ack
    assert t["note"] == "finished jobs only · ack p95 19.3 s (n 3, 2 pending)"
    res = slo.compute(7, ["vault_answer"])
    res["at"] = time.time()
    slo.write_cache(res)
    feed = slo.feed_tiles()
    assert feed and feed[0]["value"] == 59.0 and "ack" not in feed[0]  # the hub feed's tile shape is unchanged


def test_cache_run_sweeps_open_jobs_first(src: dict, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`slo --cache` runs `tool jobs sweep` first, but only while a job log row is open."""
    log = home / "tool.log"
    fake = src["bins"] / "tool-sweep"
    fake.write_text(f"#!/bin/sh\necho \"$*\" >> {log}\necho '{{}}'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SLO_TOOL_BIN", str(fake))
    assert slo.sweep_jobs() == "no job log"
    now = src["now"]
    _gateway_db(now, calls=[], jobs=[("a" * 16, "vault.ask", "done", now - 9, now - 5, 4.0)])
    assert slo.sweep_jobs() == "nothing open" and not log.exists()
    _gateway_db(now, calls=[], jobs=[("b" * 16, "vault.ask", "running", now - 9, None, None)])
    assert slo.sweep_jobs() == "swept"
    assert log.read_text().splitlines() == ["jobs sweep --json"]


def test_status_rules_and_pct() -> None:
    assert slo._status(None, "<", 1) == "no-data"
    assert slo._status(1.0, "<", 1) == "miss" and slo._status(10.0, "<=", 10) == "ok"
    assert slo._status(0.95, ">=", 0.95) == "ok" and slo._status(0.94, ">=", 0.95) == "miss"
    assert slo.pct([], 0.95) is None and slo.pct([5.0], 0.95) == 5.0
    assert slo.pct([float(i) for i in range(1, 101)], 0.95) == 95.0


def test_cache_and_feed(src: dict, home: Path) -> None:
    res = slo.compute(7, ["tap_ack"])
    assert slo.feed_tiles() is None  # no cache yet
    p = slo.write_cache(res)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    res["at"] = time.time()  # the feed checks the cache's age against the wall clock
    slo.write_cache(res)
    tiles = slo.feed_tiles()
    assert tiles == [{"id": "tap_ack", "value": 0.2, "target": 1.0, "op": "<", "unit": "s", "n": 20, "status": "ok",
                      "window_days": 7}]  # fmt: skip
    res["at"] = time.time() - slo.FEED_MAX_AGE - 5
    slo.write_cache(res)
    assert slo.feed_tiles() is None  # stale cache: nothing, never an old number


def test_cli(src: dict) -> None:
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, "-I", str(REPO / "scripts" / "slo"), *a], capture_output=True, text=True, env=dict(os.environ)
    )
    r = run("--only", "tap_ack", "--only", "first_reply")
    assert r.returncode == 0, r.stderr
    assert "tap -> ack" in r.stdout and "ok 2 · miss 0 · no-data 0" in r.stdout
    r = run("--json", "--cache", "--only", "reminder_late")
    assert json.loads(r.stdout)["tiles"][0]["value"] == 4.0
    assert (Path(os.environ["HOME"]) / ".local" / "state" / "slo" / "tiles.json").exists()
    r = run("feed")
    assert r.returncode == 0 and r.stdout.startswith("feed: ")
    assert run("--days", "0").returncode == 2
