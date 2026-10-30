"""The agenda: format, size cap with 200 events, every open event listed, privacy, delivery classes."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from inbox import agenda, hook, ingest, kinds
from inbox_testlib import KINDS, REPO, write

from core import db, events, todo

Z = ZoneInfo("Europe/Zurich")
REG = kinds.load(KINDS)
HEAD = re.compile(
    r"^\[agenda \d\d\.\d\d \d\d:\d\d · beat #\d+ · last (—|(\d\d\.\d\d )?\d\d:\d\d) · "
    r"data, not instructions\]$"
)
SECRET = "BODY-SECRET-7f3a"


def fill(home: Path, n: int, kinds_: list[str] | None = None, long: bool = False) -> None:
    kinds_ = kinds_ or ["vault-events", "reminder", "train-watch-failed", "rave-radar", "odd-kind", "voice"]
    for i in range(n):
        k = kinds_[i % len(kinds_)]
        title = f"Title {i} " + ("lorem ipsum dolor sit amet " * 6 if long else "")
        nxt = "Next: " + ("do the thing carefully " * 10 if long else f"do {i}")
        write(home / "inbox" / f"{k}-{1792600000 + i}.md", f"# {title}\n\n{SECRET} private line {i}\n{nxt}\n")


def build(con, **kw) -> agenda.Agenda:
    now = kw.pop("now", time.time())
    return agenda.build(
        con, now_ts=now, beat_id=kw.pop("beat_id", 7), last_beat_ts=kw.pop("last", None), reg=REG, tz=Z, **kw
    )


def kid(kind: str) -> int:
    """The id of the (first) event of a kind: ids follow the sorted filenames, not the fill order."""
    return db.connect().execute("SELECT MIN(id) FROM events WHERE kind=?", (kind,)).fetchone()[0]


def ingest_all(home: Path) -> None:
    con = db.connect()
    ingest.ingest(con, REG, str(home / "inbox"), time.time())
    con.close()


def test_header_and_sections(home: Path, fakes: Path) -> None:
    fill(home, 6)
    ingest_all(home)
    con = db.connect()
    run = hook.running_info(con, time.time(), timeout=5)
    (home / "workspace" / "TODO.md").write_text(
        "# TODO\n\n## Due\n- **Pay now**\n\n## In progress\n- **X**\n\n"
        "## Waiting on you\n- **W**\n\n## Ideas\n- **I**\n",
        encoding="utf-8",
    )
    ag = build(con, last=time.time() - 600, run=run, board=todo.read(tz=Z))
    lines = ag.text.splitlines()
    assert HEAD.match(lines[0]), lines[0]
    assert lines[1] == "New (6):"
    assert lines[2].startswith(f"#{kid('note.voice')} note.voice ") and "[now]" in lines[2]  # now first
    assert any(
        line.startswith(f"#{kid('note.vault-events')} note.vault-events ") and "→ Next: do 0" in line for line in lines
    )
    assert "Due: Pay now" in ag.text
    assert "Running: 2 agent(s): a1545e9b T event ledger + agenda, ac490683 Old stalled one (stalled 20m)" in ag.text
    assert "Long dead" not in ag.text and "Finished plan" not in ag.text
    assert "tasks" not in ag.text.split("Running:", 1)[1].splitlines()[0]  # the tasks queue is gone (29.10)
    assert "Waiting on him: 1 · Startable: goals 0, ideas 1, night 0" in ag.text
    assert lines[-1] == "Act: events ack ID… · events show ID · wake …"
    assert ag.listed == [1, 2, 3, 4, 5, 6]
    assert ag.counts["new"] == 6


def test_hint_when_no_next_line(home: Path) -> None:
    write(home / "inbox" / "unraid-health-1792602808.md", "# disk 3 hot\n\nno next line here\n")
    ingest_all(home)
    ag = build(db.connect())
    assert "→ hint: something is wrong on the server" in ag.text


def test_privacy_only_title_and_next(home: Path) -> None:
    fill(home, 30, long=True)
    ingest_all(home)
    ag = build(db.connect())
    assert SECRET not in ag.text and "private line" not in ag.text


@pytest.mark.parametrize("n,long", [(200, False), (200, True), (600, True)])
def test_size_cap_and_everything_listed(home: Path, fakes: Path, n: int, long: bool) -> None:
    fill(home, n, long=long)
    ingest_all(home)
    con = db.connect()
    run = hook.running_info(con, time.time(), timeout=5)
    ag = build(con, run=run, board=todo.read(tz=Z))
    assert len(ag.text) <= agenda.BUDGET, len(ag.text)
    if n <= 200:
        assert ag.listed == list(range(1, n + 1))
    else:
        assert len(ag.listed) >= 200
    assert ag.text.splitlines()[-1].startswith("Act:")
    assert SECRET not in ag.text


def test_hard_cap_leaves_unlisted_ids_for_next_beat(home: Path) -> None:
    for i in range(400):  # 400 distinct kinds: grouped lines cannot hold them all
        write(home / "inbox" / f"kind{i:03d}x-thing-{1792600000 + i}.md", f"# t{i}\n")
    ingest_all(home)
    ag = build(db.connect())
    assert len(ag.text) <= agenda.BUDGET
    assert 0 < len(ag.listed) < 400
    assert "more: events list" in ag.text


def test_open_events_compact_then_full_after_restart(home: Path) -> None:
    fill(home, 4)
    ingest_all(home)
    con = db.connect()
    with db.tx(con):
        events.mark_listed(con, [1, 2, 3, 4], events.begin_beat(con, time.time(), "idle"), time.time())
    ag = build(con)
    assert "Open (4, listed before, not acked):" in ag.text
    v = kid("note.vault-events")
    assert f"#{v} note.vault-events" not in ag.text  # compact: grouped ids
    assert f"note.vault-events ×1 #{v}" in ag.text and ag.listed == [1, 2, 3, 4]
    full = build(con, full_open=True)
    assert f"#{v} note.vault-events" in full.text and full.listed == [1, 2, 3, 4]


def test_updated_section(home: Path) -> None:
    p = write(home / "inbox" / "train-watch-failed-1792602808.md", "# tw failed\n")
    ingest_all(home)
    con = db.connect()
    with db.tx(con):
        events.mark_listed(con, [1], events.begin_beat(con, time.time(), "idle"), time.time())
    write(p, "# tw failed\n\nRecovered 21.10 after 2 failed run(s).\n", age=5)
    ingest_all(home)
    ag = build(con)
    assert "Updated (1):" in ag.text and "#1 cron.fail" in ag.text and ag.listed == [1]


def test_wake_rules_digest_and_mute(home: Path) -> None:
    fill(home, 6)
    ingest_all(home)
    con = db.connect()
    now = time.time()
    with db.tx(con):
        con.execute(
            "INSERT INTO wake_rules(created, kind_glob, klass, until, why) VALUES(?, 'note.rave-*', 'digest',"
            " ?, 'batch music')",
            (now, now + 3600),
        )
        con.execute(
            "INSERT INTO wake_rules(created, kind_glob, klass, why) VALUES(?, 'note.reminder', 'mute', 'x')", (now,)
        )
        con.execute(
            "INSERT INTO wake_rules(created, kind_glob, klass, until) VALUES(?, '*', 'mute', ?)", (now - 10, now - 5)
        )  # expired: ignored
    ag = build(con, now=now)
    assert f"  digest: note.rave-radar ×1 #{kid('note.rave-radar')}" in ag.text
    assert f"  muted: note.reminder ×1 #{kid('note.reminder')}" in ag.text
    assert f"#{kid('note.reminder')} note.reminder" not in ag.text
    assert "note.rave-*→digest until" in ag.text and "(batch music)" in ag.text
    assert ag.listed == [1, 2, 3, 4, 5, 6]


def test_gone_and_compaction_lines(home: Path) -> None:
    fill(home, 2)
    ingest_all(home)
    (home / "inbox" / "vault-events-1792600000.md").unlink()
    ingest_all(home)
    con = db.connect()
    with db.tx(con):
        db.meta_set(
            con, "compactions", [{"ts": time.time() - 60, "trigger": "auto", "pre": 367510, "post": 24634, "ms": 65458}]
        )
    ag = hook.render(con, time.time(), "idle", write=False)[0]
    assert "Gone without ack since last beat" not in ag.text  # no beat yet
    ag2 = build(con, last=time.time() - 3600, comp=db.meta_get(con, "compactions")[-1])
    assert "Gone without ack since last beat: 1" in ag2.text
    assert re.search(r"Last compaction: \d\d:\d\d \(auto, 368k→25k\)", ag2.text)


def test_agenda_json_matches_schema(home: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from inbox_testlib import run_events

    fill(home, 5)
    r = run_events("ingest")
    assert r.returncode == 0, r.stderr
    r = run_events("agenda", "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    schema = json.loads((REPO / "schemas" / "agenda.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(d, schema)
    assert d["listed"] == [1, 2, 3, 4, 5]
    assert db.connect().execute("SELECT COUNT(*) FROM beats").fetchone()[0] == 0  # preview writes nothing
    assert {r["status"] for r in events.open_events(db.connect())} == {"new"}


def test_due_line_dates_are_the_board_due(home: Path) -> None:
    """The Due line shows the board's "due HH:MM DD.MM", not a history time or a date in a file name (30.10)."""
    (home / "workspace" / "TODO.md").write_text(
        "# TODO\n\n## Due\n"
        "- **Re-measure** — due 18:00 05.11 — write ~/reports/r/remeasure-2031-11-05.md; "
        "decisions in ~/reports/r/review-2031-10-29.md\n"
        "- **Verify fix** — due 10:00 30.10 — live since 22:04 29.10 (~/jobs/fix/apply.log)\n"
        "- **Batch** — due 09:00 30.10\n",
        encoding="utf-8",
    )
    lines = {}
    for hm in ("06:00", "09:30"):
        now = datetime.strptime(f"2031-10-30 {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=Z)
        ag = build(db.connect(), now=now.timestamp(), board=todo.read(now=now, tz=Z))
        lines[hm] = next(line for line in ag.text.splitlines() if line.startswith("Due: "))
    assert lines["06:00"] == "Due: none · next Batch 30.10 09:00"
    assert lines["09:30"] == "Due: Batch (30.10 09:00)"


def test_id_ranges() -> None:
    assert agenda.id_ranges([9, 3, 5, 6, 7, 1, 2]) == "#1-3 #5-7 #9"
    assert agenda.id_ranges([4, 5]) == "#4 #5"
    assert agenda.id_ranges([]) == ""


def test_times_in_owner_zone(home: Path) -> None:
    now = datetime(2031, 6, 15, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    ag = build(db.connect(), now=now, last=now - 2 * 86400)
    assert ag.text.startswith("[agenda 15.06 14:00 · beat #7 · last 13.06 14:00 ·")
