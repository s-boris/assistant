"""context-size: the always-on parts (imports, the memory index's 200 lines, candidates), the budget exit
code, sections, the calibration through a fake `claude -p`, and the history row (counts only)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from ctxsize import cli


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:

    from core.paths import protected

    assert not protected(tmp_path), "tmp_path must lie outside the real home (ci_scratch() aside)"
    h = tmp_path / "home"
    (h / "workspace").mkdir(parents=True)
    (h / ".config/assistant").mkdir(parents=True)
    for k in list(os.environ):
        if k.startswith(("ASSISTANT_", "CONTEXT_SIZE_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("ASSISTANT_PROJECT_DIR", str(tmp_path / "proj"))
    managed = tmp_path / "managed.md"
    managed.write_text("m" * 100)
    monkeypatch.setenv("CONTEXT_SIZE_MANAGED", str(managed))
    ws = h / "workspace"
    ws.joinpath("CLAUDE.md").write_text("# How\n\n@FACTS-core.md\n\n## Talk\n" + "t" * 400 + "\n`@NOT.md` stays\n")
    ws.joinpath("FACTS-core.md").write_text(
        "# Facts\n## Done / declined ledger\n- 23.10 done: a\n- 22.10 declined: b\n"
    )
    mem = tmp_path / "proj" / "memory"
    mem.mkdir(parents=True)
    mem.joinpath("MEMORY.md").write_text("".join(f"- line {i}\n" for i in range(250)))
    return h


def run(capsys, *args) -> tuple[int, dict]:
    rc = cli.main([*args, "--json"])
    return rc, json.loads(capsys.readouterr().out)


def test_parts_and_imports(home, capsys):
    rc, d = run(capsys, "--budget", "100000")
    names = [p["name"] for p in d["parts"]]
    assert names == ["managed CLAUDE.md", "CLAUDE.md", "FACTS-core.md", "MEMORY.md"]
    assert rc == 0 and d["fixed_tokens"] == 47  # 100 chars / 2.15
    mem = d["parts"][-1]
    assert mem["note"] == "first 200 of 250 lines"
    assert d["total_tokens"] == sum(p["tokens"] for p in d["parts"])


def test_budget_exit_and_sections(home, capsys):
    rc, d = run(capsys, "--budget", "10", "--sections")
    assert rc == 1 and d["over_by"] > 0
    cm = next(p for p in d["parts"] if p["name"] == "CLAUDE.md")
    assert cm["sections"][0]["name"] == "Talk"
    fc = next(p for p in d["parts"] if p["name"] == "FACTS-core.md")
    assert fc["ledger_verbs"] == {"done": 1, "declined": 1}


def test_candidates_replace_parts(home, tmp_path, capsys):
    cand = tmp_path / "slim.md"
    cand.write_text("# Slim\n@FACTS-core.md\n")
    core = tmp_path / "core.md"
    core.write_text("x" * 43)
    rc, d = run(capsys, "--claude-md", str(cand), "--facts-core", str(core), "--budget", "100000")
    by = {p["name"]: p for p in d["parts"]}
    assert by["CLAUDE.md"]["note"] == "candidate" and by["CLAUDE.md"]["chars"] == len(cand.read_text())
    assert by["FACTS-core.md"]["chars"] == 43 and by["FACTS-core.md"]["note"] == "candidate"


def test_missing_part_exit_2(home, capsys):
    (home / "workspace" / "FACTS-core.md").unlink()
    rc, d = run(capsys, "--budget", "100000")
    assert rc == 2 and d["missing"] == ["FACTS-core.md"]


def test_calibrate_and_record(home, tmp_path, monkeypatch, capsys):
    fake = tmp_path / "claude"
    fake.write_text(
        '#!/bin/sh\nn=$(wc -c < CLAUDE.md)\necho "{\\"usage\\": {\\"input_tokens\\": $((1000 + n / 3)), '
        '\\"cache_read_input_tokens\\": 5}}"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CONTEXT_SIZE_CLAUDE", str(fake))
    monkeypatch.setenv("CONTEXT_SIZE_JOBS", str(tmp_path / "jobs"))
    assert cli.main(["calibrate"]) == 2  # needs --yes
    capsys.readouterr()
    assert cli.main(["calibrate", "--yes", "--json"]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert 2.9 < rec["chars_per_token"] < 3.1
    rc, d = run(capsys, "--budget", "100000", "--record")
    assert d["ratio"].startswith("calibrated") and d["chars_per_token"] == rec["chars_per_token"]
    row = json.loads((home / ".local/state/assistant/context-size.jsonl").read_text().splitlines()[-1])
    assert set(row) == {"at", "total", "cpt", "parts"}
