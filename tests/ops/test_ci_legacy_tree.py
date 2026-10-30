"""`assistant ci`'s legacy step runs a suite folded into the repo (tests/legacy/<suite>/run.sh) in place of
~/opt/<suite>-tests, with the staged copy in the suite's seam; without one it still finds the ~/opt suite."""

from __future__ import annotations

from pathlib import Path

MANIFEST = """
[core]
src = "core"
dest = "bin/lib/core"

[[file]]
src = "scripts/demo"
dest = "bin/demo"
mode = "0755"
suite = "demo"
seam = "DEMO_BIN"
"""


def tree_with(tmp: Path, folded: bool) -> Path:
    t = tmp / "tree"
    (t / "ops" / "deploy").mkdir(parents=True)
    (t / "ops" / "deploy" / "manifest.toml").write_text(MANIFEST)
    (t / "core").mkdir()
    (t / "packages" / "inbox").mkdir(parents=True)
    (t / "scripts").mkdir()
    (t / "scripts" / "demo").write_text("#!/bin/sh\necho demo\n")
    if folded:
        s = t / "tests" / "legacy" / "demo"
        s.mkdir(parents=True)
        (s / "run.sh").write_text('#!/bin/sh\n"$DEMO_BIN" > "$HOME/../seen" && echo tree > "$HOME/../which"\n')
    return t


def real_home_with_opt_suite(tmp: Path) -> Path:
    rh = tmp / "rh"
    s = rh / "opt" / "demo-tests"
    s.mkdir(parents=True)
    (s / "run.sh").write_text('#!/bin/sh\necho opt > "$HOME/../which"\n')
    return rh


def run_legacy(mod, tmp: Path, monkeypatch, folded: bool):
    rh = real_home_with_opt_suite(tmp)
    monkeypatch.setattr(mod.paths, "real_home", lambda: rh)
    monkeypatch.setattr(mod, "_netguard", lambda: "")
    work = tmp / "work"
    work.mkdir()
    res = mod._ci_legacy(tree_with(tmp, folded), None, work)
    return res, work / "legacy-demo"


def test_a_folded_suite_wins_and_gets_the_staged_copy(tool, tmp_path, monkeypatch):
    (name, ok, why), t = run_legacy(tool, tmp_path, monkeypatch, folded=True)
    assert (name, ok) == ("legacy", True), why
    assert (t / "which").read_text().strip() == "tree"
    assert (t / "seen").read_text().strip() == "demo"  # the staged copy ran through the seam


def test_without_a_folded_suite_the_opt_one_runs(tool, tmp_path, monkeypatch):
    (name, ok, why), t = run_legacy(tool, tmp_path, monkeypatch, folded=False)
    assert ok, why
    assert (t / "which").read_text().strip() == "opt"


LIB_MANIFEST = (
    MANIFEST
    + """
[[file]]
src = "scripts/lib/demolib.py"
dest = "bin/lib/demolib.py"
mode = "0644"

[[file]]
src = "packages/inbox/demo_in.py"
dest = "bin/lib/inbox/demo_in.py"
mode = "0644"
"""
)


