"""core.locks: exclusive advisory file locks (flock) with a timeout. Stdlib only.

  acquire(path, wait=None, mode=0o600, dir_mode=None)  -> fd, or None when still busy after `wait` seconds
                     wait=None blocks until free, wait=0 tries once (non-blocking), wait>0 polls until the deadline
  release(fd)        unlock + close (closing alone also unlocks; a dead process never holds the lock)
  hold(path, wait=None, ...)   context manager yielding the fd, or None when busy (the caller decides what busy means)

The lock file is created when missing (mode, minus the umask), and its parent directory too (dir_mode applies to
the last one, like os.makedirs). The fd is close-on-exec, so children never inherit the lock. Errors other than
"busy" (cannot create the file, no lock support) propagate.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator

__all__ = ["acquire", "hold", "release"]

_StrPath = str | os.PathLike[str]


def _try(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def acquire(
    path: _StrPath,
    *,
    wait: float | None = None,
    mode: int = 0o600,
    dir_mode: int | None = None,
    poll: float = 0.05,
) -> int | None:
    """Lock path exclusively; the fd (keep it open while holding the lock), or None when busy after `wait` s."""
    p = os.path.abspath(os.fspath(path))
    d = os.path.dirname(p)
    if dir_mode is None:
        os.makedirs(d, exist_ok=True)
    else:
        os.makedirs(d, mode=dir_mode, exist_ok=True)
    fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, mode)
    try:
        if wait is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd
        deadline = time.monotonic() + max(0.0, wait)
        while not _try(fd):
            left = deadline - time.monotonic()
            if left <= 0:
                os.close(fd)
                return None
            time.sleep(min(poll, left))
        return fd
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def release(fd: int | None) -> None:
    if fd is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


@contextlib.contextmanager
def hold(
    path: _StrPath, *, wait: float | None = None, mode: int = 0o600, dir_mode: int | None = None
) -> Iterator[int | None]:
    """with locks.hold(p, wait=0) as fd: (fd None = busy). Released on exit."""
    fd = acquire(path, wait=wait, mode=mode, dir_mode=dir_mode)
    try:
        yield fd
    finally:
        release(fd)
