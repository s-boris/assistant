"""CI runner move (29.10): the hub runner's uid gets a user (ciworker._user: ASSISTANT_REAL_HOME + nss_wrapper passwd
naming the job's HOME) and the CI driver builds the would-be ~/bin from the tree into that HOME (_runner_home), so the
legacy suites, the ratchet and inbox-lint run there without a brain file."""

from __future__ import annotations

import importlib.util
import os
import pwd
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tool(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
        mp.setenv("ASSISTANT_TEST", "1")
        spec = importlib.util.spec_from_file_location("assistant_cli_rhome", REPO / "ops" / "assistant.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _worker():
    spec = importlib.util.spec_from_file_location("ciworker_rhome", REPO / "packages" / "hub" / "ciworker.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_home_builds_the_would_be_bin_from_the_tree(tool, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASSISTANT_CI_TOOLCHAIN", str(tmp_path / "tc"))
    monkeypatch.setattr(tool.paths, "real_home", lambda: home)
    line = tool._runner_home(REPO)
    assert line.startswith("runner home: would-be ~/bin built from the tree")
    lib = home / "bin" / "lib"
    assert (lib / "core").is_symlink() and os.path.realpath(lib / "core") == str(REPO / "core")
    usage = (lib / "usage.py").read_bytes()  # an adopted helper, rendered with the managed header
    assert tool.HEADER.encode() in usage.split(b"\n", 3)[0] + usage.split(b"\n", 3)[1]
    assert (home / "bin" / "assistant").is_file() and os.access(home / "bin" / "assistant", os.X_OK)
    assert tool._runner_home(REPO) == ""  # built once per job


def test_runner_home_links_the_images_venvs(tool, tmp_path, monkeypatch):
    """The ci image's /opt/ci-heavy/<venv> (ops/hub/ci/Dockerfile) appear as ~/opt/<venv>, where the scripts look."""
    home, heavy = tmp_path / "home", tmp_path / "heavy"
    home.mkdir()
    for d in ("dive-venv", "piper-voices", "requirements"):
        (heavy / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASSISTANT_CI_TOOLCHAIN", str(tmp_path / "tc"))
    monkeypatch.setenv("ASSISTANT_CI_HEAVY", str(heavy))
    monkeypatch.setattr(tool.paths, "real_home", lambda: home)
    assert tool._runner_home(REPO).endswith(", ~/opt: 2 from the image)")
    assert sorted(p.name for p in (home / "opt").iterdir()) == ["dive-venv", "piper-voices"]
    assert os.path.realpath(home / "opt" / "dive-venv") == str(heavy / "dive-venv")


def test_runner_home_never_outside_a_runner_job(tool, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(tool.paths, "real_home", lambda: home)
    monkeypatch.delenv("ASSISTANT_CI_TOOLCHAIN", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert tool._runner_home(REPO) == ""  # not the runner
    monkeypatch.setenv("ASSISTANT_CI_TOOLCHAIN", str(tmp_path / "tc"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert tool._runner_home(REPO) == ""  # real home is not the job's HOME
    assert not (home / "bin").exists()


def test_worker_user_gives_the_uid_a_passwd_entry(tmp_path, monkeypatch):
    w = _worker()
    tc, work, home = tmp_path / "tc", tmp_path / "work", tmp_path / "work" / "home"
    (tc / "pg" / "lib").mkdir(parents=True)
    home.mkdir(parents=True)
    (tc / "pg" / "lib" / "libnss_wrapper.so").write_bytes(b"")

    def nobody(_uid):
        raise KeyError(_uid)

    monkeypatch.setattr(pwd, "getpwuid", nobody)
    env = w._user(str(tc), str(work), str(home))
    assert env["ASSISTANT_REAL_HOME"] == str(home)
    assert env["LD_PRELOAD"] == str(tc / "pg" / "lib" / "libnss_wrapper.so")
    entry = Path(env["NSS_WRAPPER_PASSWD"]).read_text().strip().split(":")
    assert entry[2] == str(os.getuid()) and entry[5] == str(home)
    assert Path(env["NSS_WRAPPER_GROUP"]).read_text().startswith("assistant-ci:x:")


def test_worker_user_leaves_a_real_user_alone(tmp_path):
    w = _worker()
    env = w._user(str(tmp_path), str(tmp_path / "work"), str(tmp_path / "home"))
    assert env == {"ASSISTANT_REAL_HOME": str(tmp_path / "home")}  # this host's uid has an entry: no preload


def test_probe_steps_only_for_probe_branches(tmp_path):
    w = _worker()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "runner-probe-steps.txt").write_text("# probe\nlegacy\nbun  # x\nruff\n")
    probe = w.skip_steps({"branch": "ci-probe-home"}, str(tmp_path)).split(",")
    assert "legacy" not in probe and "bun" not in probe and "secretscan" in probe and "sync" in probe
    assert w.skip_steps({"branch": "ci-runner-home"}, str(tmp_path)) == w.LOCAL_STEPS  # a land's job: as always
    assert w.skip_steps({"branch": "ci-probe-x"}, str(tmp_path / "none")) == w.LOCAL_STEPS
