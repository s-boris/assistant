import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from core import locks

REPO = Path(__file__).resolve().parents[2]

HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from core import locks
fd = locks.acquire(sys.argv[2])
print("held", flush=True)
time.sleep(float(sys.argv[3]))
"""


def holder(path, seconds):
    p = subprocess.Popen(
        [sys.executable, "-I", "-c", HOLDER, str(REPO), str(path), str(seconds)], stdout=subprocess.PIPE, text=True
    )
    assert p.stdout is not None and p.stdout.readline().strip() == "held"
    return p


def test_acquire_creates_file_and_dirs(tmp_path):
    p = tmp_path / "state" / "x" / ".lock"
    fd = locks.acquire(p, dir_mode=0o700)
    assert isinstance(fd, int) and p.exists()
    assert stat.S_IMODE(os.stat(p).st_mode) & 0o077 == 0
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert locks.acquire(p, wait=0) is None  # flock locks are per open file: a second fd is refused
    locks.release(fd)
    fd2 = locks.acquire(p, wait=0)
    assert fd2 is not None
    locks.release(fd2)
    locks.release(None)


def test_busy_nonblocking_and_timeout(tmp_path):
    p = tmp_path / ".lock"
    h = holder(p, 3)
    try:
        t0 = time.monotonic()
        assert locks.acquire(p, wait=0) is None
        assert time.monotonic() - t0 < 0.5
        t0 = time.monotonic()
        assert locks.acquire(p, wait=0.4) is None
        assert 0.35 <= time.monotonic() - t0 < 2
    finally:
        h.kill()
        h.wait()
    fd = locks.acquire(p, wait=2)  # the dead holder's lock is gone
    assert fd is not None
    locks.release(fd)


def test_wait_gets_the_lock_when_freed(tmp_path):
    p = tmp_path / ".lock"
    h = holder(p, 0.5)
    try:
        t0 = time.monotonic()
        fd = locks.acquire(p, wait=10)
        assert fd is not None and time.monotonic() - t0 < 5
        locks.release(fd)
        fd = locks.acquire(p)  # blocking, free now
        locks.release(fd)
    finally:
        h.wait()


def _fds_on(p):
    """Open fds of this process on `p` (not the fd count: other tests' sockets and GC close fds meanwhile)."""
    n = 0
    for f in os.listdir("/proc/self/fd"):
        try:
            n += os.readlink(f"/proc/self/fd/{f}") == str(p)
        except OSError:
            pass
    return n


def test_hold_context_and_no_fd_leak(tmp_path):
    p = tmp_path / ".lock"
    with locks.hold(p, wait=0) as fd:
        assert fd is not None
        with locks.hold(p, wait=0) as fd2:
            assert fd2 is None
    for _ in range(20):
        with locks.hold(p, wait=0):
            assert _fds_on(p) == 1
    assert _fds_on(p) == 0


def test_lock_is_not_inherited(tmp_path):
    p = tmp_path / ".lock"
    fd = locks.acquire(p)
    assert fd is not None
    assert not os.get_inheritable(fd)
    locks.release(fd)
