"""Fail-fast on legacy suites red on main. Main's record (legacy-main.json) holds each suite's last result on main's
tree; a land runs the suites it would run that are red there first, alone, and stops at once (exit 6, the full CI
never starts) when one is still red. A suite green again goes on to the full CI; a land that would not run the suite
is not held. A red land CI with a suite red outside the branch's area re-runs that suite on main and records it.
The suite here (fifodemo) is folded into the fake repo; FIFODEMO_RC and core/breaks_demo make it red."""

from __future__ import annotations

import json
import os
import subprocess

DEMO = """
[[file]]
src = "tools/demo"
dest = "bin/demo"
mode = "0755"
suite = "fifodemo"
seam = "DEMO_BIN"
"""

SUITE_SCRIPT = """#!/bin/sh
if [ -e "$ASSISTANT_CI_TREE/core/breaks_demo" ] || [ "${FIFODEMO_RC:-0}" != 0 ]; then
  echo "FAIL fifodemo case"; exit 1
fi
echo "ok fifodemo"
"""


def setup(env):
    m = env.repo / "ops/deploy/manifest.toml"
    m.write_text(m.read_text() + DEMO)
    env.write("tools/demo", "#!/bin/sh\necho demo\n")
    env.write("tests/legacy/fifodemo/run.sh", SUITE_SCRIPT)
    env.commit("demo suite")
    assert env.run("deploy").returncode == 0


def branch(env, name, rel, text):
    env.git("checkout", "-q", "-b", name, "main")
    env.write(rel, text)
    env.commit(name)
    env.git("checkout", "-q", "main")


def ci_seam(env, rc: int, suites: dict) -> tuple[str, object]:
    """A stand-in for `assistant ci`: marks that it ran, writes its legacy results where land asked, exits rc."""
    mark = env.tmp / "ci-ran"
    p = env.tmp / f"ci-{rc}.sh"
    body = json.dumps({"suites": suites})
    p.write_text(f"#!/bin/sh\ntouch {mark}\ncat > \"$ASSISTANT_CI_LEGACY_OUT\" <<'EOF'\n{body}\nEOF\nexit {rc}\n")
    p.chmod(0o755)
    return str(p), mark


def seed_red(env):
    p = env.state("legacy-main.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ok": False, "detail": "exit 1 FAIL fifodemo case", "sha": "c0ffee", "ts": 1.0, "how": "main-check"}
    p.write_text(json.dumps({"suites": {"fifodemo": rec}}))


def record(env):
    return json.loads(env.state("legacy-main.json").read_text())["suites"]


X = {"ASSISTANT_NETGUARD": "none"}


def test_a_suite_red_on_main_and_still_red_fails_before_ci(env):
    setup(env)
    seed_red(env)
    main = env.git("rev-parse", "main")
    branch(env, "core1", "core/note.txt", "core change: every suite runs\n")
    seam, mark = ci_seam(env, 0, {})
    r = env.run("land", "core1", extra={**X, "ASSISTANT_LAND_CI": seam, "FIFODEMO_RC": "1"})
    assert r.returncode == 6, r.stderr
    assert not mark.exists()  # the full CI never started
    assert "fail-fast, full CI not run: fifodemo: red on main since" in r.stderr
    assert "doesn't touch tools/demo or tests/legacy/fifodemo/" in r.stderr and "still red here" in r.stderr
    assert env.git("rev-parse", "main") == main
    last = env.history()[-1]
    assert (last["why"], last["suites"]) == ("known red on main", ["fifodemo"])


def test_a_suite_green_again_goes_to_the_full_ci_and_main_is_recorded_green(env):
    setup(env)
    seed_red(env)
    branch(env, "core2", "core/note.txt", "core change\n")
    seam, mark = ci_seam(env, 0, {"fifodemo": {"ok": True, "detail": "", "src": "tools/demo"}})
    r = env.run("land", "core2", extra={**X, "ASSISTANT_LAND_CI": seam})
    assert r.returncode == 0, r.stderr
    assert mark.exists() and "fifodemo green here; full CI" in r.stdout
    rec = record(env)["fifodemo"]
    assert rec["ok"] is True and rec["how"] == "land" and rec["sha"] == env.git("rev-parse", "main")


def test_a_land_that_does_not_run_the_suite_is_not_held(env):
    setup(env)
    seed_red(env)
    branch(env, "plain", "tools/a.txt", "a-plain\n")
    seam, mark = ci_seam(env, 0, {})
    r = env.run("land", "plain", extra={**X, "ASSISTANT_LAND_CI": seam, "FIFODEMO_RC": "1"})
    assert r.returncode == 0, r.stderr
    assert mark.exists() and "fail-fast" not in r.stdout
    assert record(env)["fifodemo"]["ok"] is False  # still red on main: nothing ran it


