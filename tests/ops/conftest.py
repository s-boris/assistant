"""Fixtures for the assistant CLI: a temp HOME, a fake harness repo (git) and a runner for ops/assistant.py."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "ops" / "assistant.py"

MANIFEST = """
[core]
src = "core"
dest = "bin/lib/core"
smoke = "python3 -I -c \\"import sys; sys.path.insert(0, sys.argv[1]); import core.paths, core.clock\\" {lib}"

[[file]]
src = "tools/hello.py"
dest = "bin/hello"
mode = "0755"
smoke = "{dest}"

[[file]]
src = "tools/a.txt"
dest = "bin/a.txt"
mode = "0644"
header = false

[[file]]
src = "tools/b.txt"
dest = "bin/b.txt"
mode = "0644"
header = false
"""


def load_tool():
    spec = importlib.util.spec_from_file_location("assistant_cli", TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.repo = tmp / "repo"
        self.home.mkdir()
        self.brain_log = tmp / "brain.log"
        stub = tmp / "brain-commit"
        stub.write_text(f'#!/bin/sh\necho "$1" >> {self.brain_log}\n')
        stub.chmod(0o755)
        self.cron_log = tmp / "cron.log"
        cstub = tmp / "crontab-stub"
        cstub.write_text(f'#!/bin/sh\necho "$1" >> {self.cron_log}\nexit ${{CRONTAB_STUB_RC:-0}}\n')
        cstub.chmod(0o755)
        self.env = dict(os.environ)
        for k in ("ASSISTANT_NOW", "VIRTUAL_ENV", "ASSISTANT_CI_SELECT"):
            self.env.pop(k, None)
        self.env.update(
            {
                "HOME": str(self.home),
                "ASSISTANT_TEST": "1",
                "ASSISTANT_LAND_LOCAL_CI": "1",  # the full local CI (the old flow); runner-as-the-test tests set 0
                "ASSISTANT_REPO": str(self.repo),
                "ASSISTANT_BRAIN_COMMIT_BIN": str(stub),
                "ASSISTANT_CRONTAB_BIN": str(cstub),
                "ASSISTANT_LOCK_WAIT": "120",
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            }
        )

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", "-C", str(cwd or self.repo), *args], env=self.env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def make_repo(self):
        (self.repo / "tools").mkdir(parents=True)
        shutil.copytree(REPO / "core", self.repo / "core", ignore=shutil.ignore_patterns("__pycache__"))
        (self.repo / "ops" / "deploy").mkdir(parents=True)
        (self.repo / "ops" / "deploy" / "manifest.toml").write_text(MANIFEST)
        self.write("tools/hello.py", "#!/bin/sh\necho hello v1\n")
        self.write("tools/a.txt", "a1\n")
        self.write("tools/b.txt", "b1\n")
        self.git("init", "-q", "-b", "main")
        self.commit("v1")

    def write(self, rel, text, cwd=None):
        p = (cwd or self.repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg, cwd=None):
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "-q", "-m", msg, cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd)

    def run(self, *args, extra=None, timeout=120):
        env = dict(self.env, **(extra or {}))
        return subprocess.run(
            [sys.executable, "-I", str(TOOL), *args], env=env, capture_output=True, text=True, timeout=timeout
        )

    def popen(self, *args, extra=None):
        env = dict(self.env, **(extra or {}))
        return subprocess.Popen(
            [sys.executable, "-I", str(TOOL), *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

    def state(self, *p):
        return self.home.joinpath(".local/state/assistant", *p)

    def deployed(self):
        return json.loads(self.state("deployed.json").read_text())

    def history(self):
        p = self.state("deploys.jsonl")
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture(autouse=True)
def _no_select_override(monkeypatch):
    """ci-nightly and `ci --full` run pytest with ASSISTANT_CI_SELECT=full in its env: the tests here check the
    selection (pytest and legacy) and the land flow themselves, so the outer run's override stays out."""
    monkeypatch.delenv("ASSISTANT_CI_SELECT", raising=False)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.make_repo()
    return e


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    return load_tool()
