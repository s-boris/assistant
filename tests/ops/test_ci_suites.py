"""The tool suites are pytest tests: `assistant ci` hands its suite selection to tests/test_suites.py (one test per
suite, id = its name, run through the driver's own suite runner) and reads their results back into the pytest
step's record, land's result file and main's record; there is no separate suite step any more."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

MANIFEST = """
[core]
src = "core"
dest = "bin/lib/core"

[[file]]
src = "scripts/demo"
dest = "bin/demo"
mode = "0755"
suite = "demo"

[[file]]
src = "scripts/redone"
dest = "bin/redone"
mode = "0755"
suite = "redone"
"""


def _tree(tmp: Path) -> Path:
    t = tmp / "tree"
    for d in ("ops/deploy", "packages/inbox", "scripts", "tests/legacy/demo", "tests/legacy/redone", ".venv-3.13/bin"):
        (t / d).mkdir(parents=True)
    (t / "ops" / "deploy" / "manifest.toml").write_text(MANIFEST)
    shutil.copy(REPO / "ops" / "assistant.py", t / "ops" / "assistant.py")
    shutil.copy(REPO / "tests" / "test_suites.py", t / "tests" / "test_suites.py")
    os.symlink(REPO / "core", t / "core")
    for n in ("demo", "redone"):
        (t / "scripts" / n).write_text(f"#!/bin/sh\necho {n}\n")
    (t / "tests" / "legacy" / "demo" / "run.sh").write_text('"$DEMO_BIN" | grep -q demo && echo "PASS demo"\n')
    (t / "tests" / "legacy" / "redone" / "run.sh").write_text(
        'echo "PASS one"\necho "FAIL - redone prints nothing useful"\necho "boom" >&2\nexit 1\n'
    )
    py = t / ".venv-3.13" / "bin" / "python"  # this interpreter (it has pytest), wherever the test runs
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    (t / ".gitignore").write_text(".venv-3.13/\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(
            ["git", "-C", str(t), "-c", "user.name=t", "-c", "user.email=t@t.invalid", *args],
            check=True,
            capture_output=True,
        )
    return t


def test_suites_run_as_pytest_tests_and_report_like_the_old_step(tool, tmp_path, monkeypatch, capsys):
    tree = _tree(tmp_path)
    out = tmp_path / "legacy-out.json"
    monkeypatch.setenv("ASSISTANT_CI_LEGACY_OUT", str(out))
    rep = tmp_path / "rep.json"
    rc = tool.ci(tree, None, True, set(tool.CI_STEPS) - {"suites"}, False, report=str(rep))
    printed = capsys.readouterr().out
    assert rc == 6
    assert "suites: all 2/2 (no base: everything); 2 still in shell form" in printed
    (step,) = json.loads(rep.read_text())["steps"]
    assert step["name"] == "pytest-3.13" and not step["ok"]
    assert step["failed"] == ["tests/test_suites.py::test_suite[redone]"]  # still red after its one retry
    assert step["rerun"]["ok"] is False
    assert "suites 2/2 ran, red: redone" in step["detail"]  # the retry's result replaced the first run's
    assert "boom" in step["tails"]["redone"] and step["suites"]["redone"][0] == 0
    assert step["selection"]["suites"]["mode"] == "all"
    got = json.loads(out.read_text())
    assert got["suites"]["demo"]["ok"] is True and got["suites"]["redone"]["ok"] is False
    assert got["selection"]["mode"] == "all"
    assert list(got["steps"]) == ["pytest-3.13"] and got["steps"]["pytest-3.13"] == step["secs"]  # the land's timeline


def test_the_failing_lines_are_the_tests_message_and_unset_means_skip(tmp_path):
    tree = _tree(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_CI_SUITES", "PYTEST_"))}
    env.update(HOME=str(tmp_path), ASSISTANT_TEST="1", ASSISTANT_NETGUARD="none")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_suites.py", "-rs"]
    r = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "2 skipped" in r.stdout, r.stdout[-800:]
    env.update(ASSISTANT_CI_SUITES="redone,nosuch", ASSISTANT_CI_SUITES_OUT=str(tmp_path / "o.json"))
    r = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout[-800:]
    assert "FAIL - redone prints nothing useful" in r.stdout and "no manifest entry has suite = 'nosuch'" in r.stdout
    assert "test_suite[demo]" not in r.stdout  # not selected: not even a test
    assert list(json.loads((tmp_path / "o.json").read_text())["suites"]) == ["redone"]


def test_suite_plan_splits_host_and_runner(tool, tmp_path, monkeypatch):
    tree = _tree(tmp_path)
    monkeypatch.setattr(tool, "host_suites", lambda _t, _rh=frozenset(): {"redone"})
    assert tool.suite_plan(tree, None, "host")[:2] == (["redone"], 1)
    assert tool.suite_plan(tree, None, "runner")[:2] == (["demo"], 1)
    names, other, sel = tool.suite_plan(tree, None)
    assert (names, other, sel["mode"]) == (["demo", "redone"], 0, "all")
    assert tool.shell_suites(tree) == ["demo", "redone"]
