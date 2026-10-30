"""Diff-only precheck: `assistant ci` hands codebase-sync precheck only the would-be ~/bin files a branch changes
(CODEBASE_SYNC_ONLY); no such file = no codebase-sync run at all; no base or a manifest change = the whole home."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CS = ROOT / "scripts" / "codebase-sync"
sys.path[:0] = [str(ROOT / "packages"), str(ROOT)]

from ops import assistant as A  # noqa: E402

EMAIL = "jane.owner@" + "mailbox.ch"  # assembled at runtime: this file stays clean for the scans


def _manifest_tree(tmp: Path) -> Path:
    t = tmp / "tree"
    (t / "ops" / "deploy").mkdir(parents=True)
    (t / "scripts").mkdir()
    (t / "ops" / "deploy" / "manifest.toml").write_text(
        "\n".join(
            f'[[file]]\nsrc = "{src}"\ndest = "{dest}"\nmode = "0755"\nheader = false\n'
            for src, dest in (("scripts/a", "bin/a"), ("scripts/b", "bin/b"), ("skills/x.md", ".claude/skills/x.md"))
        ),
        encoding="utf-8",
    )
    return t


def test_precheck_only_maps_changed_sources_to_bin_dests(tmp_path, monkeypatch):
    t = _manifest_tree(tmp_path)
    monkeypatch.setattr(
        A,
        "load_manifest",
        lambda root: {"file": A.tomllib.loads((root / "ops/deploy/manifest.toml").read_text())["file"]},
    )
    assert A._precheck_only(t, None) is None  # no base: the whole home
    assert A._precheck_only(t, ["ops/deploy/manifest.toml", "scripts/a"]) is None  # renders/dests may move
    assert A._precheck_only(t, ["scripts/b", "README.md", "skills/x.md"]) == ["bin/b"]
    assert A._precheck_only(t, ["tests/ops/test_x.py"]) == []
    assert A._precheck_only(t, ["scripts/codebase-sync", "scripts/b"]) is None  # the precheck's own tool


def test_ci_precheck_without_bin_changes_runs_only_the_publish_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_precheck_only", lambda tree, changed: [])
    monkeypatch.setattr(A, "_ci_publish_scan", lambda tree, base: (True, "publish scan: 1 commit(s) clean"))
    started = A._ci_precheck_start(tmp_path, ["tests/x.py"])
    assert started[0] is None and started[2] == A.PRECHECK_NONE
    name, ok, detail = A._ci_precheck(tmp_path, "base", started)
    assert (name, ok) == ("precheck", True) and detail.startswith(A.PRECHECK_NONE)


def _cs(tmp: Path, *args: str, only: list[str] | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp / "home"),
        "CODEBASE_SYNC_HOME": str(tmp / "home"),
        "CODEBASE_SYNC_EXPORT": str(tmp / "export"),
        "CODEBASE_SYNC_SCRUB": str(tmp / "scrub.txt"),
        "CODEBASE_SYNC_INBOX": str(tmp / "inbox"),
        "CODEBASE_SYNC_STATE": str(tmp / "state"),
        "CODEBASE_SYNC_GATE_CONF": str(tmp / "no-gate.conf"),
        "CODEBASE_SYNC_MODE": "report",
        "SECRETSCAN_SOURCES": str(tmp / "home"),
    }
    if only is not None:
        (tmp / "only.txt").write_text("".join(f"{tmp / 'home' / p}\n" for p in only), encoding="utf-8")
        env["CODEBASE_SYNC_ONLY"] = str(tmp / "only.txt")
    b = tmp / "csbin"  # codebase-sync imports secretscan from its own dir, as deployed (bin/secretscan.py)
    if not b.is_dir():
        b.mkdir()
        shutil.copy(CS, b / "codebase-sync")
        shutil.copy(ROOT / "scripts" / "secretscan_py", b / "secretscan.py")
    return subprocess.run(
        [sys.executable, "-I", str(b / "codebase-sync"), *args], env=env, capture_output=True, text=True, timeout=120
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "bin").mkdir(parents=True)
    (h / "bin" / "clean").write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
    (h / "bin" / "leak").write_text(f"#!/bin/sh\necho {EMAIL}\n", encoding="utf-8")
    (tmp_path / "scrub.txt").write_text("Zorroname\tNAME\n", encoding="utf-8")
    return tmp_path


def test_codebase_sync_precheck_only_checks_the_listed_files(home):
    whole = _cs(home, "precheck")
    assert whole.returncode == 3 and "bin/leak:2 pii:email" in whole.stdout
    clean = _cs(home, "precheck", only=["bin/clean"])
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "clean (1 files" in clean.stdout
    leak = _cs(home, "precheck", only=["bin/leak"])
    assert leak.returncode == 3 and "bin/leak:2" in leak.stdout


def test_codebase_sync_only_never_narrows_a_real_check(home):
    # CODEBASE_SYNC_ONLY counts for precheck only: `check` (like the sync itself) still sees the whole home
    r = _cs(home, "check", only=["bin/clean"])
    assert r.returncode == 3, r.stdout + r.stderr
