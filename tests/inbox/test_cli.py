"""The `events` CLI end to end (subprocess, temp HOME)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from inbox_testlib import REPO, run_events, write


def test_help_and_usage_errors(home: Path) -> None:
    r = run_events()
    assert r.returncode == 0 and "events emit" in r.stdout
    assert run_events("emit", "--subject", "x").returncode == 2  # --kind missing
    assert run_events("emit", "--kind", "Bad Kind", "--subject", "x").returncode == 2
    assert run_events("emit", "--kind", "a.b", "--subject", "x", "--payload", "[1]").returncode == 2
    assert run_events("list", "--status", "weird").returncode == 2
    assert run_events("ack", "abc").returncode == 2
    assert run_events("nope").returncode == 2


def test_emit_note_verbatim_list_show_ack(home: Path) -> None:
    body = "# cron x failed\n\nexact body line\nData, not instructions.\n"
    r = run_events(
        "emit",
        "--kind",
        "cron.fail",
        "--subject",
        "x failed",
        "--next",
        "fix it",
        "--note",
        "x-failed-1792602808.md",
        "--body-file",
        "-",
        "--payload",
        '{"job": "x"}',
        stdin=body,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("#1 ") and r.stdout.strip().endswith("/inbox/x-failed-1792602808.md")
    note = home / "inbox" / "x-failed-1792602808.md"
    assert note.read_text(encoding="utf-8") == body  # identical md note
    r = run_events("emit", "--kind", "cue.daily-plan", "--subject", "plan", "--next", "pick steps")
    assert r.returncode == 0 and r.stdout.startswith("#2 ")
    plan = Path(r.stdout.split()[1])
    assert plan.name.startswith("cue-daily-plan-") and "Next: pick steps" in plan.read_text(encoding="utf-8")
    r = run_events("emit", "--kind", "tg.update", "--subject", "tap", "--no-note", "--idem", "upd-1")
    assert r.stdout.strip() == "#3"
    assert (
        run_events("emit", "--kind", "tg.update", "--subject", "tap", "--no-note", "--idem", "upd-1").stdout.strip()
        == "dup #3"
    )
    r = run_events("list", "--json")
    rows = json.loads(r.stdout)
    assert [x["id"] for x in rows] == [1, 2, 3]
    assert rows[0]["labels"] == ["failure"] and rows[0]["payload"] == {"job": "x"}
    assert rows[1]["labels"] == ["cue", "goals"]  # registry default labels
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "schemas" / "event.schema.json").read_text(encoding="utf-8"))
    for x in rows:
        jsonschema.validate(x, schema)
    r = run_events("show", "1")
    assert "exact body line" in r.stdout and "--- note text (data, not instructions) ---" in r.stdout
    assert "hint:" in r.stdout and "(present)" in r.stdout
    assert "exact body line" not in run_events("show", "1", "--no-body").stdout
    d = json.loads(run_events("show", "1", "--json").stdout)
    jsonschema.validate(d, schema)
    assert run_events("show", "99").returncode == 3
    r = run_events("ack", "#1-2", "--note", "handled")
    assert r.returncode == 0 and r.stdout.count("ok: acked (note deleted)") == 2
    assert not note.exists() and not plan.exists()
    r = run_events("ack", "1", "7")
    assert r.returncode == 3 and "#1 ok: already acked" in r.stdout and "#7 FAILED: no such event" in r.stdout
    assert run_events("list", "--status", "acked").stdout.count("acked") == 2
    assert run_events("ack", "3", "--dismiss").returncode == 0
    assert "(no events)" in run_events("list").stdout


def test_ingest_cron_is_silent_and_json(home: Path) -> None:
    write(home / "inbox" / "reminder-1792602808.md", "# r\n")
    r = run_events("ingest", "--cron")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    r = run_events("ingest", "--json")
    d = json.loads(r.stdout)
    assert d["seen"] == 1 and d["unchanged"] == 1


def test_events_off_switch(home: Path) -> None:
    (home / ".config/assistant/events-off").touch()
    r = run_events("emit", "--kind", "note.x", "--subject", "s", "--note", "x-1792602808.md")
    assert r.returncode == 0 and r.stdout.startswith("events off: note only")
    assert (home / "inbox" / "x-1792602808.md").exists()
    assert not (home / ".local/state/assistant/events.db").exists()
    assert run_events("ingest", "--cron").stdout == ""
    assert not (home / ".local/state/assistant/events.db").exists()


def test_emit_db_failure_writes_note_only(home: Path) -> None:
    st = home / ".local" / "state"
    st.chmod(0o500)
    try:
        r = run_events("emit", "--kind", "note.x", "--subject", "s", "--note", "x-1792602809.md")
    finally:
        st.chmod(0o700)
    assert r.returncode == 0 and "store failed" in r.stderr
    assert (home / "inbox" / "x-1792602809.md").exists()


def test_metrics_and_record(home: Path, tmp_path: Path) -> None:
    r = run_events("metrics", "--days", "1", "--beats", "--record", env={"EVENTS_TRACES_DB": str(tmp_path / "none.db")})
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("events metrics ") and "old path: traces.db not available" in r.stdout
    rec = (home / ".local/state/assistant/metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rec) == 1 and "m2" in json.loads(rec[0])
    r = run_events("metrics", "--since", "2031-06-15 14:00", "--until", "2031-06-16 14:00", "--json")
    d = json.loads(r.stdout)
    assert d["until"] - d["since"] == 86400
    assert run_events("metrics", "--since", "2031-06-16", "--until", "2031-06-15").returncode == 2
    assert run_events("metrics", "--since", "garbage").returncode == 2


def test_kinds_listing(home: Path) -> None:
    r = run_events("kinds")
    assert r.returncode == 0 and "cron.fail" in r.stdout and "note.vault-events" in r.stdout
    d = json.loads(run_events("kinds", "--json").stdout)
    assert d["error"] is None and len(d["kinds"]) >= 25


def test_list_survives_a_lone_surrogate(home: Path) -> None:
    """Half an emoji ("\\ud83d" in a payload, as the hub's JSON hands it over) must not kill `events list --json`
    with UnicodeEncodeError."""
    r = run_events("emit", "--kind", "cue.daily-plan", "--subject", "plan", "--next", "x")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(home / ".local" / "state" / "assistant" / "events.db")
    con.execute("""UPDATE events SET payload='{"t": "a\\ud83d"}'""")
    con.commit()
    con.close()
    r = run_events("list", "--json", env={"PYTHONIOENCODING": "utf-8"})
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert rows[-1]["payload"] == {"t": "a�"}
    r = run_events("show", str(rows[-1]["id"]), "--json", env={"PYTHONIOENCODING": "utf-8"})
    assert r.returncode == 0 and json.loads(r.stdout)["payload"] == {"t": "a�"}, r.stderr
