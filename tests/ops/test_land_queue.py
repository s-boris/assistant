"""The deploy lock is first come, first served: a ticket per waiter in deploy.queue/, only the front one tries the
flock, dead waiters' tickets are pruned, "queued, position N" while waiting, and the wait budget counts per position
(it restarts when a waiter moves up; exit 7 = the queue stood still that long)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from core import locks


def branch(env, name, rel, text):
    env.git("checkout", "-q", "-b", name, "main")
    env.write(rel, text)
    env.commit(name)
    env.git("checkout", "-q", "main")


def test_lands_get_the_lock_in_arrival_order(env):
    assert env.run("deploy").returncode == 0
    names = ["q1", "q2", "q3", "q4"]
    for i, n in enumerate(names):
        branch(env, n, f"tools/q{i}.txt", f"{n}\n")
    go = env.tmp / "go"  # the first land's CI holds the lock until every land has its ticket
    hold = f"i=0; while [ ! -e {go} ] && [ $i -lt 1200 ]; do sleep 0.05; i=$((i+1)); done"  # at most ~60 s
    slow_ci = {"ASSISTANT_LAND_CI": f"sh -c '{hold}'", "ASSISTANT_LAND_SERIAL": "1"}  # CI under the lock (old flow)
    seq = env.state("deploy.queue", ".seq")

    def taken() -> int:
        """Tickets handed out so far (.seq); a read between a taker's truncate and write is retried."""
        for _ in range(200):
            try:
                return int(seq.read_text())
            except FileNotFoundError:
                return 0
            except ValueError:
                time.sleep(0.01)
        raise AssertionError(f".seq stays unreadable: {seq.read_text()!r}")

    procs = []
    try:
        for n in names:
            t0 = taken()
            procs.append(env.popen("land", n, extra=slow_ci))
            end = time.monotonic() + 60  # the next land starts once this one holds its ticket: the order is fixed
            while taken() == t0 and procs[-1].poll() is None and time.monotonic() < end:
                time.sleep(0.05)
            assert taken() == t0 + 1, (n, taken(), t0, procs[-1].poll())
    finally:
        go.touch()
        outs = [p.communicate(timeout=120) for p in procs]
    for p, o in zip(procs, outs, strict=True):
        assert p.returncode == 0, o
    lands = sorted((h["land_start"], h["branch"]) for h in env.history() if h.get("op") == "land" and h.get("ok"))
    assert [b for _t, b in lands] == names  # served in the order they came, not whoever polled first
    assert "queued, position 1" in outs[1][0]
    assert not list(env.state("deploy.queue").glob("*.ticket"))  # every ticket gone with its waiter


def ticket(tool, n: int, pid: int, start: str, what: str = "other") -> None:
    q = tool._queue_dir()
    q.mkdir(parents=True, exist_ok=True)
    (q / f"{n}.ticket").write_text(json.dumps({"pid": pid, "start": start, "what": what, "since": 0}))
    (q / ".seq").write_text(str(n))


def test_dead_and_unreadable_tickets_are_pruned(tool, monkeypatch, capsys):
    monkeypatch.setenv("ASSISTANT_LOCK_WAIT", "5")
    gone = subprocess.Popen(["true"])
    gone.wait()
    ticket(tool, 1, gone.pid, "1")  # its waiter exited
    ticket(tool, 2, os.getpid(), "not-my-start")  # a reused pid
    (tool._queue_dir() / "3.ticket").write_text("{not json")
    (tool._queue_dir() / ".seq").write_text("3")
    t0 = time.monotonic()
    with tool.Lock("t"):
        assert time.monotonic() - t0 < 2
        assert not list(tool._queue_dir().glob("*.ticket"))
    assert "queued" not in capsys.readouterr().out  # a free lock with no one alive ahead: no wait, no line


@pytest.fixture
def held(tool):
    """The deploy lock held by someone else (its own open file description), released by calling the result."""
    fd = locks.acquire(tool.st("deploy.lock"), wait=0)
    assert fd is not None
    done = []

    def release():
        if not done:
            done.append(1)
            locks.release(fd)

    yield release
    release()


@pytest.fixture
def ahead(tool):
    """A live waiter at the front of the queue (a sleeping process's ticket)."""
    p = subprocess.Popen(["sleep", "60"])
    ps = tool._proc_stat(p.pid)
    assert ps
    ticket(tool, 1, p.pid, ps[2], "land ahead")
    yield p
    p.kill()
    p.wait()


def test_a_queue_that_stands_still_times_out_with_its_position(tool, monkeypatch, capsys, held, ahead):
    monkeypatch.setenv("ASSISTANT_LOCK_WAIT", "1")
    assert [t["what"] for t in tool.lock_queue()] == ["land ahead"]
    with pytest.raises(tool.Fail) as e:
        with tool.Lock("t"):
            pass
    assert e.value.code == 7 and "queue position 2 unchanged for 1 s" in str(e.value)
    assert "queued, position 2 (t)" in capsys.readouterr().out
    assert [t.name for t in tool._queue_dir().glob("*.ticket")] == ["1.ticket"]  # ours went, the live one stays


def test_moving_up_restarts_the_wait(tool, monkeypatch, capsys, held, ahead):
    """2.4 s in all with a 1.6 s budget: no timeout, since the waiter moved up after 1 s."""
    monkeypatch.setenv("ASSISTANT_LOCK_WAIT", "1.6")
    threading.Timer(1.0, ahead.kill).start()  # the waiter ahead dies: its ticket is pruned
    threading.Timer(2.4, held).start()
    t0 = time.monotonic()
    with tool.Lock("t"):
        took = time.monotonic() - t0
    out = capsys.readouterr().out
    assert took > 1.6, took
    assert "queued, position 2 (t)" in out and "queued, position 1 (t)" in out
    assert not list(tool._queue_dir().glob("*.ticket"))


def test_status_lists_the_queue(tool, monkeypatch, capsys, ahead):
    assert tool.status(True) == 0
    info = json.loads(capsys.readouterr().out)
    assert [(q["pos"], q["what"]) for q in info["queue"]] == [(1, "land ahead")]
    assert info["legacy_red"] == {}


def test_land_writes_its_phase_for_the_dashboard_and_drops_it(env):
    """The pre-lock phase (rebase, CI, runner, waiting for lock) is a record in deploy.queue/land-<pid>.phase, so the
    dash's land queue says what a land does instead of "starting"; the record goes when the land ends."""
    assert env.run("deploy").returncode == 0
    branch(env, "ph1", "tools/ph.txt", "ph\n")
    q = env.state("deploy.queue")
    seen = env.tmp / "seen.json"
    ci = {"ASSISTANT_LAND_CI": f"sh -c 'cat {q}/land-*.phase > {seen}'"}
    r = env.run("land", "ph1", extra=ci)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads(seen.read_text())
    assert rec["what"] == "land ph1" and rec["phase"] == "CI local" and rec["rerun"] == 0
    assert rec["pid"] > 0 and rec["start"] and rec["since"] >= rec["land_since"] > 0
    assert not list(q.glob("land-*.phase"))
