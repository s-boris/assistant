"""ci-gate enforce-gated: enforce applies only to lands whose changed paths hit the gated globs (main's
ops/ci/gated-paths.json, else the built-in default); the rest land as shadow. Each land row says gated + effective;
`ci-gate status` shows the scope, the overrides with their reasons and the done-when share (gated lands that carried
a green eval subset). Runner and evals are seams: no hub, no evals."""

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


def evals_seam(env, name, n):
    p = env.tmp / f"{name}.sh"
    cases = [{"id": f"c{i}"} for i in range(n)]
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps({'cases': cases})}\nEOF\n")
    p.chmod(0o755)
    return str(p)


def last_land(env):
    return [h for h in env.history() if h.get("op") == "land" and h.get("ok")][-1]


def test_enforce_gated_scopes_the_gate(env):
    assert env.run("deploy").returncode == 0
    r = env.run("ci-gate", "enforce-gated", "--reason", "try it early")
    assert r.returncode == 3 and "signed green lands" in r.stderr
    assert env.run("ci-gate", "enforce-gated", "--reason", "test of the scope", "--force").returncode == 0
    red = {"ASSISTANT_LAND_CI": "true", "ASSISTANT_LAND_RUNNER": seam(env, "red", RED), "ASSISTANT_LAND_EVALS": "false"}
    # not gated: lands like shadow (red runner, failing evals dry run)
    branch(env, "u1", "notes/a.txt", "a\n")
    r = env.run("land", "u1", extra=red)
    assert r.returncode == 0, r.stderr
    g = last_land(env)["gate"]
    assert g["mode"] == "enforce-gated" and g["gated"] == [] and g["effective"] == "shadow"
    assert g["evals"]["mode"] == "dry-run"
    # gated (tools): refused without a signed record, then lands with a green runner and a green subset
    branch(env, "g1", "packages/tools/x.py", "x = 1\n")
    before = env.git("rev-parse", "main")
    r = env.run("land", "g1", extra=red)
    assert r.returncode == 6 and "no signed green CI record" in r.stderr
    assert env.git("rev-parse", "main") == before
    ok = {**red, "ASSISTANT_LAND_RUNNER": seam(env, "green", GREEN), "ASSISTANT_LAND_EVALS": evals_seam(env, "ev", 2)}
    r = env.run("land", "g1", extra=ok)
    assert r.returncode == 0, r.stderr
    g = last_land(env)["gate"]
    assert g["gated"] == ["tools"] and g["effective"] == "enforce"
    assert g["evals"]["mode"] == "run" and g["evals"]["cases"] == 2
    # gated (reflexes) with the main session's override: logged with its reason, counted apart
    branch(env, "g2", "scripts/reflex", "#!/bin/sh\n")
    r = env.run("land", "g2", "--override", "evals box down, reviewed by hand", extra={**red, **MAIN})
    assert r.returncode == 0, r.stderr
    g = last_land(env)["gate"]
    assert g["override"] == "evals box down, reviewed by hand" and g["gated"] == ["reflexes"]
    st = json.loads(env.run("ci-gate", "status", "--json").stdout)
    assert st["mode"] == "enforce-gated" and "tools" in st["scope"] and "workflows" in st["scope"]
    m = st["gated_metric"]
    assert (m["lands"], m["gated"], m["green"], m["override"], m["share"]) == (3, 2, 1, 1, 0.5)
    assert st["overrides"][-1]["reason"] == "evals box down, reviewed by hand"
    txt = env.run("ci-gate", "status").stdout
    assert "gated lands (last 3): 2 · green subset 1 (50%)" in txt and "override" in txt
    # rollback: back to shadow, logged
    assert env.run("ci-gate", "shadow", "--reason", "rollback of the scoped gate").returncode == 0
    assert [(h["prev"], h["mode"]) for h in env.history() if h.get("op") == "ci-gate"][-1] == (
        "enforce-gated",
        "shadow",
    )


def test_gated_globs_come_from_main(env):
    assert env.run("deploy").returncode == 0
    env.write("ops/ci/gated-paths.json", json.dumps({"_doc": "x", "docs": ["docs/*"]}))
    env.commit("gate scope")
    assert env.run("ci-gate", "enforce-gated", "--reason", "test of the scope", "--force").returncode == 0
    red = {"ASSISTANT_LAND_CI": "true", "ASSISTANT_LAND_RUNNER": seam(env, "red", RED), "ASSISTANT_LAND_EVALS": "true"}
    branch(env, "t1", "packages/tools/y.py", "y = 1\n")  # not in this scope: shadow
    assert env.run("land", "t1", extra=red).returncode == 0
    assert last_land(env)["gate"]["effective"] == "shadow"
    branch(env, "d1", "docs/a.md", "a\n")
    r = env.run("land", "d1", extra=red)
    assert r.returncode == 6 and "no signed green CI record" in r.stderr
    st = json.loads(env.run("ci-gate", "status", "--json").stdout)
    assert list(st["scope"]) == ["docs"]
