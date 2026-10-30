"""botlib: the few helpers that several ~/bin scripts share word for word. Keep it small and boring.
Import:  sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "lib")); import botlib
Thin layer over the harness core (~/bin/lib/core, repo ~/opt/assistant): atomic_write / save_json /
load_json / write_inbox delegate to core.jsonstate and core.notes (atomic + fsync; a replaced file keeps its mode);
send_tg and lockdown_mode live here. Without an importable core (a copy outside ~/bin) the local fallback bodies run.
New code imports core directly. Tests: ~/opt/botlib-tests/run.sh (BOTLIB_PATH=/copy/botlib.py tests a copy)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_here = os.path.dirname(os.path.realpath(__file__))
for _d in (_here, os.path.join(_here, "lib")):  # ~/bin/lib/core next to this file; a staged overlay keeps it in lib/
    if os.path.isfile(os.path.join(_d, "core", "jsonstate.py")):
        if _d not in sys.path:
            sys.path.insert(0, _d)
        break
try:
    from core import jsonstate as _js
    from core import notes as _notes
except ImportError:  # no core: the pre-core bodies below
    _js = _notes = None
CORE = _js is not None


def atomic_write(path, text):
    """Write text through a hidden temp file + rename (readers never see half a file); creates the parent dir."""
    if _js is not None:
        _js.write_text(path, text)
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_json(path, default=None):
    """Parsed JSON file, or default when it is missing or unreadable."""
    if _js is not None:
        return _js.load(path, default)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, **dump_kw):
    atomic_write(path, json.dumps(obj, **dump_kw))


def write_inbox(inbox_dir, prefix, text):
    """New note <inbox_dir>/<prefix>-<epoch>[-n].md (never overwrites one); returns its Path."""
    if _notes is not None:
        return _notes.write_new(inbox_dir, prefix, text, now=time.time())
    t, n = int(time.time()), 0
    f = Path(inbox_dir) / f"{prefix}-{t}.md"
    while f.exists():
        n += 1
        f = Path(inbox_dir) / f"{prefix}-{t}-{n}.md"
    atomic_write(f, text)
    return f


def send_tg(cmd, text, timeout=60, soft=False):
    """Text the owner through the tg sender `cmd` -> (ok, stderr/stdout or error text; callers shorten it).
    soft=True: a non-urgent message (TG_SOFT=1 in the sender's env, as `tg --soft`: it waits out quiet hours and a
    sleep-in morning); else the env is inherited (plain `tg`: the alert class, out at once). argv stays [cmd, text]."""
    env = dict(os.environ, TG_SOFT="1") if soft else None
    try:
        p = subprocess.run(
            [cmd, text], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=env
        )
        return p.returncode == 0, (p.stderr or p.stdout or "").strip()
    except Exception as e:
        return False, str(e)


def lockdown_mode(unraid_ssh):
    """root: a full-root key -> the direct `bash -s` + COLLECTOR path. verbs/emulate: the owner's SSH
    lockdown (~/opt/unraid-lockdown): the same collector runs server-side as one allowlisted verb.
    ~/bin/unraid-ssh (`unraid_ssh`) decides (auto-detects the forced command)."""
    try:
        m = subprocess.run([unraid_ssh, "mode"], capture_output=True, text=True, timeout=90).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "root"
    return m if m in ("verbs", "emulate") else "root"
