"""`assistant deploy|rollback --target hub`: stage + plan when the hub is unreachable, --plan changes nothing,
live when /health runs the new release, CURRENT restored (exit 4) when it does not, rollback to the previous one."""

import json
import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "ops" / "assistant.py"


def run(repo: Path, *args: str):
    env = dict(os.environ, ASSISTANT_REPO=str(repo), ASSISTANT_LOCK_WAIT="30")
    return subprocess.run(
        [sys.executable, "-I", str(TOOL), *args], env=env, capture_output=True, text=True, timeout=120
    )


def rel(home: Path) -> Path:
    return home / ".local/share/claude-hub/releases"


def current(home: Path) -> str | None:
    try:
        return (rel(home) / "CURRENT").read_text().split()[0]
    except OSError:
        return None


def hist(home: Path) -> list[dict]:
    p = home / ".local/state/assistant/deploys.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_unreachable_stages_and_prints_plan(hubhome, hubrepo):
    repo, _ = hubrepo
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    r = run(repo, "deploy", "--target", "hub", "--plan")
    assert r.returncode == 0, r.stderr
    assert "nothing changed" in r.stdout and "unreachable" in r.stdout and current(hubhome) is None
    assert not (hubhome / ".local/share/claude-hub/kit").exists()
    r = run(repo, "deploy", "--target", "hub", "--wait", "1")
    assert r.returncode == 0, r.stderr
    assert current(hubhome) == sha[:12] and "staged as CURRENT" in r.stdout and "hub kit paste install" in r.stdout
    assert (hubhome / ".local/share/claude-hub/kit/install.sh").is_file()
    h = hist(hubhome)[-1]
    assert h["target"] == "hub" and h["staged"] is True and h["ok"] is True
    # bin-only flags stay apart
    assert run(repo, "deploy", "--plan").returncode == 2
    assert run(repo, "deploy", "--target", "hub", "--adopt", "bin/x").returncode == 2


def test_live_then_failed_then_rollback(hubhome, hubrepo, fakehub):
    repo, commit = hubrepo
    r = run(repo, "deploy", "--target", "hub", "--wait", "10")
    assert r.returncode == 0, r.stderr + r.stdout
    first = current(hubhome)
    assert first and "live" in r.stdout
    # v2 that never comes up (the hub keeps running v1): restored, exit 4
    (repo / "packages/hub/hubd.py").write_text((repo / "packages/hub/hubd.py").read_text() + "# v2\n")
    v2 = commit("v2")[:12]
    fakehub.mode, fakehub.fixed = "stuck", first
    r = run(repo, "deploy", "--target", "hub", "--wait", "2")
    assert r.returncode == 4 and "CURRENT restored" in r.stderr, r.stderr
    assert current(hubhome) == first and hist(hubhome)[-1]["ok"] is False
    # the loader marks v2's attempt bad: fails at once, not after --wait
    fakehub.mode = "bad"
    r = run(repo, "deploy", "--target", "hub", "--wait", "60")
    assert r.returncode == 4 and "marked bad" in r.stderr and current(hubhome) == first
    # v2 comes up; then rollback --target hub goes back to v1
    fakehub.mode = "follow"
    r = run(repo, "deploy", "--target", "hub", "--wait", "10")
    assert r.returncode == 0 and current(hubhome) == v2, r.stderr
    r = run(repo, "rollback", "--target", "hub", "--plan")
    assert r.returncode == 0 and f"{v2} -> {first}" in r.stdout and current(hubhome) == v2
    r = run(repo, "rollback", "--target", "hub", "--wait", "10")
    assert r.returncode == 0 and current(hubhome) == first, r.stderr
    assert run(repo, "rollback", "--target", "hub", "--to", "f" * 7).returncode == 3
    r = run(repo, "rollback", "--target", "hub", "--to", v2[:7], "--wait", "10")
    assert r.returncode == 0 and current(hubhome) == v2, r.stderr


def test_degraded_checks_do_not_fail(hubhome, hubrepo, fakehub):
    repo, _ = hubrepo
    fakehub.checks = {"pg": "down", "phoenix": "ok", "ci": "ok"}
    r = run(repo, "deploy", "--target", "hub", "--wait", "1")
    assert r.returncode == 0 and "degraded" in r.stdout and "pg down" in r.stdout, r.stderr
