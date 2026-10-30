import time


def branch(env, name, rel, text, base="main"):
    env.git("checkout", "-q", "-b", name, base)
    env.write(rel, text)
    env.commit(name)
    env.git("checkout", "-q", "main")


def test_parallel_lands_are_serialized(env):
    assert env.run("deploy").returncode == 0
    branch(env, "b1", "tools/a.txt", "a-from-b1\n")
    branch(env, "b2", "tools/b.txt", "b-from-b2\n")
    slow_ci = {"ASSISTANT_LAND_CI": "sh -c 'sleep 1'"}
    p1 = env.popen("land", "b1", extra=slow_ci)
    time.sleep(0.2)
    p2 = env.popen("land", "b2", extra=slow_ci)
    o1, o2 = p1.communicate(timeout=120), p2.communicate(timeout=120)
    assert p1.returncode == 0, o1
    assert p2.returncode == 0, o2
    assert (env.home / "bin/a.txt").read_text() == "a-from-b1\n"
    assert (env.home / "bin/b.txt").read_text() == "b-from-b2\n"
    rows = sorted((h for h in env.history() if h.get("op") == "land" and h.get("ok")), key=lambda h: h["land_start"])
    assert len(rows) == 2 and rows[0]["end"] <= rows[1]["land_start"]  # the lock-held parts never overlap
    # CI runs outside the lock, so which land reaches the lock first is not fixed (p1's head start is lost under
    # load): main is the branch of the later land row, and it carries both changes
    assert {r["branch"] for r in rows} == {"b1", "b2"}
    assert env.git("rev-parse", "main") == env.git("rev-parse", rows[1]["branch"])
    assert env.git("show", "main:tools/a.txt") == "a-from-b1" and env.git("show", "main:tools/b.txt") == "b-from-b2"
    assert env.git("worktree", "list").count("\n") == 0  # temp worktrees removed


def test_land_uses_existing_worktree_and_stamp(env, tmp_path):
    assert env.run("deploy").returncode == 0
    wt = tmp_path / "wt"
    env.git("worktree", "add", "-q", "-b", "b3", str(wt), "main")
    env.write("tools/a.txt", "a3\n", cwd=wt)
    env.commit("b3", cwd=wt)
    r = env.run("land", "b3", extra={"ASSISTANT_LAND_CI": "true"})
    assert r.returncode == 0, r.stderr
    assert (env.home / "bin/a.txt").read_text() == "a3\n"


def test_land_row_has_the_local_ci_timeline(env):
    """The land row: when the local CI started (wall time) and each of its steps' seconds (ci writes them into
    ASSISTANT_CI_LEGACY_OUT), for the dashboard's land timeline."""
    assert env.run("deploy").returncode == 0
    branch(env, "t1", "tools/a.txt", "a-t1\n")
    ci = env.tmp / "ci-steps.sh"
    ci.write_text(
        '#!/bin/sh\necho \'{"steps": {"pytest-3.13": 4.5, "legacy": 2.25, "x": "no"}}\' > "$ASSISTANT_CI_LEGACY_OUT"\n'
    )
    ci.chmod(0o755)
    seam = f"sh {ci}"
    t = time.time()
    r = env.run("land", "t1", extra={"ASSISTANT_LAND_CI": seam})
    assert r.returncode == 0, r.stderr
    row = next(h for h in env.history() if h.get("op") == "land" and h.get("ok"))
    g = row["gate"]
    assert t - 1 <= g["ci_local_at"] <= row["land_start"] and row["land_cmd_start"] <= g["ci_local_at"]
    assert g["ci_local_steps"] == {"pytest-3.13": 4.5, "legacy": 2.25}


def test_land_ci_failure_leaves_main(env):
    assert env.run("deploy").returncode == 0
    main = env.git("rev-parse", "main")
    branch(env, "bad", "tools/a.txt", "a-bad\n")
    r = env.run("land", "bad", extra={"ASSISTANT_LAND_CI": "false"})
    assert r.returncode == 6 and env.git("rev-parse", "main") == main
    assert (env.home / "bin/a.txt").read_text() == "a1\n"


