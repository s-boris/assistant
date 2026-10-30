"""Receipts: channel prompts through the agenda hook (event_id tags -> deliveries + ack by channel; 0.1.22 prompts
-> baseline lines), fail-open, timing; the DDL v2 migration; the agenda's undelivered owner events. Synthetic values."""

from __future__ import annotations

import json
import os
import resource
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

import pytest
from inbox import agenda, kinds, receipts
from inbox_testlib import EVENTS, HOOK

from core import db, todo

BEAT = "[heartbeat] Idle 10+ min, 0 new note(s) in ~/inbox. Run your heartbeat routine."
SRC = "plugin:telegram-plus:telegram"


def tag(eid: int | None, mid: int, text: str = "hi", ts: str = "2031-06-15T12:00:00.000Z", **extra: str) -> str:
    attrs = f'source="{SRC}" chat_id="100" message_id="{mid}" user="o" ts="{ts}"'
    if eid is not None:
        attrs += f' event_id="{eid}"'
    for k, v in extra.items():
        attrs += f' {k}="{v}"'
    return f"<channel {attrs}>\n{text}\n</channel>"


def run_hook(home: Path, prompt: str, cwd: str | None = None, env: dict[str, str] | None = None):
    data = json.dumps({"prompt": prompt, "cwd": cwd or str(home / "workspace"), "hook_event_name": "UserPromptSubmit"})
    t0 = time.monotonic()
    r = subprocess.run(
        ["/bin/sh", str(HOOK)],
        input=data,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "EVENTS_BIN": str(EVENTS), **(env or {})},
    )
    return r, time.monotonic() - t0


def tg_row(con: sqlite3.Connection, kind: str, ts: float, mid: int, text: str = "secret words") -> int:
    with db.tx(con):
        cur = con.execute(
            "INSERT INTO events(ts, source, kind, subject, payload, labels, delivery, idem_key)"
            " VALUES(?, 'tg-adapter', ?, ?, ?, '[\"tg\",\"live\"]', 'now', ?)",
            (
                ts,
                kind,
                text,
                json.dumps({"message_id": mid, "update_id": 900 + mid, "bot": "b", "text": text}),
                f"k{mid}",
            ),
        )
    return int(cur.lastrowid or 0)


def test_migration_v1_store_gets_v2_tables(home: Path) -> None:
    p = home / ".local/state/assistant/events.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    for s in db.ddl_statements():
        c.execute(s)
    c.execute("INSERT INTO meta VALUES('schema', '1')")
    c.execute("INSERT INTO outbox(ts, bot, chat, op, body) VALUES(1, 'b', 'c', 'send', 'x')")
    c.commit()
    c.close()
    con = db.connect()
    assert db.meta_get(con, "schema") == 2
    cols = [r[1] for r in con.execute("PRAGMA table_info(outbox)")]
    assert (
        cols[-3:] == ["reply_to_event", "files", "result"]
        and con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    )
    assert receipts.has_deliveries(con)
    assert db.migrate(con) == 2  # idempotent
    con.close()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_v2_outbox_column_already_there(home: Path) -> None:
    # a store where a writer already added one of the columns (e.g. a test DB): only the missing one is added
    p = home / ".local/state/assistant/events.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    for s in db.ddl_statements():
        c.execute(s)
    c.execute("INSERT INTO meta VALUES('schema', '1')")
    c.execute("ALTER TABLE outbox ADD COLUMN reply_to_event INTEGER")
    c.commit()
    c.close()
    con = db.connect()
    assert [r[1] for r in con.execute("PRAGMA table_info(outbox)")][-3:] == ["reply_to_event", "files", "result"]
    con.close()


def test_channel_tags_parsing() -> None:
    two = tag(5, 10) + tag(6, 11, event_type="button")
    assert [t["event_id"] for t in receipts.channel_tags(two)] == ["5", "6"]
    assert receipts.channel_tags("<system-reminder>x</system-reminder>\n" + tag(7, 12))[0]["event_id"] == "7"
    assert receipts.channel_tags("please look at " + tag(7, 12)) == []  # typed text quoting a tag
    assert receipts.channel_tags(BEAT) == []
    assert receipts.channel_tags('<channel source="other-plugin" x="1">hi</channel>') == []


