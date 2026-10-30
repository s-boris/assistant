"""Shared helpers for the event ledger tests (conftest.py puts this directory on sys.path; pytest runs with
--import-mode=importlib). Values are synthetic."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "packages"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

EVENTS = REPO / "scripts" / "events"
HOOK = REPO / "hooks" / "agenda.sh"
KINDS = REPO / "schemas" / "kinds.toml"
PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def write(path: Path, text: str, age: float = 60.0, now: float | None = None) -> Path:
    import time

    path.write_text(text, encoding="utf-8")
    t = (now if now is not None else time.time()) - age
    os.utime(path, (t, t))
    return path


def fake_bin(home: Path, name: str, script: str) -> Path:
    p = home / "bin" / name
    p.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
    p.chmod(0o755)
    return p


AGENT_TABLE = """agent-watch 2031-06-15 14:00 Zurich: subagents written in the last 2 h (stalled = no growth >15 min)
  ID                 STATE    RUN  SILENT  LAST         OWNER TAG  DESCRIPTION
  a1545e9bee6fea8aa  running  1m   0m      tool_use     -          T event ledger + agenda
  ac490683e1cb7d37e  stalled  1m   20m     tool_result  -          Old stalled one
  a410962194af56d37  stalled  1m   90m     tool_result  -          Long dead
  ae03da0f7f4e85cb2  done     0m   2m      final        -          Finished plan
"""


def run_events(
    *args: str, stdin: str | None = None, env: dict[str, str] | None = None, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PY, "-I", str(EVENTS), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
