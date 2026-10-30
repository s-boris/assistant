"""The brain's scheduled cues as typed cue.* events (`cue NAME`, waiting-nudge's own cue), and the registry
rows that run them from hub schedules. Temp HOMEs only; the ledger is the local SQLite store."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import pytest
from inbox_testlib import KINDS, PY, REPO
from test_writers import _copy_notes, errors, ingest_view, notes_in, rows

from core import notes as core_notes
from core import schedules

NOW = "2031-06-15T08:31:00+02:00"  # a Sunday
DATE = "2031-06-15"
CUES = ("weekly-digest", "birthdays", "idea-scan", "self-cleanup", "calendar-gap-pass", "daily-plan")


def overlay(tmp: Path) -> Path:
    """cue and waiting-nudge in a bin dir whose lib/ holds the repo's core + inbox (the deployed layout)."""
    b = tmp / "ovl"
    (b / "lib").mkdir(parents=True)
    for n in ("cue", "waiting-nudge"):
        shutil.copy2(REPO / "scripts" / n, b / n)
    (b / "lib" / "core").symlink_to(REPO / "core")
    (b / "lib" / "inbox").symlink_to(REPO / "packages" / "inbox")
    return b


def cue(b: Path, home: Path, *args: str, shop: str = "off") -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home), TZ="Europe/Zurich", ASSISTANT_NOW=NOW, CUE_SHOP=shop)
    env.pop("ASSISTANT_LIB", None)
    return subprocess.run([PY, "-I", str(b / "cue"), *args], env=env, capture_output=True, text=True, timeout=60,
                          preexec_fn=lambda: os.umask(0o022))  # fmt: skip


