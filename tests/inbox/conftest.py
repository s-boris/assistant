"""Hermetic fixtures for the event ledger: temp HOME (ASSISTANT_TEST=1 refuses the real one), fake agent-watch,
no network. Helpers live in inbox_testlib.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from inbox_testlib import AGENT_TABLE, KINDS, fake_bin  # noqa: E402


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:

    from core.paths import protected

    assert not protected(tmp_path), "tmp_path must lie outside the real home (ci_scratch() aside)"
    h = tmp_path / "home"
    for d in ("inbox/files", "workspace", "bin", ".config/assistant", ".local/state"):
        (h / d).mkdir(parents=True, exist_ok=True)
    for k in list(os.environ):
        if k.startswith(("ASSISTANT_", "EVENTS_", "HB_GATE_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("ASSISTANT_TZ", "Europe/Zurich")
    monkeypatch.setenv("ASSISTANT_KINDS", str(KINDS))
    monkeypatch.setenv("ASSISTANT_PROJECT_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("USAGE_TEST", "1")
    (tmp_path / "projects").mkdir()
    return h


@pytest.fixture
def fakes(home: Path) -> Path:
    (home / "aw.out").write_text(AGENT_TABLE, encoding="utf-8")
    fake_bin(home, "agent-watch", f'cat "{home}/aw.out"')
    return home
