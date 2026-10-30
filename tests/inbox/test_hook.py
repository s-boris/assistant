"""The agenda hook through its shell wrapper: beats only, main session only, beats row, fail-open everywhere."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from inbox import hook
from inbox_testlib import EVENTS, HOOK, PY, REPO, write

from core import db

BEAT = "[heartbeat] Idle 10+ min, 2 new note(s) in ~/inbox. Run your heartbeat routine."


def run_hook(
    home: Path,
    prompt: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 10,
    transcript: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], float]:
    data = json.dumps(
        {
            "prompt": prompt,
            "cwd": cwd if cwd is not None else str(home / "workspace"),
            "hook_event_name": "UserPromptSubmit",
            **({"transcript_path": transcript} if transcript is not None else {}),
        }
    )
    t0 = time.monotonic()
    r = subprocess.run(
        ["/bin/sh", str(HOOK)],
        input=data,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "EVENTS_BIN": str(EVENTS), **(env or {})},
    )
    return r, time.monotonic() - t0


def ctx(r: subprocess.CompletedProcess[str]) -> str:
    # an empty stdout = the hook failed open: its events call hit agenda.sh's 2 s budget (seen under host load ~40,
    # tests/quarantine.txt), or crashed; say so instead of a bare JSONDecodeError
    assert r.stdout.strip(), f"hook printed nothing (2 s budget hit or crash; rc {r.returncode}): {r.stderr[-300:]!r}"
    d = json.loads(r.stdout)
    assert d["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return d["hookSpecificOutput"]["additionalContext"]


def notes(home: Path, n: int = 2) -> None:
    for i in range(n):
        write(home / "inbox" / f"vault-events-{1792602800 + i}.md", f"# feed {i}\nNext: act {i}\n")


def test_beat_prints_agenda_marks_and_records(home: Path) -> None:
    notes(home)
    r0 = subprocess.run([PY, "-I", str(EVENTS), "ingest"], capture_output=True, text=True, env=os.environ)
    assert r0.returncode == 0, r0.stderr
    r, dt = run_hook(home, BEAT)
    assert r.returncode == 0 and r.stderr == ""
    text = ctx(r)
    assert text.startswith("[agenda ") and "beat #1 · last —" in text and "#1 note.vault-events" in text
    con = db.connect()
    b = dict(con.execute("SELECT * FROM beats").fetchone())
    assert b["ok"] == 1 and b["source"] == "notes" and b["chars"] == len(text) and json.loads(b["listed"]) == [1, 2]
    assert [r["status"] for r in con.execute("SELECT status FROM events")] == ["listed", "listed"]
    assert dt < 2.0
    r2, _ = run_hook(home, "[heartbeat] Idle 60+ min, 0 new note(s) in ~/inbox. Run your heartbeat routine.")
    t2 = ctx(r2)
    assert "beat #2 · last " in t2 and "beat #2 · last —" not in t2 and "Open (2, listed before" in t2
    assert con.execute("SELECT source FROM beats WHERE id=2").fetchone()[0] == "idle"
    log = (home / ".local/state/assistant/agenda-hook.log").read_text(encoding="utf-8")
    assert log.count(" ok beat=") == 2


@pytest.mark.parametrize(
    "prompt",
    [
        "hello there",
        "what does the heartbeat plugin do?",
        "x" * 301 + "[heartbeat]",
        "<task-notification>agent done</task-notification>",
    ],
)
def test_non_beats_print_nothing(home: Path, prompt: str) -> None:
    r, _ = run_hook(home, prompt)
    assert (r.returncode, r.stdout) == (0, "")
    assert not (home / ".local/state/assistant/events.db").exists()


def test_other_cwd_prints_nothing(home: Path) -> None:
    r, _ = run_hook(home, BEAT, cwd=str(home / "jobs"))
    assert (r.returncode, r.stdout) == (0, "")


def test_main_session_after_cd_still_gets_the_agenda(home: Path) -> None:
    # The session's cwd follows a `cd` in its Bash (~/inbox, ~); its transcript stays in ~/workspace's project folder.
    main = os.path.join(os.environ["ASSISTANT_PROJECT_DIR"], "0b1e2c3d-main.jsonl")
    for cwd in (str(home / "inbox"), str(home)):
        r, _ = run_hook(home, BEAT, cwd=cwd, transcript=main)
        assert r.returncode == 0 and ctx(r).startswith("[agenda ")


@pytest.mark.parametrize("transcript", ["", "relative.jsonl", "/elsewhere/-home-node-jobs/1.jsonl"])
def test_other_cwd_and_other_transcript_prints_nothing(home: Path, transcript: str) -> None:
    r, _ = run_hook(home, BEAT, cwd=str(home / "jobs"), transcript=transcript)
    assert (r.returncode, r.stdout) == (0, "")
    assert not (home / ".local/state/assistant/events.db").exists()


@pytest.mark.parametrize("switch", ["agenda-off", "events-off"])
def test_switches(home: Path, switch: str) -> None:
    (home / ".config/assistant" / switch).touch()
    r, _ = run_hook(home, BEAT)
    assert (r.returncode, r.stdout) == (0, "")


def test_missing_store_is_created(home: Path) -> None:
    r, _ = run_hook(home, BEAT)
    assert r.returncode == 0 and ctx(r).startswith("[agenda ")
    assert (home / ".local/state/assistant/events.db").exists()


def test_unwritable_state_fails_open(home: Path) -> None:
    st = home / ".local" / "state"
    st.chmod(0o500)
    try:
        r, dt = run_hook(home, BEAT)
    finally:
        st.chmod(0o700)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert dt < 2.5


def test_locked_store_fails_open_and_logs(home: Path) -> None:
    db.connect().close()
    holder = subprocess.Popen(
        [
            PY,
            "-I",
            "-c",
            (
                "import sqlite3, sys, time\n"
                f"c = sqlite3.connect({str(home / '.local/state/assistant/events.db')!r}, isolation_level=None)\n"
                "c.execute('BEGIN EXCLUSIVE'); c.execute(\"INSERT INTO meta VALUES('lock', '1')\")\n"
                "print('locked', flush=True); time.sleep(6)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        r, dt = run_hook(home, BEAT)
    finally:
        holder.kill()
        holder.wait()
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert dt < 2.5
    log = (home / ".local/state/assistant/agenda-hook.log").read_text(encoding="utf-8")
    assert " fail source=notes " in log and "locked" in log


def test_slow_hook_is_cut_at_two_seconds(home: Path) -> None:
    r, dt = run_hook(home, BEAT, env={"EVENTS_HOOK_TEST_SLEEP": "5"})
    assert (r.returncode, r.stdout) == (0, "")
    assert 1.8 < dt < 3.0


def test_broken_install_fails_open(home: Path) -> None:
    r, _ = run_hook(home, BEAT, env={"EVENTS_BIN": str(home / "bin" / "missing-events")})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    bad = home / "lib-broken"
    (bad / "inbox").mkdir(parents=True)
    (bad / "inbox" / "__init__.py").write_text("raise ImportError('broken')\n", encoding="utf-8")
    (bad / "core").mkdir()
    (bad / "core" / "__init__.py").write_text("", encoding="utf-8")
    r2, _ = run_hook(home, BEAT, env={"ASSISTANT_LIB": str(bad)})
    assert (r2.returncode, r2.stdout) == (0, "")


@pytest.mark.parametrize(
    "prompt,src",
    [
        ("[heartbeat] Just restarted (settings change). Run your heartbeat routine; ...", "restart"),
        ("[heartbeat] Idle 10+ min, 3 new note(s) in ~/inbox. Run your heartbeat routine.", "notes"),
        ("[heartbeat] Idle 60+ min, 0 new note(s) in ~/inbox. Run your heartbeat routine.", "idle"),
        ("The heartbeat plugin sent a message:\n[heartbeat] Idle 10+ min, 1 new note(s) in ~/inbox.", "notes"),
        ("hello", None),
        ("x" * 400 + "[heartbeat]", None),
    ],
)
def test_beat_source(prompt: str, src: str | None) -> None:
    assert hook.beat_source(prompt) == src


def test_restart_beat_lists_open_events_in_full(home: Path) -> None:
    notes(home, 3)
    subprocess.run([PY, "-I", str(EVENTS), "ingest"], check=True, capture_output=True, env=os.environ)
    run_hook(home, BEAT)
    r, _ = run_hook(home, "[heartbeat] Just restarted (test). Run your heartbeat routine.")
    t = ctx(r)
    assert "Open (3, listed before, not acked):" in t and "#1 note.vault-events" in t and "→ Next: act 0" in t
    con = db.connect()
    assert con.execute("SELECT source, listed FROM beats WHERE id=2").fetchone()[:] == ("restart", "[1, 2, 3]")


def test_hook_source_has_no_repo_imports_outside_core_and_inbox() -> None:
    """The deployed hook runs with -I -S: stdlib + core + inbox only."""
    import ast

    std = {
        "json",
        "os",
        "re",
        "sqlite3",
        "sys",
        "time",
        "datetime",
        "typing",
        "subprocess",
        "pathlib",
        "fnmatch",
        "hashlib",
        "dataclasses",
        "collections",
        "tomllib",
        "argparse",
        "statistics",
        "zoneinfo",
        "contextlib",
        "__future__",
        "pwd",
    }
    for f in [*(REPO / "packages" / "inbox").glob("*.py"), *(REPO / "core").glob("*.py")]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            else:
                continue
            for m in mods:
                # any stdlib module is fine under -I -S (core grows: tempfile, fcntl, urllib, ...); no third party
                ok = (
                    m in std or m in sys.stdlib_module_names or m in ("core", "inbox", "assistant_profile", "tripclock")
                )
                assert ok, f"{f.name}: {m}"
