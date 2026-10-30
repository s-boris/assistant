"""H12: the land gate. Shadow logs the hub runner's answer and lands anyway; enforce refuses without a signed green
record or a green eval subset; --override is the main session's (refused for a subagent) and logged; ci-gate modes
are logged and enforce needs 5 signed green lands (or --force). Runner and evals are seams (ASSISTANT_LAND_RUNNER,
ASSISTANT_LAND_EVALS): no hub, no evals."""

from __future__ import annotations

import json


def branch(env, name, rel, text):
    env.git("checkout", "-q", "-b", name, "main")
    env.write(rel, text)
    env.commit(name)
    env.git("checkout", "-q", "main")


def seam(env, name, obj):
    p = env.tmp / f"{name}.sh"
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(obj)}\nEOF\n")
    p.chmod(0o755)
    return str(p)


GREEN = {"state": "done", "ok": True, "signed_green": True, "secs": 42.0}
RED = {"state": "done", "ok": False, "signed_green": False, "secs": 40.0, "failed": ["pytest-3.13"]}
MAIN = {"AI_AGENT": "claude-code_main"}


def test_shadow_logs_the_runner_and_lands(env):
    assert env.run("deploy").returncode == 0
    branch(env, "s1", "tools/a.txt", "a\n")
    ex = {"ASSISTANT_LAND_CI": "true", "ASSISTANT_LAND_RUNNER": seam(env, "red", RED), "ASSISTANT_LAND_EVALS": "false"}
    r = env.run("land", "s1", extra=ex)
    assert r.returncode == 0, r.stderr
    rec = [h for h in env.history() if h.get("op") == "land" and h.get("ok")][-1]
    assert rec["gate"]["mode"] == "shadow" and rec["runner"]["signed_green"] is False
    assert rec["gate"]["evals"]["mode"] == "dry-run" and "ci_local_secs" in rec["gate"]
    branch(env, "s2", "tools/b.txt", "b\n")
    r = env.run("land", "s2", extra={**ex, "ASSISTANT_LAND_RUNNER": seam(env, "green", GREEN)})
    assert r.returncode == 0, r.stderr
    st = json.loads(env.run("ci-gate", "status", "--json").stdout)
    assert st["mode"] == "shadow" and st["signed_green_lands"] == 1 and st["needed"] == 5


def test_enforce_refuses_without_signed_record_and_override_is_main_only(env):
    assert env.run("deploy").returncode == 0
    r = env.run("ci-gate", "enforce", "--reason", "try it early")
    assert r.returncode == 3 and "signed green lands" in r.stderr
    assert env.run("ci-gate", "enforce", "--reason", "test of enforce", "--force").returncode == 0
    branch(env, "e1", "tools/a.txt", "a\n")
    before = env.git("rev-parse", "main")
    ex = {"ASSISTANT_LAND_CI": "true", "ASSISTANT_LAND_RUNNER": seam(env, "red", RED), "ASSISTANT_LAND_EVALS": "true"}
    r = env.run("land", "e1", extra=ex)
    assert r.returncode == 6 and "no signed green CI record" in r.stderr
    assert env.git("rev-parse", "main") == before
    r = env.run("land", "e1", "--override", "hub down, local CI green", extra={**ex, "AI_AGENT": "claude-code_x_agent"})
    assert r.returncode == 2 and "main session" in r.stderr
    r = env.run("land", "e1", "--override", "short", extra={**ex, **MAIN})
    assert r.returncode == 2
    r = env.run("land", "e1", "--override", "hub down, local CI green", extra={**ex, **MAIN})
    assert r.returncode == 0, r.stderr
    rec = [h for h in env.history() if h.get("op") == "land" and h.get("ok")][-1]
    assert rec["gate"]["override"] == "hub down, local CI green" and rec["gate"]["by"] == "claude-code_main"
    branch(env, "e2", "tools/b.txt", "b\n")
    r = env.run(
        "land", "e2", extra={**ex, "ASSISTANT_LAND_RUNNER": seam(env, "g", GREEN), "ASSISTANT_LAND_EVALS": "false"}
    )
    assert r.returncode == 6 and "eval subset failed" in r.stderr
    r = env.run("land", "e2", extra={**ex, "ASSISTANT_LAND_RUNNER": seam(env, "g", GREEN)})
    assert r.returncode == 0, r.stderr
    assert env.run("ci-gate", "off", "--reason", "rollback test").returncode == 0
    modes = [(h["prev"], h["mode"]) for h in env.history() if h.get("op") == "ci-gate"]
    assert modes == [("shadow", "enforce"), ("enforce", "off")]


def test_ci_stamps_only_the_tree_it_tested(env, tool):
    """The stamp's key is the tree read when the run starts: a commit or an edit during the run gets no stamp (land
    trusts a stamp instead of running CI)."""
    head, tree = env.git("rev-parse", "HEAD"), env.git("rev-parse", "HEAD^{tree}")
    assert tool._ci_unchanged(env.repo, head, tree) == ""
    env.write("tools/new.txt", "n\n")
    assert tool._ci_unchanged(env.repo, head, tree) == "uncommitted changes"
    env.commit("committed while CI ran")
    assert tool._ci_unchanged(env.repo, head, tree) == "HEAD moved during the run"


def test_runner_is_the_test_run_unless_the_switch(env):
    """The land runs `ci --host` here and needs the runner's signed green in every mode but off;
    ASSISTANT_LAND_LOCAL_CI=1 (~/.config/assistant/land-local-ci) is the rollback to the full local run."""
    assert env.run("deploy").returncode == 0
    branch(env, "r1", "tools/a.txt", "a\n")
    ex = {
        "ASSISTANT_LAND_LOCAL_CI": "0",
        "ASSISTANT_LAND_CI": "true",
        "ASSISTANT_LAND_RUNNER": seam(env, "red", RED),
        "ASSISTANT_LAND_EVALS": "false",
    }
    r = env.run("land", "r1", extra=ex)  # shadow mode, yet no land without the runner's signed green
    assert r.returncode == 6 and "land-local-ci" in r.stderr
    r = env.run("land", "r1", extra={**ex, "ASSISTANT_LAND_RUNNER": seam(env, "green", GREEN)})
    assert r.returncode == 0, r.stderr
    rec = [h for h in env.history() if h.get("op") == "land" and h.get("ok")][-1]
    assert rec["gate"]["tests"] == "runner"
    branch(env, "r2", "tools/b.txt", "b\n")
    r = env.run("land", "r2", extra={**ex, "ASSISTANT_LAND_LOCAL_CI": "1"})  # rollback: the runner counts as before
    assert r.returncode == 0, r.stderr
    rec = [h for h in env.history() if h.get("op") == "land" and h.get("ok")][-1]
    assert rec["gate"]["tests"] == "local" and rec["runner"]["signed_green"] is False