def test_the_staged_copy_sees_the_trees_lib_modules(tool, tmp_path, monkeypatch):
    """A branch that changes a lib module (botlib) and its callers together: the suite's copy imports the tree's
    module from its own lib/, not the live ~/bin/lib one; the linked core/inbox are never written through."""
    t = tree_with(tmp_path, folded=True)
    (t / "ops" / "deploy" / "manifest.toml").write_text(LIB_MANIFEST)
    (t / "scripts" / "lib").mkdir()
    (t / "scripts" / "lib" / "demolib.py").write_text("WHO = 'tree'\n")
    (t / "packages" / "inbox" / "demo_in.py").write_text("X = 1\n")
    (t / "tests" / "legacy" / "demo" / "run.sh").write_text(
        '#!/bin/sh\ncat "$(dirname "$DEMO_BIN")/lib/demolib.py" > "$HOME/../lib-seen"\n'
    )
    rh = real_home_with_opt_suite(tmp_path)
    monkeypatch.setattr(tool.paths, "real_home", lambda: rh)
    monkeypatch.setattr(tool, "_netguard", lambda: "")
    work = tmp_path / "work"
    work.mkdir()
    name, ok, why = tool._ci_legacy(t, None, work)
    assert (name, ok) == ("legacy", True), why
    seen = (work / "legacy-demo" / "lib-seen").read_text()
    assert "WHO = 'tree'" in seen and tool.HEADER in seen  # rendered as it deploys
    assert (t / "packages" / "inbox" / "demo_in.py").read_text() == "X = 1\n"  # not written through the link
    assert (work / "overlay" / "lib" / "inbox").is_symlink() and (work / "overlay" / "lib" / "core").is_symlink()


def test_the_staged_copy_sees_the_would_be_lib(tool, tmp_path, monkeypatch):
    """A script under test imports from its own lib/ dir: the tree's managed lib files and the live unmanaged ones."""
    t = tree_with(tmp_path, folded=True)
    (t / "ops" / "deploy" / "manifest.toml").write_text(
        MANIFEST + '\n[[file]]\nsrc = "scripts/lib/managed_helper.py"\ndest = "bin/lib/managed_helper.py"\n'
        'mode = "0644"\n\n[[file]]\nsrc = "packages/inbox/writer.py"\ndest = "bin/lib/inbox/writer.py"\nmode = "0644"\n'
    )
    (t / "scripts" / "lib").mkdir()
    (t / "scripts" / "lib" / "managed_helper.py").write_text("WHO = 'tree'\n")
    (t / "packages" / "inbox" / "writer.py").write_text("WHO = 'tree inbox'\n")
    (t / "scripts" / "demo").write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib'))\n"
        "import live_helper, managed_helper\nprint(live_helper.WHO, managed_helper.WHO)\n"
    )
    (t / "tests" / "legacy" / "demo" / "run.sh").write_text('#!/bin/sh\n"$DEMO_BIN" > "$HOME/../seen"\n')
    rh = real_home_with_opt_suite(tmp_path)
    live = rh / "bin" / "lib"
    (live / "__pycache__").mkdir(parents=True)
    (live / "live_helper.py").write_text("WHO = 'live'\n")
    (live / "managed_helper.py").write_text("WHO = 'stale live copy'\n")
    monkeypatch.setattr(tool.paths, "real_home", lambda: rh)
    monkeypatch.setattr(tool, "_netguard", lambda: "")
    work = tmp_path / "work"
    work.mkdir()
    name, ok, why = tool._ci_legacy(t, None, work)
    assert ok, why
    assert (work / "legacy-demo" / "seen").read_text().split() == ["live", "tree"]
    lib = work / "overlay" / "lib"
    assert (lib / "live_helper.py").is_symlink() and not (lib / "__pycache__").exists()
    assert (t / "packages" / "inbox" / "writer.py").read_text() == "WHO = 'tree inbox'\n"  # never written through


