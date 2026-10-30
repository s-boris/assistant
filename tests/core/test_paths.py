import os
from pathlib import Path

import pytest

from core import paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.delenv("ASSISTANT_REPO", raising=False)
    return h


def test_layout(home):
    assert paths.home() == home
    assert paths.state("events.db") == home / ".local/state/assistant/events.db"
    assert paths.config("agenda-off") == home / ".config/assistant/agenda-off"
    assert paths.inbox("x.md") == home / "inbox/x.md"
    assert paths.bin_dir("assistant") == home / "bin/assistant"
    assert paths.repo("core") == home / "opt/assistant/core"


def test_repo_override(home, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_REPO", str(tmp_path / "r"))
    assert paths.repo("ops") == tmp_path / "r/ops"


def test_test_mode_refuses_real_home(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("HOME", str(paths.real_home()))
    for fn in (paths.home, paths.state, paths.config, paths.inbox, paths.bin_dir):
        with pytest.raises(paths.RealHomeRefused):
            fn()
    with pytest.raises(paths.RealHomeRefused):
        paths.guard(paths.real_home() / "inbox")


def test_test_mode_refuses_symlink_into_real_home(home, tmp_path):
    link = tmp_path / "sneaky"
    os.symlink(paths.real_home(), link)
    with pytest.raises(paths.RealHomeRefused):
        paths.guard(link / "bin")


def test_test_mode_needs_home(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.delenv("HOME", raising=False)
    with pytest.raises(paths.RealHomeRefused):
        paths.home()


def test_live_mode_allows_real_home(monkeypatch):
    monkeypatch.delenv("ASSISTANT_TEST", raising=False)
    monkeypatch.setenv("HOME", str(paths.real_home()))
    assert paths.state() == paths.real_home() / ".local/state/assistant"
    monkeypatch.setenv("ASSISTANT_TEST", "0")
    assert not paths.test_mode()


def test_real_home_ignores_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.real_home() != tmp_path
    assert isinstance(paths.real_home(), Path)


def test_test_mode_allows_only_inside_ci_scratch(monkeypatch, tmp_path):
    """Local CI run dirs (pytest basetemp) live under ~/.cache/aci: allowed; the real home, the scratch root itself
    and a symlink out of the scratch into live state stay refused."""
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    sc = paths.ci_scratch()
    assert sc == paths.real_home() / ".cache/aci"
    assert paths.guard(sc / "assistant-ci.x/py3.13/bt/t0/home") == sc / "assistant-ci.x/py3.13/bt/t0/home"
    for bad in (sc, paths.real_home() / ".cache", paths.real_home() / ".cache/acix/home"):
        with pytest.raises(paths.RealHomeRefused):
            paths.guard(bad)
    assert not paths.protected(tmp_path) and paths.protected(paths.real_home() / "inbox")
    if sc.is_dir():  # the live box: a link planted inside the scratch dir must not reach live state
        d = sc / f"test-paths-{os.getpid()}"
        d.mkdir(exist_ok=True)
        try:
            os.symlink(paths.real_home() / "inbox", d / "out")
            with pytest.raises(paths.RealHomeRefused):
                paths.guard(d / "out" / "x")
        finally:
            (d / "out").unlink(missing_ok=True)
            d.rmdir()