def test_land_rebase_conflict(env):
    assert env.run("deploy").returncode == 0
    branch(env, "c1", "tools/a.txt", "conflict-1\n")
    env.write("tools/a.txt", "main-moved\n")
    main = env.commit("main moves")
    r = env.run("land", "c1", extra={"ASSISTANT_LAND_CI": "true"})
    assert r.returncode == 5 and "git rebase main" in r.stderr
    assert env.git("rev-parse", "main") == main


def test_land_smoke_failure_resets_main(env):
    assert env.run("deploy").returncode == 0
    main = env.git("rev-parse", "main")
    branch(env, "boom", "tools/hello.py", "#!/bin/sh\nexit 3\n")
    r = env.run("land", "boom", extra={"ASSISTANT_LAND_CI": "true"})
    assert r.returncode == 4
    assert env.git("rev-parse", "main") == main
    assert "hello v1" in (env.home / "bin/hello").read_text()


def test_land_refuses_main_and_dirty(env):
    assert env.run("land", "main").returncode == 2
    branch(env, "d1", "tools/a.txt", "d1\n")
    env.write("tools/b.txt", "dirty\n")
    assert env.run("land", "d1", extra={"ASSISTANT_LAND_CI": "true"}).returncode == 3


def test_land_adopts_existing_unmanaged_file(env):
    assert env.run("deploy").returncode == 0
    live = env.home / "bin/new-tool"
    live.write_text("#!/bin/sh\necho old hand-made\n")
    live.chmod(0o755)
    env.git("checkout", "-q", "-b", "ad1", "main")
    env.write("tools/new-tool", "#!/bin/sh\necho managed\n")
    m = env.repo / "ops/deploy/manifest.toml"
    m.write_text(m.read_text() + '\n[[file]]\nsrc = "tools/new-tool"\ndest = "bin/new-tool"\nsmoke = "{dest}"\n')
    env.commit("ad1")
    env.git("checkout", "-q", "main")
    main = env.git("rev-parse", "main")
    r = env.run("land", "ad1", extra={"ASSISTANT_LAND_CI": "true"})
    assert r.returncode == 3 and "--adopt bin/new-tool" in r.stderr
    assert env.git("rev-parse", "main") == main and "old hand-made" in live.read_text()
    r = env.run("land", "ad1", "--adopt", "bin/new-tool", extra={"ASSISTANT_LAND_CI": "true"})
    assert r.returncode == 0, r.stderr
    assert "managed" in live.read_text() and "bin/new-tool" in env.deployed()["files"]
    assert env.history()[-1]["adopt"] == ["bin/new-tool"]


def test_land_starts_assistant_publish_and_a_failing_one_never_fails_the_land(env, tmp_path):
    assert env.run("deploy").returncode == 0
    log = tmp_path / "publish.log"
    stub = tmp_path / "assistant-publish"
    stub.write_text(f'#!/bin/sh\necho "$*" >> {log}\nexit 1\n')
    stub.chmod(0o755)
    branch(env, "pub1", "tools/a.txt", "a-pub\n")
    r = env.run("land", "pub1", extra={"ASSISTANT_LAND_CI": "true", "ASSISTANT_PUBLISH_BIN": str(stub)})
    assert r.returncode == 0, r.stderr
    assert log.read_text() == "push --cron --bg\n"
    branch(env, "pub2", "tools/a.txt", "a-pub2\n")
    r = env.run("land", "pub2", extra={"ASSISTANT_LAND_CI": "true"})  # no seam in test mode: nothing started
    assert r.returncode == 0 and log.read_text() == "push --cron --bg\n"


