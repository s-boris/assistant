"""Hermetic fixtures for tg-adapter's Python side: a temp HOME (ASSISTANT_TEST=1 refuses the real one), stub
commands on PATH, a fake bun. Helpers live in tg_testlib.py."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tg_testlib import FAKE_BUN, kill_adapter, stub  # noqa: E402


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:

    from core.paths import protected

    assert not protected(tmp_path), "tmp_path must lie outside the real home (ci_scratch() aside)"
    h = tmp_path / "home"
    # the CI runner's jq (and git) live only in its toolchain's bin/: keep it on PATH, as the @needs_jq checks see it
    # (read before the ASSISTANT_* cleanup below drops ASSISTANT_CI_TOOLCHAIN)
    tc = os.environ.get("ASSISTANT_CI_TOOLCHAIN", "")
    tcbin = f"{tc}/bin:" if tc and os.path.isdir(f"{tc}/bin") else ""
    for d in ("inbox", "bin", "stub", ".config/assistant", ".local/state"):
        (h / d).mkdir(parents=True, exist_ok=True)
    for k in list(os.environ):
        if k.startswith(("ASSISTANT_", "TG_ADAPTER", "TG_SEND", "USAGE", "TELEGRAM_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("USAGE_TEST", "1")
    monkeypatch.setenv("PATH", f"{h / 'stub'}:{tcbin}/usr/local/bin:/usr/bin:/bin")
    stub(h, "tripclock", "echo Europe/Zurich")
    if not shutil.which("ps", path=os.environ["PATH"]):  # the hub CI runner has no procps: tg's caller lookup
        stub(h, "ps", '[ "$1 $2 $3" = "-o comm= -p" ] && exec cat "/proc/$4/comm"; exit 1')
    yield h
    kill_adapter(h)


@pytest.fixture
def fake_adapter(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An adapter dir with src/cli.ts and node_modules, run by the fake bun."""
    d = tmp_path / "adapter"
    (d / "src").mkdir(parents=True)
    (d / "src" / "cli.ts").write_text("// fake\n")
    (d / "node_modules").mkdir()
    fb = tmp_path / "fakebun"
    fb.write_text(FAKE_BUN)
    fb.chmod(0o755)
    (home / "fake-bun").mkdir()
    monkeypatch.setenv("TG_ADAPTER_DIR", str(d))
    monkeypatch.setenv("TG_ADAPTER_BUN", str(fb))
    return d
