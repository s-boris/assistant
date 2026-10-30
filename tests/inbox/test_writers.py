"""Writers on the emit contract: cron-fail, remind (note-for-me path), plans-watch.

Each writer must produce the typed event row AND the identical md note: same file name (= idem_key), same bytes,
same mode as the golden notes in fixtures/writers/golden.json, a row whose
kind/subject/next/labels/delivery match what ingest makes of that note plus a payload valid against
schemas/payloads/<kind>.schema.json; a DB failure or a missing ledger leaves the note and one emit-errors.log line."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest
from inbox_testlib import EVENTS, PY, REPO

FIX = Path(__file__).parent / "fixtures" / "writers"
GOLDEN = FIX / "golden.json"
SRC = REPO / "scripts"
REAL_LIB = (
    Path(next((e.pw_dir for e in pwd.getpwall() if e.pw_uid == os.getuid()), "/nonexistent-home")) / "bin" / "lib"
)
T0 = 1939293600  # 2031-06-15 12:40 UTC
PLANS_NOW = "2031-06-16T10:00:00+02:00"


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


def overlay(tmp: Path) -> Path:
    """The scripts under test in a bin dir whose lib/ has the repo's core + inbox (the CI overlay's layout) and the
    other ~/bin/lib modules they import (assistant_profile, remindwhen, tripclock, usage)."""
    b = tmp / "ovl"
    (b / "lib").mkdir(parents=True)
    for n in ("cron-fail", "remind", "plans-watch"):
        shutil.copy2(SRC / n, b / n)
    (b / "lib" / "core").symlink_to(REPO / "core")
    (b / "lib" / "inbox").symlink_to(REPO / "packages" / "inbox")
    if REAL_LIB.is_dir():
        for e in REAL_LIB.iterdir():
            if not (b / "lib" / e.name).exists() and e.name not in ("core", "inbox", "__pycache__"):
                (b / "lib" / e.name).symlink_to(e)
    return b


def notes_in(d: Path) -> dict:
    return {
        p.name: {"text": p.read_text(encoding="utf-8"), "mode": oct(stat.S_IMODE(p.stat().st_mode))}
        for p in sorted(d.iterdir())
        if p.is_file() and not p.name.startswith(".")
    }


def rows(home: Path) -> list[dict]:
    p = home / ".local" / "state" / "assistant" / "events.db"
    if not p.exists():
        return []
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id")]
    finally:
        con.close()


def ingest_view(note_dir: Path, tmp: Path) -> dict[str, dict]:
    """What ingest makes of these notes in a fresh store: name -> kind/subject/next/labels/delivery."""
    from inbox import ingest, kinds

    from core import db

    con = db.connect(tmp / "ingest-view.db")
    try:
        ingest.ingest(con, kinds.load(), str(note_dir), now=T0 + 10**6, min_age=0)
        return {
            r["idem_key"]: {k: r[k] for k in ("kind", "subject", "next", "labels", "delivery")}
            for r in con.execute("SELECT * FROM events")
        }
    finally:
        con.close()


def check_typed(row: dict, view: dict, source: str, typed_subject: bool = False) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    v = view[row["idem_key"]]
    keys = (
        ("kind", "next", "labels", "delivery") if typed_subject else ("kind", "subject", "next", "labels", "delivery")
    )
    assert {k: row[k] for k in keys} == {k: v[k] for k in keys}
    assert row["source"] == source and row["status"] == "new"
    pl = json.loads(row["payload"])
    assert pl["type"] == row["kind"] and pl["v"] == 1
    schema = json.loads((REPO / "schemas" / "payloads" / f"{row['kind']}.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(pl, schema)


def errors(home: Path) -> list[str]:
    p = home / ".local" / "state" / "assistant" / "emit-errors.log"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


# ---------------------------------------------------------------- cron-fail


def cron_fail(b: Path, home: Path, *args: str, events: str | None = None) -> subprocess.CompletedProcess:
    shim = home.parent / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "date").write_text(
        f'#!/bin/sh\ncase "$*" in "+%s") echo {T0};; "+%s%N") echo {T0}000000000;; *) exec /bin/date "$@";; esac\n',
        encoding="utf-8",
    )
    (shim / "date").chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}:/usr/bin:/bin", TZ="UTC", USAGE="off", HOME=str(home), ASSISTANT_NOW=str(T0))
    env["CRON_FAIL_EVENTS"] = events if events is not None else str(EVENTS)
    return subprocess.run(
        ["bash", str(b / "cron-fail"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        preexec_fn=lambda: os.umask(0o022),
    )


def test_cron_fail_golden(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    r = cron_fail(b, home, "demo-job", "sh", "-c", "echo out; echo err >&2; exit 3")
    assert r.returncode == 3 and r.stdout == "out\n", r.stderr
    failed = notes_in(home / "inbox")
    r = cron_fail(b, home, "demo-job", "true")
    assert r.returncode == 0 and r.stdout == ""
    recovered = notes_in(home / "inbox")
    g = golden()["cron-fail"]
    assert failed == g["failed"] and recovered == g["recovered"]  # identical md notes (bytes + mode)
    rs = rows(home)
    assert len(rs) == 1 and errors(home) == []
    row = rs[0]
    view = ingest_view(_copy_notes(g["recovered"], tmp_path / "v"), tmp_path)
    check_typed(row, view, "cron-fail")
    pl = json.loads(row["payload"])
    assert (pl["job"], pl["cmd"], pl["rc"], pl["runs"], pl["since"]) == ("demo-job", "sh", 3, 1, T0)
    assert pl["recovered"] == T0 and pl["failed_runs"] == 1 and row["updated_ts"] is not None
    assert "recovered" in json.loads(row["labels"])
    # ingest then sees nothing new: no second row, no second update
    r = subprocess.run([PY, str(EVENTS), "ingest", "--min-age", "0", "--json"], capture_output=True, text=True)
    st = json.loads(r.stdout)
    assert st["new"] == 0 and st["updated"] == 0 and len(rows(home)) == 1


def _copy_notes(notes: dict, d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for n, v in notes.items():
        (d / n).write_text(v["text"], encoding="utf-8")
    return d


def test_cron_fail_streak_and_exit_codes_unchanged(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    for _ in range(3):
        assert cron_fail(b, home, "--after", "2", "flappy", "sh", "-c", "exit 5").returncode == 5
    assert len(list((home / "inbox").glob("flappy-failed-*.md"))) == 1 and len(rows(home)) == 1
    assert cron_fail(b, home, "--ok", "4", "okjob", "sh", "-c", "exit 4").returncode == 4
    assert not list((home / "inbox").glob("okjob-*")) and len(rows(home)) == 1
    assert cron_fail(b, home, "bad name", "true").returncode == 2
    assert errors(home) == []


def test_cron_fail_without_events_writes_the_note_and_counts(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    g = golden()["cron-fail"]
    assert cron_fail(b, home, "demo-job", "sh", "-c", "echo err >&2; exit 3", events="/nonexistent").returncode == 3
    assert notes_in(home / "inbox") == g["failed"] and rows(home) == []
    assert cron_fail(b, home, "demo-job", "true", events="/nonexistent").returncode == 0
    assert notes_in(home / "inbox") == g["recovered"]
    e = errors(home)
    assert len(e) == 2 and "cron.fail cron-fail emit exit 127" in e[0] and "update exit 127" in e[1]


def test_cron_fail_store_failure_keeps_the_note(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    st = home / ".local" / "state" / "assistant"
    st.mkdir(parents=True)
    (st / "events.db").mkdir()  # the store can't be opened
    assert cron_fail(b, home, "demo-job", "sh", "-c", "echo err >&2; exit 3").returncode == 3
    assert notes_in(home / "inbox") == golden()["cron-fail"]["failed"]
    assert len(errors(home)) == 1 and " cron.fail " in errors(home)[0]


# ---------------------------------------------------------------- remind


def remind(b: Path, home: Path, *args: str, now: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home), TZ="Europe/Zurich", USAGE="off", REMIND_NOW=now)
    env.update(
        REMIND_DB=str(home / "rem.json"), REMIND_TG="/bin/false", REMIND_TG_BUTTONS="/bin/false", REMIND_BUTTONS="0"
    )
    env.pop("REMIND_INBOX", None)
    return subprocess.run(
        [PY, str(b / "remind"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        preexec_fn=lambda: os.umask(0o022),
    )


def test_remind_note_for_me_golden(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    shutil.copy(FIX / "remind-store.json", home / "rem.json")
    r = remind(b, home, "due", now="2031-06-15 14:00")
    assert r.returncode == 0, r.stderr
    got = notes_in(home / "inbox")
    assert got == golden()["remind"] and len(got) == 2  # one for me, one owner reminder whose tg failed
    view = ingest_view(_copy_notes(got, tmp_path / "v"), tmp_path)
    rs = rows(home)
    assert sorted(x["idem_key"] for x in rs) == sorted(got) and errors(home) == []
    for row in rs:
        check_typed(row, view, "remind", typed_subject=True)
        assert row["subject"] == got[row["idem_key"]]["text"].splitlines()[0][:160]
        pl = json.loads(row["payload"])
        assert pl["for"] == ("owner" if "OWNER" in row["subject"] else "me") and pl["tg_failed"] == (
            pl["for"] == "owner"
        )


def test_remind_test_inbox_is_never_emitted(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    shutil.copy(FIX / "remind-store.json", home / "rem.json")
    other = tmp_path / "other-inbox"
    env = dict(os.environ, HOME=str(home), USAGE="off", REMIND_NOW="2031-06-15 14:00", REMIND_DB=str(home / "rem.json"))
    env.update(REMIND_TG="/bin/false", REMIND_TG_BUTTONS="/bin/false", REMIND_INBOX=str(other))
    assert subprocess.run([PY, str(b / "remind"), "due"], env=env, capture_output=True, timeout=60).returncode == 0
    assert len(list(other.iterdir())) == 2 and rows(home) == []


def test_remind_falls_back_when_the_ledger_breaks(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    (b / "lib" / "inbox").unlink()
    (b / "lib" / "inbox").mkdir()
    (b / "lib" / "inbox" / "__init__.py").write_text("raise RuntimeError('broken install')\n", encoding="utf-8")
    shutil.copy(FIX / "remind-store.json", home / "rem.json")
    assert remind(b, home, "due", now="2031-06-15 14:00").returncode == 0
    assert notes_in(home / "inbox") == golden()["remind"] and rows(home) == []
    assert len(errors(home)) == 2 and all(" remind fallback RuntimeError" in e for e in errors(home))


# ---------------------------------------------------------------- plans-watch


def _plans_fixture(d: Path) -> dict[str, str]:
    def ev(cursor, what, date, start, chat, key, ctype, status="planned"):
        f = {"kind": "event", "what": what, "status": status, "date": date, "date_text": date, "start": start}
        return {
            "cursor": cursor, "source": "telegram", "ts": "2031-06-15T18:00:00+02:00", "chat_key": key, "chat": chat,
            "chat_type": ctype, "label": "plan_datetime", "actionable": True, "priority": False, "ref": f"m{cursor}",
            "facts": [f],
        }  # fmt: skip

    fx = {
        "events": [
            ev(1, "Climbing evening", "2031-06-20", "19:00", "Anna", "4711", "user"),
            ev(2, "Board game night", "2031-06-27", "20:00", "Crew", "4712", "group", "proposed"),
        ]
    }
    d.mkdir(parents=True, exist_ok=True)
    (d / "fx.json").write_text(json.dumps(fx), encoding="utf-8")
    vault = d / "vault"
    vault.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\nfx = json.load(open(os.environ['FX']))\na = sys.argv[1:]\n"
        "if a[:1] == ['health']:\n    sys.exit(0)\n"
        "since, n = int(a[a.index('--since') + 1]), int(a[a.index('--max') + 1])\n"
        "ev = [e for e in fx['events'] if e['cursor'] > since][:n]\n"
        "print(json.dumps({'events': ev, 'next_cursor': ev[-1]['cursor'] if ev else since, 'more': False}))\n",
        encoding="utf-8",
    )
    rem = d / "remind"
    rem.write_text('#!/bin/sh\necho \'{"items": [], "recent": []}\'\n', encoding="utf-8")
    for p in (vault, rem):
        p.chmod(0o755)
    return {
        "FX": str(d / "fx.json"), "PLANS_WATCH_CLIENT": str(vault), "PLANS_WATCH_REMIND": str(rem),
        "PLANS_WATCH_CALCACHE": str(d / "none.json"), "PLANS_WATCH_STATE": str(d / "state"),
    }  # fmt: skip


def plans(b: Path, home: Path, tmp: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home), USAGE="off", **_plans_fixture(tmp / "pw"))
    env.pop("PLANS_WATCH_INBOX", None)
    return subprocess.run([PY, str(b / "plans-watch"), *args], env=env, capture_output=True, text=True, timeout=60)


def test_plans_watch_golden(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    r = plans(b, home, tmp_path, "run", "--now", PLANS_NOW)
    assert r.returncode == 0, r.stderr
    got = notes_in(home / "inbox")
    assert got == golden()["plans-watch"] and len(got) == 1
    ((name, note),) = got.items()
    assert note["mode"] == "0o600"  # private, as before
    rs = rows(home)
    assert len(rs) == 1 and errors(home) == []
    check_typed(rs[0], ingest_view(_copy_notes(got, tmp_path / "v"), tmp_path), "plans-watch")
    pl = json.loads(rs[0]["payload"])
    assert pl["new"] == 2 and pl["again"] == 0 and all(f"`{k}`" in note["text"] for k in pl["keys"])
    assert pl["cats"] == {"group": 1, "plan": 1} and pl["first_date"] == "2031-06-20"
    r = plans(b, home, tmp_path, "open", "--json")  # open/done semantics untouched
    assert sorted(x["key"] for x in json.loads(r.stdout)) == sorted(pl["keys"])


# ---------------------------------------------------------------- the library


def test_writer_note_reoccurrence_dup_and_classification(home: Path) -> None:
    from inbox import writer

    from core import db, events

    r1 = writer.note(None, "back-offer.md", "Owner is home.\nData.\n", source="remind", payload={"days": 3})
    assert r1.id and not r1.dup
    con = db.connect()
    try:
        assert con.execute("SELECT kind FROM events WHERE id=?", (r1.id,)).fetchone()[0] == "note.back-offer"
        r2 = writer.note(None, "back-offer.md", "Owner is home.\nData.\n", source="remind", payload={"days": 3})
        assert r2.dup and r2.id == r1.id  # open: a dup
        (home / "inbox" / "back-offer.md").unlink()
        writer.note(None, "back-offer.md", "Owner is home.\nData.\n", source="remind", payload={"days": 3})
        assert (home / "inbox" / "back-offer.md").exists()  # a dup still restores a missing note
        assert events.ack(con, [r1.id])[0].ok and not (home / "inbox" / "back-offer.md").exists()
        r3 = writer.note(None, "back-offer.md", "Owner is home.\nData.\n", source="remind", payload={"days": 4})
        assert not r3.dup and r3.id != r1.id  # after the ack: a new occurrence
        keys = [k for (k,) in con.execute("SELECT idem_key FROM events ORDER BY id")]
        assert keys == [f"back-offer.md#{r1.id}", "back-offer.md"]
    finally:
        con.close()


def test_update_cli_states(home: Path) -> None:
    r = subprocess.run([PY, str(EVENTS), "update", "--note", "nope-failed-1.md"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "missing"
    assert subprocess.run([PY, str(EVENTS), "update", "--note", "../x"], capture_output=True).returncode == 2
    (home / ".config" / "assistant" / "events-off").write_text("", encoding="utf-8")
    r = subprocess.run([PY, str(EVENTS), "update", "--note", "nope-failed-1.md"], capture_output=True, text=True)
    assert r.stdout.strip() == "off"


def test_metrics_writers_section(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    assert cron_fail(b, home, "demo-job", "sh", "-c", "exit 3").returncode == 3
    (home / "inbox" / "other-failed-1939293000.md").write_text("# other failed (cron)\n", encoding="utf-8")
    os.utime(home / "inbox" / "other-failed-1939293000.md", (T0, T0))
    subprocess.run(
        [PY, str(EVENTS), "ingest", "--min-age", "0"], check=True, env=dict(os.environ, ASSISTANT_NOW=str(T0 + 5))
    )
    with open(home / ".local" / "state" / "assistant" / "emit-errors.log", "a", encoding="utf-8") as f:
        f.write(f"{T0} cron.fail cron-fail emit exit 1\n")
    r = subprocess.run(
        [PY, str(EVENTS), "metrics", "--since", str(T0 - 60), "--until", str(T0 + 60), "--json"],
        capture_output=True,
        text=True,
    )
    w = json.loads(r.stdout)["writers"]
    assert w["kinds"] == {"cron.fail": {"typed": 1, "ingested": 1}} and w["dupes"] == 0 and w["emit_errors"] == 1
