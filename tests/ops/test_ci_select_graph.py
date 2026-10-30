"""`assistant ci`'s impact selection against the real import graph: whatever module a land changes, the selection
holds every test file that imports it (directly, or through the test helpers and conftests it imports), or runs
everything. The importers are found here by a plain AST walk, independent of the selector's own index. Core modules,
the CI driver and the gated paths go by this graph (no blanket full run); a data file a module loads by its bare
file name (core/schedules.toml) reaches that module's importers; a pyproject.toml change in the lint tools' tables
only runs no extra test; a one-word stdlib import is never a repo file, and "assistant" alone (a state directory
word) is not the driver."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tool(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
        mp.setenv("ASSISTANT_TEST", "1")
        spec = importlib.util.spec_from_file_location("assistant_cli_selgraph", REPO / "ops" / "assistant.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True).stdout
    return [t for t in out.splitlines() if (REPO / t).is_file()]


def _imports(path: Path) -> set[str]:
    """Dotted names a Python file imports (absolute imports; `from a import b` gives a and a.b)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            out.add(n.module)
            out |= {f"{n.module}.{a.name}" for a in n.names}
    return out


def _module(rel: str) -> str:
    return rel[: -len(".py")].replace("/", ".").removeprefix("packages.")


def _importers(tracked: list[str], tests: list[str], rel: str) -> set[str]:
    """Test files that import `rel` (core.x or x, hubkit.y or packages.hubkit.y), directly or through a helper
    module / conftest under tests/ that does."""
    mod = _module(rel)
    names = {mod, "packages." + mod if not mod.startswith("core.") else mod}
    helpers = [t for t in tracked if t.startswith("tests/") and t.endswith(".py") and not t.startswith("tests/legacy/")]
    users = {h for h in helpers if _imports(REPO / h) & names}
    for _ in range(3):  # a helper that imports a user (tests/hubstore/hubfix.py style chains)
        more = {h for h in helpers if any(_module(u).split(".")[-1] in {i.split(".")[-1] for i in _imports(REPO / h)}
                                          for u in users if not u.rsplit("/", 1)[-1].startswith("test_"))}  # fmt: skip
        if more <= users:
            break
        users |= more
    out = {t for t in users if t in tests}
    for c in (u for u in users if u.endswith("/conftest.py")):  # a conftest's dir: every test below it
        out |= {t for t in tests if t.startswith(c.rsplit("/", 1)[0] + "/")}
    return out


def _modules(tracked: list[str]) -> list[str]:
    core = [t for t in tracked if t.startswith("core/") and t.endswith(".py") and t != "core/__init__.py"]
    pkgs = [t for t in tracked if t.startswith(("packages/memrows/", "packages/inbox/", "packages/loop/"))]
    return sorted(core) + ["ops/assistant.py"] + sorted(t for t in pkgs if t.endswith(".py"))[:4]


def test_every_importer_of_a_changed_module_is_selected(tool, tracked):
    tests = tool._test_files(REPO, tracked)
    misses: dict[str, list[str]] = {}
    impacted = 0
    for rel in _modules(tracked):
        r = tool.select_tests(REPO, [rel], tracked)
        if r["mode"] == "full":
            continue
        impacted += 1
        lost = sorted(_importers(tracked, tests, rel) - set(r["files"]))
        if lost:
            misses[rel] = lost[:5]
    assert not misses, misses
    assert impacted >= 5  # most core modules no longer run everything


def test_core_modules_and_the_driver_go_by_the_graph(tool, tracked):
    r = tool.select_tests(REPO, ["core/ciblock.py"], tracked)
    assert r["mode"] == "impacted", r
    assert len(r["files"]) < r["of"]
    r = tool.select_tests(REPO, ["ops/assistant.py"], tracked)
    assert r["mode"] == "impacted", r
    assert "tests/ops/test_ci_select.py" in r["files"] and "tests/ops/test_land.py" in r["files"]
    assert "tests/test_suites.py" in r["files"]  # it runs the suites through this driver
    assert "tests/inbox/test_writers.py" not in r["files"]  # "assistant" there is ~/.local/state/assistant
    assert tool.select_tests(REPO, ["core/__init__.py"], tracked)["mode"] == "full"


def test_a_core_data_file_reaches_its_loaders_importers(tool, tracked):
    tests = tool._test_files(REPO, tracked)
    r = tool.select_tests(REPO, ["core/schedules.toml"], tracked)
    if r["mode"] == "full":
        return
    assert _importers(tracked, tests, "core/schedules.py") <= set(r["files"])
    assert "tests/core/test_schedule_registry.py" in r["files"]


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()


def test_pyproject_lint_tables_only(tool, tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(d))
    _git(d, "config", "user.email", "t@example.invalid")
    _git(d, "config", "user.name", "t")
    base_text = '[project]\nname = "x"\ndependencies = ["a"]\n\n[tool.ruff]\nline-length = 100\n'
    (d / "pyproject.toml").write_text(base_text)
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    base = _git(d, "rev-parse", "HEAD")
    (d / "pyproject.toml").write_text(base_text.replace("100", "120") + "# a comment\n[tool.basedpyright]\nx = 1\n")
    assert tool._sel_lint_only(d, base)
    (d / "pyproject.toml").write_text(base_text + '\n[tool.pytest.ini_options]\naddopts = "-x"\n')
    assert not tool._sel_lint_only(d, base)
    (d / "pyproject.toml").write_text(base_text.replace('["a"]', '["a", "b"]'))
    assert not tool._sel_lint_only(d, base)
    assert not tool._sel_lint_only(d, None)
    (d / "pyproject.toml").write_text("not = [toml")
    assert not tool._sel_lint_only(d, base)
