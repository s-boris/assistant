"""Hermetic fixtures for packages/loop: a temp HOME (ASSISTANT_TEST=1 refuses the real one), a temp harness repo
with a main branch, fake bins (traces, events, product, evals, tool, memory, memory2) that answer from JSON files
and log their argv, a memory2 state (candidates.jsonl + mem.db with records/facts/review), and an eval corpus.
Helpers live in loop_testlib.py. All values are fake."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loop_testlib import CANDS, FAKE, SIG, git, make_mem_db  # noqa: E402


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from core.paths import protected

    assert not protected(tmp_path), "tmp_path must lie outside the real home (ci_scratch() aside)"
    h = tmp_path / "home"
    for d in ("inbox", "bin", "jobs", "opt/evals-private/cases", ".local/state/memory2"):
        (h / d).mkdir(parents=True)
    for k in list(os.environ):
        if k.startswith(("ASSISTANT_", "LOOP_", "MEMORY", "EVALS_", "SELF_REVIEW")):
            monkeypatch.delenv(k, raising=False)
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "sig.json").write_text(json.dumps(SIG))
    for name in ("traces", "events", "product", "evals", "tool", "memory", "memory2"):
        p = h / "bin" / name
        p.write_text(FAKE.format(py=sys.executable))
        p.chmod(0o755)
    repo = h / "opt" / "assistant"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
    corpus = h / "opt" / "evals-private"
    (corpus / "cases" / "10-core.yaml").write_text(
        'cases:\n  - id: case-a\n    area: home\n    tags: [core]\n    msg: "hi"\n    expect:\n      - {judge: "ok"}\n'
    )
    git(corpus, "init", "-q", "-b", "main")
    git(corpus, "add", "-A")
    git(corpus, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
    m2 = h / ".local" / "state" / "memory2"
    (m2 / "candidates.jsonl").write_text("".join(json.dumps(c) + "\n" for c in CANDS))
    make_mem_db(m2 / "mem.db")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-22T03:30:00+02:00")
    monkeypatch.setenv("ASSISTANT_TZ", "Europe/Zurich")
    monkeypatch.setenv("LOOP_FIXTURES", str(fx))
    monkeypatch.setenv("EVALS_CORPUS", str(corpus))
    return h
