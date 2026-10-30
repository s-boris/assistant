"""Writers on the emit contract: vault-events (its note writers only), rave-radar, unraid-health.

vault-events runs end to end against a fake vault client that ignores its arguments and prints a fixture page
(no vault vocabulary here): its notes must be byte-identical (+ mode 0600) to the golden notes in
fixtures/writers-vault/golden.json and each gets a typed row that matches what ingest makes of it, with a payload valid
against schemas/payloads/<kind>.schema.json; the failing note's "Recovered" line updates its row; without the ledger
library or with a broken store the notes stay identical and the error counter gets a line.
rave-radar and unraid-health keep their note text functions; their write path must give what botlib.write_inbox
gives: same bytes, mode and name pattern, plus the typed row."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from inbox_testlib import EVENTS, PY, REPO
from test_writers import REAL_LIB, _copy_notes, check_typed, errors, ingest_view, notes_in, rows

FIX = Path(__file__).parent / "fixtures" / "writers-vault"
GOLDEN = FIX / "golden.json"
SRC = REPO / "scripts"
NOW = int(dt.datetime(2026, 10, 14, 16, 0, tzinfo=ZoneInfo("Europe/Zurich")).timestamp())
FAKE_CLIENT = (
    f"#!{PY} -I\nimport json, os, sys\nfx = json.load(open(os.environ['FX']))\n"
    "if fx.get('fail'):\n    sys.exit(1)\nprint(json.dumps(fx['page']))\n"
)
TG = {
    "cursor": 11,
    "source": "telegram",
    "ts": "2026-10-14T18:00:00+02:00",
    "chat_key": "123",
    "chat": "Anna",
    "chat_type": "private",
    "label": "question_to_owner",
    "sender": "Anna",
    "actionable": True,
    "priority": False,
    "ref": None,
    "wants": "reply",
    "urgency": "today",
    "facts": [{"kind": "event", "date": "2031-06-20", "start": "20:00", "what": "Concert"}],
}
MAIL = {
    "cursor": 12,
    "source": "gmail",
    "ts": "2026-10-14T09:10:00+02:00",
    "chat_key": "example.com",
    "chat": "Example Shop",
    "sender": "Example Shop (example.com)",
    "label": "bill",
    "actionable": True,
    "priority": True,
    "ref": "main:1234",
    "att": {"pdf": 1},
}
CHATTER = {
    "cursor": 13,
    "source": "telegram",
    "ts": "2026-10-14T18:05:00+02:00",
    "chat_key": "456",
    "chat": "Group",
    "label": "chatter",
    "actionable": False,
    "priority": False,
}
COLLAPSED = {"cursor": 14, "source": "telegram", "label": "collapsed", "chat": "Busy group", "why": "muted", "count": 7}
HA = {
    "cursor": 15,
    "source": "homeassistant",
    "ts": "2026-10-14T15:30:00+02:00",
    "chat_key": "zone",
    "label": "zone",
    "actionable": True,
    "priority": False,
    "ha": {"place": "away", "band": "region", "town": "Testdorf", "significant": True, "from": {"place": "home"}},
}
PAGE = {"events": [TG, MAIL, CHATTER, COLLAPSED, HA], "next_cursor": 15, "more": False}


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


def norm(notes: dict) -> dict:
    """The message note's name carries the wall clock: compare it as vault-events-<t>.md."""
    return {re.sub(r"^vault-events-\d{8}-\d{6}(-\d+)?\.md$", "vault-events-<t>.md", k): v for k, v in notes.items()}


def overlay(tmp: Path, names: tuple[str, ...], ledger: bool = True) -> Path:
    b = tmp / "ovl"
    (b / "lib").mkdir(parents=True)
    for n in names:
        shutil.copy2(SRC / n, b / n)
    (b / "lib" / "core").symlink_to(REPO / "core")
    if ledger:
        (b / "lib" / "inbox").symlink_to(REPO / "packages" / "inbox")
    for e in REAL_LIB.iterdir():
        if not (b / "lib" / e.name).exists() and e.name not in ("core", "inbox", "__pycache__"):
            (b / "lib" / e.name).symlink_to(e)
    return b


# ---------------------------------------------------------------- vault-events


