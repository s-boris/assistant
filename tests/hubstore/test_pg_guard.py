"""hubfix.Pg's parent-death guard: a test process killed outright (a CI timeout, SIGKILL) takes its throwaway Postgres
along instead of leaving an orphan postmaster; a normal stop() leaves no guard behind."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import hubfix
import pytest

HERE = Path(__file__).resolve().parent


def _gone(pid: int, wait: float) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            s = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return True
        if s[s.rindex(")") + 2] in "ZX":
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def pg_ok():
    if hubfix.pg_bin() is None:
        pytest.skip("no Postgres binaries (/usr/lib/postgresql/*/bin)")


def test_a_killed_test_process_takes_its_postgres_along(pg_ok, tmp_path):
    code = (
        "import pathlib, sys, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import hubfix\n"
        "pg = hubfix.Pg(pathlib.Path(sys.argv[2])).start()\n"
        "print(pg.guard.pid, flush=True)\n"
        "time.sleep(300)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code, str(HERE), str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        guard = int(p.stdout.readline() if p.stdout else "0")
        postmaster = int((tmp_path / "pgdata" / "postmaster.pid").read_text().split()[0])
        assert not _gone(postmaster, 0.1)
        p.kill()  # what subprocess.run(timeout=…) does to pytest
        p.wait(10)
        assert _gone(postmaster, 30), "the guard stops the orphaned postmaster"
        assert _gone(guard, 30)
        assert not (tmp_path / "pgdata" / "postmaster.pid").exists()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(10)
        hubfix.Pg(tmp_path).stop()


def test_stop_ends_the_guard(pg_ok, tmp_path):
    pg = hubfix.Pg(tmp_path).start()
    assert pg.guard is not None and pg.guard.poll() is None
    pg.stop()
    assert pg.guard.poll() is not None
    pg.stop()  # twice is fine (a test stops it, then the fixture)
