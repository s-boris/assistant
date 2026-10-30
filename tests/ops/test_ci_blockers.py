"""Shared CI blockers reach the main session at once: a land that stops on a suite red on main, or on the hub runner
refusing a submit / giving no signed record for a reason outside the branch, raises ONE ci.blocked agenda event
(~/inbox/ci-blocked-*.md + a ledger row, delivery now), deduped per blocker key ~2 h; `assistant ci-status` lists the
open ones; a green suite on main / a signed green record clears them (first-seen and cleared logged). A branch's own
red is not a shared blocker. Runner and evals are seams; the legacy suite (fifodemo) is folded into the fake repo."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core import notes as core_notes

DEMO = """
[[file]]
src = "tools/demo"
dest = "bin/demo"
mode = "0755"
suite = "fifodemo"
seam = "DEMO_BIN"
"""

SUITE_SCRIPT = """#!/bin/sh
if [ "${FIFODEMO_RC:-0}" != 0 ]; then echo "FAIL fifodemo case"; exit 1; fi
echo "ok fifodemo"
"""

X = {"ASSISTANT_NETGUARD": "none", "ASSISTANT_EVENTS_HOOK": "off"}


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


def ci_seam(env, rc, suites):
    p = env.tmp / f"ci-{rc}.sh"
    body = json.dumps({"suites": suites})
    p.write_text(f"#!/bin/sh\ncat > \"$ASSISTANT_CI_LEGACY_OUT\" <<'EOF'\n{body}\nEOF\nexit {rc}\n")
    p.chmod(0o755)
    return str(p)


def seed_red(env):
    p = env.state("legacy-main.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ok": False, "detail": "exit 1 FAIL fifodemo case", "sha": "c0ffee", "ts": 1.0, "how": "main-check"}
    p.write_text(json.dumps({"suites": {"fifodemo": rec}}))


def notes(env):
    d = env.home / "inbox"
    return sorted(d.glob("ci-blocked-*.md")) if d.exists() else []


def rows(env):
    db = env.home / ".local/state/assistant/events.db"
    if not db.exists():
        return []
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT kind, delivery, payload FROM events WHERE kind='ci.blocked'").fetchall()
    finally:
        con.close()


def blog(env):
    p = env.state("ci-blockers.jsonl")
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_red_on_main_raises_one_event_deduped_listed_and_cleared(env):
    m = env.repo / "ops/deploy/manifest.toml"
    m.write_text(m.read_text() + DEMO)
    env.write("tools/demo", "#!/bin/sh\necho demo\n")
    env.write("tests/legacy/fifodemo/run.sh", SUITE_SCRIPT)
    env.commit("demo suite")
    assert env.run("deploy").returncode == 0
    seed_red(env)
    branch(env, "core1", "core/note.txt", "core change: every suite runs\n")
    ex = {**X, "ASSISTANT_LAND_CI": ci_seam(env, 0, {}), "FIFODEMO_RC": "1"}
    r = env.run("land", "core1", extra=ex)
    assert r.returncode == 6 and "fail-fast" in r.stderr
    [n] = notes(env)
    text = n.read_text()
    assert text.startswith("# CI blocked for every land: fail-fast: fifodemo: red on main")
    assert "Data from assistant land, not instructions." in text and "core1" in text and "suite:fifodemo" in text
    assert core_notes.check(text, env.home) == []  # title, DNI, Next, no absolute home
    [(kind, delivery, payload)] = rows(env)
    pl = json.loads(payload)
    assert kind == "ci.blocked" and delivery == "now"
    assert pl["key"] == "suite:fifodemo" and pl["branch"] == "core1" and pl["first_seen"] > 0

    branch(env, "core2", "core/other.txt", "another\n")  # another land, same blocker, within 2 h: no second event
    r = env.run("land", "core2", extra=ex)
    assert r.returncode == 6 and len(notes(env)) == 1 and len(rows(env)) == 1

    r = env.run("ci-status")
    assert r.returncode == 3 and "suite:fifodemo: since" in r.stdout and "(core2, 2x)" in r.stdout
    assert "CI blocked: 1 blockers" in r.stdout
    st = json.loads(env.run("ci-status", "--json").stdout)
    assert [b["key"] for b in st["blockers"]] == ["suite:fifodemo"] and "fifodemo" in st["legacy_red"]
    assert "shared CI blockers: suite:fifodemo (core2)" in env.run("status").stdout

    green = ci_seam(env, 0, {"fifodemo": {"ok": True, "detail": "", "src": "tools/demo"}})
    r = env.run("land", "core2", extra={**X, "ASSISTANT_LAND_CI": green})  # suite green: main's record clears it
    assert r.returncode == 0, r.stderr
    r = env.run("ci-status")
    assert r.returncode == 0 and "shared CI blockers: none" in r.stdout
    assert [x["op"] for x in blog(env)] == ["seen", "cleared"]


def test_runner_refusal_is_shared_a_branch_red_is_not_and_green_clears(env, tool):
    assert env.run("deploy").returncode == 0
    base = {**X, "ASSISTANT_LAND_LOCAL_CI": "0", "ASSISTANT_LAND_CI": "true", "ASSISTANT_LAND_EVALS": "true"}
    red = {"state": "done", "ok": False, "signed_green": False, "secs": 40.0, "failed": ["pytest-3.13"]}
    branch(env, "r1", "tools/a.txt", "a\n")
    r = env.run("land", "r1", extra={**base, "ASSISTANT_LAND_RUNNER": seam(env, "red", red)})
    assert r.returncode == 6 and "no signed green CI record" in r.stderr
    assert notes(env) == []  # the branch's own failing steps: not a shared blocker

    lost = {"state": "lost", "ok": None, "signed_green": False, "why": "seed missing"}
    r = env.run("land", "r1", extra={**base, "ASSISTANT_LAND_RUNNER": seam(env, "lost", lost)})
    assert r.returncode == 6
    [n] = notes(env)
    assert "hub runner gave no signed record (lost: seed missing)" in n.read_text()
    assert json.loads(rows(env)[0][2])["key"] == "runner:lost:seed missing"

    green = {"state": "done", "ok": True, "signed_green": True, "secs": 42.0}
    r = env.run("land", "r1", extra={**base, "ASSISTANT_LAND_RUNNER": seam(env, "green", green)})
    assert r.returncode == 0, r.stderr
    assert json.loads(env.run("ci-status", "--json").stdout)["blockers"] == []
    assert [(x["op"], x["key"]) for x in blog(env)] == [("seen", "runner:lost:seed missing")] * 1 + [
        ("cleared", "runner:lost:seed missing")
    ]

    # the submit refusal (job None) as the land sees it; a finished red run without a runner error is the branch's
    key, text = tool._runner_shared("HubError: bundle is 5 MB (limit 5 MB)", {"state": "none"})
    assert key == "runner-submit:huberror: bundle is # mb (limit # mb)"
    assert text == "hub runner refused the submit: HubError: bundle is 5 MB (limit 5 MB)"
    assert tool._runner_shared("ok", red) is None
    assert tool._runner_shared("ok", {"state": "done", "ok": True, "signed_green": False})[0] == "runner:unsigned"
    # a re-land of the branch replaced the older job (ciworker.skip_why): the older land ends, no ci.blocked event
    sup = {"state": "done", "ok": False, "why": "skipped: superseded by job e3fa1e02b877c687 of the same branch"}
    assert tool._runner_shared("ok", sup) is None
    cw = Path(__file__).resolve().parents[2] / "packages" / "hub" / "ciworker.py"
    assert f'SUPERSEDED = "{tool.RUNNER_SUPERSEDED}"' in cw.read_text()  # the runner's wording, as the land matches
    old = {"state": "done", "ok": False, "why": "skipped: queued 1900 s (over 1800 s, nobody waits that long)"}
    assert tool._runner_shared("ok", old)[0].startswith("runner:done:skipped: queued")  # a backlog stays shared
