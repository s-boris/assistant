#!/usr/bin/env python3
"""writeaudit.py LOG SCRIPT [ARGS...]: run a Python SCRIPT as __main__ under a write audit (hddheat tests).

Every write-side file operation the script makes (open/os.open for writing or creating, mkdir, rename/replace,
remove/unlink, rmdir, chmod, chown, utime, truncate, link, symlink, mkfifo/mknod) and every program it starts
(subprocess, os.exec*, os.posix_spawn*, os.spawn*, os.system) is appended to LOG as one tab-separated line:
  run <pid> <argv>         once per audited run (the suite counts them)
  w <pid> <event> <path>   path = realpath, so a symlink into the live dir counts as the live dir
  f <pid> <event> <fd N>   the same on an already open fd (os.fdopen, fchmod): its file was logged as w when opened
  x <pid> <event> <prog>   prog = the program as resolved on PATH
Caught by sys.addaudithook (PEP 578), so it sees the script and every library it imports, wherever the path came
from (env, HOME or a hard-coded literal) and without racing anything else that writes the same directory.
Bytecode writing is off, so imports add no lines. The audit itself writes only LOG (opened before the hook).
"""
import os
import runpy
import shutil
import sys

if len(sys.argv) < 3:
    sys.exit("usage: writeaudit.py LOG SCRIPT [ARGS...]")
log_path, script = sys.argv[1], sys.argv[2]
sys.argv = sys.argv[2:]
sys.path[0] = os.path.dirname(os.path.realpath(script))   # as `python3 SCRIPT` would set it
sys.dont_write_bytecode = True
_log = open(log_path, "a", buffering=1, encoding="utf-8")
_pid = str(os.getpid())
_busy = False
_WFLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_PATH_EVENTS = {"os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.utime",
                "os.truncate", "os.link", "os.symlink", "os.mkfifo", "os.mknod"}
_EXEC_EVENTS = {"subprocess.Popen", "os.exec", "os.posix_spawn", "os.spawn", "os.system"}


def _real(p):
    if isinstance(p, int):
        return f"<fd {p}>"
    try:
        return os.path.realpath(os.fsdecode(p))
    except (TypeError, ValueError):
        return repr(p)


def _w(event, p):
    if isinstance(p, int):
        _put("f", _pid, event, f"<fd {p}>")
    else:
        _put("w", _pid, event, _real(p))


def _put(*cols):
    _log.write("\t".join(str(c).replace("\t", " ").replace("\n", " ") for c in cols) + "\n")


def _prog(event, args):
    if event == "os.system":
        return "sh -c " + os.fsdecode(args[0])
    if event == "subprocess.Popen":
        exe, argv = args[0], args[1]
    elif event == "os.spawn":
        exe, argv = args[1], args[2]
    else:
        exe, argv = args[0], args[1]
    if exe is None:
        exe = argv if isinstance(argv, (str, bytes)) else list(argv)[0]
    exe = os.fsdecode(exe)
    return _real(shutil.which(exe) or exe) if os.sep not in exe else _real(exe)


def _hook(event, args):
    global _busy
    if _busy:
        return
    _busy = True
    try:
        if event == "open":
            path, mode, flags = args
            if isinstance(mode, str):
                writing = any(c in mode for c in "wax+")
            else:
                writing = isinstance(flags, int) and bool(flags & _WFLAGS)
            if writing and path is not None:
                _w("open", path)
        elif event in _PATH_EVENTS:
            _w(event, args[0])
            if event in ("os.rename", "os.link", "os.symlink"):
                _w(event, args[1])
        elif event in _EXEC_EVENTS:
            _put("x", _pid, event, _prog(event, args))
    except Exception as e:  # an audit bug must show up as a line, never hide a write
        _put("w", _pid, event, f"<audit error {e!r}>")
    finally:
        _busy = False


_put("run", _pid, " ".join(sys.argv))
sys.addaudithook(_hook)
runpy.run_path(script, run_name="__main__")
