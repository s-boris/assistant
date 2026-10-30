"""core.jsonstate: state files (JSON or text) read leniently and written atomically. Stdlib only.

  load(path, default=None, strict=False)   parsed JSON; default when the file is missing, or (strict=False) when
                                           it is unreadable or broken; strict=True re-raises that OSError/ValueError
  save(path, obj, newline=False, mode=None, dir_mode=None, **dump_kw)
                                           json.dumps(obj, **dump_kw) (+ "\\n" with newline=True), written atomically
  write_text(path, text, encoding="utf-8", mode=None, dir_mode=None)
  write_bytes(path, data, mode=None, dir_mode=None)
  current_umask() / fsync_dir(d)           helpers for other core modules

Atomic = a hidden temp file in the same directory (".<name>.<random>.tmp"), written, flushed and fsynced, then
renamed over the target and the directory fsynced: a reader sees the old file or the new one, never half of one,
and a crash after the rename keeps the new bytes. Missing parent directories are created (dir_mode applies to the
last one, like os.makedirs). File mode: `mode` when given; else the mode of the file being replaced (a private
0600 file stays private); else a new file gets 0666 minus the umask, as open() would give it.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

__all__ = ["current_umask", "fsync_dir", "load", "save", "write_bytes", "write_text"]

_StrPath = str | os.PathLike[str]


def load(path: _StrPath, default: Any = None, *, strict: bool = False) -> Any:
    """Parsed JSON at path; default when it is missing. Unreadable or broken: default (strict=False), else the
    original OSError / ValueError propagates (the caller decides how loud to be)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        if strict:
            raise
        return default


def current_umask() -> int:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    old = os.umask(0o022)  # fallback: read it by setting it (not thread-safe; only without /proc)
    os.umask(old)
    return old


def fsync_dir(d: _StrPath) -> None:
    """fsync a directory (makes a rename in it durable); errors are ignored."""
    try:
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _target_mode(path: str, mode: int | None) -> int:
    if mode is not None:
        return mode
    try:
        st = os.stat(path)
        return st.st_mode & 0o7777
    except OSError:
        return 0o666 & ~current_umask()


def write_bytes(path: _StrPath, data: bytes, *, mode: int | None = None, dir_mode: int | None = None) -> None:
    """Atomically replace path with data (see the module doc for modes and directories)."""
    p = os.path.abspath(os.fspath(path))
    d, name = os.path.split(p)
    if dir_mode is None:
        os.makedirs(d, exist_ok=True)
    else:
        os.makedirs(d, mode=dir_mode, exist_ok=True)
    want = _target_mode(p, mode)
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), want)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(d)


def write_text(
    path: _StrPath, text: str, *, encoding: str = "utf-8", mode: int | None = None, dir_mode: int | None = None
) -> None:
    write_bytes(path, text.encode(encoding), mode=mode, dir_mode=dir_mode)


def save(
    path: _StrPath,
    obj: Any,
    *,
    newline: bool = False,
    mode: int | None = None,
    dir_mode: int | None = None,
    **dump_kw: Any,
) -> None:
    """Atomically write json.dumps(obj, **dump_kw) (plus a final newline when asked) to path."""
    write_text(path, json.dumps(obj, **dump_kw) + ("\n" if newline else ""), mode=mode, dir_mode=dir_mode)
