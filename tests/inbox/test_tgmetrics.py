"""`events metrics --tg`: T1–T8 on a synthetic store and logs, --record (daily row + 30-day text nulling), --db."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import approvals
from inbox import tgmetrics
from inbox_testlib import run_events

from core import db, todo

NOW = 1949576400.0  # 2031-10-12 14:00 Zurich
DAY = 86400


def ev(con: sqlite3.Connection, kind: str, ts: float, payload: dict, subject: str = "his text", **kw: str) -> int:
    cur = con.execute(
        "INSERT INTO events(ts, source, kind, subject, next, payload, delivery) VALUES(?, ?, ?, ?, 'nx', ?, ?)",
        (ts, kw.get("source", "tg-adapter"), kind, subject, json.dumps(payload), kw.get("delivery", "now")),
    )
    return int(cur.lastrowid or 0)


def fill(con: sqlite3.Connection) -> None:
    with db.tx(con):
        for i, ms in enumerate([100, 200, 300, 400, 1500]):
            ev(con, "tg.button", NOW - 3600 - i, {"ack_ms": ms, "update_id": 15 + i, "bot": "b"})
        a = ev(con, "tg.message", NOW - 3000, {"update_id": 20, "bot": "b", "text": "x"})
        b = ev(con, "tg.message", NOW - 2000, {"update_id": 21, "bot": "b"})
        ev(con, "tg.message", NOW - 1000, {"update_id": 23, "bot": "b"})  # no receipt: dropped; update 22 missing
        ev(con, "tg.message", NOW - 60, {"update_id": 24, "bot": "b"})  # too young to judge
        con.execute("INSERT INTO deliveries(event_id, receipt_ts, how) VALUES(?, ?, 'channel')", (a, NOW - 2998))
        con.execute("INSERT INTO deliveries(event_id, receipt_ts, how) VALUES(?, ?, 'transcript')", (b, NOW - 1900))
        ev(con, "tg.bound", NOW - 500, {"ok": False, "ms": 900})
        db.meta_set(con, "dropped:not_allowed", 0)
        for i, (st, lag) in enumerate([("sent", 0.4), ("sent", 0.6), ("failed", None)]):
            con.execute(
                "INSERT INTO outbox(ts, bot, chat, op, body, status, sent_ts) VALUES(?, 'b', 'c', 'send', ?, ?, ?)",
                (NOW - 900 + i, f"body {i}", st, None if lag is None else NOW - 900 + i + lag),
            )
        ev(con, "outbox.failed", NOW - 890, {}, source="tg-adapter")


def logs(tmp: Path) -> dict[str, str]:
    (tmp / "restart.log").write_text(
        "2031-10-12 10:00:00 woke session after restart: load telegram-plus 0.2.1\n"
        "2031-10-12 11:00:00 woke session after restart: settings change\n"
        "2031-09-01 11:00:00 woke session after restart: telegram long ago\n",
        encoding="utf-8",
    )
    (tmp / "ensure.log").write_text(
        f"{NOW - 7200:.3f} start tree=a\n{NOW - 7100:.3f} start tree=a\n{NOW - 7000:.3f} start tree=a\n"
        f"{NOW - 6000:.3f} ok\n",
        encoding="utf-8",
    )
    (tmp / "baseline.jsonl").write_text(
        "\n".join(json.dumps({"hook_ts": NOW - 100 * i, "lag_s": float(i)}) for i in range(1, 11)) + "\n",
        encoding="utf-8",
    )
    return {
        "baseline": str(tmp / "baseline.jsonl"),
        "restart": str(tmp / "restart.log"),
        "deploys": str(tmp / "none.jsonl"),
        "ensure": str(tmp / "ensure.log"),
        "approvals": str(tmp / "approvals.db"),
    }


def test_compute(home: Path, tmp_path: Path) -> None:
    con = db.connect()
    fill(con)
    res = tgmetrics.compute(con, NOW - DAY, NOW, now=NOW, logs=logs(tmp_path), tz=todo.owner_tz())
    t1, t2, t3, t4, t5, t7, t8 = (res[k] for k in ("T1", "T2", "T3", "T4", "T5", "T7", "T8"))
    assert t1["n"] == 5 and t1["ok"] is False and t1["p95_ms"] > 1000
    assert t2["dropped"] >= 1 and t2["bound_not_ok"] == 1 and t2["update_gap"] == 1 and t2["gap_unexplained"] == 1
    assert t2["ok"] is False
    assert t3["n"] == 2 and t3["how"] == {"channel": 1, "transcript": 1} and t3["ok"] is False  # p95 ~ 95 s
    assert t3["baseline_0122"]["n"] == 10 and t3["baseline_0122"]["p50_s"] == 5.5
    assert t4["sent"] == 2 and t4["failed"] == 1 and t4["outbox_failed_events"] == 1 and t4["duplicates"] == 0
    assert t4["ok"] is False  # 1 of 3 failed > 0.5 %
    assert (t5["restarts"], t5["telegram_restarts"]) == (2, 1)
    assert t7["max_per_day"] == 3 and t7["ok"] is False
    assert t8["ok"] is None and "P7" in t8["why"]
    text = tgmetrics.render(res, todo.owner_tz())
    assert text.count("\n") == 8 and "his text" not in text and "T2 dropped      MISS" in text
    con.close()


def test_empty_store_is_no_data(home: Path, tmp_path: Path) -> None:
    con = db.connect()
    res = tgmetrics.compute(con, NOW - DAY, NOW, now=NOW, logs={}, tz=todo.owner_tz())
    assert all(res[f"T{i}"]["ok"] is None for i in (1, 2, 3, 4, 6, 7, 8)) and res["T5"]["ok"] is True
    con.close()


def test_duplicates_and_approvals(home: Path, tmp_path: Path) -> None:
    con = db.connect()
    with db.tx(con):
        for i in range(2):
            con.execute(
                "INSERT INTO outbox(ts, bot, chat, op, body, status, sent_ts)"
                " VALUES(?, 'b', 'c', 'send', 'same', 'sent', ?)",
                (NOW - 100 + i, NOW - 99 + i),
            )
    a = sqlite3.connect(tmp_path / "approvals.db")
    a.executescript(approvals.DDL)
    q = "INSERT INTO approvals(nonce, action_sha, created, expires, status, decided_ts, used_ts)"
    q += " VALUES(?, 's', 1, 10, ?, ?, ?)"
    a.execute(q, ("n1", "approved", 2, 11))  # used after expiry
    a.execute(q, ("n2", "approved", 2, 5))  # fine
    a.execute(q, ("n3", "denied", 2, None))  # fine: declined, never used
    a.commit()
    a.close()
    res = tgmetrics.compute(
        con, NOW - DAY, NOW, now=NOW, logs={"approvals": str(tmp_path / "approvals.db")}, tz=todo.owner_tz()
    )
    assert res["T4"]["duplicates"] == 1 and res["T8"] == {"ok": False, "violations": 1}
    con.close()


def test_t8_sql_is_the_approvals_audit() -> None:
    assert tgmetrics.T8_SQL == approvals.AUDIT_SQL


def test_prune_text(home: Path) -> None:
    con = db.connect()
    with db.tx(con):
        old = ev(con, "tg.message", NOW - 31 * DAY, {"update_id": 1, "message_id": 5, "text": "secret", "user": "o"})
        new = ev(con, "tg.message", NOW - 29 * DAY, {"update_id": 2, "text": "keep"})
        other = ev(con, "cron.fail", NOW - 40 * DAY, {"text": "job output"}, subject="job", source="cron")
    assert tgmetrics.prune_text(con, NOW) == 1
    assert tgmetrics.prune_text(con, NOW) == 0  # idempotent
    r = dict(con.execute("SELECT subject, next, payload FROM events WHERE id=?", (old,)).fetchone())
    p = json.loads(r["payload"])
    assert r["subject"] == "" and r["next"] == "" and p["update_id"] == 1 and p["message_id"] == 5
    assert "text" not in p and "user" not in p and p["_nulled"] == int(NOW)
    assert json.loads(con.execute("SELECT payload FROM events WHERE id=?", (new,)).fetchone()[0])["text"] == "keep"
    assert con.execute("SELECT subject FROM events WHERE id=?", (other,)).fetchone()[0] == "job"
    con.close()


def test_cli_tg_record_and_db(home: Path) -> None:
    con = db.connect()
    with db.tx(con):
        ev(con, "tg.message", NOW - 31 * DAY, {"text": "secret"})
    con.close()
    env = {"ASSISTANT_NOW": str(NOW)}
    r = run_events("metrics", "--tg", "--days", "7", "--record", env=env)
    assert r.returncode == 0, r.stderr
    assert "Telegram metrics" in r.stdout.splitlines()[0] and "text nulled in 1 row(s)" in r.stdout
    line = json.loads((home / ".local/state/assistant/tg-metrics.jsonl").read_text(encoding="utf-8"))
    assert line["day"] == "2031-10-12" and line["pruned"] == 1 and "T8" in line
    r = run_events("metrics", "--tg", "--json", env=env)
    assert r.returncode == 0 and set(json.loads(r.stdout)) >= {"T1", "T7"}
    other = home / "other.db"
    c = db.connect(other)
    c.close()
    r = run_events("metrics", "--tg", "--db", os.fspath(other), "--record", env=env)
    assert r.returncode == 0 and "text nulled" not in r.stdout  # another store is only read
    r = run_events("metrics", "--db", os.fspath(other), env=env)
    assert r.returncode == 2 and "--db goes with --tg" in r.stderr