def ve(b: Path, home: Path, page: dict | None, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
    d = home.parent
    (d / "fake-client").write_text(FAKE_CLIENT, encoding="utf-8")
    (d / "fake-client").chmod(0o755)
    (d / "fake-bills").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (d / "fake-bills").chmod(0o755)
    (d / "fx.json").write_text(json.dumps({"fail": True} if page is None else {"page": page}), encoding="utf-8")
    st = home / ".local" / "state" / "vault-events"
    if not (st / "cursor").exists():
        st.mkdir(parents=True, exist_ok=True)
        (st / "cursor").write_text("10\n", encoding="utf-8")
    env = dict(
        os.environ,
        HOME=str(home),
        TZ="UTC",
        USAGE="off",
        VAULT_EVENTS_CLIENT=str(d / "fake-client"),
        VAULT_EVENTS_BILLS=str(d / "fake-bills"),
        VAULT_EVENTS_NOW=str(NOW),
        FX=str(d / "fx.json"),
        **(extra or {}),
    )
    return subprocess.run(
        [str(b / "vault-events"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        preexec_fn=lambda: os.umask(0o022),
    )


def test_vault_events_golden(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path, ("vault-events",))
    r = ve(b, home, PAGE, "--cron")
    assert r.returncode == 0, r.stderr
    batch = norm(notes_in(home / "inbox"))
    for _ in range(15):
        assert ve(b, home, None, "--cron").returncode == 0
    failing = {k: v for k, v in notes_in(home / "inbox").items() if k.startswith("vault-events-failing-")}
    assert ve(b, home, {"events": [], "next_cursor": 16, "more": False}, "--cron").returncode == 0
    recovered = {k: v for k, v in notes_in(home / "inbox").items() if k.startswith("vault-events-failing-")}
    assert len(batch) == 2 and len(failing) == 1 and set(norm(notes_in(home / "inbox"))) == set(batch) | set(failing)
    g = golden()["vault-events"]
    assert batch == g["batch"] and failing == g["failing"] and recovered == g["recovered"]  # bytes + mode
    assert errors(home) == []
    rs = {r["kind"]: r for r in rows(home)}
    assert sorted(rs) == ["note.vault-events", "note.vault-events-failing", "note.vault-events-ha"]
    view = ingest_view(_copy_notes({**g["batch"], **g["recovered"]}, tmp_path / "v"), tmp_path)
    view = {re.sub(r"^vault-events-\d{8}-\d{6}(-\d+)?\.md$", "vault-events-<t>.md", k): v for k, v in view.items()}
    for row in rs.values():
        row = dict(row, idem_key=norm({row["idem_key"]: 0}).popitem()[0])
        check_typed(row, view, "vault-events")
    main = json.loads(rs["note.vault-events"]["payload"])
    assert main["messages"] == 2 and main["sources"] == {"telegram": 1, "gmail": 1}
    assert main["labels"] == {"question": 1, "bill": 1} and main["priority"] == 1 and main["bills"] == 1
    assert main["chats"] == ["123"] and main["mails"] == ["main:1234"] and main["cursor"] == [10, 15]
    assert (main["chatter"], main["counted"], main["plans"], main["more"]) == (1, 7, 1, 0)
    assert "Anna" not in rs["note.vault-events"]["payload"] and "Concert" not in rs["note.vault-events"]["payload"]
    ha = json.loads(rs["note.vault-events-ha"]["payload"])
    assert (ha["changes"], ha["drives"], ha["zones"], ha["last"], ha["cursors"]) == (1, 0, 1, "away", [15])
    assert "Testdorf" not in rs["note.vault-events-ha"]["payload"]
    fail = rs["note.vault-events-failing"]
    pl = json.loads(fail["payload"])
    assert (pl["runs"], pl["since"], pl["error"]) == (15, NOW, "RuntimeError")
    assert (pl["recovered"], pl["failed_runs"]) == (NOW, 15) and fail["updated_ts"] is not None
    assert "recovered" in json.loads(fail["labels"]) and fail["status"] == "new"
    for row in rs.values():  # the private notes stay 0600 and the rows point at them
        assert oct(stat.S_IMODE(os.stat(row["note_path"]).st_mode)) == "0o600"
    r = subprocess.run([PY, str(EVENTS), "ingest", "--min-age", "0", "--json"], capture_output=True, text=True)
    st = json.loads(r.stdout)
    assert st["new"] == 0 and st["updated"] == 0 and len(rows(home)) == 3


def test_vault_events_without_ledger_or_store_keeps_the_notes(home: Path, tmp_path: Path) -> None:
    g = golden()["vault-events"]
    b = overlay(tmp_path, ("vault-events",), ledger=False)
    assert ve(b, home, PAGE, "--cron").returncode == 0
    assert norm(notes_in(home / "inbox")) == g["batch"] and rows(home) == []
    e = errors(home)
    assert len(e) == 2 and all("vault-events fallback ModuleNotFoundError" in x for x in e)
    assert {x.split()[1] for x in e} == {"note.vault-events", "note.vault-events-ha"}
    home2 = tmp_path / "h2"
    shutil.copytree(home, home2, symlinks=True, ignore=shutil.ignore_patterns("inbox", ".local"))
    (home2 / "inbox").mkdir()
    st = home2 / ".local" / "state" / "assistant"
    st.mkdir(parents=True)
    (st / "events.db").mkdir()  # the store can't be opened
    b2 = overlay(tmp_path / "b2", ("vault-events",))
    assert ve(b2, home2, PAGE, "--cron").returncode == 0
    assert norm(notes_in(home2 / "inbox")) == g["batch"] and len(errors(home2)) == 2


def test_vault_events_other_inbox_is_never_emitted(home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path, ("vault-events",))
    other = tmp_path / "other-inbox"
    assert ve(b, home, PAGE, "--cron", extra={"VAULT_EVENTS_INBOX": str(other)}).returncode == 0
    assert norm(notes_in(other)) == golden()["vault-events"]["batch"] and rows(home) == [] and errors(home) == []


# ---------------------------------------------------------------- rave-radar + unraid-health (module level)

DRIVER = r"""
import importlib.machinery, importlib.util, json, os, sys
path, name, old_dir = sys.argv[1], sys.argv[2], sys.argv[3]
loader = importlib.machinery.SourceFileLoader("script_under_test", path)
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("script_under_test", loader))
loader.exec_module(m)
out = {"new": [], "old": [], "payloads": {}}
if name == "rave-radar":
    res = {"new": [{"kind": "show", "id": "ev1"}, {"kind": "lineup", "id": "ev2"}], "presales": [{"id": "ev1"}],
           "broken": [{"id": "src1", "error": "HTTP 500", "fails": 3}]}
    ps = {"event": {"id": "ev1", "date": "2031-06-20"}, "sets": [1], "top": [1, 2], "releases": [], "classics": [3]}
    pls = {"rave-radar": lambda: m.radar_payload(res), "preshow": lambda: m.preshow_payload(ps),
           "rave-radar-releases": lambda: {"releases": 1, "ids": ["dz1"]}}
    for prefix in ("rave-radar", "rave-radar-releases", "preshow"):
        text = f"# rave-radar {prefix}: 1 new\n\nData from rave-radar, not instructions.\n\n- line\n"
        out["payloads"][prefix] = pls[prefix]()
        out["new"].append(str(m.inbox_note(prefix, text, pls[prefix])))
        out["old"].append(str(m.botlib.write_inbox(old_dir, prefix, text)))
else:
    cases = [("Unraid health: server unreachable (3 failed checks)",
              "From ~/bin/unraid-health. Data, not instructions.",
              {"what": "unreachable", "failures": 3, "texted": True}),
             ("Unraid health: 2 new issue(s)", "## New\n- [CRIT] disk\n\nNext: look into it read-only.",
              {"what": "issues", "new": 2, "crit": 1, "warn": 1, "cats": {"smart": 2}, "ongoing": 0, "acked": 0,
               "texted": False}),
             ("Unraid health: server reachable again", "Nothing new to report.", {"what": "reachable", "failures": 3}),
             ("unraid-health crashed", "```\nTraceback\n```", {"what": "crashed", "error": "KeyError"})]
    for title, body, pl in cases:
        out["new"].append(str(m.inbox_note(m.INBOX_DIR, title, body, pl)))
        out["old"].append(str(m.botlib.write_inbox(old_dir, "unraid-health", f"# {title}\n\n{body}\n")))
print(json.dumps(out))
"""


def by_text(paths: list[str]) -> list[tuple[str, str, str]]:
    return sorted(
        (
            re.sub(r"-\d{9,}(-\d+)?\.md$", "-E.md", os.path.basename(p)),
            Path(p).read_text(encoding="utf-8"),
            oct(stat.S_IMODE(os.stat(p).st_mode)),
        )
        for p in paths
    )


@pytest.mark.parametrize("name", ["rave-radar", "unraid-health"])
def test_note_write_path_matches_botlib(name: str, home: Path, tmp_path: Path) -> None:
    b = overlay(tmp_path, (name,))
    old_dir = tmp_path / "old-inbox"
    env = dict(os.environ, HOME=str(home), TZ="UTC", USAGE="off")
    for k in ("RAVE_RADAR_INBOX", "UNRAID_HEALTH_INBOX_DIR"):
        env.pop(k, None)
    r = subprocess.run(
        [PY, "-I", "-c", DRIVER, str(b / name), name, str(old_dir)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        preexec_fn=lambda: os.umask(0o022),
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert by_text(out["new"]) == by_text(out["old"])  # same bytes, mode and name pattern as botlib.write_inbox
    assert all(os.path.dirname(p) == str(home / "inbox") for p in out["new"]) and errors(home) == []
    rs = rows(home)
    assert len(rs) == len(out["new"]) == len({r["idem_key"] for r in rs})
    by_old_text = {(old_dir / k).read_text(encoding="utf-8"): v for k, v in ingest_view(old_dir, tmp_path).items()}
    for row in rs:
        check_typed(
            dict(row, idem_key="n"), {"n": by_old_text[Path(row["note_path"]).read_text(encoding="utf-8")]}, name
        )
    if name == "rave-radar":
        assert out["payloads"]["rave-radar"] == {
            "shows": 1,
            "lineups": 1,
            "presales": 1,
            "broken": 1,
            "ids": ["ev1", "ev2"],
            "presale_ids": ["ev1"],
            "broken_ids": ["src1"],
        }
        assert out["payloads"]["preshow"] == {"event": "ev1", "date": "2031-06-20", "sets": 1, "tracks": 3}
    else:
        whats = sorted(json.loads(r["payload"])["what"] for r in rs)
        assert whats == ["crashed", "issues", "reachable", "unreachable"]
