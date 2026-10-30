"""`assistant ci` cleans up on every exit path and sweeps what killed runs left: the run dir goes and what the tests
left running (test Postgres detached by pg_ctl, hubd, helpers) stops on green, red, timeout, SIGTERM and SIGINT;
pytest's basetemp is inside the run dir; the start-up sweep removes stale assistant-ci.* dirs, stops only orphan
test Postgres inside them (ppid 1) and never touches a dir a live process holds (another builder's run)."""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ALL_BUT_PYTEST = "sync,ruff,pyright,suites,ast,schemas,ratchet,inbox-lint,secretscan,precheck,bun"
OLD = 4 * 3600

SUITE = """
import os, pathlib, subprocess, sys, time
import pytest

OUT = pathlib.Path(os.environ["CI_PROBE"])
MODE = os.environ.get("CI_MODE", "pass")


@pytest.fixture
def torn():
    yield
    (OUT / "teardown").write_text("1")


def test_leaves_a_detached_child(tmp_path, torn):
    (OUT / "tmp_path").write_text(str(tmp_path))
    (OUT / "worker").write_text(os.environ.get("PYTEST_XDIST_WORKER", ""))
    # what pg_ctl does to Postgres: a child in its own session that outlives pytest (it holds the run dir by env)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    (OUT / "leftover.pid").write_text(str(p.pid))
    (OUT / "started").write_text("1")
    if MODE == "hang":
        time.sleep(300)
    assert MODE != "fail"
"""


def _state(pid: int) -> str | None:
    try:
        s = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return s[s.rindex(")") + 2 :].split()[0]


def _gone(pid: int, wait: float = 10.0) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if _state(pid) in (None, "Z", "X"):
            return True
        time.sleep(0.05)
    return False


def _ppid(pid: int) -> int:
    s = Path(f"/proc/{pid}/stat").read_text()
    return int(s[s.rindex(")") + 2 :].split()[1])


def _kids(pid: int) -> list[int]:
    out = []
    for d in os.listdir("/proc"):
        try:
            if d.isdigit() and _ppid(int(d)) == pid:
                out.append(int(d))
        except (OSError, ValueError):
            pass
    return out


def _kill(*procs) -> None:
    """SIGKILL each (a pid or a Popen) and its children (the fake postmasters' backends)."""
    for p in procs:
        pid = p if isinstance(p, int) else p.pid
        for k in [pid, *_kids(pid)]:
            try:
                os.kill(k, signal.SIGKILL)
            except OSError:
                pass
        if not isinstance(p, int):
            p.wait(5)


def _age(d: Path, secs: float = OLD) -> None:
    t = time.time() - secs
    for c in d.iterdir():
        os.utime(c, (t, t), follow_symlinks=False)
    os.utime(d, (t, t))


def _sleeper(cwd: Path | None = None, env: dict | None = None, argv: list[str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        argv or ["sleep", "300"], cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL
    )


def _orphan(argv: list[str], cwd: Path) -> int:
    """argv started by a shell that exits at once: the process is re-parented (ppid 1 unless a subreaper)."""
    r = subprocess.run(
        ["/bin/sh", "-c", 'cd "$1"; shift; "$@" </dev/null >/dev/null 2>&1 & echo $!', "sh", str(cwd), *argv],
        capture_output=True,
        text=True,
        timeout=10,
    )
    pid = int(r.stdout.strip())
    end = time.monotonic() + 5
    while time.monotonic() < end and Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0] != argv[0].encode():
        time.sleep(0.02)  # the fork has not exec'd yet
    return pid


def _child_of(pid: int) -> int:
    end = time.monotonic() + 5
    while time.monotonic() < end:
        kids = _kids(pid)
        if kids:
            return kids[0]
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} has no child")


def _fake_postgres(tmp_path: Path) -> Path:
    """`postgres -c 'sleep 300; :' -D DIR`: a shell under Postgres's name and argv shape, with a child (a backend)."""
    b = tmp_path / "pgbin"
    b.mkdir(exist_ok=True)
    pg = b / "postgres"
    if not pg.exists():
        pg.symlink_to("/bin/sh")
    return pg


# ---------------------------------------------------------------- the whole ci, end to end


@pytest.fixture
def suite(env, tmp_path):
    env.write("pytest.ini", "[pytest]\ntestpaths = tests\naddopts = -q -p no:cacheprovider\n")
    env.write("tests/test_leftover.py", SUITE)
    env.commit("a suite that leaves a process behind")
    (tmp_path / "probe").mkdir()
    (tmp_path / "ttmp").mkdir()
    (tmp_path / "tc").mkdir()
    return tmp_path


