"""The land queue (verify, then land): with the switch on, `land BRANCH` runs the quick checks, queues the branch and
returns (--no-wait) or follows it (--wait); one worker lands batches through today's land() as a child (one runner
call, one deploy, a land row and a board poke per member), splits a red batch in halves, defers a conflict between
queued branches, runs gated entries in their own lane and, after a crash, never deploys a tree that was not tested.
Seams: ASSISTANT_LANDQ_CHECK (the quick checks), ASSISTANT_LAND_CI (the batch's CI), ASSISTANT_LAND_RUNNER,
ASSISTANT_LAND_EVALS, ASSISTANT_BOARD_MARK_BIN, ASSISTANT_LANDQ_TICK."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import time

import pytest


def branch(env, name, rel, text, base="main"):
    env.git("checkout", "-q", "-b", name, base)
    env.write(rel, text)
    env.commit(name)
    env.git("checkout", "-q", "main")


def until(cond, what, timeout=90.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _alive(pid) -> bool:
    try:
        st = open(f"/proc/{int(pid)}/stat").read()
    except (OSError, ValueError, TypeError):
        return False
    return st[st.rindex(")") + 2] not in "ZX"


@pytest.fixture
def q(env):
    """The queue on, a gate file every batch CI waits on while it exists (hold), a CI log of the trees it passed,
    a runner seam that counts its calls and a board stub that logs its pokes."""
    t = env.tmp
    hold = t / "hold"
    passed = t / "ci-passed.log"
    runner_log = t / "runner.log"
    board_log = t / "board.log"
    ci = t / "ci.sh"
    ci.write_text(
        "#!/bin/sh\n"
        f"git rev-parse 'HEAD^{{tree}}' >> {t / 'ci-runs.log'}\n"
        f"i=0; while [ -e {hold} ] && [ $i -lt 2400 ]; do sleep 0.05; i=$((i+1)); done\n"
        "test ! -e tools/red.txt || exit 1\n"
        f"git rev-parse 'HEAD^{{tree}}' >> {passed}\n"
    )
    ci.chmod(0o755)
    runner = t / "runner.sh"
    runner.write_text(
        f'#!/bin/sh\necho "$1" >> {runner_log}\n'
        'echo \'{"state": "done", "ok": true, "signed_green": true, "secs": 1.0}\'\n'
    )
    runner.chmod(0o755)
    board = t / "board"
    board.write_text(f'#!/bin/sh\necho "$*" >> {board_log}\n')
    board.chmod(0o755)
    env.env.update(
        {
            "ASSISTANT_LAND_QUEUE": "1",
            "ASSISTANT_LANDQ_CHECK": "true",
            "ASSISTANT_LAND_CI": f"sh {ci}",
            "ASSISTANT_LAND_RUNNER": str(runner),
            "ASSISTANT_LAND_EVALS": "true",
            "ASSISTANT_BOARD_MARK_BIN": str(board),
            "ASSISTANT_LANDQ_TICK": "0.1",
        }
    )
    assert env.run("deploy").returncode == 0

    class Q:
        pass

    o = Q()
    o.hold, o.passed, o.runner_log, o.board_log = hold, passed, runner_log, board_log

    def entry(eid):
        p = env.state("landq", "entries", eid + ".json")
        return json.loads(p.read_text()) if p.exists() else {}

    def status():
        r = env.run("land", "status", "--json")
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def batches():
        d = env.state("landq", "batches")
        return sorted((json.loads(p.read_text()) for p in d.glob("*.json")), key=lambda b: b["n"]) if d.exists() else []

    def rows():
        return [h for h in env.history() if h.get("op") == "land" and h.get("ok")]

    def queue(name, *extra):
        r = env.run("land", name, "--no-wait", *extra)
        assert r.returncode == 0, r.stderr
        return r.stdout.split("queued ", 1)[1].split()[0]

    o.entry, o.status, o.batches, o.rows, o.queue = entry, status, batches, rows, queue
    yield o
    hold.unlink(missing_ok=True)
    pids = []  # nothing of a failed test keeps running: the worker and the batch lands
    w = env.state("landq", "worker.json")
    if w.exists():
        pids.append(json.loads(w.read_text()).get("pid"))
    pids += [b.get("pid") for b in batches()]
    for pid in pids:
        if pid and _alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid), signal.SIGKILL)


def test_queued_then_status_then_wait(env, q):
    """--no-wait returns "queued qN" after the quick checks; status shows it; `land status qN --wait` follows it to
    landed (exit 0, main's commit); the land row carries the landq block and the member's own land_cmd_start."""
    branch(env, "w1", "tools/a.txt", "a-w1\n")
    q.hold.touch()
    t0 = time.time()
    eid = q.queue("w1")
    assert eid == "q1" and time.time() - t0 < 60
    st = q.status()
    assert st["switch"] is True and [(e["id"], e["branch"]) for e in st["entries"]] == [("q1", "w1")]
    assert st["entries"][0]["state"] in ("queued", "testing")
    until(lambda: q.entry("q1").get("state") == "testing", "q1 testing")
    r = env.run("land", "status", "q1")
    assert r.returncode == 0 and "testing" in r.stdout and "batch b1" in r.stdout, r.stdout
    q.hold.unlink()
    r = env.run("land", "status", "q1", "--wait", timeout=120)
    assert r.returncode == 0, r.stderr
    main = env.git("rev-parse", "main")
    assert f"landed w1 on main ({main[:12]})" in r.stdout
    assert (env.home / "bin/a.txt").read_text() == "a-w1\n"
    (row,) = [h for h in q.rows() if h.get("branch") == "w1"]
    assert row["landq"]["id"] == "q1" and row["landq"]["batch"] == "b1" and row["landq"]["size"] == 1
    assert row["land_cmd_start"] <= q.entry("q1")["since"] <= row["land_start"]
    assert main == env.git("rev-parse", "w1")  # one member on an unmoved main: main fast-forwards to the branch
    assert "landq/b-1" not in env.git("branch", "--list")  # the batch branch goes
    assert env.git("worktree", "list").count("\n") == 0


def test_a_batch_of_three_is_one_runner_call_one_deploy_three_rows_three_pokes(env, q):
    branch(env, "a0", "tools/q0.txt", "a0\n")
    for i in (1, 2, 3):
        branch(env, f"b{i}", f"tools/q{i}.txt", f"b{i}\n")
    q.hold.touch()
    q.queue("a0")  # holds the batch lane until hold goes
    until(lambda: q.entry("q1").get("state") == "testing", "a0 testing")
    ids = [q.queue(f"b{i}") for i in (1, 2, 3)]
    assert ids == ["q2", "q3", "q4"]
    q.hold.unlink()
    for eid in ids:
        r = env.run("land", "status", eid, "--wait", timeout=120)
        assert r.returncode == 0, r.stderr
    bs = q.batches()
    assert [[m["branch"] for m in b["members"]] for b in bs] == [["a0"], ["b1", "b2", "b3"]]
    b2 = bs[1]
    assert b2["outcome"] == "landed"
    runner_trees = q.runner_log.read_text().split()
    assert runner_trees.count(b2["tree"]) == 1 and len(runner_trees) == 2  # one runner call per batch
    rows = [h for h in q.rows() if (h.get("landq") or {}).get("batch") == b2["id"]]
    assert sorted(r["branch"] for r in rows) == ["b1", "b2", "b3"]
    assert len({r["release"] for r in rows}) == 1 and sum(1 for r in rows if "member_of" not in r["landq"]) == 1
    rel = rows[0]["release"]
    assert env.brain_log.read_text().count(f"after assistant land {rel}") == 1  # one deploy
    main = env.git("rev-parse", "main")
    commits = env.git("rev-list", "--reverse", f"{bs[0]['tip']}..main").split()
    assert len(commits) == 3 and commits[-1] == main
    by = {r["branch"]: r for r in rows}
    assert [by[f"b{i}"]["landq"]["sha"] for i in (1, 2, 3)] == commits  # each member's own last commit on main
    starts = [by[f"b{i}"]["landq"]["from"] for i in (1, 2, 3)]  # and where its own commits start (the CI tab's text)
    assert starts == [bs[0]["tip"][:12], commits[0][:12], commits[1][:12]]
    pokes = [ln.split() for ln in q.board_log.read_text().splitlines()]
    landed = {p[2]: p[4] for p in pokes if p[:2] == ["poke", "landed"]}
    assert {k: v for k, v in landed.items() if k != "a0"} == {"b1": commits[0], "b2": commits[1], "b3": commits[2]}
    for i, c in zip((1, 2, 3), commits, strict=True):  # the members' branches now point at their commit on main
        assert env.git("rev-parse", f"b{i}") == c
    assert all((env.home / f"bin/{n}").exists() is False for n in ("q1.txt",))  # unmanaged files stay out of ~/bin
    assert env.git("show", "main:tools/q3.txt") == "b3"


def test_a_red_batch_lands_the_others_and_names_only_the_red_branch(env, q):
    branch(env, "a0", "tools/q0.txt", "a0\n")
    branch(env, "r1", "tools/q1.txt", "r1\n")
    branch(env, "r2", "tools/red.txt", "red\n")
    branch(env, "r3", "tools/q3.txt", "r3\n")
    q.hold.touch()
    q.queue("a0")
    until(lambda: q.entry("q1").get("state") == "testing", "a0 testing")
    ids = [q.queue(n) for n in ("r1", "r2", "r3")]
    q.hold.unlink()
    until(lambda: all(q.entry(i).get("state") in ("landed", "red") for i in ids), "the batch settled", 150)
    assert [q.entry(i)["state"] for i in ids] == ["landed", "red", "landed"]
    red = q.entry(ids[1])
    assert red["rc"] == 6 and sorted(red["mates"]) == ["r1", "r3"]
    assert "/land-" not in red["red"] and "its worktree" in red["red"]  # never the batch's temp checkout
    outcomes = [([m["branch"] for m in b["members"]], b["outcome"]) for b in q.batches()]
    assert outcomes[1:] == [
        (["r1", "r2", "r3"], "split"),
        (["r1", "r2"], "split"),
        (["r1"], "landed"),
        (["r2"], "red"),  # its tree is the red r1+r2 tree: red without another run
        (["r3"], "landed"),
    ]
    assert len(q.runner_log.read_text().split()) == 3  # a0, r1, r3: a red CI ends a land before the runner's answer
    assert len((env.tmp / "ci-runs.log").read_text().split()) == 5  # a0, r1-3, r1-2, r1, r3: none for r2 alone
    notes = sorted((env.home / "inbox").glob("land-red-*.md"))
    assert len(notes) == 1, notes
    text = notes[0].read_text()
    assert "r2" in text.splitlines()[0] and "Batch-mates: r1, r3" in text and "not instructions" in text
    con = sqlite3.connect(env.home / ".local/state/assistant/events.db")
    try:
        ev = con.execute("SELECT kind, delivery, payload FROM events WHERE kind='land.red'").fetchall()
    finally:
        con.close()
    assert len(ev) == 1 and ev[0][1] == "beat", ev
    pl = json.loads(ev[0][2])
    assert (
        pl["entry"] == ids[1] and pl["branch"] == "r2" and pl["state"] == "red" and sorted(pl["mates"]) == ["r1", "r3"]
    )
    assert env.git("show", "main:tools/q3.txt") == "r3" and "red.txt" not in env.git("ls-tree", "main", "tools/")
    assert sorted(h["branch"] for h in q.rows()) == ["a0", "r1", "r3"]


def test_a_conflict_between_queued_branches_is_deferred_then_a_conflict(env, q):
    branch(env, "a0", "tools/q0.txt", "a0\n")
    branch(env, "c1", "tools/a.txt", "c1\n")
    branch(env, "c2", "tools/a.txt", "c2\n")
    q.hold.touch()
    q.queue("a0")
    until(lambda: q.entry("q1").get("state") == "testing", "a0 testing")
    assert q.queue("c1") == "q2"
    p = env.popen("land", "c2", "--wait")  # the waiter gets exit 5 (no note)
    until(lambda: q.entry("q3").get("state") == "queued", "c2 queued")
    q.hold.unlink()
    out, err = p.communicate(timeout=150)
    assert p.returncode == 5, (out, err)
    assert "q3 c2 conflict" in err and "conflicts with main" in err
    bs = q.batches()
    assert [[m["branch"] for m in b["members"]] for b in bs if b.get("members")][:2] == [["a0"], ["c1"]]
    assert q.entry("q3")["defer"] == 1 and q.entry("q2")["state"] == "landed"
    assert (env.home / "bin/a.txt").read_text() == "c1\n"
    assert not list((env.home / "inbox").glob("land-red-*.md"))


def test_a_gated_entry_runs_in_its_own_lane(env, q):
    """A gated path (skills/*) runs beside the batch lane: it lands while the batch lane is still held."""
    branch(env, "a0", "tools/q0.txt", "a0\n")
    branch(env, "g1", "skills/x/SKILL.md", "a skill\n")
    ci = env.tmp / "ci-gated.sh"  # only a0's tree waits for hold
    ci.write_text(
        "#!/bin/sh\n"
        f"i=0; while [ -e tools/q0.txt ] && [ -e {q.hold} ] && [ $i -lt 2400 ]; do sleep 0.05; i=$((i+1)); done\n"
    )
    env.env["ASSISTANT_LAND_CI"] = f"sh {ci}"
    q.hold.touch()
    q.queue("a0")
    until(lambda: q.entry("q1").get("state") == "testing", "a0 testing")
    r = env.run("land", "g1", "--wait", timeout=150)
    assert r.returncode == 0, r.stderr
    g = q.entry("q2")
    assert g["lane"] == "gated" and g["gated"] == ["skills"] and g["state"] == "landed"
    assert q.entry("q1")["state"] == "testing"  # the batch lane still holds a0
    (gb,) = [b for b in q.batches() if b["members"][0]["branch"] == "g1"]
    assert gb["lane"] == "gated"
    q.hold.unlink()
    r = env.run("land", "status", "q1", "--wait", timeout=150)
    assert r.returncode == 0, r.stderr
    assert env.git("show", "main:skills/x/SKILL.md") == "a skill" and env.git("show", "main:tools/q0.txt") == "a0"


def test_a_worker_killed_mid_batch_never_deploys_an_untested_tree(env, q):
    branch(env, "k1", "tools/a.txt", "a-k1\n")
    main0 = env.git("rev-parse", "main")
    q.hold.touch()
    q.queue("k1")
    b = until(lambda: next((x for x in q.batches() if x.get("pid")), None), "the batch's land started")
    w = json.loads(env.state("landq", "worker.json").read_text())
    os.kill(int(w["pid"]), signal.SIGKILL)
    os.killpg(int(b["pid"]), signal.SIGKILL)  # the land and its CI: its own session
    until(lambda: not _alive(b["pid"]) and not _alive(w["pid"]), "worker and land gone")
    assert env.git("rev-parse", "main") == main0 and not q.rows()
    assert (env.home / "bin/a.txt").read_text() == "a1\n"
    assert not q.passed.exists()  # its CI never passed
    q.hold.unlink()
    r = env.run("land", "status", "q1", "--wait", timeout=150)  # a new worker: back to the queue, tested, landed
    assert r.returncode == 0, r.stderr
    (row,) = q.rows()
    assert row["tree"] in q.passed.read_text().split()  # the deployed tree passed its CI
    bs = q.batches()
    assert [x["outcome"] for x in bs] == ["requeued", "landed"]
    assert (env.home / "bin/a.txt").read_text() == "a-k1\n"
    assert env.git("worktree", "list").count("\n") == 0 and "landq/" not in env.git("branch", "--list")


def test_switch_off_is_todays_path(env, q):
    env.env.pop("ASSISTANT_LAND_QUEUE")
    branch(env, "o1", "tools/a.txt", "a-o1\n")
    r = env.run("land", "o1", "--no-wait")  # --no-wait means nothing with the switch off
    assert r.returncode == 0, r.stderr
    assert "queued" not in r.stdout and "landed o1 on main" in r.stdout
    assert not env.state("landq", "entries").exists()
    (row,) = q.rows()
    assert row["branch"] == "o1" and "landq" not in row
    r = env.run("land", "status")
    assert r.returncode == 0 and "land queue: off" in r.stdout


def test_quick_check_red_and_rebase_conflict_fail_at_once(env, q):
    branch(env, "f1", "tools/a.txt", "f1\n")
    r = env.run("land", "f1", extra={"ASSISTANT_LANDQ_CHECK": "false"})
    assert r.returncode == 6 and "quick checks failed" in r.stderr
    assert not env.state("landq", "entries").exists()
    env.write("tools/a.txt", "main moved\n")
    env.commit("main moves")
    r = env.run("land", "f1")
    assert r.returncode == 5 and "git rebase main" in r.stderr
    r = env.run("land", "status", "q9")
    assert r.returncode == 3