def test_receipts_ack_and_baseline(home: Path) -> None:
    con = db.connect()
    now = time.time()
    a = tg_row(con, "tg.message", now - 5, 10)
    b = tg_row(con, "tg.button", now - 3, 11)
    r, dt = run_hook(home, tag(a, 10) + tag(b, 11, event_type="button"))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    rows = {x["event_id"]: dict(x) for x in con.execute("SELECT * FROM deliveries")}
    assert set(rows) == {a, b} and all(x["how"] == "channel" and x["receipt_ts"] for x in rows.values())
    st = {x["id"]: (x["status"], x["ack_by"]) for x in con.execute("SELECT id, status, ack_by FROM events")}
    assert st == {a: ("acked", "channel"), b: ("acked", "channel")}
    first = rows[a]["receipt_ts"]
    run_hook(home, tag(a, 10, redelivered="1"))  # a redelivered copy keeps the first receipt
    assert con.execute("SELECT receipt_ts FROM deliveries WHERE event_id=?", (a,)).fetchone()[0] == first
    log = (home / ".local/state/assistant/agenda-hook.log").read_text(encoding="utf-8")
    assert f"receipt ids={a},{b} baseline=0" in log and "secret" not in log
    # 0.1.22 prompts (no event_id): baseline lines, ids and times only
    r, _ = run_hook(home, tag(None, 77, text="owner words", ts="2031-06-15T12:00:00.000Z"))
    assert r.stdout == ""
    bl = home / ".local/state/assistant/receipts-baseline.jsonl"
    line = json.loads(bl.read_text(encoding="utf-8").splitlines()[-1])
    assert line["message_id"] == "77" and line["event_type"] == "message" and isinstance(line["lag_s"], float)
    assert "owner words" not in bl.read_text(encoding="utf-8") and stat.S_IMODE(bl.stat().st_mode) == 0o600
    assert dt < 2.0
    con.close()


def test_receipt_refuses_mismatch_wrong_kind_and_other_sessions(home: Path) -> None:
    con = db.connect()
    now = time.time()
    a = tg_row(con, "tg.message", now - 5, 10)
    with db.tx(con):
        n = con.execute("INSERT INTO events(ts, source, kind, subject) VALUES(?, 'cron', 'cron.fail', 'x')", (now,))
    other = int(n.lastrowid or 0)
    run_hook(home, tag(a, 99))  # message_id disagrees with the row: a forged tag
    run_hook(home, tag(other, 1))  # not an owner-delivery kind
    run_hook(home, tag(a, 10), cwd=str(home / "jobs"))  # not the main session
    assert con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    assert con.execute("SELECT status FROM events WHERE id=?", (a,)).fetchone()[0] == "new"
    log = (home / ".local/state/assistant/agenda-hook.log").read_text(encoding="utf-8")
    assert "skipped=1" in log
    con.close()


@pytest.mark.parametrize("switch", ["agenda-off", "events-off"])
def test_receipt_switches(home: Path, switch: str) -> None:
    con = db.connect()
    a = tg_row(con, "tg.message", time.time(), 10)
    (home / ".config/assistant" / switch).touch()
    r, _ = run_hook(home, tag(a, 10))
    assert (r.returncode, r.stdout) == (0, "")
    assert con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    con.close()


def test_receipt_fails_open_on_a_locked_store(home: Path) -> None:
    con = db.connect()
    a = tg_row(con, "tg.message", time.time(), 10)
    lock = db.connect()
    lock.execute("BEGIN IMMEDIATE")
    try:
        r, dt = run_hook(home, tag(a, 10))
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "") and dt < 2.0
    log = (home / ".local/state/assistant/agenda-hook.log").read_text(encoding="utf-8")
    assert " receipt-fail " in log
    con.close()


def test_receipt_fails_open_on_a_broken_store(home: Path) -> None:
    p = home / ".local/state/assistant/events.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not a database at all" * 100)
    r, _ = run_hook(home, tag(3, 10))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_receipt_speed(home: Path) -> None:
    """p95 of the whole hook's CPU time (sh, timeout and python start included) under 0.3 s over 20 receipts. CPU, not
    wall time: CI shares the box (two runner jobs, suites side by side), where a hook's wall time is mostly waiting for
    a CPU; a hook that does too much work fails here at any load. Waiting is the 2 s fail-open cut's (test_hook.py),
    and each receipt must land (a hook cut off records none)."""
    con = db.connect()
    now = time.time()
    mids = [100 + i for i in range(20)]
    ids = [tg_row(con, "tg.message", now, mid) for mid in mids]
    cpu = []
    for eid, mid in zip(ids, mids, strict=True):
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)  # the hook and what it waited for, once it is reaped
        r, _ = run_hook(home, tag(eid, mid))
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        assert r.returncode == 0
        cpu.append((r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime))
    cpu.sort()
    assert cpu[int(len(cpu) * 0.95) - 1] < 0.3, cpu
    n = con.execute("SELECT COUNT(*) FROM deliveries WHERE receipt_ts IS NOT NULL").fetchone()[0]
    assert n == len(ids)
    con.close()


