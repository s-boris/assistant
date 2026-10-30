"""wake: the brain's wake rules (hold, delivery rules, timers) and `wake check`, the view the heartbeat gate reads.
Temp HOME (conftest), fake remind, a fixed clock; no network."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from inbox import agenda, kinds, wake
from inbox_testlib import KINDS, PY, REPO, fake_bin, write

from core import db, events

Z = ZoneInfo("Europe/Zurich")
NOW_ISO = "2031-06-15T14:00:00+02:00"
NOW = datetime(2031, 6, 15, 14, 0, tzinfo=Z).timestamp()
WAKE = REPO / "scripts" / "wake"
REMIND = r"""echo "$*" >> "$HOME/remind.calls"
case "$1" in
  when) if [ "$2" = "tomorrow evening" ]; then echo '{"ok": true, "kind": "at", "at": "2031-06-16 19:00"}'
        else echo '{"ok": false, "error": "could not read"}'; exit 2; fi;;
  list) cat "$HOME/remind.list" 2>/dev/null || echo '{"items": []}';;
  add) if [ "$4" = "never" ]; then echo 'remind: bad time "never"' >&2; exit 2; fi
       [ -e "$HOME/remind.fail" ] && exit 1; echo "added reminder for Sun 2031-06-15 16:00";;
  cancel) [ "$2" = "gone" ] && { echo "no such reminder" >&2; exit 1; }; echo "cancelled $2";;
