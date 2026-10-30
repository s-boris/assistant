"""29.10 speed: the land runs the eval subset only when an eval case covers a changed path (evals' own --changed
selection, as a dry run first). No covered case -> no eval step, "evals: skipped (no covered paths)" in the land
output, mode "skipped" in the gate record. A selection that fails falls back to the run (never to a skip). Runner
and evals are seams: no hub, no evals."""

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


def evals_seam(env, name, n, run_rc=0):
    """Dry run: n covered cases. Run: marks the call in NAME.ran and exits run_rc."""
    p = env.tmp / f"{name}.sh"
    ran = env.tmp / f"{name}.ran"
    cases = json.dumps({"cases": [f"c{i}" for i in range(n)]})
    p.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do [ "$a" = --dry-run ] && { cat <<\'EOF\'\n' + cases + "\nEOF\nexit 0; }; done\n"
        f"echo \"RUN $*\" >> {ran}\ncat <<'EOF'\n{cases}\nEOF\nexit {run_rc}\n"
    )
    p.chmod(0o755)
    return str(p), ran


GREEN = {"state": "done", "ok": True, "signed_green": True, "secs": 42.0}


def last_land(env):
    return [h for h in env.history() if h.get("op") == "land" and h.get("ok")][-1]


def test_no_covered_paths_no_eval_step(env):
    assert env.run("deploy").returncode == 0
    assert env.run("ci-gate", "enforce", "--reason", "test of the eval scope", "--force").returncode == 0
    exe, ran = evals_seam(env, "none", 0, run_rc=1)  # a run would fail: the land must not reach it
    ex = {"ASSISTANT_LAND_CI": "true", "ASSISTANT_LAND_RUNNER": seam(env, "g", GREEN), "ASSISTANT_LAND_EVALS": exe}
    branch(env, "n1", "notes/a.txt", "a\n")
    r = env.run("land", "n1", extra=ex)
    assert r.returncode == 0, r.stderr
    assert "evals: skipped (no covered paths)" in r.stdout
    assert not ran.exists()
    ev = last_land(env)["gate"]["evals"]
    assert ev["mode"] == "skipped" and ev["ok"] and ev["cases"] == 0


def test_covered_paths_run_the_subset(env):
    assert env.run("deploy").returncode == 0
    assert env.run("ci-gate", "enforce", "--reason", "test of the eval scope", "--force").returncode == 0
    exe, ran = evals_seam(env, "two", 2)
    ex = {"ASSISTANT_LAND_CI": "true", "ASSISTANT_LAND_RUNNER": seam(env, "g", GREEN), "ASSISTANT_LAND_EVALS": exe}
    branch(env, "c1", "notes/b.txt", "b\n")
    r = env.run("land", "c1", extra=ex)
    assert r.returncode == 0, r.stderr
    assert ran.read_text().count("RUN ") == 1
    # two lands' eval steps queue on evals' run lock instead of failing (29.10: "another run is active")
    assert "--lock-wait 1800" in ran.read_text()
    assert "evals: ran 2 covered case(s), green" in r.stdout
    ev = last_land(env)["gate"]["evals"]
    assert ev["mode"] == "run" and ev["cases"] == 2
    # covered and red: refused
    exe, _ = evals_seam(env, "red", 1, run_rc=3)
    branch(env, "c2", "notes/c.txt", "c\n")
    r = env.run("land", "c2", extra={**ex, "ASSISTANT_LAND_EVALS": exe})
    assert r.returncode == 6 and "eval subset failed" in r.stderr


def test_shadow_lists_without_running(env):
    assert env.run("deploy").returncode == 0
    exe, ran = evals_seam(env, "sh", 0)
    ex = {"ASSISTANT_LAND_CI": "true", "ASSISTANT_LAND_RUNNER": seam(env, "g", GREEN), "ASSISTANT_LAND_EVALS": exe}
    branch(env, "s1", "notes/d.txt", "d\n")
    r = env.run("land", "s1", extra=ex)
    assert r.returncode == 0, r.stderr
    assert "evals: skipped (no covered paths)" in r.stdout and not ran.exists()
    exe, ran = evals_seam(env, "sh2", 3)
    branch(env, "s2", "notes/e.txt", "e\n")
    r = env.run("land", "s2", extra={**ex, "ASSISTANT_LAND_EVALS": exe})
    assert r.returncode == 0, r.stderr
    assert not ran.exists() and "listed (shadow, not run) 3 covered case(s)" in r.stdout
    assert last_land(env)["gate"]["evals"]["mode"] == "dry-run"


def test_unreadable_selection_never_skips(env):
    assert env.run("deploy").returncode == 0
    assert env.run("ci-gate", "enforce", "--reason", "test of the eval scope", "--force").returncode == 0
    ex = {"ASSISTANT_LAND_CI": "true", "ASSISTANT_LAND_RUNNER": seam(env, "g", GREEN), "ASSISTANT_LAND_EVALS": "false"}
    branch(env, "f1", "notes/f.txt", "f\n")
    r = env.run("land", "f1", extra=ex)
    assert r.returncode == 6 and "eval subset failed" in r.stderr
