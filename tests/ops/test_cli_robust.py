"""CLI robustness: git conflict markers in the parsed config files and a stdout reader that goes away early."""

import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "ops" / "assistant.py"
ALL_STEPS = "sync,ruff,pyright,pytest,suites,ast,schemas,ratchet,inbox-lint,secretscan,precheck,bun"

CONFLICTED = """[[file]]
src = "tools/hello.py"
dest = "bin/hello"
<<<<<<< HEAD
smoke = "{dest}"
=======
smoke = "{dest} --check"
>>>>>>> feature
"""


def run_closed_stdout(env, *args):
    """Run the CLI with stdout on a pipe whose reader is already gone (every write fails with EPIPE)."""
    r, w = os.pipe()
    os.close(r)
    try:
        return subprocess.run(
            [sys.executable, "-I", str(TOOL), *args],
            env=env.env,
            stdout=w,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    finally:
        os.close(w)


def test_conflict_lines(tool, tmp_path):
    p = tmp_path / "m.toml"
    p.write_text("a = 1\n<<<<<<< HEAD\nb = 2\n=======\n||||||| base\nb = 3\n>>>>>>> wp9\nc = '======='\n========\n")
    assert tool.conflict_lines(p) == [2, 4, 5, 7]
    assert tool.conflict_lines(tmp_path / "missing.toml") == []


def test_ci_refuses_conflicted_manifest(env):
    env.write("ops/deploy/manifest.toml", CONFLICTED)
    env.commit("conflicted")
    r = env.run("ci", "--path", str(env.repo), "--skip", ALL_STEPS, "--no-stamp")
    assert r.returncode == 2, r.stderr
    assert "Traceback" not in r.stderr
    assert "manifest.toml:4" in r.stderr and "manifest.toml:6" in r.stderr and "manifest.toml:8" in r.stderr


def test_ci_refuses_conflicted_lint_json(env):
    env.write("ops/lint/bin-baseline.json", '{\n<<<<<<< HEAD\n"counts": {}\n=======\n>>>>>>> wp9\n}\n')
    env.commit("conflicted baseline")
    r = env.run("ci", "--path", str(env.repo), "--skip", ALL_STEPS, "--no-stamp")
    assert r.returncode == 2 and "Traceback" not in r.stderr
    assert "bin-baseline.json:2" in r.stderr


def test_deploy_refuses_conflicted_manifest(env):
    env.write("ops/deploy/manifest.toml", CONFLICTED)
    env.commit("conflicted")
    r = env.run("deploy")
    assert r.returncode == 2 and "Traceback" not in r.stderr
    assert "manifest.toml:4" in r.stderr
    assert not (env.home / "bin/hello").exists()


def test_invalid_toml_is_a_clear_error(env):
    env.write("ops/deploy/manifest.toml", "[[file]\nsrc = 1\n")
    env.commit("broken toml")
    r = env.run("deploy")
    assert r.returncode == 1 and "Traceback" not in r.stderr
    assert "manifest.toml: invalid TOML" in r.stderr


def test_status_with_closed_stdout(env):
    assert env.run("deploy").returncode == 0
    r = run_closed_stdout(env, "status")
    assert r.returncode == 0 and r.stderr == ""


def test_help_and_drift_with_closed_stdout(env):
    r = run_closed_stdout(env, "--help")
    assert r.returncode == 0 and r.stderr == ""
    assert env.run("deploy").returncode == 0
    (env.home / "bin/a.txt").write_text("edited live\n")
    r = run_closed_stdout(env, "drift")
    assert r.returncode == 3 and r.stderr == ""  # the command's own exit code stands


def test_deploy_finishes_with_closed_stdout(env):
    r = run_closed_stdout(env, "deploy")
    assert r.returncode == 0 and r.stderr == ""
    assert (env.home / "bin/hello").is_file() and env.deployed()["release"]
    assert env.history()[-1]["ok"]