esac"""


@pytest.fixture
def wk(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ASSISTANT_NOW", NOW_ISO)
    fake_bin(home, "remind", REMIND)
    return home


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PY, "-I", str(WAKE), *args], capture_output=True, text=True, timeout=30, env={**os.environ, **(env or {})}
    )


def rules(active_only: bool = True) -> list[dict]:
    con = db.connect()
    sql = "SELECT * FROM wake_rules" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    return [dict(r) for r in con.execute(sql)]


def calls(home: Path) -> list[str]:
    p = home / "remind.calls"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


# ------------------------------------------------------------------------------------------------ time


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("+30m", NOW + 1800),
        ("30m", NOW + 1800),
        ("+2h", NOW + 7200),
        ("+1d", NOW + 86400),
        ("+1h30m", NOW + 5400),
        ("+90min", NOW + 5400),
        ("16:30", datetime(2031, 6, 15, 16, 30, tzinfo=Z).timestamp()),
        ("09:00", datetime(2031, 6, 16, 9, 0, tzinfo=Z).timestamp()),  # passed today: tomorrow
        ("tomorrow 07:15", datetime(2031, 6, 16, 7, 15, tzinfo=Z).timestamp()),
        ("morgen 7:15", datetime(2031, 6, 16, 7, 15, tzinfo=Z).timestamp()),
        ("today 18:00", datetime(2031, 6, 15, 18, 0, tzinfo=Z).timestamp()),
        ("16.06 08:00", datetime(2031, 6, 16, 8, 0, tzinfo=Z).timestamp()),
        ("16.06. 08:00", datetime(2031, 6, 16, 8, 0, tzinfo=Z).timestamp()),
        ("05.01 08:00", datetime(2032, 1, 5, 8, 0, tzinfo=Z).timestamp()),  # long past this year: next year's
        ("01.06.2031 08:00", datetime(2031, 6, 1, 8, 0, tzinfo=Z).timestamp()),
        ("2031-06-20 10:00", datetime(2031, 6, 20, 10, 0, tzinfo=Z).timestamp()),
        ("2031-06-20T10:00", datetime(2031, 6, 20, 10, 0, tzinfo=Z).timestamp()),
        ("2031-06-20T10:00:00+00:00", datetime(2031, 6, 20, 12, 0, tzinfo=Z).timestamp()),
        ("1939300000", 1939300000.0),
    ],
)
def test_parse_when(raw: str, want: float) -> None:
    assert wake.parse_when(raw, NOW, Z, phrases=False) == want


def _utc(h: int, m: int) -> float:
    return datetime(2026, 10, 25, h, m, tzinfo=ZoneInfo("UTC")).timestamp()


@pytest.mark.parametrize(
    ("now", "raw", "want"),
    [  # 25.10.2026: 03:00 CEST -> 02:00 CET, 02:00-02:59 twice (00:00-00:59 and 01:00-01:59 UTC)
        (_utc(0, 30), "02:45", _utc(0, 45)),  # first pass: the first 02:45
        (_utc(0, 50), "02:40", _utc(1, 40)),  # first pass, 02:40 gone: the second 02:40 is next
        (_utc(1, 30), "02:45", _utc(1, 45)),  # second pass: still today, not tomorrow
        (_utc(1, 50), "02:40", datetime(2026, 10, 26, 2, 40, tzinfo=Z).timestamp()),  # both gone: tomorrow
        (_utc(1, 30), "03:30", _utc(2, 30)),
        (_utc(0, 0) - 3600, "01:30", _utc(0, 0) - 1800),  # 01:00 CEST: 01:30 CEST as before
    ],
)
def test_parse_when_dst_autumn(now: float, raw: str, want: float) -> None:
    assert wake.parse_when(raw, now, Z, phrases=False) == want


@pytest.mark.parametrize("raw", ["", "+0m", "+30", "25:00", "31.02 08:00", "soon-ish", "+-5m"])
def test_parse_when_refuses(raw: str) -> None:
    with pytest.raises(wake.Usage):
        wake.parse_when(raw, NOW, Z, phrases=False)


def test_parse_when_phrase_via_remind(wk: Path) -> None:
    assert wake.parse_when("tomorrow evening", NOW, Z) == datetime(2031, 6, 16, 19, 0, tzinfo=Z).timestamp()
    with pytest.raises(wake.Usage):
        wake.parse_when("when pigs fly", NOW, Z)
    assert "when tomorrow evening --json" in calls(wk)


def test_split_when() -> None:
    assert wake._split_when(["tomorrow", "09:00", "why", "x"]) == ("tomorrow 09:00", ["why", "x"])
    assert wake._split_when(["16.06", "08:00", "why"]) == ("16.06 08:00", ["why"])
    assert wake._split_when(["09:00", "10:00"]) == ("09:00", ["10:00"])
    assert wake._split_when(["+2h", "09:00"]) == ("+2h", ["09:00"])
    assert wake._split_when([]) == ("", [])


# ------------------------------------------------------------------------------------------------ next / on / rm


def test_next_hold_replaces_and_limits(wk: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert wake.main(["next", "09:00", "night:", "nothing", "startable"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("hold #1: idle beats held until 16.06 09:00 (night: nothing startable).")
    assert "still wake" in out
    assert wake.main(["next", "+2h", "quieter"]) == 0
    assert "replaced 1 older hold(s)" in capsys.readouterr().out
    act = rules()
    assert [(r["klass"], r["kind_glob"], r["until"], r["why"]) for r in act] == [("hold", None, NOW + 7200, "quieter")]
    assert len(rules(active_only=False)) == 2
    assert wake.main(["next", "+25h", "too long"]) == 2
    assert "at most 24 h" in capsys.readouterr().err
    assert wake.main(["next", "+2h"]) == 2
    assert "needs a why" in capsys.readouterr().err
    assert wake.main(["next", "2031-06-15 13:00", "past"]) == 2
    assert wake.main(["next"]) == 2


def test_on_rules(wk: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert wake.main(["on", "note.rave-*", "digest", "music", "batch"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("rule #1: note.rave-* → digest until 16.06 14:00 (music batch); registry kinds: note.rave")
    assert wake.main(["on", "note.odd-kind", "mute", "--until", "+3h", "exact"]) == 0
    assert "an unknown kind, matched by its exact name" in capsys.readouterr().out
    assert wake.main(["on", "note.nothing-*", "mute", "--until=20:00", "never matches"]) == 0
    assert "never reaches unknown kinds" in capsys.readouterr().out
    assert wake.main(["on", "note.voice*", "digest", "calls later"]) == 0
    assert "includes now-kinds: note.voice" in capsys.readouterr().out
    got = [(r["kind_glob"], r["klass"], r["until"]) for r in rules()]
    assert got[:3] == [
        ("note.rave-*", "digest", NOW + 86400),
        ("note.odd-kind", "mute", NOW + 3 * 3600),
        ("note.nothing-*", "mute", datetime(2031, 6, 15, 20, 0, tzinfo=Z).timestamp()),
    ]
    for bad in (
        ["on", "note.x", "loud", "why"],
        ["on", "Note X", "mute", "why"],
        ["on", "note.x", "mute"],
        ["on", "note.x", "mute", "--until", "+31d", "too long"],
        ["on", "note.x", "mute", "--until"],
        ["on", "note.x"],
    ):
        assert wake.main(bad) == 2, bad
    assert "kind registry" in capsys.readouterr().err


def test_rm(wk: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for a in (
        ["next", "+1h", "h"],
        ["on", "note.a", "mute", "a"],
        ["on", "note.b", "mute", "b"],
        ["on", "x", "now", "c"],
    ):
        assert wake.main(a) == 0
    capsys.readouterr()
    assert wake.main(["rm", "#2,3"]) == 0
    assert "ended 2 rule(s)" in capsys.readouterr().out
    assert wake.main(["rm", "hold"]) == 0
    assert [r["id"] for r in rules()] == [4]
    assert wake.main(["rm", "all"]) == 0
    assert rules() == []
    assert wake.main(["rm", "abc"]) == 2
    assert wake.main(["rm"]) == 2
    assert wake.main(["rm", "t:ab12", "t:gone"]) == 3
    out = capsys.readouterr().out
    assert "t:ab12 cancelled" in out and "t:gone FAILED" in out
    assert "cancel ab12" in calls(wk)


# ------------------------------------------------------------------------------------------------ at / list


def test_at_timer_via_remind(wk: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert wake.main(["at", "16:00", "check", "the", "build"]) == 0
    assert "timer: added reminder" in capsys.readouterr().out
    assert calls(wk)[-1] == "add --tag wake 2031-06-15 16:00 wake timer: check the build"
    assert wake.main(["at", "tomorrow", "09:00", "x"]) == 0
    assert calls(wk)[-1] == "add --tag wake 2031-06-16 09:00 wake timer: x"
    assert wake.main(["at", "next week", "phrase"]) == 0  # a phrase goes to remind as typed
    assert calls(wk)[-1] == "add --tag wake next week wake timer: phrase"
    assert wake.main(["at", "never", "x"]) == 2
    assert wake.main(["at", "16:00"]) == 2
    assert wake.main(["at", "2031-06-15 13:00", "past"]) == 2
    (wk / "remind.fail").touch()
    assert wake.main(["at", "+1h", "x"]) == 3


def test_list_json_and_text(wk: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (wk / "remind.list").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "ab12",
                        "at": "2031-06-15 16:00",
                        "text": "wake timer: check",
                        "owner": False,
                        "tag": "wake",
                    },
                    {"id": "zz", "at": "2031-06-15 17:00", "text": "other", "owner": False, "tag": None},
                    {"id": "ow", "at": "2031-06-15 18:00", "text": "his", "owner": True, "tag": "wake"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert wake.main(["list"]) == 0
    assert capsys.readouterr().out.startswith("(no active rules)\nt:ab12 2031-06-15 16:00 · wake timer: check")
    wake.main(["on", "note.a", "mute", "a"])
    wake.main(["next", "+1h", "h"])
    wake.main(["rm", "1"])
    capsys.readouterr()
    assert wake.main(["list", "--all", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert [r["klass"] for r in d["rules"]] == ["hold"]
    assert [r["id"] for r in d["ended"]] == [1]
    assert d["timers"] == [{"id": "ab12", "at": "2031-06-15 16:00", "text": "wake timer: check"}]
    assert wake.main(["list"]) == 0
    assert capsys.readouterr().out.startswith("#2    hold: idle beats held until 15:00 · h")
    assert wake.main(["list", "--bogus"]) == 2


# ------------------------------------------------------------------------------------------------ check (gate view)


def notes(home: Path, *names: str) -> None:
    for n in names:
        write(home / "inbox" / n, "# x\n")


def test_check_classes_rules_and_invariants(wk: Path) -> None:
    notes(wk, "voice-1.md", "rave-radar-1792602808.md", "odd-thing-1792602808.md", "train-watch-failed-1792602808.md")
    (wk / "inbox" / ".hidden.md").write_text("x", encoding="utf-8")
    v = wake.check(str(wk / "inbox"), NOW)
    assert v["db"] == "missing" and v["hold"] is None and v["rules"] == 0
    assert v["counts"] == {"now": 1, "beat": 3, "digest": 0, "mute": 0}
    assert v["kinds"]["now"] == {"note.voice": 1}
    assert v["classes"]["beat"] == [
        "odd-thing-1792602808.md",
        "rave-radar-1792602808.md",
        "train-watch-failed-1792602808.md",
    ]
    wake.main(["on", "*", "mute", "everything known"])  # a wildcard never reaches the unknown kind
    wake.main(["next", "+2h", "quiet"])
    v = wake.check(str(wk / "inbox"), NOW)
    assert v["counts"] == {"now": 0, "beat": 1, "digest": 0, "mute": 3}
    assert v["classes"]["beat"] == ["odd-thing-1792602808.md"]
    assert v["hold"]["until"] == NOW + 7200 and v["hold"]["why"] == "quiet" and v["rules"] == 1
    wake.main(["on", "note.odd-thing", "digest", "named exactly"])
    wake.main(["on", "note.voice", "now", "newest wins"])
    v = wake.check(str(wk / "inbox"), NOW)
    assert v["counts"] == {"now": 1, "beat": 0, "digest": 1, "mute": 2}
    assert wake.check(str(wk / "inbox"), NOW + 86400 + 1)["counts"] == {"now": 1, "beat": 3, "digest": 0, "mute": 0}


def test_check_without_registry_counts_everything_as_beat(wk: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    notes(wk, "voice-1.md", "rave-radar-1.md")
    bad = wk / "kinds.toml"
    bad.write_text("[kinds\n", encoding="utf-8")
    monkeypatch.setenv("ASSISTANT_KINDS", str(bad))
    wake.main(["on", "*", "mute", "x"])
    v = wake.check(str(wk / "inbox"), NOW)
    assert v["registry_error"] and v["counts"] == {"now": 0, "beat": 2, "digest": 0, "mute": 0}


def test_check_cli_json_and_store_errors(wk: Path) -> None:
    notes(wk, "voice-1.md")
    r = run("check", "--json", "--inbox", str(wk / "inbox"))
    assert r.returncode == 0, r.stderr
    v = json.loads(r.stdout)
    assert v["ok"] is True and v["counts"]["now"] == 1 and v["ts"] == NOW
    r = run("check")
    assert r.returncode == 0 and "now: 1 (note.voice ×1)" in r.stdout and "(no store yet)" in r.stdout
    assert run("check", "--bogus").returncode == 2
    dbp = wk / ".local" / "state" / "assistant" / "events.db"
    dbp.parent.mkdir(parents=True, exist_ok=True)
    dbp.write_bytes(b"this is not a database" * 100)
    r = run("check", "--json")
    assert r.returncode == 3 and "store" in r.stderr  # the gate then falls back to today's rules


def test_check_locked_store_fails_fast(wk: Path) -> None:
    con = db.connect()
    con.execute("BEGIN EXCLUSIVE")
    try:
        r = run("check", "--json")
        assert r.returncode in (0, 3)  # WAL readers are not blocked by a writer; either way no hang
    finally:
        con.execute("ROLLBACK")


def test_cli_usage_and_help(wk: Path) -> None:
    r = run("--help")
    assert r.returncode == 0 and "wake next" in r.stdout
    assert run().returncode == 2
    assert run("frobnicate").returncode == 2
    r = run("next", "+1h", "cli", "hold")
    assert r.returncode == 0 and r.stdout.startswith("hold #1")


# ------------------------------------------------------------------------------------------------ agenda + core


def test_effective_delivery_known_kinds() -> None:
    rs = [
        {"kind_glob": "note.*", "klass": "mute"},
        {"kind_glob": None, "klass": "hold"},
        {"kind_glob": "note.exact", "klass": "digest"},
    ]
    known = {"note.reg"}
    assert events.effective_delivery({"kind": "note.reg"}, rs, known) == "mute"
    assert events.effective_delivery({"kind": "note.new"}, rs, known) == "beat"
    assert events.effective_delivery({"kind": "note.exact"}, rs[2:], known) == "digest"
    assert events.effective_delivery({"kind": "note.new"}, rs) == "mute"  # without the registry: as before
    assert events.effective_delivery({"kind": "x", "delivery": "odd"}, [], known) == "beat"


def test_agenda_footer_shows_hold_first(wk: Path) -> None:
    for a in (["on", "note.a", "mute", "a"], ["on", "note.b", "digest", "b"], ["on", "note.c", "mute", "c"]):
        wake.main(a)
    wake.main(["next", "+2h", "nothing startable"])
    con = db.connect()
    ag = agenda.build(con, now_ts=NOW, beat_id=1, last_beat_ts=None, reg=kinds.load(KINDS), tz=Z)
    line = next(x for x in ag.text.splitlines() if x.startswith("Last compaction"))
    assert "Wake rules: #4 idle beats held until 15.06 16:00 (nothing startable), #3 note.c→mute" in line
    assert line.endswith("+1")


def test_store_is_private(wk: Path) -> None:
    wake.main(["on", "note.a", "mute", "a"])
    p = wk / ".local" / "state" / "assistant" / "events.db"
    assert p.stat().st_mode & 0o077 == 0
    with sqlite3.connect(p) as c:
        assert c.execute("SELECT COUNT(*) FROM wake_rules").fetchone()[0] == 1
