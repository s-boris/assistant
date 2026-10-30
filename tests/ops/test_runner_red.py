"""The runner's red steps reach the land's refusal line; runner-skip.txt file lines hold for files named on the
pytest command line too (an impacted selection), not only for collected directories."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "packages"), str(ROOT)]

from ops import assistant as A  # noqa: E402


def test_runner_red_names_failing_tests_and_suites():
    rec = {
        "steps": [
            {"name": "ruff", "ok": True, "detail": "clean"},
            {
                "name": "pytest-3.13",
                "ok": False,
                "detail": "2 failed",
                "failed": [f"tests/x.py::t{i}" for i in range(7)],
            },
            {"name": "legacy", "ok": False, "detail": "1 red: foo", "tails": {"foo": "exit 1"}},
            {"name": "schemas", "ok": False, "detail": "bad.json: not a schema"},
        ]
    }
    red = A._runner_red(rec)
    assert red.startswith(
        "pytest-3.13: tests/x.py::t0, tests/x.py::t1, tests/x.py::t2, tests/x.py::t3, tests/x.py::t4 +2"
    )
    assert "; legacy: foo; schemas: bad.json: not a schema" in red
    assert A._runner_red({"steps": [{"name": "ruff", "ok": True}]}) == ""


def test_runner_skip_file_lines_hold_for_named_files(tmp_path):
    t = tmp_path / "t"
    (t / "tests" / "a").mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "conftest.py", t / "tests" / "conftest.py")
    (t / "tests" / "runner-skip.txt").write_text("tests/a/test_host.py\ntests/a/test_mixed.py::test_host\n")
    (t / "tests" / "a" / "test_host.py").write_text("def test_one():\n    pass\n")
    (t / "tests" / "a" / "test_mixed.py").write_text("def test_host():\n    pass\n\n\ndef test_ok():\n    pass\n")
    (t / "pytest.ini").write_text("[pytest]\n")
    # the runner finds pytest through PYTHONPATH (its toolchain): keep the env, minus the outer run's pytest vars
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(HOME=str(tmp_path), ASSISTANT_CI_RUNNER="1")
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests/a/test_host.py", "tests/a/test_mixed.py"]
    r = subprocess.run(cmd, cwd=t, env=env, capture_output=True, text=True, timeout=120)
    assert "1 passed, 2 deselected" in r.stdout, r.stdout + r.stderr
    r = subprocess.run(cmd, cwd=t, env={**env, "ASSISTANT_CI_RUNNER": "0"}, capture_output=True, text=True, timeout=120)
    assert "3 passed" in r.stdout, r.stdout + r.stderr


def test_a_subdir_conftest_holds_when_a_parent_file_comes_between(tmp_path):
    """`assistant ci` names the impacted files sorted, then the smoke set: a subdir's file after a file of the parent
    dir that followed one of its own lost the subdir's conftest fixtures (pytest 9.1). The root conftest sorts."""
    t = tmp_path / "t"
    (t / "tests" / "hub").mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "conftest.py", t / "tests" / "conftest.py")
    (t / "tests" / "hub" / "conftest.py").write_text("import pytest\n\n\n@pytest.fixture\ndef hubfx():\n    return 1\n")
    (t / "tests" / "hub" / "test_a.py").write_text("def test_a():\n    pass\n")
    (t / "tests" / "test_top.py").write_text("def test_top():\n    pass\n")
    (t / "tests" / "hub" / "test_b.py").write_text("def test_b(hubfx):\n    assert hubfx == 1\n")
    (t / "pytest.ini").write_text("[pytest]\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(HOME=str(tmp_path))
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests/hub/test_a.py", "tests/test_top.py",
           "tests/hub/test_b.py::test_b"]  # fmt: skip
    r = subprocess.run(cmd, cwd=t, env=env, capture_output=True, text=True, timeout=120)
    assert "3 passed" in r.stdout and "error" not in r.stdout, r.stdout + r.stderr
