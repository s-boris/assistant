"""`evals run --pack DIR` puts a candidate CLAUDE.md / FACTS-core.md / MEMORY.md over the materialized
setup (files present in DIR win, missing ones stay), and refuses an empty folder."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[2] / "scripts" / "evals"


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    for k, v in {
        "EVALS_NO_SUDO": "1",
        "EVALS_STATE": f"{tmp_path}/state",
        "EVALS_JOBS": f"{tmp_path}/jobs",
        "EVALS_REPORTS": f"{tmp_path}/reports",
        "EVALS_CORPUS": f"{tmp_path}/corpus",
        "USAGE": "off",
        "HOME": str(tmp_path / "home"),
    }.items():
        monkeypatch.setenv(k, v)
    ns: dict = {"__file__": str(EVALS), "__name__": "evals_under_test"}
    exec(compile(EVALS.read_text(encoding="utf-8"), str(EVALS), "exec"), ns)
    return ns


def test_pack_replaces_present_files_only(engine: dict, tmp_path: Path) -> None:
    setup = tmp_path / "setup"
    ws = setup / "workspace"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("full\n")
    (ws / "FACTS-core.md").write_text("core\n")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "CLAUDE.md").write_text("slim\n")
    (pack / "MEMORY.md").write_text("index\n")
    h = engine["apply_pack"](str(setup), str(pack))
    assert len(h) == 10
    assert (ws / "CLAUDE.md").read_text() == "slim\n"
    assert (ws / "FACTS-core.md").read_text() == "core\n"
    mem = setup / ".claude/projects/-home-node-workspace/memory/MEMORY.md"
    assert mem.read_text() == "index\n"
    assert engine["apply_pack"](str(setup), None) == ""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit):
        engine["apply_pack"](str(setup), str(empty))
    assert os.path.isfile(EVALS)
