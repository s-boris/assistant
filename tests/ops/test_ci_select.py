"""`assistant ci`'s test impact selection, one retry and quarantine (land speed, 28.10): the changed paths pick the
pytest files they can reach (imports + path references) plus a smoke set; shared core, CI config, gated paths and
the protected set run everything; the retry parses node ids; quarantine lines need by= and expires= and are
read from the base commit; the worker count follows the host load and the cgroup quota. Uses the real tree."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def _tool_once(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
        mp.setenv("ASSISTANT_TEST", "1")
        spec = importlib.util.spec_from_file_location("assistant_cli_sel", REPO / "ops" / "assistant.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tool(_tool_once, tmp_path, monkeypatch):
    """One module object for the file: its _sel_refs cache parses the real tree's ~1100 files once, not per test
    (97 s -> a few at load ~12: this file is in the smoke set, so it bounded every land's pytest step)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    return _tool_once


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True).stdout
    return [t for t in out.splitlines() if (REPO / t).is_file()]


def sel(tool, tracked, changed, base=None):
    return tool.select_tests(REPO, changed, tracked, base)


def test_doc_only_runs_just_the_smoke_set(tool, tracked):
    r = sel(tool, tracked, ["docs/land-speed-note.md"])
    assert r["mode"] == "impacted"
    assert set(r["files"]) == {s for s in tool.SELECT_SMOKE if s in tracked}
    assert r["of"] > 100


@pytest.mark.parametrize(
    "path",
    [
        "core/paths.py",  # shared core (by the import graph: nearly every test reaches it)
        "ops/ci/gated-paths.json",  # gate config (gated itself)
        "pyproject.toml",
        "uv.lock",
        "tests/conftest.py",  # the root one (a deeper conftest: its dir's tests)
        "tests/quarantine.txt",
        "core/watchjobs.py",  # gated: workflows
        "ops/lint/wf_determinism.py",  # lint code (its allowlists: their readers)
        "schemas/tools.toml",
        "packages/hub/hubverify.py",  # protected set
        "packages/hub/policy/egress.toml",
        "packages/tools/policy.cedar",
        "packages/hub/hubci.py",  # the runner's signer
        "skills/x/SKILL.md",  # gated: skills
        "packages/tools/domains/cat.py",  # gated: tools
    ],
)
def test_shared_ci_gated_and_protected_paths_run_everything(tool, tracked, path):
    r = sel(tool, tracked, ["docs/x.md", path])
    assert r["mode"] == "full", (path, r)


def test_no_base_or_forced_runs_everything(tool, tracked, monkeypatch):
    assert sel(tool, tracked, None)["mode"] == "full"
    monkeypatch.setenv("ASSISTANT_CI_SELECT", "full")
    assert sel(tool, tracked, ["docs/x.md"])["mode"] == "full"


def test_a_deeper_conftest_runs_the_tests_below_its_dir(tool, tracked):
    r = sel(tool, tracked, ["tests/tg/conftest.py"])
    assert r["mode"] == "impacted", r
    picked = set(r["files"])  # its dir's tests, and the tests elsewhere that import or name it
    assert {f for f in tracked if f.startswith("tests/tg/test_") and f.endswith(".py")} <= picked
    assert "tests/core/test_paths.py" not in set(r["files"]) - set(tool.SELECT_SMOKE) and r["of"] > len(picked)


def test_a_lint_data_file_runs_the_tests_naming_it(tool, tracked):
    """ops/lint/ast-allow.json: read by the driver's ast step (it runs on every land), so only the tests that name it;
    the lint code runs everything, and core's data stays whole (core/schedules.toml: core, and gated)."""
    r = sel(tool, tracked, ["ops/lint/ast-allow.json"])
    assert r["mode"] == "impacted", r
    assert r["of"] > 3 * len(r["files"])
    assert sel(tool, tracked, ["core/schedules.toml"])["mode"] == "full"


