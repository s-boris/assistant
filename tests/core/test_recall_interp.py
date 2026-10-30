"""Scripts that run recall's search in-process (search_hits: FTS5 + the numpy semantic half) must use recall's own
interpreter. kb moved to python3.13 on 29.10 (no numpy there) and quietly fell back to text hits only."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _shebang(p: Path) -> list[str]:
    first = p.read_bytes().split(b"\n", 1)[0].decode()
    assert first.startswith("#!"), p
    return first[2:].split()


def _callers() -> list[Path]:
    """Every script that calls recall's search_hits() itself (kb today), recall excluded."""
    out = []
    for p in sorted(SCRIPTS.iterdir()):
        if p.name == "recall" or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if text.startswith("#!") and ".search_hits(" in text:
            out.append(p)
    return out


def test_kb_is_a_caller():
    assert SCRIPTS / "kb" in _callers()


@pytest.mark.parametrize("script", _callers(), ids=lambda p: p.name)
def test_caller_shares_recalls_interpreter(script):
    assert _shebang(script) == _shebang(SCRIPTS / "recall"), (
        f"{script.name} loads recall in-process: give it recall's shebang, or its semantic search is off"
    )


@pytest.mark.parametrize("script", _callers(), ids=lambda p: p.name)
def test_caller_interpreter_has_numpy(script):
    """On this host the interpreter must import numpy (python3-numpy, ~/boot.sh). The CI runner has no
    /usr/bin/python3 and runs these scripts with its own Python (numpy in the ci image): skipped there."""
    cmd = _shebang(script)
    if not os.path.exists(cmd[0]):
        pytest.skip(f"{cmd[0]} not on this machine (the CI runner reshebangs it)")
    p = subprocess.run([*cmd, "-c", "import numpy"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f"{cmd[0]} can't import numpy: {script.name}'s semantic search would be off\n{p.stderr}"