def test_land_marks_the_board_with_the_branch_and_its_pre_rebase_hashes(env, tmp_path):
    assert env.run("deploy").returncode == 0
    log = tmp_path / "mark.log"
    stub = tmp_path / "board-lint"
    stub.write_text(f'#!/bin/sh\necho "$*" >> {log}\necho "board: closed ab12cd one item"\nexit 0\n')
    stub.chmod(0o755)
    branch(env, "bm1", "tools/a.txt", "a-bm1\n")
    old = env.git("rev-parse", "bm1")
    env.write("tools/b.txt", "main moves\n")
    env.commit("main moves")  # the land rebases bm1: its pre-rebase hash is what a board line may still name
    r = env.run("land", "bm1", extra={"ASSISTANT_LAND_CI": "true", "ASSISTANT_BOARD_MARK_BIN": str(stub)})
    assert r.returncode == 0, r.stderr
    new = env.git("rev-parse", "main")
    assert new != old
    assert log.read_text() == f"poke landed bm1 --hash {new} --also {old}\n"
    assert "board: closed ab12cd one item" in r.stdout
    failing = tmp_path / "board-lint-bad"
    failing.write_text("#!/bin/sh\nexit 7\n")
    failing.chmod(0o755)
    branch(env, "bm2", "tools/a.txt", "a-bm2\n")
    r = env.run("land", "bm2", extra={"ASSISTANT_LAND_CI": "true", "ASSISTANT_BOARD_MARK_BIN": str(failing)})
    assert r.returncode == 0, r.stderr  # a failing mark never fails the land
    branch(env, "bm3", "tools/a.txt", "a-bm3\n")
    r = env.run("land", "bm3", extra={"ASSISTANT_LAND_CI": "true"})  # no seam in test mode: nothing runs
    assert r.returncode == 0 and log.read_text().count("\n") == 1


def test_cis_run_outside_the_lock_and_a_moved_main_is_checked_again(env):
    """Two lands' CIs overlap (each waits until both have started: under the lock that could never happen); the
    second finds main moved by the first, rebases and runs its CI again outside the lock, then lands."""
    assert env.run("deploy").returncode == 0
    branch(env, "o1", "tools/a.txt", "a-o1\n")
    branch(env, "o2", "tools/b.txt", "b-o2\n")
    marks = env.tmp / "marks"
    marks.mkdir()
    wait_both = (
        f"touch {marks}/$$; i=0; while [ $(ls {marks} | wc -l) -lt 2 ] && [ $i -lt 3600 ]; do sleep 0.05; "
        "i=$((i+1)); done; [ $i -lt 3600 ]"  # up to 180 s for the other land's start: a loaded box (full CI beside)
    )
    ci = {"ASSISTANT_LAND_CI": f"sh -c '{wait_both}'"}
    p1 = env.popen("land", "o1", extra=ci)
    p2 = env.popen("land", "o2", extra=ci)
    o1, o2 = p1.communicate(timeout=300), p2.communicate(timeout=300)
    assert p1.returncode == 0 and p2.returncode == 0, (o1, o2)
    assert len(list(marks.iterdir())) == 3  # two overlapping CIs, then the later land's second one
    assert "main moved to" in o1[0] + o2[0]
    rows = [h for h in env.history() if h.get("op") == "land" and h.get("ok")]
    assert sorted(r["verify"]["tries"] for r in rows) == [1, 2]
    assert all(not r["verify"]["locked"] and r["lock_wait_secs"] >= 0 and r["land_cmd_start"] for r in rows)
    # the re-run selects against the green tree of the first pass (only main's new commits), not main
    again = next(r for r in rows if r["verify"]["tries"] == 2)
    first = next(r for r in rows if r["verify"]["tries"] == 1)
    assert "rerun_base" in again["gate"] and "rerun_base" not in first["gate"]
    assert again["gate"]["rerun_base"] != first["from"][:12]
    assert (env.home / "bin/a.txt").read_text() == "a-o1\n" and (env.home / "bin/b.txt").read_text() == "b-o2\n"


def test_serial_switch_keeps_ci_under_the_lock(env):
    assert env.run("deploy").returncode == 0
    branch(env, "s1", "tools/a.txt", "a-s1\n")
    env.home.joinpath(".config/assistant").mkdir(parents=True, exist_ok=True)
    env.home.joinpath(".config/assistant/land-serial").touch()
    r = env.run("land", "s1", extra={"ASSISTANT_LAND_CI": "true"})
    assert r.returncode == 0, r.stderr
    row = [h for h in env.history() if h.get("op") == "land" and h.get("ok")][-1]
    assert row["verify"] == {"tries": 1, "locked": True, "serial": True}