def payload_schema() -> dict:
    return json.loads((REPO / "schemas" / "payloads" / "cue.schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", CUES)
def test_each_cue_is_a_typed_row_and_its_note(home: Path, tmp_path: Path, name: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    b = overlay(tmp_path)
    r = cue(b, home, name)
    assert r.returncode == 0, r.stderr
    got = notes_in(home / "inbox")
    assert len(got) == 1 and errors(home) == []
    note, meta = next(iter(got.items()))
    text = meta["text"]
    assert core_notes.check(text, home) == []  # title, DNI line, Next line, no absolute home
    assert f"cue.{name}" in text and DATE in text.splitlines()[0]
    (row,) = rows(home)
    assert row["idem_key"] == note and row["kind"] == f"cue.{name}" and row["source"] == "cue"
    # ingest would give the note the same kind, title, Next line, labels and delivery
    view = ingest_view(_copy_notes(got, tmp_path / "v"), tmp_path)[note]
    assert {k: row[k] for k in ("kind", "subject", "next", "labels", "delivery")} == view
    pl = json.loads(row["payload"])
    assert pl["type"] == f"cue.{name}" and pl["cue"] == name and pl["date"] == DATE
    jsonschema.validate(pl, payload_schema())


def test_note_names_keep_the_cron_prefixes(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    for name in CUES:
        assert cue(b, home, name).returncode == 0
    prefixes = sorted(n.rsplit("-", 1)[0] for n in notes_in(home / "inbox"))
    assert prefixes == sorted(["digest", "birthdays", "idea-scan", "self-cleanup", "calendar-gap-pass", "daily-plan"])
    # a second run is a second cue (a new name), never a lost one
    assert cue(b, home, "weekly-digest").returncode == 0
    assert len(notes_in(home / "inbox")) == len(CUES) + 1 and len(rows(home)) == len(CUES) + 1


def test_daily_plan_carries_the_shop_lines(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    shop = tmp_path / "shop"
    shop.write_text('#!/bin/sh\n[ "$*" = "plan --trips --days 1 --line" ] || exit 9\necho "🛒 Town: Store (2 items)"\n'
                    'echo noise >&2\n', encoding="utf-8")  # fmt: skip
    shop.chmod(0o755)
    assert cue(b, home, "daily-plan", shop=str(shop)).returncode == 0
    (text,) = [m["text"] for m in notes_in(home / "inbox").values()]
    lines = text.splitlines()
    assert "🛒 Town: Store (2 items)" in lines and "noise" not in text
    assert lines.index("🛒 Town: Store (2 items)") < next(i for i, ln in enumerate(lines) if ln.startswith("Next:"))
    assert json.loads(rows(home)[0]["payload"])["items"] == 1
    # a failing shop = no lines, the cue still goes out
    shop.write_text("#!/bin/sh\necho partial\nexit 3\n", encoding="utf-8")
    (home / "inbox" / next(iter(notes_in(home / "inbox")))).unlink()
    assert cue(b, home, "daily-plan", shop=str(shop)).returncode == 0
    (text,) = [m["text"] for m in notes_in(home / "inbox").values()]
    assert "partial" not in text and "🛒" in text  # the body's own mention only


def test_dry_run_list_and_usage(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    r = cue(b, home, "self-cleanup", "--dry-run")
    assert r.returncode == 0 and r.stdout.startswith(f"# Weekly self-cleanup {DATE}\n")
    assert notes_in(home / "inbox") == {} and rows(home) == []
    r = cue(b, home, "list")
    assert r.returncode == 0 and all(f"cue.{n}" in r.stdout for n in CUES)
    assert cue(b, home, "nope").returncode == 2 and cue(b, home).returncode == 2
    assert cue(b, home, "--help").returncode == 0


def test_cue_falls_back_when_the_ledger_breaks(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path)
    (b / "lib" / "inbox").unlink()
    (b / "lib" / "inbox").mkdir()
    (b / "lib" / "inbox" / "__init__.py").write_text("raise RuntimeError('broken install')\n", encoding="utf-8")
    assert cue(b, home, "idea-scan").returncode == 0
    (name,) = notes_in(home / "inbox")
    assert name.startswith("idea-scan-") and rows(home) == []
    assert len(errors(home)) == 1 and " cue.idea-scan cue fallback RuntimeError" in errors(home)[0]


# ---------------------------------------------------------------- waiting-nudge (its own cue)


def waiting_feed(home: Path, *titles: str) -> None:
    """The board's feed (~/.local/state/board/board.json, fresh) with these Waiting items, created 2031-06-10."""
    created = datetime.datetime(2031, 6, 10, 12, 0).timestamp()
    rows = [{"id": f"w{n}", "section": "Waiting on you", "title": t, "created": created, "asked": None}
            for n, t in enumerate(titles)]  # fmt: skip
    p = home / ".local" / "state" / "board" / "board.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ts": time.time(), "mode": "owner", "items": rows}), encoding="utf-8")


def nudge(b: Path, home: Path, today: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home), WAITING_NUDGE_TODAY=today)
    for k in ("WAITING_NUDGE_INBOX", "WAITING_NUDGE_FEED", "WAITING_NUDGE_NOW", "WAITING_NUDGE_BOARD", "ASSISTANT_LIB"):
        env.pop(k, None)
    (b / "sleepin").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # no sleep-in hold
    (b / "sleepin").chmod(0o755)
    return subprocess.run([PY, str(b / "waiting-nudge"), "--cron"], env=env, capture_output=True, text=True,
                          timeout=60, preexec_fn=lambda: os.umask(0o022))  # fmt: skip


def test_waiting_nudge_emits_cue_waiting_nudge(home: Path, tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    b = overlay(tmp_path)
    waiting_feed(home, "Tax OK?", "Login")
    assert nudge(b, home, "2031-06-10").returncode == 0  # created today: nothing due yet
    assert notes_in(home / "inbox") == {}
    r = nudge(b, home, "2031-06-15")
    assert r.returncode == 0, r.stderr
    got = notes_in(home / "inbox")
    assert list(got) == ["waiting-nudge-2031-06-15.md"] and got["waiting-nudge-2031-06-15.md"]["mode"] == "0o644"
    (row,) = rows(home)
    assert row["kind"] == "cue.waiting-nudge" and row["source"] == "waiting-nudge" and errors(home) == []
    view = ingest_view(_copy_notes(got, tmp_path / "v"), tmp_path)["waiting-nudge-2031-06-15.md"]
    assert {k: row[k] for k in ("kind", "subject", "next", "labels", "delivery")} == view
    pl = json.loads(row["payload"])
    assert pl["items"] == 2 and pl["date"] == "2031-06-15"
    jsonschema.validate(pl, payload_schema())
    # a rerun the same day rewrites the note and refreshes the same row
    waiting_feed(home, "Tax OK?")
    assert nudge(b, home, "2031-06-15").returncode == 0
    (row,) = rows(home)
    assert "Login" not in notes_in(home / "inbox")["waiting-nudge-2031-06-15.md"]["text"]
    assert json.loads(row["payload"])["items"] == 1


# ---------------------------------------------------------------- registry + kinds


def test_class_c_rows_run_the_cues_from_hub_schedules() -> None:
    reg = schedules.load()
    c = {e.name: e for e in reg.jobs.values() if e.cls == "C"}
    assert len(c) == 8
    src = (REPO / "scripts" / "cue").read_text(encoding="utf-8")
    for e in c.values():
        # cron-back 29.10: the cues run from ~/crontab again (cron-fail --after 1 around the same argv)
        assert e.runs_on == "cron" and e.target == schedules.CRON_BACK, e.name
        assert e.movable() is None and e.queue_name and e.after >= 1, e.name
        if e.argv[0] == "~/bin/cue":
            assert len(e.argv) == 2 and f'"{e.argv[1]}": Cue(' in src and f'"{e.name}",' in src, e.name
        else:
            assert e.argv == ("~/bin/waiting-nudge", "--cron"), e.name
    assert sorted(e.argv[1] for e in c.values() if e.argv[0] == "~/bin/cue") == sorted(CUES)


def test_cue_kinds_are_registered() -> None:
    with open(KINDS, "rb") as f:
        k = tomllib.load(f)["kinds"]
    for n in (*CUES, "waiting-nudge"):
        e = k[f"cue.{n}"]
        assert "cue" in e["labels"] and e["payload"] == "payloads/cue.schema.json" and e["hint"] and e["jobs"]
    assert "note.daily-plan" not in k and "note.waiting-nudge" not in k