def test_a_red_ci_rechecks_main_and_the_next_land_stops_at_once(env):
    setup(env)
    branch(env, "core3", "core/note.txt", "core change\n")
    red = {"fifodemo": {"ok": False, "detail": "exit 1 FAIL fifodemo case", "src": "tools/demo"}}
    seam, mark = ci_seam(env, 6, red)
    ex = {**X, "ASSISTANT_LAND_CI": seam, "FIFODEMO_RC": "1"}
    r = env.run("land", "core3", extra=ex)
    assert r.returncode == 6 and mark.exists()
    assert "fifodemo red on main too (not this branch's doing" in r.stderr
    rec = record(env)["fifodemo"]
    assert rec["ok"] is False and rec["how"] == "main-check" and rec["sha"] == env.git("rev-parse", "main")
    mark.unlink()
    r = env.run("land", "core3", extra=ex)
    assert r.returncode == 6 and not mark.exists() and "fail-fast" in r.stderr


def test_a_branch_that_breaks_the_suite_is_told_so(env):
    setup(env)
    branch(env, "core4", "core/breaks_demo", "x\n")
    red = {"fifodemo": {"ok": False, "detail": "exit 1 FAIL fifodemo case", "src": "tools/demo"}}
    seam, _mark = ci_seam(env, 6, red)
    r = env.run("land", "core4", extra={**X, "ASSISTANT_LAND_CI": seam})
    assert r.returncode == 6
    assert "fifodemo green on main: this branch breaks it" in r.stderr
    assert record(env)["fifodemo"]["ok"] is True  # main's own result


def test_ci_records_main_only_from_a_clean_main_checkout(tool, tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    genv = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid")
    genv.update(GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], env=genv, capture_output=True, text=True, check=True)

    git("init", "-q", "-b", "main")
    (repo / "f").write_text("1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "one")
    head = git("rev-parse", "HEAD").stdout.strip()
    leg = {"s1": {"ok": False, "detail": "exit 1", "src": "scripts/s1"}}
    out = tmp_path / "out.json"
    monkeypatch.setenv("ASSISTANT_CI_LEGACY_OUT", str(out))
    tool._legacy_report(repo, head, "tree1", leg, True)  # dirty: the out file, not main's record
    assert json.loads(out.read_text())["suites"] == leg and tool.legacy_main() == {}
    git("checkout", "-q", "-b", "side")
    (repo / "f").write_text("2\n")
    git("commit", "-q", "-am", "two")
    tool._legacy_report(repo, git("rev-parse", "HEAD").stdout.strip(), "tree2", leg, False)  # not main
    assert tool.legacy_main() == {}
    tool._legacy_report(repo, head, "tree1", leg, False)  # main itself, clean
    rec = tool.legacy_main()["s1"]
    assert (rec["ok"], rec["how"], rec["sha"]) == (False, "ci", head)
    assert list(tool.legacy_red()) == ["s1"]


def test_only_runs_the_named_suites_and_reports_each(tool, tmp_path, monkeypatch):
    t = tmp_path / "tree"
    (t / "ops/deploy").mkdir(parents=True)
    (t / "ops/deploy/manifest.toml").write_text(
        '[core]\nsrc = "core"\ndest = "bin/lib/core"\n'
        + "".join(
            f'\n[[file]]\nsrc = "scripts/{n}"\ndest = "bin/{n}"\nmode = "0755"\nsuite = "{n}"\n' for n in ("s1", "s2")
        )
    )
    (t / "core").mkdir()
    (t / "packages/inbox").mkdir(parents=True)
    for n, rc in (("s1", 0), ("s2", 1)):
        (t / "scripts").mkdir(exist_ok=True)
        (t / "scripts" / n).write_text("#!/bin/sh\n")
        (t / "tests/legacy" / n).mkdir(parents=True)
        (t / "tests/legacy" / n / "run.sh").write_text(f"#!/bin/sh\necho 'FAIL {n} case'\nexit {rc}\n")
    monkeypatch.setattr(tool.paths, "real_home", lambda: tmp_path / "rh")
    monkeypatch.setattr(tool, "_netguard", lambda: "")
    work = tmp_path / "w"
    work.mkdir()
    res: dict = {}
    _name, ok, why = tool._ci_legacy(t, None, work, results=res)  # no base: every suite
    assert not ok and "s2: exit 1 FAIL s2 case" in why
    assert why.startswith("1 red: s2; ")  # the red names lead: a long FAIL tail can't cut them from the record
    assert all(isinstance(res[s].pop("secs"), float) for s in ("s1", "s2"))  # per-suite time (the runner's record)
    assert res["s1"]["ok"] is True and res["s2"] == {"ok": False, "detail": "exit 1 FAIL s2 case", "src": "scripts/s2",
                                                   "tail": "FAIL s2 case", "fails": ["FAIL s2 case"]}  # fmt: skip
    work2 = tmp_path / "w2"
    work2.mkdir()
    res = {}
    _name, ok, why = tool._ci_legacy(t, None, work2, only={"s1"}, results=res)
    assert ok and list(res) == ["s1"]
    assert [e["suite"] for e in tool._legacy_selected(tool.load_manifest(t), ["scripts/s2"], t)] == ["s2"]