def _agenda(con: sqlite3.Connection, now: float) -> agenda.Agenda:
    tz = todo.owner_tz()
    return agenda.build(con, now_ts=now, beat_id=1, last_beat_ts=None, reg=kinds.load(), tz=tz)


def test_agenda_undelivered_line_without_text(home: Path) -> None:
    con = db.connect()
    now = time.time()
    old = tg_row(con, "tg.message", now - 7 * 60, 10, text="very private words")
    young = tg_row(con, "tg.button", now - 60, 11, text="young press")
    done = tg_row(con, "tg.message", now - 20 * 60, 12, text="received words")
    with db.tx(con):
        con.execute("INSERT INTO deliveries(event_id, receipt_ts, how) VALUES(?, ?, 'channel')", (done, now - 19 * 60))
        con.execute("UPDATE events SET status='acked', ack_by='channel' WHERE id=?", (done,))
    ag = _agenda(con, now)
    assert f"Undelivered owner event #{old} (tg.message, 7 min)" in ag.text
    assert "private" not in ag.text and "young" not in ag.text and "received" not in ag.text
    assert f"#{young}" not in ag.text and ag.listed == [old] and ag.counts["undelivered"] == 1
    assert "New (" not in ag.text  # owner rows never appear as ordinary events
    # a receipt that arrives late (bridge redelivery) clears it
    with db.tx(con):
        con.execute("INSERT INTO deliveries(event_id, receipt_ts, how) VALUES(?, ?, 'transcript')", (old, now))
    assert "Undelivered" not in _agenda(con, now + 1).text
    con.close()


def test_other_tg_rows_never_listed_and_tg_bound_receipts(home: Path) -> None:
    con = db.connect()
    now = time.time()
    with db.tx(con):
        dig = con.execute(
            "INSERT INTO events(ts, source, kind, subject, delivery) VALUES(?, 'tg-adapter', 'tg.bound', 'user: words',"
            " 'digest')",
            (now - 3600,),
        ).lastrowid
        con.execute(
            "INSERT INTO events(ts, source, kind, subject, delivery) VALUES(?, 'tg-adapter', 'tg.other', 'user: more',"
            " 'beat')",
            (now - 3600,),
        )
        und = con.execute(
            "INSERT INTO events(ts, source, kind, subject, delivery) VALUES(?, 'tg-bridge', 'bridge.undelivered',"
            " 'undelivered owner event #1 (tg.message)', 'now')",
            (now - 60,),
        ).lastrowid
    ag = _agenda(con, now)
    assert "words" not in ag.text and "more" not in ag.text and "Undelivered owner event" not in ag.text
    assert f"#{und} bridge.undelivered" in ag.text and dig not in ag.listed
    bound = tg_row(con, "tg.bound", now - 5, 40)  # note-on bound outcome, delivered through the channel
    run_hook(home, tag(bound, 40))
    assert con.execute("SELECT ack_by FROM events WHERE id=?", (bound,)).fetchone()[0] == "channel"
    con.close()


def test_agenda_many_undelivered_grouped(home: Path) -> None:
    con = db.connect()
    now = time.time()
    ids = [tg_row(con, "tg.reaction", now - 3600, 200 + i) for i in range(8)]
    ag = _agenda(con, now)
    assert ag.text.count("Undelivered owner event #") == 5 and f"+3 more undelivered: #{ids[5]}-{ids[7]}" in ag.text
    assert sorted(ag.listed) == ids
    con.close()


def test_hook_beat_shows_undelivered_and_marks_listed(home: Path) -> None:
    con = db.connect()
    a = tg_row(con, "tg.message", time.time() - 600, 10)
    r, _ = run_hook(home, BEAT)
    text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f"Undelivered owner event #{a} (tg.message, 10 min)" in text
    assert con.execute("SELECT status FROM events WHERE id=?", (a,)).fetchone()[0] == "listed"
    con.close()
