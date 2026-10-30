"""The precheck ci step's privacy scan: `assistant-publish scan --no-identity` over the branch's new commits."""

import subprocess

import pytest

HIT = "  abcdef123456 core/x.py:3 scrub:Z…(7) #0123456789abcdef"
STUB = f"""#!/bin/sh
echo "$*" >> "$STUB_LOG"
[ "$STUB_RC" = 3 ] && {{ echo "  allowed abcdef123456 a.txt:1 deny:rule#1 #fedcba9876543210"; echo "{HIT}"; }}
[ "$STUB_RC" = 1 ] && echo "assistant-publish: error: gitleaks is missing" >&2
exit "$STUB_RC"
"""


@pytest.fixture
def setup(tool, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "new")
    stub = tmp_path / "assistant-publish"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("ASSISTANT_PUBLISH_BIN", str(stub))
    monkeypatch.setenv("STUB_LOG", str(log))
    return tool, repo, base, log


def test_clean_scan_passes_with_the_branch_range(setup, monkeypatch):
    tool, repo, base, log = setup
    monkeypatch.setenv("STUB_RC", "0")
    ok, detail = tool._ci_publish_scan(repo, base)
    assert ok and detail == "publish scan: 1 commit(s) clean"
    assert log.read_text() == f"scan --repo {repo} --rev {base}..HEAD --no-identity\n"


def test_a_hit_blocks_and_shows_labels_only(setup, monkeypatch):
    tool, repo, base, _ = setup
    monkeypatch.setenv("STUB_RC", "3")
    ok, detail = tool._ci_publish_scan(repo, base)
    assert not ok and "BLOCKS" in detail and HIT.strip() in detail and "allowed" not in detail


def test_a_scan_that_cannot_run_fails(setup, monkeypatch):
    tool, repo, base, _ = setup
    monkeypatch.setenv("STUB_RC", "1")
    ok, detail = tool._ci_publish_scan(repo, base)
    assert not ok and "could not run (exit 1)" in detail and "gitleaks is missing" in detail


def test_nothing_new_no_base_or_no_seam_skips(setup, monkeypatch):
    tool, repo, base, log = setup
    monkeypatch.setenv("STUB_RC", "3")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert tool._ci_publish_scan(repo, head) == (True, "publish scan: no new commits")
    assert tool._ci_publish_scan(repo, None) == (True, "publish scan: no base")
    monkeypatch.delenv("ASSISTANT_PUBLISH_BIN")
    assert tool._ci_publish_scan(repo, base) == (True, "publish scan: skipped (test)")
    assert not log.exists()
