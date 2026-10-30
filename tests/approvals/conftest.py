"""Hermetic fixtures for the approvals library: a temp HOME (ASSISTANT_TEST=1 refuses the real one), a fake inbound
journal and access file (owner 111)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

OWNER = "111"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:

    from core.paths import protected

    assert not protected(tmp_path), "tmp_path must lie outside the real home (ci_scratch() aside)"
    h = tmp_path / "home"
    (h / ".local/state").mkdir(parents=True)
    for k in list(os.environ):
        if k.startswith(("ASSISTANT_", "APPROVALS_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    j = h / "journal.jsonl"
    j.write_text("")
    (h / "access.json").write_text('{"allowFrom": ["111", "222"]}')
    monkeypatch.setenv("APPROVALS_JOURNAL", str(j))
    monkeypatch.setenv("APPROVALS_ACCESS", str(h / "access.json"))
    return h