def test_secretscan_is_staged_beside_every_suites_copy(tool, tmp_path, monkeypatch):
    """A script imports secretscan from its own dir (as deployed: ~/bin/secretscan.py beside it): the overlay has
    the tree's copy, rendered as it deploys, even when the secretscan suite itself is not among the ones run."""
    t = tree_with(tmp_path, folded=True)
    (t / "ops" / "deploy" / "manifest.toml").write_text(
        MANIFEST + '\n[[file]]\nsrc = "scripts/secretscan_py"\ndest = "bin/secretscan.py"\nmode = "0755"\n'
        'suite = "secretscan"\nseam = "SECRETSCAN_UNDER_TEST"\n'
    )
    (t / "scripts" / "secretscan_py").write_text("#!/usr/bin/python3 -I\nWHO = 'tree scanner'\n")
    (t / "tests" / "legacy" / "demo" / "run.sh").write_text(
        '#!/bin/sh\ncat "$(dirname "$DEMO_BIN")/secretscan.py" > "$HOME/../ss-seen"\n'
    )
    rh = real_home_with_opt_suite(tmp_path)
    monkeypatch.setattr(tool.paths, "real_home", lambda: rh)
    monkeypatch.setattr(tool, "_netguard", lambda: "")
    work = tmp_path / "work"
    work.mkdir()
    name, ok, why = tool._ci_legacy(t, None, work, only={"demo"})
    assert (name, ok) == ("legacy", True), why
    seen = (work / "legacy-demo" / "ss-seen").read_text()
    assert "WHO = 'tree scanner'" in seen and tool.HEADER in seen
    assert not (work / "legacy-secretscan").exists()  # its own suite did not run


# a suite logs its start, then waits until all three have started (a barrier: none ends before the last one starts;
# run one after another, the first gives up after 30 s and goes red), then sleeps 1 s
BARRIER = """S="{tmp}/starts"
echo {n} >> "$S"
i=0
while :; do
  k=0; while read -r _; do k=$((k + 1)); done < "$S"
  [ "$k" -ge 3 ] && break
  i=$((i + 1)); [ "$i" -ge 300 ] && echo "FAIL - {n} ran alone" && exit 3
  sleep 0.1
done
sleep 1
"""


def _three_suites(tmp: Path) -> Path:
    """Suites a, b, c: each logs its start and waits for the other two (BARRIER), then 1 s; b is red; slow.txt starts
    c first."""
    t = tmp / "tree"
    (t / "ops" / "deploy").mkdir(parents=True)
    man = '[core]\nsrc = "core"\ndest = "bin/lib/core"\n'
    for n in ("a", "b", "c"):
        man += f'\n[[file]]\nsrc = "scripts/{n}"\ndest = "bin/{n}"\nmode = "0755"\nsuite = "{n}"\n'
        (t / "scripts").mkdir(exist_ok=True)
        (t / "scripts" / n).write_text("#!/bin/sh\n")
        s = t / "tests" / "legacy" / n
        s.mkdir(parents=True)
        rc = 1 if n == "b" else 0
        err = "echo 'jq: command not found' >&2\n" if n == "b" else ""
        (s / "run.sh").write_text(
            "#!/bin/sh\n" + BARRIER.format(tmp=tmp, n=n) + f'{err}echo "FAIL - {n} x"\nexit {rc}\n'
        )
    (t / "ops" / "deploy" / "manifest.toml").write_text(man)
    (t / "core").mkdir()
    (t / "packages" / "inbox").mkdir(parents=True)
    (t / "tests" / "legacy" / "slow.txt").write_text("# slowest first\nc  # 1 s\n")
    return t


