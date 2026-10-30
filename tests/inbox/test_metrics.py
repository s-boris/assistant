"""M2/M3, the old-path proxy and the §1 beat baselines on synthetic data."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from inbox import metrics

from core import db

Z = ZoneInfo("Europe/Zurich")
T0 = 1_900_000_000.0


def ev(
    con,
    eid,
    ts,
    *,
    status="listed",
    delivery="beat",
    first_listed=None,
    acked=None,
    updated=None,
    source="emit",
    kind="note.x",
):
    con.execute(
        "INSERT INTO events(id, ts, source, kind, delivery, status, first_listed_ts, acked_ts, updated_ts,"
        " idem_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (eid, ts, source, kind, delivery, status, first_listed, acked, updated, f"k{eid}"),
    )


def beat(con, bid, ts, listed, ok=1, source="idle"):
    con.execute(
        "INSERT INTO beats(id, ts, source, ok, ms, chars, listed) VALUES(?,?,?,?,?,?,?)",
        (bid, ts, source, ok, 10, 100, json.dumps(listed)),
    )


@pytest.fixture
def con(home: Path) -> sqlite3.Connection:
    return db.connect()


def test_m2_listed_first_within_two_latency_and_hook_fail(con: sqlite3.Connection) -> None:
    with db.tx(con):
        ev(con, 1, T0 + 10, first_listed=T0 + 100)  # listed in beat 1
        ev(con, 2, T0 + 20, first_listed=T0 + 700, delivery="now")  # missed beat 1, listed in beat 2
        ev(con, 3, T0 + 30, first_listed=T0 + 1300)  # only in beat 3
        ev(con, 4, T0 + 40, status="acked", acked=T0 + 50)  # acked before any beat: not counted
        ev(con, 5, T0 + 5000, status="new")  # no beat after it yet: pending
        beat(con, 1, T0 + 100, [1])
        beat(con, 2, T0 + 700, [1, 2])
        beat(con, 3, T0 + 1300, [1, 2, 3])
        beat(con, 4, T0 + 1900, [], ok=0)
    r = metrics.m2(con, T0, T0 + 6000, None)
    assert (r["events"], r["counted"], r["pending"]) == (5, 3, 1)
    assert r["listed_first"] == pytest.approx(1 / 3, abs=1e-4)
    assert r["listed_2"] == pytest.approx(2 / 3, abs=1e-4)
    assert r["beats"] == 4 and r["hook_fail"] == 0.25
    assert r["latency_min"]["now"]["n"] == 1 and r["latency_min"]["now"]["p95"] == pytest.approx(11.3, abs=0.1)


def test_m3_restart_stale_gone(con: sqlite3.Connection) -> None:
    now = T0 + 6 * 3600
    with db.tx(con):
        ev(con, 1, T0 + 10)  # pending at the restart, listed: ok
        ev(con, 2, T0 + 20)  # pending, not listed: a miss
        ev(con, 3, T0 + 30, status="acked", acked=T0 + 40)  # closed before: not pending
        ev(con, 4, T0 + 50, status="gone", updated=T0 + 60)  # gone before: not pending
        ev(con, 5, T0 + 3600, status="new")  # new, older than 3 h at `now`
        ev(con, 6, now - 600, status="new")  # new, fresh
        beat(con, 1, T0 + 100, [1], source="restart")
    r = metrics.m3(con, T0, now, now)
    assert r["restarts"][0]["pending"] == 2 and r["restarts"][0]["missing"] == [2] and not r["restarts_ok"]
    assert r["stale_new"] == 1 and r["gone_no_ack"] == 1


def traces_db(path: Path, turns: list[tuple]) -> str:
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE turns(id TEXT, start_ts REAL, trigger TEXT, start_kind TEXT, api_calls INTEGER,"
        " input INTEGER, output INTEGER, cache_read INTEGER, cache_w5 INTEGER, cache_w1h INTEGER, cost REAL,"
        " first_reply_ms INTEGER)"
    )
    c.executemany("INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", turns)
    c.commit()
    c.close()
    return str(path)


def test_old_path_proxy(con: sqlite3.Connection, tmp_path: Path) -> None:
    tdb = traces_db(
        tmp_path / "traces.db",
        [
            ("b1", T0 + 300, "inbox", "notes", 4, 1, 1, 100, 0, 0, 0.1, None),
            ("b2", T0 + 900, "heartbeat", "idle", 3, 1, 1, 100, 0, 0, 0.1, None),
        ],
    )
    with db.tx(con):
        ev(con, 1, T0 + 10, source="inbox", status="gone", updated=T0 + 400)  # still there at b1: seen
        ev(con, 2, T0 + 20, source="inbox", status="gone", updated=T0 + 200)  # deleted before b1
        ev(con, 3, T0 + 500, source="inbox", status="new")  # open, b2 next
        ev(con, 4, T0 + 950, source="inbox", status="new")  # no beat after: pending
    tc = metrics._ro(tdb)
    r = metrics.old_path(con, T0, T0 + 1000, tc)
    assert (r["notes"], r["pending"], r["deleted_before_first_beat"]) == (3, 1, 1)
    assert r["seen_by_a_beat"] == pytest.approx(2 / 3, abs=1e-4)
    assert r["to_first_beat_min"]["p50"] == pytest.approx(4.8, abs=0.1)


def test_beats_baseline_reproduces_formulas(con: sqlite3.Connection, tmp_path: Path) -> None:
    turns = [
        ("i1", T0 + 10, "heartbeat", "idle", 2, 10, 5, 1000, 0, 0, 0.2, None),
        ("i2", T0 + 20, "heartbeat", "idle", 4, 20, 5, 3000, 0, 0, 0.4, None),
        ("n1", T0 + 30, "inbox", "notes", 6, 30, 5, 5000, 100, 0, 0.6, None),
        ("o1", T0 + 40, "owner", "text", 3, 1, 1, 1, 0, 0, 0.1, 11000),
        ("o2", T0 + 50, "owner", "text", 3, 1, 1, 1, 0, 0, 0.1, 21000),
    ]
    tdb = traces_db(tmp_path / "traces.db", turns)
    log = tmp_path / "hb-gate.log"
    log.write_text(
        "2030-03-17 21:46:50 wake due: x\n2030-03-17 21:47:00 skip agent running: 1\n2020-01-01 00:00:00 wake old\n",
        encoding="utf-8",
    )
    with db.tx(con):
        db.meta_set(
            con,
            "compactions",
            [
                {"ts": T0 + 60, "trigger": "auto", "pre": 367000, "post": 8000, "ms": 51000},
                {"ts": T0 + 70, "trigger": "auto", "pre": 369000, "post": 30000, "ms": 60000},
            ],
        )
    res = metrics.compute(con, T0, T0 + 86400, now=T0 + 86400, traces_db=tdb, hbgate_log=str(log), tz=Z, beats=True)
    b = res["beats"]
    assert b["beat_turns"] == 3 and b["per_day"] == 3.0
    assert b["tokens"]["mean"] == pytest.approx((1015 + 3025 + 5135) / 3, abs=1)
    assert b["tokens"]["p50"] == 3025 and b["idle"]["n"] == 2 and b["notes"]["n"] == 1
    assert b["tokens"]["calls"] == 4.0 and b["idle"]["calls"] == 3.0
    assert b["context_per_call"] == pytest.approx(((1010 / 2) + (3020 / 4) + (5130 / 6)) / 3, abs=1)
    assert b["cost_per_beat"] == pytest.approx(0.4) and b["cost_per_day"] == pytest.approx(1.2)
    assert b["owner_first_reply_s"]["p50"] == 16.0
    assert b["compactions"]["n"] == 2 and b["compactions"]["post_min"] == 8000 and b["compactions"]["post_max"] == 30000
    assert b["hb_gate"] == {"wake": 1, "skip": 1}
    text = metrics.render(res, Z)
    assert "beats 3 (3.0/day)" in text and "hb-gate 1 wake / 1 skip" in text and "compactions 2" in text


def test_pct_matches_statistics_inclusive() -> None:
    import statistics

    xs = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]
    assert metrics.pct(xs, 0.95) == pytest.approx(statistics.quantiles(xs, n=20, method="inclusive")[18])
    assert metrics.pct([], 0.5) is None and metrics.pct([7], 0.95) == 7.0
