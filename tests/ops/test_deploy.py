import os
import stat
import threading

HEADER = "# managed: ~/opt/assistant — edit there, deploy with assistant land"


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_round_trip_drift_and_rollback(env):
    r = env.run("deploy")
    assert r.returncode == 0, r.stderr
    hello = env.home / "bin/hello"
    lines = hello.read_text().splitlines()
    assert lines[0] == "#!/bin/sh" and lines[1] == HEADER
    assert mode(hello) == 0o755 and (env.home / "bin/a.txt").read_text() == "a1\n"
    link = env.home / "bin/lib/core"
    assert link.is_symlink() and os.readlink(link).startswith("core.releases/")
    assert (link / "paths.py").is_file()
    v1 = {p: (p.read_bytes(), mode(p)) for p in (hello, env.home / "bin/a.txt")}
    v1_link = os.readlink(link)

    # v2 changes bytes, mode and core
    env.write("tools/hello.py", "#!/bin/sh\necho hello v2\n")
    env.write(
        "ops/deploy/manifest.toml",
        (env.repo / "ops/deploy/manifest.toml")
        .read_text()
        .replace('dest = "bin/a.txt"\nmode = "0644"', 'dest = "bin/a.txt"\nmode = "0600"'),
    )
    env.write("core/extra.py", "X = 2\n")
    env.commit("v2")
    assert env.run("deploy").returncode == 0
    assert mode(env.home / "bin/a.txt") == 0o600 and "v2" in hello.read_text()
    assert os.readlink(link) != v1_link and (link / "extra.py").is_file()
    assert env.run("drift").returncode == 0

    # a live edit is drift: deploy refuses, nothing changes
    hello.write_text(hello.read_text() + "# live edit\n")
    d = env.run("drift")
    assert d.returncode == 3 and "bin/hello: modified" in d.stdout
    env.write("tools/b.txt", "b3\n")
    env.commit("v3")
    r = env.run("deploy")
    assert r.returncode == 3 and "drift" in r.stderr
    assert (env.home / "bin/b.txt").read_text() == "b1\n"
    assert env.history()[-1]["refused"] == "drift"
    assert env.run("rollback").returncode == 3  # rollback over drift needs --force

    # rollback restores v1 bytes + modes + core link exactly
    r = env.run("rollback", "--force")
    assert r.returncode == 0, r.stderr
    for p, (data, m) in v1.items():
        assert p.read_bytes() == data and mode(p) == m
    assert os.readlink(link) == v1_link
    assert env.run("drift").returncode == 0
    assert env.deployed()["release"] == env.git("rev-parse", "--short=12", "HEAD~2")


def test_mode_drift_detected(env):
    assert env.run("deploy").returncode == 0
    os.chmod(env.home / "bin/hello", 0o700)
    d = env.run("drift")
    assert d.returncode == 3 and "mode 0700 != 0755" in d.stdout


def test_core_tamper_detected(env):
    assert env.run("deploy").returncode == 0
    (env.home / "bin/lib/core/paths.py").write_text("broken = True\n")
    d = env.run("drift")
    assert d.returncode == 3 and "core release modified" in d.stdout


def test_pycache_is_not_drift(env):
    assert env.run("deploy").returncode == 0
    pc = env.home / "bin/lib/core/__pycache__"
    pc.mkdir(exist_ok=True)  # the smoke import may already have written one
    (pc / "paths.cpython-311.pyc").write_bytes(b"x")
    assert env.run("drift").returncode == 0


def test_smoke_failure_rolls_back(env):
    assert env.run("deploy").returncode == 0
    hello = env.home / "bin/hello"
    before = (hello.read_bytes(), mode(hello), os.readlink(env.home / "bin/lib/core"))
    rel1 = env.deployed()["release"]
    env.write("tools/hello.py", "#!/bin/sh\nexit 1\n")
    env.write("core/new.py", "Y = 1\n")
    env.commit("broken")
    r = env.run("deploy")
    assert r.returncode == 4 and "smoke failed for bin/hello" in r.stderr
    assert (hello.read_bytes(), mode(hello), os.readlink(env.home / "bin/lib/core")) == before
    assert env.deployed()["release"] == rel1
    last = env.history()[-1]
    assert last["rolled_back"] and last["failed"] == ["bin/hello"]
    assert env.run("drift").returncode == 0


def test_first_deploy_rollback_removes_everything(env):
    assert env.run("deploy").returncode == 0
    r = env.run("rollback")
    assert r.returncode == 0, r.stderr
    assert not (env.home / "bin/hello").exists() and not (env.home / "bin/lib/core").is_symlink()
    assert env.deployed() == {}


def test_unmanaged_file_refused_then_adopted(env):
    (env.home / "bin").mkdir()
    old = env.home / "bin/hello"
    old.write_text("#!/bin/sh\necho hand-made\n")
    os.chmod(old, 0o750)
    r = env.run("deploy")
    assert r.returncode == 3 and "--adopt bin/hello" in r.stderr
    assert old.read_text() == "#!/bin/sh\necho hand-made\n"
    assert env.run("deploy", "--adopt", "bin/hello").returncode == 0
    assert HEADER in old.read_text()
    assert env.run("rollback").returncode == 0
    assert old.read_text() == "#!/bin/sh\necho hand-made\n" and mode(old) == 0o750


def test_rollback_to_kept_release(env):
    assert env.run("deploy").returncode == 0
    rel1 = env.deployed()["release"]
    env.write("tools/a.txt", "a2\n")
    env.commit("v2")
    assert env.run("deploy").returncode == 0
    r = env.run("rollback", "--to", rel1[:8])
    assert r.returncode == 0, r.stderr
    assert (env.home / "bin/a.txt").read_text() == "a1\n" and env.deployed()["release"] == rel1
    assert env.run("rollback", "--to", "deadbeef").returncode == 3


def test_brain_commit_before_and_after(env):
    assert env.run("deploy").returncode == 0
    lines = env.brain_log.read_text().splitlines()
    assert lines[0].startswith("before assistant deploy") and lines[-1].startswith("after assistant deploy")


def test_atomic_core_swap_and_pruning(env):
    assert env.run("deploy").returncode == 0
    target = env.home / "bin/lib/core/clock.py"
    errors, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                assert "def now" in target.read_text()
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(5):
            env.write("core/gen.py", f"N = {i}\n")
            env.commit(f"core {i}")
            assert env.run("deploy").returncode == 0
    finally:
        stop.set()
        t.join()
    assert errors == []
    rels = sorted(p.name for p in (env.home / "bin/lib/core.releases").iterdir())
    assert len(rels) == 3 and env.deployed()["release"] in rels
    assert len(list(env.state("releases").iterdir())) == 3
    assert not list((env.home / "bin/lib").glob(".core.*.tmp"))


def test_state_files_private(env):
    assert env.run("deploy").returncode == 0
    for p in ("deployed.json", "deploys.jsonl", "deploy.lock"):
        assert mode(env.state(p)) == 0o600


def test_refuses_real_home(env):
    from core import paths

    r = env.run("status", extra={"HOME": str(paths.real_home())})
    assert r.returncode == 3 and "real home" in r.stderr


def test_status_and_version(env):
    assert env.run("status").stdout.startswith("nothing deployed")
    assert env.run("deploy").returncode == 0
    s = env.run("status")
    assert s.returncode == 0 and "drift: none" in s.stdout and "1 ok deploys" in s.stdout
    assert env.deployed()["release"] in env.run("--version").stdout