def test_a_manifest_change_runs_the_tests_of_the_entries_it_changes(tool, tracked, monkeypatch):
    import copy

    man = tool.load_manifest(REPO)
    base_man = copy.deepcopy({"file": man["file"], "core": man.get("core")})
    (e,) = [e for e in base_man["file"] if e["src"] == "scripts/tg-voice"]
    e["smoke"] = "true"  # the base's entry differs: tg-voice counts as changed
    monkeypatch.setattr(tool, "_leg_manifest_at", lambda tree, base: base_man)
    r = sel(tool, tracked, ["ops/deploy/manifest.toml"], base="HEAD")
    assert r["mode"] == "impacted", r
    assert {"tests/ops/test_manifest_smokes.py", "tests/tg/test_senders.py"} <= set(r["files"])
    assert sel(tool, tracked, ["ops/deploy/manifest.toml"])["mode"] == "full"  # no base to diff against
    base_man["core"] = {**(base_man["core"] or {}), "dest": "bin/lib/core-old"}
    assert sel(tool, tracked, ["ops/deploy/manifest.toml"], base="HEAD")["mode"] == "full"  # [core] changed
    monkeypatch.setattr(tool, "_leg_manifest_at", lambda tree, base: None)
    assert sel(tool, tracked, ["ops/deploy/manifest.toml"], base="HEAD")["mode"] == "full"  # unreadable at base


def test_a_runner_skip_change_runs_the_tests_its_lines_name(tool, tracked, monkeypatch):
    lines = ["tests/tg/test_tg_flags.py::test_x @noexec  # why", "tests/legacy/tts  # a suite: legacy_select's", "# x"]
    monkeypatch.setattr(tool, "_sel_skip_lines", lambda tree, base: lines)
    r = sel(tool, tracked, ["tests/runner-skip.txt"], base="HEAD")
    assert r["mode"] == "impacted", r
    assert "tests/tg/test_tg_flags.py" in r["files"] and r["of"] > 2 * len(r["files"])
    assert sel(tool, tracked, ["tests/runner-skip.txt"])["mode"] == "full"  # no base: no diff to read
    lines.append("docs")  # a line naming something that is no test file
    assert sel(tool, tracked, ["tests/runner-skip.txt"], base="HEAD")["mode"] == "full"


def test_a_package_module_reaches_its_importers_and_path_users(tool, tracked):
    r = sel(tool, tracked, ["packages/memrows/store.py"])
    assert r["mode"] == "impacted"
    importers = [t for t in tracked if t.startswith("tests/memrows/test_") and "memrows" in (REPO / t).read_text()]
    assert importers and set(importers) <= set(r["files"])
    assert "tests/core/test_clock.py" not in r["files"]
    assert len(r["files"]) < r["of"] / 2
    assert sel(tool, tracked, ["packages/hubkit/feed.py"])["mode"] == "full"  # most of the suite imports hubkit


def test_a_test_helper_reaches_every_test_using_it(tool, tracked):
    r = sel(tool, tracked, ["tests/hubstore/hubfix.py"])
    users = [t for t in tracked if t.startswith("tests/hubstore/test_") and "hubfix" in (REPO / t).read_text()]
    assert users and set(users) <= set(r["files"]) or r["mode"] == "full"


def test_a_changed_test_file_runs_itself(tool, tracked):
    r = sel(tool, tracked, ["tests/core/test_clock.py"])
    assert "tests/core/test_clock.py" in r["files"]


def test_a_deleted_module_still_reaches_its_importers(tool, tracked):
    keep = [t for t in tracked if t != "packages/memrows/store.py"]
    r = tool.select_tests(REPO, ["packages/memrows/store.py"], keep)
    assert "tests/memrows/test_memrows.py" in r["files"]


def test_a_managed_script_reaches_the_tests_that_run_it(tool, tracked):
    r = sel(tool, tracked, ["scripts/voice-hub"])
    assert r["mode"] == "impacted" and "tests/hub/test_voice_hub.py" in r["files"]
    assert len(r["files"]) < 20


def test_selection_keys_drop_generic_names(tool):
    keys = tool._sel_keys("packages/hubkit/feed.py")
    assert {"feed", "feed.py", "hubkit/feed.py", "hubkit.feed", "packages/hubkit"} <= keys
    assert "packages" not in keys and "py" not in keys and "hubkit" not in keys
    assert "run" not in tool._sel_keys("tests/legacy/x/run.sh")


# ---------------------------------------------------------------- retry + quarantine


def test_node_ids_from_failure_lines(tool):
    lines = [
        "FAILED tests/a/test_x.py::test_one[p-1] - AssertionError: x - y",
        "ERROR tests/b/test_y.py - ImportError",
        "FAILED tests/a/test_x.py::test_one[p-1] - again",
        "FAILED nonsense",
    ]
    assert tool._node_ids(lines) == ["tests/a/test_x.py::test_one[p-1]", "tests/b/test_y.py"]
    assert tool._pytest_count("2 failed, 10 passed, 3 skipped in 1.2s") == 12
    assert tool._pytest_count("no tests ran in 0.1s") is None


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()


