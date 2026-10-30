"""scripts/tg's argument handling: -h/--help prints the header and exits 0, no text exits 2 with the usage, and any
other `--` argument is refused with exit 2. None of them sends anything: not the direct sender, not the outbox, no
shadow row and no usage count (every seam is a recording stub, and the live adapter's pid file is present so the
outbox path would be taken)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from tg_testlib import TG, stub, wait_for

SEAMS = ("sent", "outbox", "adapter", "usage")


def seams(home: Path) -> dict:
    for name in SEAMS:
        rec = f'printf "%s\\n" "$*" >> "{home}/{name}.txt"; cat >> "{home}/{name}.stdin" 2>/dev/null; exit 0'
        stub(home, name, rec)
    stub(home, "sleepin", '[ "$1" = gate ] && [ "$2" = -- ] || exit 9; shift 2; exec "$@"')
    state = home / ".local/state/tg-adapter"
    (state / "live").mkdir(parents=True, exist_ok=True)
    (state / "live" / "adapter.pid").write_text("1\ntg-adapter\n")
    return {
        **os.environ,
        "TG_SEND_BIN": str(home / "stub" / "sent"),
        "TG_OUTBOX_BIN": str(home / "stub" / "outbox"),
        "TG_ADAPTER_BIN": str(home / "stub" / "adapter"),
        "TG_ADAPTER_STATE_DIR": str(state),
        "TG_SLEEPIN_BIN": str(home / "stub" / "sleepin"),
    }


def run_tg(home: Path, *args: str, script: Path = TG) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(script), *args], capture_output=True, text=True, env=seams(home), stdin=subprocess.DEVNULL, timeout=9
    )


def assert_nothing_sent(home: Path) -> None:
    time.sleep(0.3)  # the usage count and shadow record run in the background
    assert [n for n in SEAMS if (home / f"{n}.txt").exists()] == []


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_the_header_and_sends_nothing(home, flag):
    r = run_tg(home, flag)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("Text the owner on Telegram from scripts and cron jobs:  tg [--soft|--urgent]")
    assert "Test seams:" in r.stdout and "#" not in r.stdout.splitlines()[0]
    assert "soft=" not in r.stdout  # stops at the code
    assert_nothing_sent(home)


@pytest.mark.parametrize("args", [("--soft", "--help"), ("disk 3 hot", "-h")])
def test_help_anywhere_sends_nothing(home, args):
    assert run_tg(home, *args).returncode == 0
    assert_nothing_sent(home)


def test_help_skips_the_deployed_managed_line(home, tmp_path):
    lines = TG.read_text().split("\n")
    lines.insert(1, "# managed: ~/opt/assistant — edit there, deploy with assistant land")
    deployed = tmp_path / "tg"
    deployed.write_text("\n".join(lines))
    r = run_tg(home, "--help", script=deployed)
    assert r.returncode == 0
    assert r.stdout.startswith("Text the owner") and "managed:" not in r.stdout
    assert_nothing_sent(home)


@pytest.mark.parametrize("args", [(), ("",), ("  ", ""), ("--soft",), ("--urgent", " ")])
def test_no_text_prints_usage_exits_2_and_sends_nothing(home, args):
    r = run_tg(home, *args)
    assert r.returncode == 2
    assert "no text" in r.stderr and "Text the owner on Telegram" in r.stderr
    assert r.stdout == ""
    assert_nothing_sent(home)


@pytest.mark.parametrize("args", [("--force",), ("--", "hi"), ("--soft", "--quiet", "hi"), ("disk hot", "--verbose")])
def test_unknown_double_dash_argument_is_refused(home, args):
    r = run_tg(home, *args)
    assert r.returncode == 2
    assert "unknown option" in r.stderr and "nothing sent" in r.stderr
    assert_nothing_sent(home)


def test_plain_text_still_sends(home):
    """Positive control for the stubs: a normal text goes to the outbox (the live pid file is present)."""
    r = run_tg(home, "disk", "3 hot", "--soft")
    assert r.returncode == 0, r.stderr
    assert (home / "outbox.txt").read_text() == "send tg --soft\n"
    assert (home / "outbox.stdin").read_text() == "disk 3 hot"
    assert not (home / "sent.txt").exists()
    assert wait_for(lambda: (home / "usage.txt").exists()) and "tg.send" in (home / "usage.txt").read_text()
