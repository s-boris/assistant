"""Hermetic fixtures for memrows: a temp HOME (ASSISTANT_TEST=1 refuses the real one) and a secretscan stub
(exit 1 when the text holds the word PLANTEDSECRET)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:

    from core.paths import protected

    assert not protected(tmp_path), "tmp_path must lie outside the real home (ci_scratch() aside)"
    h = tmp_path / "home"
    (h / "workspace").mkdir(parents=True)
    (h / "bin").mkdir()
    for k in list(os.environ):
        if k.startswith(("ASSISTANT_", "MEMORY")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-05T12:00:00")
    stub = h / "bin" / "secretscan"
    stub.write_text("#!/bin/sh\ngrep -q PLANTEDSECRET && exit 1\nexit 0\n")
    stub.chmod(0o755)
    return h