def test_suites_run_side_by_side_slow_first_results_in_manifest_order(tool, tmp_path, monkeypatch):
    """Ordering, not wall time (the runner shares its CPUs between jobs): the launch order is the step's own, and the
    suites' barrier proves they overlap."""
    rh = tmp_path / "rh"
    (rh / "opt").mkdir(parents=True)
    monkeypatch.setattr(tool.paths, "real_home", lambda: rh)
    monkeypatch.setattr(tool, "_netguard", lambda: "")
    monkeypatch.setenv("ASSISTANT_CI_LEGACY_WORKERS", "3")
    real, groups, launched = tool.BgGroup, [], []

    def launch(cmd, cwd=None, **kw):  # each launch: the suite and how many launched suites still run
        busy = sum(not g.done.is_set() for g in groups)
        groups.append(real(cmd, cwd=cwd, **kw))
        launched.append((Path(cwd).name, busy))
        return groups[-1]

    monkeypatch.setattr(tool, "BgGroup", launch)
    work = tmp_path / "work"
    work.mkdir()
    res: dict = {}
    name, ok, why = tool._ci_legacy(_three_suites(tmp_path), None, work, results=res)
    # slow.txt's suite first, then manifest order, each while the earlier ones still run (none passes the barrier
    # before the last one starts): three at once, not one after another
    assert launched == [("c", 0), ("a", 1), ("b", 2)]
    assert (name, ok) == ("legacy", False)
    assert why.startswith("1 red: b; b: exit 1 FAIL - b x")  # b went past the barrier: it never ran alone
    assert list(res) == ["a", "b", "c"]  # manifest order, whatever finished first
    assert [res[n]["ok"] for n in "abc"] == [True, False, True] and all(res[n]["secs"] >= 1 for n in "abc")
    assert sorted((tmp_path / "starts").read_text().split()) == ["a", "b", "c"]
    assert res["b"]["tail"] == "FAIL - b x | jq: command not found"  # stderr kept: the runner's record says why
    assert tool.legacy_tails(res) == {"b": "FAIL - b x | jq: command not found"}


def test_legacy_tails_fit_one_signed_extra(tool):
    import json

    res = {f"suite-{i:02}": {"ok": False, "tail": "x" * 400} for i in range(40)}
    res["green"] = {"ok": True}
    tails = tool.legacy_tails(res)
    assert len(tails) == 40 and "green" not in tails
    assert len(json.dumps(tails)) <= tool.LEGACY_TAILS  # hub/hubci.py drops an extra over 6000 JSON chars
    assert tool.legacy_tails({"a": {"ok": True}}) == {}
    assert tool._legacy_tail("ok 1\nFAIL - x\n", "") == "FAIL - x | ok 1"  # no stderr: stdout's last lines


def test_bun_on_the_runner_uses_the_toolchains_node_modules(tool, tmp_path, monkeypatch):
    import hashlib

    t = tmp_path / "tree"
    d = t / "adapters" / "tg"
    d.mkdir(parents=True)
    (d / "package.json").write_text("{}")
    (d / "bun.lock").write_text("lock-1")
    tc = tmp_path / "tc"
    monkeypatch.setenv("ASSISTANT_CI_TOOLCHAIN", str(tc))
    work = tmp_path / "work"
    work.mkdir()
    name, ok, why = tool._ci_bun(t, None, work)
    assert (name, ok) == ("bun", False) and "no vendored node_modules" in why  # never a network install there
    key = hashlib.sha256(b"lock-1").hexdigest()[:16]
    (tc / "bun-deps" / key / "node_modules" / "grammy").mkdir(parents=True)
    (tc / "bun-deps" / key / "node_modules" / "grammy" / "index.js").write_text("x")
    calls = []
    monkeypatch.setattr(
        tool, "run", lambda cmd, **kw: calls.append(cmd) or tool.subprocess.CompletedProcess(cmd, 0, "1 pass\n", "")
    )
    name, ok, why = tool._ci_bun(t, None, work)
    assert ok and (d / "node_modules" / "grammy" / "index.js").read_text() == "x"
    assert [c[1:] for c in calls] == [["test"]]  # no `bun install`


def test_legacy_workers(tool, monkeypatch):
    monkeypatch.setenv("ASSISTANT_CI_LEGACY_WORKERS", "1")
    assert tool.legacy_workers(50) == 1
    monkeypatch.setenv("ASSISTANT_CI_LEGACY_WORKERS", "")
    monkeypatch.setenv("ASSISTANT_CI_TOOLCHAIN", "/tc")
    monkeypatch.setattr(tool, "_host_cpus", lambda: 8.0)
    assert tool.legacy_workers(50) == 8 and tool.legacy_workers(3) == 3
    monkeypatch.setattr(tool, "_host_cpus", lambda: 32.0)
    assert tool.legacy_workers(50) == tool.LEGACY_WORKERS
