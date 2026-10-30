"""The runner is the land's test run: `ci --host` runs only the host-bound part here: the smoke set
(untagged lines of tests/runner-skip.txt) and the host-bound legacy suites; the runner runs every
other legacy suite with the staged scripts' /usr/bin/python3 shebang pointed at its own interpreter."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tool(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
        mp.setenv("ASSISTANT_TEST", "1")
        spec = importlib.util.spec_from_file_location("assistant_cli_host", REPO / "ops" / "assistant.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, skip: str) -> Path:
    (tmp_path / "tests" / "a").mkdir(parents=True)
    for n in ("test_x.py", "test_y.py", "test_z.py"):
        (tmp_path / "tests" / "a" / n).write_text("def test_ok():\n    pass\n")
    (tmp_path / "tests" / "legacy" / "in-tree").mkdir(parents=True)
    (tmp_path / "tests" / "legacy" / "in-tree" / "run.sh").write_text("exit 0\n")
    (tmp_path / "tests" / "runner-skip.txt").write_text(skip)
    return tmp_path


def test_smoke_set_is_the_host_bound_lines(tool, tmp_path):
    tree = _tree(
        tmp_path,
        "# comment\ntests/a/test_x.py\ntests/a/test_y.py::test_ok\ntests/a/test_z.py @noexec\n"
        "tests/legacy/in-tree\ntests/a/gone.py\n",
    )
    sel = tool.smoke_tests(tree)
    assert sel["mode"] == "smoke" and sel["files"] == ["tests/a/test_x.py", "tests/a/test_y.py::test_ok"]


def test_host_suites_from_the_list_real_home_and_opt(tool, tmp_path, monkeypatch):
    tree = _tree(tmp_path, "tests/legacy/in-tree\ntests/legacy/tagged @noexec\n")
    monkeypatch.setattr(
        tool,
        "load_manifest",
        lambda _t: {"file": [{"suite": "in-tree"}, {"suite": "in-opt"}, {"suite": "tagged"}, {"src": "x"}]},
    )
    assert tool.host_suites(tree, {"rh-tests"}) == {"in-tree", "in-opt", "tagged", "rh"}  # tagged: no run.sh yet
    (tree / "tests" / "legacy" / "tagged").mkdir()
    (tree / "tests" / "legacy" / "tagged" / "run.sh").write_text("exit 0\n")
    assert tool.host_suites(tree) == {"in-tree", "in-opt"}


def test_reshebang_points_python3_scripts_at_this_interpreter(tool):
    assert tool._reshebang(b"#!/usr/bin/python3 -I\nprint(1)\n") == (
        b"#!" + sys.executable.encode() + b" -I\nprint(1)\n"
    )
    assert tool._reshebang(b"#!/bin/bash\necho\n") == b"#!/bin/bash\necho\n"


def test_empty_smoke_set_runs_no_tests(tool, tmp_path, monkeypatch):
    """Every test line gone from runner-skip.txt: `ci --host` runs no pytest at all, never the whole suite."""
    import json
    import subprocess

    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no deployed.json: no base
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    tree = tmp_path / "tree"
    (tree / "tests" / "a").mkdir(parents=True)
    (tree / "tests" / "a" / "test_x.py").write_text("def test_boom():\n    raise SystemExit(3)\n")
    (tree / "tests" / "runner-skip.txt").write_text("tests/legacy/gone\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(
            ["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@t.invalid", *args],
            check=True,
            capture_output=True,
        )
    rep = tmp_path / "rep.json"
    rc = tool.ci(tree, None, True, set(tool.CI_STEPS) - {"pytest"}, False, report=str(rep), host=True)
    steps = json.loads(rep.read_text())["steps"]
    assert rc == 0 and [(s["name"], s["ok"]) for s in steps] == [("pytest-3.13", True)]
    assert "no host-bound tests" in steps[0]["detail"]