def _extra(t: Path, mode: str, **more) -> dict:
    return {
        "TMPDIR": str(t / "ttmp"),
        "CI_PROBE": str(t / "probe"),
        "CI_MODE": mode,
        "ASSISTANT_NETGUARD": "none",
        **more,
    }


def _ci_args(env, t: Path) -> list[str]:
    return ["ci", "--path", str(env.repo), "--skip", ALL_BUT_PYTEST, "--no-stamp", "--toolchain", str(t / "tc")]


def _run_dirs(t: Path) -> list[str]:
    return glob.glob(str(t / "ttmp" / "assistant-ci.*"))


def _assert_clean(t: Path) -> None:
    probe = t / "probe"
    assert _gone(int((probe / "leftover.pid").read_text())), "the detached test child still runs"
    assert not _run_dirs(t), "the run dir is still there"


def test_green_run_reaps_leftovers_and_removes_its_dir(env, suite):
    t = suite
    r = env.run(*_ci_args(env, t), extra=_extra(t, "pass"), timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "stopped 1 leftover process(es)" in r.stdout
    _assert_clean(t)
    tp = (t / "probe" / "tmp_path").read_text()
    assert "/assistant-ci." in tp and "/py3.13/bt/" in tp, "pytest's basetemp is inside the run dir"


def test_red_run_cleans_up_too(env, suite):
    t = suite
    r = env.run(*_ci_args(env, t), extra=_extra(t, "fail"), timeout=180)
    assert r.returncode == 6, r.stdout + r.stderr
    assert "FAIL pytest-3.13" in r.stdout
    _assert_clean(t)


def test_pytest_timeout_interrupts_the_group_then_cleans_up(env, suite):
    t = suite
    t0 = time.monotonic()
    r = env.run(*_ci_args(env, t), extra=_extra(t, "hang", ASSISTANT_CI_PYTEST_TIMEOUT="4"), timeout=180)
    assert r.returncode == 6, r.stdout + r.stderr
    assert "FAIL timeout" in r.stdout
    assert time.monotonic() - t0 < 90
    assert (t / "probe" / "teardown").exists(), "pytest got SIGINT first: its fixtures tore down"
    _assert_clean(t)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_to_ci_cleans_up(env, suite, sig):
    t = suite
    p = env.popen(*_ci_args(env, t), extra=_extra(t, "hang"))
    try:
        end = time.monotonic() + 90
        while time.monotonic() < end and not (t / "probe" / "started").exists() and p.poll() is None:
            time.sleep(0.1)
        assert (t / "probe" / "started").exists(), "the suite never started"
        p.send_signal(sig)
        out, err = p.communicate(timeout=90)
    finally:
        _kill(p)
    assert p.returncode == 128 + sig, out + err
    assert "FAIL stopped" in out and "Traceback" not in err
    assert (t / "probe" / "teardown").exists()
    _assert_clean(t)


def test_ci_start_sweeps_stale_dirs_but_never_a_live_one(env, suite):
    t = suite
    tt = t / "ttmp"
    stale, young, held = (tt / f"assistant-ci.{n}" for n in ("stale0", "young0", "held0"))
    for d in (stale, young, held):
        (d / "pytest-3.13").mkdir(parents=True)
    holder = _sleeper(cwd=held / "pytest-3.13")
    try:
        _age(stale)
        _age(held)
        r = env.run(*_ci_args(env, t), extra=_extra(t, "pass"), timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"sweep: removed {stale}" in r.stdout
        assert f"sweep: kept {held}: in use by pid {holder.pid}" in r.stdout
        assert not stale.exists() and young.is_dir() and held.is_dir()
        assert holder.poll() is None, "a process of another run is never touched"
    finally:
        _kill(holder)


# ---------------------------------------------------------------- the sweep and the reap, unit


def test_sweep_keeps_young_owned_and_held_dirs(tool, tmp_path):
    root = tmp_path / "t"
    dirs = {n: root / f"assistant-ci.{n}" for n in ("old", "young", "owned", "cwd", "env", "fd", "argv")}
    for d in dirs.values():
        (d / "tmp").mkdir(parents=True)
    me = tool._proc_stat(os.getpid())
    (dirs["owned"] / tool.CI_OWNER).write_text(f'{{"pid": {os.getpid()}, "start": "{me[2]}"}}')
    (root / "assistant-ci.file").write_text("not a dir")
    fd = open(dirs["fd"] / "tmp" / "open.log", "w")  # noqa: SIM115 (held open across the sweep)
    holders = [
        _sleeper(cwd=dirs["cwd"] / "tmp"),
        _sleeper(env={"PATH": os.environ["PATH"], "TMPDIR": str(dirs["env"] / "tmp")}),
        subprocess.Popen(["sleep", "300"], stdout=fd),
        _sleeper(argv=["sh", "-c", "sleep 300; :", str(dirs["argv"] / "tmp" / "x")]),
    ]
    try:
        for n, d in dirs.items():
            if n != "young":
                _age(d)
        lines = tool._ci_sweep(root, grace=1.0)
        assert f"removed {dirs['old']}" in lines and len(lines) == 5, lines
        assert not dirs["old"].exists()
        for n in ("young", "owned", "cwd", "env", "fd", "argv"):
            assert dirs[n].is_dir(), n
        assert all(h.poll() is None for h in holders), "the sweep never signals a holder"
        assert (root / "assistant-ci.file").exists()
    finally:
        fd.close()
        _kill(*holders)


def test_sweep_stops_only_orphan_test_postgres(tool, tmp_path):
    root = tmp_path / "t"
    gone, kept = root / "assistant-ci.orphan", root / "assistant-ci.child"
    for d in (gone, kept):
        (d / "pgdata").mkdir(parents=True)
    other = tmp_path / "elsewhere" / "pgdata"
    other.mkdir(parents=True)
    pg = _fake_postgres(tmp_path)
    orphan = _orphan([str(pg), "-c", "sleep 300; :", "-D", str(gone / "pgdata")], gone / "pgdata")
    stranger = _orphan([str(pg), "-c", "sleep 300; :", "-D", str(other)], other)  # never ours
    child = _sleeper(argv=[str(pg), "-c", "sleep 300; :", "-D", str(kept / "pgdata")], cwd=kept / "pgdata")
    try:
        if _ppid(orphan) != 1:
            pytest.skip("a subreaper adopts orphans here: no ppid-1 Postgres to sweep")
        backend = _child_of(orphan)
        _age(gone)
        _age(kept)
        lines = tool._ci_sweep(root, grace=1.0)
        assert f"removed {gone} (stopped orphan test Postgres pid {orphan})" in lines, lines
        assert any(ln.startswith(f"kept {kept}: in use by pid") and str(child.pid) in ln for ln in lines), lines
        assert _gone(orphan) and _gone(backend), "the orphan postmaster and its backend stopped"
        assert not gone.exists() and kept.is_dir()
        assert child.poll() is None, "a Postgres with a live parent is never touched"
        assert _state(stranger) not in (None, "Z"), "a Postgres outside assistant-ci.* is never touched"
    finally:
        _kill(child, orphan, stranger)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tc_pg() -> str:
    """The CI toolchain's Postgres root in the hub runner (tests/hubstore/hubfix.py does the same), else ""."""
    tc = os.environ.get("ASSISTANT_CI_TOOLCHAIN", "")
    return os.path.join(tc, "pg") if tc and os.path.isdir(os.path.join(tc, "pg")) else ""


def _pg_bin() -> Path | None:
    got = sorted(glob.glob(_tc_pg() + "/usr/lib/postgresql/*/bin/initdb"), key=lambda p: int(Path(p).parents[1].name))
    return Path(got[-1]).parent if got else None


def test_sweep_stops_a_real_orphan_postgres(tool, tmp_path):
    b = _pg_bin()
    if b is None:
        pytest.skip("no Postgres binaries")
    root = tmp_path / "t"
    d = root / "assistant-ci.realpg"
    data = d / "pgdata"
    d.mkdir(parents=True)
    env = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    if _tc_pg():  # the toolchain's libraries for Postgres only (never the runner's Python), a passwd entry for initdb
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hubstore"))
        import hubfix

        env = hubfix.pg_env()
    subprocess.run([str(b / "initdb"), "-D", str(data), "-U", "t", "--auth=trust", "--no-instructions", "--no-sync"],
                   check=True, capture_output=True, env=env, timeout=120)  # fmt: skip
    subprocess.run([str(b / "pg_ctl"), "-D", str(data), "start", "-w", "-t", "60", "-l", str(d / "pg.log"), "-o",
                    f"-p {_free_port()} -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c fsync=off"],
                   check=True, capture_output=True, env=env, timeout=90)  # fmt: skip
    pid = int((data / "postmaster.pid").read_text().split()[0])
    try:
        if _ppid(pid) != 1:
            pytest.skip("a subreaper adopts orphans here")
        lines = tool._ci_sweep(root, age=0, grace=10.0)
        assert lines == [f"removed {d} (stopped orphan test Postgres pid {pid})"], lines
        assert _gone(pid) and not d.exists()
    finally:
        _kill(pid)


def test_reap_stops_what_holds_the_run_dir_and_nothing_else(tool, tmp_path):
    run_dir = tmp_path / "assistant-ci.mine"
    (run_dir / "tmp").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    pg = _fake_postgres(tmp_path)
    inside = _orphan(["sleep", "300"], run_dir / "tmp")
    postmaster = _orphan([str(pg), "-c", "sleep 300; :", "-D", str(run_dir / "pgdata")], run_dir / "tmp")
    outside = _sleeper(cwd=tmp_path / "other")
    by_env = _sleeper(cwd=tmp_path / "other", env={"PATH": os.environ["PATH"], "HOME": str(run_dir / "home")})
    mention = _sleeper(cwd=tmp_path / "other", argv=["sh", "-c", f"ls {run_dir} >/dev/null; sleep 300; :"])
    try:
        lines = tool._ci_reap(run_dir, grace=1.0)
        assert sorted(int(ln.split()[2]) for ln in lines) == sorted([inside, postmaster, by_env.pid]), lines
        assert _gone(inside) and _gone(postmaster) and _gone(by_env.pid)
        assert outside.poll() is None
        assert mention.poll() is None, "a shell that only names the dir (someone's `ls`) is never touched"
    finally:
        _kill(inside, postmaster, outside, by_env, mention)


# ---------------------------------------------------------------- child runners


def test_run_group_interrupts_then_kills_the_whole_group(tool, tmp_path):
    mark, gc = tmp_path / "sigint", tmp_path / "grandchild.pid"
    code = (
        "import signal, subprocess, sys, time\n"
        "g = subprocess.Popen(['sh', '-c', 'trap \"\" INT; sleep 300'])\n"
        f"open({str(gc)!r}, 'w').write(str(g.pid))\n"
        f"signal.signal(signal.SIGINT, lambda *a: (open({str(mark)!r}, 'w').write('1'), sys.exit(0)))\n"
        "time.sleep(300)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        tool.run_group([sys.executable, "-I", "-c", code], timeout=3, grace=2.0)
    assert mark.exists(), "SIGINT first"
    assert _gone(int(gc.read_text())), "the group's SIGINT-deaf member got SIGKILL"


def test_run_child_gives_sigterm_before_sigkill(tool, tmp_path):
    mark = tmp_path / "sigterm"
    code = (
        "import signal, sys, time\n"
        f"signal.signal(signal.SIGTERM, lambda *a: (open({str(mark)!r}, 'w').write('1'), sys.exit(0)))\n"
        "time.sleep(300)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        tool.run_child([sys.executable, "-I", "-c", code], timeout=2, grace=10.0)
    assert mark.exists()


def test_bg_group_times_out_like_run_group(tool, tmp_path):
    mark, gc = tmp_path / "sigint", tmp_path / "grandchild.pid"
    code = (
        "import signal, subprocess, sys, time\n"
        "g = subprocess.Popen(['sh', '-c', 'trap \"\" INT; sleep 300'])\n"
        f"open({str(gc)!r}, 'w').write(str(g.pid))\n"
        f"signal.signal(signal.SIGINT, lambda *a: (open({str(mark)!r}, 'w').write('1'), sys.exit(0)))\n"
        "time.sleep(300)\n"
    )
    g = tool.BgGroup([sys.executable, "-I", "-c", code], timeout=3, grace=2.0)
    with pytest.raises(subprocess.TimeoutExpired):
        g.wait()
    assert mark.exists(), "SIGINT first"
    assert _gone(int(gc.read_text())), "the group's SIGINT-deaf member got SIGKILL"


def test_bg_group_stop_interrupts_then_kills(tool, tmp_path):
    mark, gc = tmp_path / "sigint", tmp_path / "grandchild.pid"
    code = (
        "import signal, subprocess, sys, time\n"
        "g = subprocess.Popen(['sh', '-c', 'trap \"\" INT; sleep 300'])\n"
        f"open({str(gc)!r}, 'w').write(str(g.pid))\n"
        f"signal.signal(signal.SIGINT, lambda *a: (open({str(mark)!r}, 'w').write('1'), sys.exit(0)))\n"
        "print('up', flush=True)\n"
        "time.sleep(300)\n"
    )
    g = tool.BgGroup([sys.executable, "-I", "-c", code], timeout=300, grace=2.0)
    end = time.monotonic() + 30
    while time.monotonic() < end and not gc.exists():
        time.sleep(0.05)
    t0 = time.monotonic()
    g.stop()
    assert time.monotonic() - t0 < 20
    assert mark.exists(), "SIGINT first"
    assert _gone(int(gc.read_text())), "the group's SIGINT-deaf member got SIGKILL"
    r = g.wait()  # collected, not a timeout
    assert r.stdout.startswith("up")


def test_bg_groups_run_at_once(tool):
    code = "import time; time.sleep(2); print('done')"
    t0 = time.monotonic()
    gs = [tool.BgGroup([sys.executable, "-I", "-c", code], timeout=60) for _ in range(3)]
    rs = [g.wait() for g in gs]
    assert time.monotonic() - t0 < 5.5, "three 2 s steps at once, not one after the other"
    assert all(r.returncode == 0 and r.stdout.strip() == "done" for r in rs)
    assert all(1.5 < g.secs < 5.5 for g in gs)


def test_xdist_args(tool, monkeypatch, tmp_path):
    py, env = Path(sys.executable), dict(os.environ)
    monkeypatch.delenv("ASSISTANT_CI_PYTEST_WORKERS", raising=False)
    monkeypatch.setattr(tool, "_mem_cap", lambda: None)
    monkeypatch.setattr(tool.os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    has = importlib.util.find_spec("xdist") is not None  # a toolchain older than the lock that added it: serial
    monkeypatch.setattr(tool, "_host_cpus", lambda: 4.0)  # the hub runner: its cgroup quota
    assert tool._xdist_args(py, env, toolchain=True) == (["-n", "4", "--dist", tool.PYTEST_DIST] if has else [])
    assert tool._xdist_args(py, env, toolchain=True, nfiles=1) == [], "one file: serial"
    monkeypatch.setattr(tool, "_host_cpus", lambda: 32.0)
    want = ["-n", str(tool.PYTEST_WORKERS), "--dist", tool.PYTEST_DIST] if has else []
    assert tool._xdist_args(py, env, toolchain=False) == want
    monkeypatch.setenv("ASSISTANT_CI_PYTEST_WORKERS", "0")
    assert tool._xdist_args(py, env, toolchain=False) == []
    monkeypatch.setenv("ASSISTANT_CI_PYTEST_WORKERS", "3")
    assert tool._xdist_args(py, env, toolchain=True) == (["-n", "3", "--dist", tool.PYTEST_DIST] if has else [])
    bare = tmp_path / "bare-python"
    bare.write_text("#!/bin/sh\nexit 1\n")  # an interpreter without xdist: serial
    bare.chmod(0o755)
    assert tool._xdist_args(bare, env, toolchain=False) == []


def test_step_lines_keep_their_order_while_pytest_runs_beside_them(env, suite):
    """The suite runs in the background (here on 2 xdist workers when installed); schemas finishes first but its
    line and report entry come after pytest's, as in a serial run; the report carries each step's own seconds."""
    t = suite
    rep = t / "report.json"
    args = ["ci", "--path", str(env.repo), "--skip", ALL_BUT_PYTEST.replace(",schemas", ""), "--no-stamp",
            "--toolchain", str(t / "tc"), "--report", str(rep)]  # fmt: skip
    r = env.run(*args, extra=_extra(t, "pass", ASSISTANT_CI_PYTEST_WORKERS="2"), timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith(("ok  ", "FAIL"))]
    assert [ln.split()[1] for ln in lines] == ["pytest-3.13", "schemas"], lines
    assert "1 passed" in lines[0] and "stopped 1 leftover process(es)" in lines[0]
    if importlib.util.find_spec("xdist") is not None:
        assert (t / "probe" / "worker").read_text().startswith("gw"), "the test ran on an xdist worker"
    steps = json.loads(rep.read_text())["steps"]
    assert [s["name"] for s in steps] == ["pytest-3.13", "schemas"]
    assert steps[0]["secs"] > 0
    _assert_clean(t)


def test_xdist_workers_get_their_own_home_and_tmpdir():
    """In `assistant ci` (ASSISTANT_CI_WORKER_DIRS=1) each xdist worker has its own HOME and TMPDIR inside the
    run's (tests/conftest.py): no two workers share a state file, a lock or a fixed temp name."""
    w = os.environ.get("PYTEST_XDIST_WORKER")
    if not w or os.environ.get("ASSISTANT_CI_WORKER_DIRS") != "1":
        pytest.skip("not an xdist worker of assistant ci")
    assert Path(os.environ["HOME"]).name == w and Path(os.environ["TMPDIR"]).name == w
    assert Path(os.environ["HOME"]).parent.name == "home"
    assert tempfile.gettempdir() == os.environ["TMPDIR"]