def test_quarantine_needs_by_and_expiry_and_comes_from_base(tool, tmp_path):
    d = tmp_path / "r"
    (d / "tests").mkdir(parents=True)
    _git(d.parent, "init", "-q", "-b", "main", str(d))
    _git(d, "config", "user.email", "t@example.invalid")
    _git(d, "config", "user.name", "t")
    q = d / "tests" / "quarantine.txt"
    q.write_text(
        "# header\n"
        "tests/a/test_x.py::test_ok by=main-session expires=2026-11-10  # timing under load\n"
        "tests/a/test_x.py::test_old by=me expires=2026-10-01\n"
        "tests/a/test_x.py::test_far by=me expires=2027-06-01\n"
        "tests/a/test_x.py::test_noby expires=2026-11-01\n"
    )
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "q")
    base = _git(d, "rev-parse", "HEAD")
    q.write_text("tests/a/test_x.py::test_branch by=me expires=2026-11-01\n")  # the branch's own line
    live, bad = tool.quarantine(d, base, today="2026-10-28")
    assert set(live) == {"tests/a/test_x.py::test_ok"} and live["tests/a/test_x.py::test_ok"]["by"] == "main-session"
    assert len(bad) == 3 and any("expired" in b for b in bad) and any("days out" in b for b in bad)
    assert tool._quarantined("tests/a/test_x.py::test_ok", live)
    assert not tool._quarantined("tests/a/test_x.py::test_branch", live)
    live2, _ = tool.quarantine(d, None, today="2026-10-28")  # no base: the tree's own file
    assert set(live2) == {"tests/a/test_x.py::test_branch"}


def test_repo_quarantine_file_is_valid(tool):
    live, bad = tool.quarantine(REPO, None)
    assert not [b for b in bad if "needs by=" in b], bad


# ---------------------------------------------------------------- workers + land row


def test_workers_follow_override_files_and_load(tool, monkeypatch):
    monkeypatch.setenv("ASSISTANT_CI_PYTEST_WORKERS", "3")
    assert tool.pytest_workers(100, 2, False) == 3
    assert tool.pytest_workers(1, 2, True) == 3  # set = taken as it is (no file or quota cap)
    monkeypatch.setenv("ASSISTANT_CI_PYTEST_WORKERS", "1")
    assert tool.pytest_workers(100, 2, False) == 0
    monkeypatch.delenv("ASSISTANT_CI_PYTEST_WORKERS")
    monkeypatch.setattr(tool.os, "getloadavg", lambda: (0.0, 0, 0))
    monkeypatch.setattr(tool, "_host_cpus", lambda: 32.0)
    monkeypatch.setattr(tool, "_mem_cap", lambda: None)
    assert tool.pytest_workers(2, 2, False) == 2
    assert tool.pytest_workers(1, 2, False) == 0  # one file: serial
    monkeypatch.setattr(tool, "_host_cpus", lambda: 32.0)
    monkeypatch.setattr(tool, "_mem_cap", lambda: None)
    monkeypatch.setattr(tool.os, "getloadavg", lambda: (40.0, 0, 0))
    assert tool.pytest_workers(100, 2, False) == 2  # overloaded host: the floor
    monkeypatch.setattr(tool.os, "getloadavg", lambda: (4.0, 0, 0))
    assert tool.pytest_workers(100, 2, False) == tool.PYTEST_WORKERS
    monkeypatch.setattr(tool, "_host_cpus", lambda: 4.0)
    monkeypatch.setattr(tool, "_mem_cap", lambda: 5)
    assert tool.pytest_workers(100, 1, True) == 4  # the runner: its quota
    monkeypatch.setattr(tool, "_mem_cap", lambda: 3)
    assert tool.pytest_workers(100, 1, True) == 3  # ... and its memory


def test_land_row_tests_record(tool):
    loc = {
        "pytest-3.11": {"selection": {"mode": "impacted", "files": 5, "of": 140, "tests": 40}},
        "pytest-3.13": {
            "selection": {"mode": "impacted", "files": 5, "of": 140, "tests": 40},
            "rerun": {"ids": ["tests/a.py::t"], "ok": True, "still": []},
        },
    }
    run = {"state": "done", "selection": {"mode": "full", "files": 140, "of": 140, "tests": 820}}
    r = tool._land_tests(loc, run)
    assert r["selection"] == "full" and r["tests"]["runner"]["tests"] == 820
    assert r["rerun"] == {"local pytest-3.13": loc["pytest-3.13"]["rerun"]}
    assert tool._land_tests(loc, {"state": "done"})["selection"] == "impacted"
    assert tool._land_tests(None, {"state": "none"}) == {"selection": "stamp", "tests": {}}
