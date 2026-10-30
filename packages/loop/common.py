"""loop.common: paths, the command runner, ids and state files shared by the loop modules. Stdlib only.

Paths follow $HOME (core.paths: ASSISTANT_TEST=1 refuses the real home). Overrides for tests:
LOOP_STATE (state dir), LOOP_BIN (the dir holding traces/product/evals/tool/events/memory/memory2),
MEMORY2_STATE (memory2's state: candidates.jsonl, mem.db), EVALS_CORPUS (the private eval corpus),
EVALS_STATE (evals' state: history.jsonl, runs/).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any

from core import clock, jsonstate, paths, todo

STATE_MODE = 0o700
FILE_MODE = 0o600


def state_dir() -> Path:
    v = os.environ.get("LOOP_STATE", "")
    return paths.guard(v) if v else paths.guard(paths.home() / ".local" / "state" / "loop")


def reflex_db() -> Path:
    """The reflex registry (SQLite). inbox.sources reads the same path for the agenda counter."""
    return state_dir() / "reflex.db"


def bin_path(name: str) -> str:
    d = os.environ.get("LOOP_BIN", "")
    return str(Path(d) / name) if d else str(paths.bin_dir(name))


def memory2_state() -> Path:
    v = os.environ.get("MEMORY2_STATE", "")
    return paths.guard(v) if v else paths.guard(paths.home() / ".local" / "state" / "memory2")


def corpus_dir() -> Path:
    v = os.environ.get("EVALS_CORPUS", "")
    return paths.guard(v) if v else paths.guard(paths.home() / "opt" / "evals-private")


def evals_state() -> Path:
    """The evals state dir (history.jsonl, runs/RUN/result.json); EVALS_STATE overrides it (tests)."""
    v = os.environ.get("EVALS_STATE", "")
    return paths.guard(v) if v else paths.guard(paths.home() / ".local" / "state" / "evals")


def jobs_dir() -> Path:
    return paths.guard(paths.home() / "jobs")


def tz() -> tzinfo:
    return todo.owner_tz()


def now() -> float:
    return clock.now()


def now_local() -> datetime:
    return datetime.fromtimestamp(now(), tz())


def today() -> date:
    return now_local().date()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(now() if ts is None else ts, tz()).isoformat(timespec="seconds")


def short_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:6]
    return f"{prefix}{h}"


def slug(s: str, n: int = 32) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return out[:n].rstrip("-") or "x"


def run(argv: list[str], timeout: float = 60, stdin: str | None = None) -> tuple[int, str, str]:
    """Run a command; (exit code, stdout, stderr). A missing binary or a timeout is exit 127 / 124, never a raise."""
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{os.path.basename(argv[0])}: timed out after {timeout:g} s"
    except OSError as e:
        return 126, "", f"{argv[0]}: {e}"
    return p.returncode, p.stdout, p.stderr


def run_json(argv: list[str], timeout: float = 60) -> tuple[Any, str | None]:
    """(parsed JSON, None) or (None, a one-line reason)."""
    rc, out, err = run(argv, timeout)
    if rc != 0 and not out.strip().startswith(("{", "[")):
        return None, f"{os.path.basename(argv[0])} exit {rc}: {(err.strip().splitlines() or [''])[-1][:160]}"
    try:
        return json.loads(out), None
    except ValueError:
        return None, f"{os.path.basename(argv[0])}: not JSON"


def load(name: str, default: Any) -> Any:
    return jsonstate.load(state_dir() / name, default)


def save(name: str, obj: Any) -> None:
    jsonstate.save(
        state_dir() / name,
        obj,
        newline=True,
        mode=FILE_MODE,
        dir_mode=STATE_MODE,
        indent=1,
        ensure_ascii=False,
        sort_keys=True,
    )


def lock_path() -> Path:
    return state_dir() / ".lock"


def fmt_num(x: float | int | None, unit: str = "") -> str:
    if x is None:
        return "?"
    if unit == "tokens" or (isinstance(x, (int, float)) and abs(x) >= 10_000):
        if abs(x) >= 1_000_000:
            return f"{x / 1_000_000:.2f}M"
        if abs(x) >= 1_000:
            return f"{x / 1_000:.0f}k"
    if unit == "s":
        return f"{x:.1f} s"
    if unit == "%":
        return f"{x:.1f} %"
    if isinstance(x, float):
        return f"{x:.2f}".rstrip("0").rstrip(".")
    return str(x)
