"""H12: the ci container's runner (ciworker) against a temp spool, a fake release + toolchain and a fake CI driver;
the protected-path verifier (hubverify). No hub, no network, no real toolchain."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HUB = REPO / "packages" / "hub"


def _mod(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_h12test", HUB / f"{name}.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


DRIVER = r"""
import json, os, subprocess, sys
a = sys.argv[1:]
rep = a[a.index("--report") + 1]
path = a[a.index("--path") + 1]
head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
ok = os.path.isdir(os.environ["ASSISTANT_CI_TOOLCHAIN"]) and os.environ.get("ASSISTANT_NETGUARD") == "none"
fail = os.path.exists(os.path.join(path, "FAIL"))
steps = [{"name": "ruff", "ok": True, "detail": "clean", "secs": 0.1},
         {"name": "pytest-3.13", "ok": ok and not fail, "detail": "--skip " + a[a.index("--skip") + 1], "secs": 0.2}]
json.dump({"ok": ok and not fail, "head": head, "steps": steps, "secs": 0.3}, open(rep, "w"))
sys.exit(0 if ok and not fail else 6)
"""


def make_release(root: Path) -> Path:
    rel = root / "releases" / "abcdef123456"
    (rel / "app" / "ci").mkdir(parents=True)
    shutil.copy(HUB / "ciworker.py", rel / "app" / "ciworker.py")
    (rel / "app" / "ci" / "assistant.py").write_text(DRIVER)
    tc = root / "releases" / "toolchains" / "0123456789ab"
    (tc / "bin").mkdir(parents=True)
    (tc / "bin" / "tool").write_text("x\n")
    man = {"id": "0123456789ab", "files": {"bin/tool": _sha(tc / "bin" / "tool")}}
    (tc / "manifest.json").write_text(json.dumps(man))
    meta = {
        "format": 1,
        "rid": rel.name,
        "ci": {"toolchain": "0123456789ab", "manifest_sha256": _sha(tc / "manifest.json")},
    }
    (rel / "release.json").write_text(json.dumps(meta))
    return rel


def git_repo(d: Path) -> tuple[str, str, Path]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True, env=env)
    (d / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(d), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "a"], check=True, env=env)
    head = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout
    b = d.parent / "x.bundle"
    subprocess.run(["git", "-C", str(d), "bundle", "create", "-q", str(b), "HEAD", "main"], check=True, env=env)
    return head, tree.strip(), b


def _hb(spool: Path) -> dict:
    try:
        return json.loads((spool / "ci-heartbeat.json").read_text())
    except (OSError, ValueError):
        return {}


def test_runner_runs_jobs_and_survives_a_bad_one(tmp_path):
    rel = make_release(tmp_path)
    _tc_git(rel)  # the runner has git only in its toolchain
    spool = tmp_path / "spool"
    for d in ("jobs", "run", "results"):
        (spool / "ci" / d).mkdir(parents=True)
    head, tree, bundle = git_repo(tmp_path / "repo")
    jobs = spool / "ci" / "jobs"
    shutil.copy(bundle, jobs / "aaaaaaaaaaaaaaaa.bundle")
    (jobs / "aaaaaaaaaaaaaaaa.json").write_text(
        json.dumps({"id": "aaaaaaaaaaaaaaaa", "commit": head, "tree": tree, "base": head, "schema": "t_x"})
    )
    shutil.copy(bundle, jobs / "bbbbbbbbbbbbbbbb.bundle")
    (jobs / "bbbbbbbbbbbbbbbb.json").write_text(
        json.dumps({"id": "bbbbbbbbbbbbbbbb", "commit": head, "tree": "0" * 40})
    )
    env = {
        "PATH": os.environ["PATH"],
        "HUB_SPOOL": str(spool),
        "HUB_RELEASE": rel.name,
        "HUB_HEARTBEAT": str(tmp_path / "hb"),
        "HUB_CI_WORK": str(tmp_path / "work"),  # not /tmp
    }
    p = subprocess.Popen(
        [sys.executable, "-I", "-B", str(rel / "app" / "ciworker.py")],
        env=env,
        cwd=rel,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        res = spool / "ci" / "results"
        end = time.monotonic() + 60
        while time.monotonic() < end and len(list(res.glob("*.json"))) < 2:
            time.sleep(0.2)
        good = json.loads((res / "aaaaaaaaaaaaaaaa.json").read_text())
        bad = json.loads((res / "bbbbbbbbbbbbbbbb.json").read_text())
        # the beater thread's first beat can land after main's post-check beat (toolchain_ok still None); the
        # next one comes EVERY (5 s) later: wait for it instead of reading the file once
        end = time.monotonic() + 30
        while time.monotonic() < end and _hb(spool).get("toolchain_ok") is not True:
            time.sleep(0.2)
    finally:
        p.terminate()
        out = p.communicate(timeout=10)[0].decode()
    assert good["ok"] is True, (good, out)
    assert good["tree"] == tree and good["schema"] == "t_x" and good["runner"]["toolchain"] == "0123456789ab"
    assert "secretscan" in good["steps"][1]["detail"] and "precheck" in good["steps"][1]["detail"]
    assert bad["ok"] is False and "another tree" in bad["error"]
    assert not list(jobs.iterdir()) and not list((spool / "ci" / "run").iterdir())
    hb = _hb(spool)
    assert hb["release"] == rel.name and hb["toolchain_ok"] is True
    assert (tmp_path / "hb").exists()


def test_runner_refuses_a_changed_toolchain(tmp_path):
    rel = make_release(tmp_path)
    (tmp_path / "releases" / "toolchains" / "0123456789ab" / "bin" / "tool").write_text("changed\n")
    w = _mod("ciworker")
    w.REL_DIR = str(rel)
    tc, why = w.toolchain()
    assert tc is None and "changed" in why
    res = w.run_job({"id": "c" * 16, "commit": "0" * 40, "tree": "0" * 40}, "/nonexistent.bundle")
    assert res["ok"] is False and "toolchain" in res["error"]


def test_runner_recovers_jobs_of_a_killed_run(tmp_path, monkeypatch):
    w = _mod("ciworker")
    monkeypatch.setenv("HUB_SPOOL", str(tmp_path))
    for d in ("jobs", "run", "results"):
        (tmp_path / "ci" / d).mkdir(parents=True)
    (tmp_path / "ci" / "run" / ("d" * 16 + ".json")).write_text(json.dumps({"id": "d" * 16, "commit": "1" * 40}))
    w.recover()
    r = json.loads((tmp_path / "ci" / "results" / ("d" * 16 + ".json")).read_text())
    assert r["ok"] is False and "restarted" in r["error"]
    assert not list((tmp_path / "ci" / "run").iterdir())


def test_runner_requeues_jobs_a_restart_cut_off_once(tmp_path, monkeypatch):
    """A hub deploy restarts the runner mid-job: the job goes back to the queue at its old place (its bundle is
    still there) and the land waiting on it waits on; cut off a second time, it fails (no crash loop)."""
    w = _mod("ciworker")
    monkeypatch.setenv("HUB_SPOOL", str(tmp_path))
    for d in ("jobs", "run", "results"):
        (tmp_path / "ci" / d).mkdir(parents=True)
    run, jobs = tmp_path / "ci" / "run", tmp_path / "ci" / "jobs"
    (run / ("e" * 16 + ".json")).write_text(json.dumps({"id": "e" * 16, "commit": "1" * 40, "created": 1000.0}))
    (jobs / ("e" * 16 + ".bundle")).write_bytes(b"b")
    (jobs / ("f" * 16 + ".json")).write_text(json.dumps({"id": "f" * 16, "commit": "2" * 40}))  # queued later
    w.recover()
    back = json.loads((jobs / ("e" * 16 + ".json")).read_text())
    assert back["requeued"] == 1 and not list(run.iterdir()) and not list((tmp_path / "ci" / "results").iterdir())
    assert (jobs / ("e" * 16 + ".json")).stat().st_mtime == 1000.0
    assert w.next_job()[0]["id"] == "e" * 16  # first again: its old place
    w.recover()  # cut off again
    r = json.loads((tmp_path / "ci" / "results" / ("e" * 16 + ".json")).read_text())
    assert r["ok"] is False and "restarted" in r["error"] and not (jobs / ("e" * 16 + ".bundle")).exists()


def test_runner_skips_stale_and_superseded_jobs(tmp_path, monkeypatch):
    """A job queued past QUEUE_MAX_AGE (no land waits that long) or with a newer job of its branch waiting: not run;
    its result says why and is not ok (its bundle still goes into the mirror: skip_job, toolchain permitting)."""
    w = _mod("ciworker")
    monkeypatch.setenv("HUB_SPOOL", str(tmp_path))
    (tmp_path / "ci" / "jobs").mkdir(parents=True)
    now = 10_000.0
    old = {"id": "a" * 16, "branch": "x", "created": now - w.QUEUE_MAX_AGE - 1}
    assert w.skip_why(old, now).startswith("skipped: queued")
    mine = {"id": "b" * 16, "branch": "x", "created": now - 60}
    assert w.skip_why(mine, now) == ""
    newer = {"id": "c" * 16, "branch": "x", "created": now - 10}
    (tmp_path / "ci" / "jobs" / ("c" * 16 + ".json")).write_text(json.dumps(newer))
    assert w.skip_why(mine, now).startswith("skipped: superseded by job " + "c" * 16)
    assert w.skip_why({**mine, "branch": "y"}, now) == ""  # another branch's job is no reason
    assert w.skip_why({**mine, "branch": ""}, now) == ""  # no branch: never superseded
    w.REL_DIR = str(tmp_path / "no-release")  # no toolchain: no mirror fetch, still a result
    res = w.skip_job({"id": "b" * 16, "commit": "1" * 40, "tree": "2" * 40}, "/nonexistent.bundle", "skipped: x")
    assert res["ok"] is False and res["error"] == "skipped: x" and res["steps"] == [] and res["tree"] == "2" * 40


def _release_with(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_verifier_baseline_then_refuses_until_approved(tmp_path):
    v = _mod("hubverify")
    state = tmp_path / "state"
    r1 = _release_with(
        tmp_path / "r1",
        {"app/hubverify.py": "v1", "app/hubsecrets.py": "s1", "app/hubd.py": "d1", "app/policy/main.cedar": "p1"},
    )
    assert set(v.scan(str(r1))) == {"app/hubverify.py", "app/hubsecrets.py", "app/policy/main.cedar"}
    gw = _release_with(tmp_path / "gw", {"app/tools/policy.cedar": "c", "app/tools/hubgw.py": "g"})
    assert set(v.scan(str(gw))) == {"app/tools/policy.cedar"}  # H7's Cedar policy, not the gateway code
    ok, info = asyncio.run(v.gate(str(r1), str(state), None, "r1"))
    assert ok and info["how"] == "baseline"
    r2 = _release_with(
        tmp_path / "r2",
        {"app/hubverify.py": "v1", "app/hubsecrets.py": "s1", "app/hubd.py": "d2", "app/policy/main.cedar": "p1"},
    )
    assert asyncio.run(v.gate(str(r2), str(state), None))[1]["how"] == "accepted"  # unprotected change: no ✅
    r3 = _release_with(
        tmp_path / "r3", {"app/hubverify.py": "v1", "app/hubsecrets.py": "s2-leaks", "app/policy/main.cedar": "p1"}
    )
    asked = []

    async def none(sha):
        asked.append(sha)
        return None

    ok, info = asyncio.run(v.gate(str(r3), str(state), none))
    assert not ok and info["how"] == "refused" and asked == [v.action_sha(info["digest"])]

    async def boom(sha):
        raise OSError("store down")

    assert asyncio.run(v.gate(str(r3), str(state), boom))[0] is False  # fail closed
    (state / "protected-approved").write_text(info["digest"] + "\n")
    ok, info2 = asyncio.run(v.gate(str(r3), str(state), none))
    assert ok and info2["how"] == "console"
    r4 = _release_with(tmp_path / "r4", {"app/hubverify.py": "v2"})

    async def tapped(sha):
        return "f" * 32

    ok, info4 = asyncio.run(v.gate(str(r4), str(state), tapped))
    assert ok and info4["how"] == "approval"
    st = json.loads((state / "protected.json").read_text())["accepted"]
    assert st[info4["digest"]]["nonce"] == "f" * 32 and len(st) == 3
    assert oct(os.stat(state / "protected.json").st_mode & 0o777) == "0o600"


def test_verifier_state_not_writable_fails_closed(tmp_path):
    v = _mod("hubverify")
    r = _release_with(tmp_path / "r", {"app/hubverify.py": "v"})
    blocker = tmp_path / "state"
    blocker.write_text("a file, not a folder")
    ok, info = asyncio.run(v.gate(str(r), str(blocker), None))
    assert not ok and info["how"].startswith("state unreadable")
    state = tmp_path / "ro"
    state.mkdir()
    state.chmod(0o500)
    try:
        if os.access(state, os.W_OK):  # root ignores the mode
            pytest.skip("state folder stays writable for this user")
        ok, info = asyncio.run(v.gate(str(r), str(state), None))
        assert not ok and "state not writable" in info["how"]
    finally:
        state.chmod(0o700)


@pytest.mark.parametrize("body", ["{not json", '["a list"]', '{"accepted": []}'])
def test_verifier_unreadable_state_never_rebaselines(tmp_path, body):
    v = _mod("hubverify")
    r = _release_with(tmp_path / "r", {"app/hubverify.py": "v"})
    state = tmp_path / "state"
    state.mkdir()
    (state / "protected.json").write_text(body)
    ok, info = asyncio.run(v.gate(str(r), str(state), None))
    assert not ok and info["how"].startswith("state unreadable")
    assert (state / "protected.json").read_text() == body  # left for the owner to look at
    (state / "protected.json").unlink()  # his console removes it: the next release is the baseline
    assert asyncio.run(v.gate(str(r), str(state), None))[1]["how"] == "baseline"


@pytest.mark.skipif(shutil.which("git") is None, reason="no git")
def test_hubapprovals_split_keeps_the_names():
    src = (HUB / "hubstore.py").read_text()
    for n in ("approval_request", "approval_decide", "approval_consume", "approval_audit"):
        assert f"async def {n}" not in src and f"    {n},\n" in src
        assert f"async def {n}" in (HUB / "hubapprovals.py").read_text()


def test_heartbeat_tmp_is_per_thread(tmp_path, monkeypatch):
    """main() and the beater thread beat at once at start: with one tmp name per process, one thread truncated the
    other's file after its rename and a reader saw an empty ci-heartbeat.json (test_loader's flaky test_ci_role)."""
    import threading

    cw = _mod("ciworker")
    seen: list[str] = []
    both_open = threading.Barrier(2)
    real_chmod = os.chmod

    def chmod(p, mode):  # both threads hold their tmp before either renames it
        seen.append(str(p))
        both_open.wait(10)
        real_chmod(p, mode)

    monkeypatch.setattr(cw.os, "chmod", chmod)
    hb = str(tmp_path / "ci-heartbeat.json")
    ts = [threading.Thread(target=cw._atomic, args=(hb, json.dumps({"n": i}).encode())) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert len(set(seen)) == 2, seen
    assert json.loads(Path(hb).read_text())["n"] in (0, 1)
    assert not list(tmp_path.glob("*.tmp"))


def test_runner_work_lives_on_the_state_mount_and_old_folders_go(tmp_path, monkeypatch):
    """The job's checkout, HOME, TMPDIR (and the tests' Postgres data) go to <state>/work, not the 1 GB /tmp tmpfs;
    a killed run's leftovers are swept at start, nothing else in that folder."""
    w = _mod("ciworker")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.delenv("HUB_CI_WORK", raising=False)
    monkeypatch.setenv("HUB_LOADER_STATE", str(state / "loader.json"))
    root = w.workroot()
    assert root == str(state / "work") and os.path.isdir(root)
    (state / "work" / "ci-old" / "repo").mkdir(parents=True)
    (state / "work" / "keep.txt").write_text("x")
    assert w.sweep(root) == 1
    assert sorted(os.listdir(root)) == ["keep.txt"]
    monkeypatch.setenv("HUB_LOADER_STATE", str(tmp_path / "nowhere" / "loader.json"))
    assert w.workroot() == "/tmp", "no state folder: the tmpfs as before"
    monkeypatch.setenv("HUB_CI_WORK", str(tmp_path / "explicit"))
    assert w.workroot() == str(tmp_path / "explicit")


def test_runner_env_names_the_toolchains_wheels(tmp_path):
    w = _mod("ciworker")
    tc = tmp_path / "tc"
    (tc / "bin").mkdir(parents=True)
    env = w._env(str(tc), str(tmp_path / "w1"))
    assert "HUB_VENDOR_CACHE" not in env and env["PATH"].startswith(f"{tc}/bin:")
    (tc / "hub-vendor").mkdir()
    env = w._env(str(tc), str(tmp_path / "w2"))
    assert env["HUB_VENDOR_CACHE"] == str(tc / "hub-vendor")
    assert env["LD_LIBRARY_PATH"] == str(tc / "lib"), "pg/lib stays off the runner's own library path"


PAR_DRIVER = r"""
import json, os, sys, time
a = sys.argv[1:]
rep = a[a.index("--report") + 1]
path = a[a.index("--path") + 1]
work = os.path.dirname(rep)
root = os.path.dirname(work)  # the runner's work root: shared by every job, so the barrier lives there
me = os.path.basename(work)
open(os.path.join(root, "started-" + me), "w").close()
end = time.monotonic() + 4
while time.monotonic() < end and len([n for n in os.listdir(root) if n.startswith("started-")]) < 2:
    time.sleep(0.05)
together = len([n for n in os.listdir(root) if n.startswith("started-")]) >= 2
home = os.environ["HOME"]
foreign = sorted(n for n in os.listdir(home) if n.startswith("mine-"))
open(os.path.join(home, "mine-" + me), "w").close()
open(os.path.join(os.environ["TMPDIR"], "scratch"), "w").write(me)
time.sleep(1.5)  # both still running: the runner's sampler sees 2 jobs at once
info = {"together": together, "home": home, "tmp": os.environ["TMPDIR"], "repo": path, "foreign": foreign,
        "scratch": open(os.path.join(os.environ["TMPDIR"], "scratch")).read() == me,
        "workers": os.environ.get("ASSISTANT_CI_PYTEST_WORKERS"),
        "legacy": os.environ.get("ASSISTANT_CI_LEGACY_WORKERS"),
        "passwd": os.environ.get("NSS_WRAPPER_PASSWD"), "t": time.time()}
steps = [{"name": "probe", "ok": True, "detail": json.dumps(info), "secs": 0.1}]
json.dump({"ok": True, "steps": steps, "secs": 0.2}, open(rep, "w"))
"""


def _tc_git(rel: Path) -> None:
    """The fake toolchain gets the real git (the runner has git only in its own toolchain: ASSISTANT_CI_TOOLCHAIN's
    bin/git + libexec/lib/share, which ciworker's _env points at; here the host's git), so this runs in both."""
    tc = rel.parent / "toolchains" / "0123456789ab"
    real = os.environ.get("ASSISTANT_CI_TOOLCHAIN", "")
    if real and (Path(real) / "bin" / "git").is_file():
        (tc / "bin" / "git").symlink_to(Path(real) / "bin" / "git")
        for d in ("libexec", "lib", "share"):
            if (Path(real) / d).is_dir():
                (tc / d).symlink_to(Path(real) / d)
    else:
        (tc / "bin" / "git").symlink_to(shutil.which("git") or "/usr/bin/git")


def _par_run(tmp_path, slots: int | None, n: int = 2) -> tuple[list[dict], dict, str]:
    """n jobs queued at once into a real ciworker process (PAR_DRIVER as its CI driver) -> results, heartbeat seen
    with the most jobs, output."""
    rel = make_release(tmp_path)
    (rel / "app" / "ci" / "assistant.py").write_text(PAR_DRIVER)
    _tc_git(rel)
    spool = tmp_path / "spool"
    for d in ("jobs", "run", "results"):
        (spool / "ci" / d).mkdir(parents=True)
    if slots is not None:
        (spool / "ci" / "slots.json").write_text(json.dumps({"slots": slots}))
    cg = tmp_path / "cg"  # an 8-CPU, 4 GB container with 0.5 GB in use
    cg.mkdir()
    (cg / "cpu.max").write_text("800000 100000\n")
    (cg / "memory.max").write_text(str(4 << 30) + "\n")
    (cg / "memory.stat").write_text(f"anon {512 << 20}\nfile 0\nshmem 0\n")
    (cg / "memory.events").write_text("oom 0\noom_kill 0\n")
    head, tree, bundle = git_repo(tmp_path / "repo")
    jobs = spool / "ci" / "jobs"
    ids = [c * 16 for c in "abcd"[:n]]
    for jid in ids:
        shutil.copy(bundle, jobs / f"{jid}.bundle")
        (jobs / f"{jid}.json").write_text(
            json.dumps({"id": jid, "commit": head, "tree": tree, "branch": f"b-{jid[0]}", "schema": "t_x"})
        )
    work = tmp_path / "work"
    env = {
        "PATH": os.environ["PATH"],
        "HUB_SPOOL": str(spool),
        "HUB_RELEASE": rel.name,
        "HUB_CI_WORK": str(work),
        "HUB_CI_CGROUP": str(cg),
        "HUB_CI_START_GAP": "1",
        "HUB_HEARTBEAT": str(tmp_path / "hb"),
    }
    p = subprocess.Popen(
        [sys.executable, "-I", "-B", str(rel / "app" / "ciworker.py")],
        env=env,
        cwd=rel,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    most: dict = {}
    try:
        res = spool / "ci" / "results"
        end = time.monotonic() + 90
        while time.monotonic() < end and len(list(res.glob("*.json"))) < n:
            hb = _hb(spool)
            if len(hb.get("jobs") or []) > len(most.get("jobs") or []):
                most = hb
            time.sleep(0.1)
    finally:
        p.terminate()
        out = p.communicate(timeout=30)[0].decode()
    got = [json.loads((spool / "ci" / "results" / f"{jid}.json").read_text()) for jid in ids]
    assert not [x for x in work.iterdir() if x.name.startswith("ci-")], out  # every job removed its own folder
    return got, most, out


def test_two_jobs_run_at_once_without_sharing_state(tmp_path):
    """Slots 2 (ciworker.SLOTS): two queued jobs run at the same time, each in its own checkout, HOME, TMPDIR and
    nss passwd; the second gets half the CPUs as workers (the first, alone at its start, the driver's own count);
    the result says it ran with another job."""
    got, most, out = _par_run(tmp_path, None)
    infos = [json.loads(r["steps"][0]["detail"]) for r in got]
    assert all(r["ok"] for r in got), (got, out)
    assert all(i["together"] for i in infos), (infos, out)  # both drivers were inside their run at once
    for k in ("home", "tmp", "repo"):
        assert len({i[k] for i in infos}) == 2, (k, infos)
    assert all(i["foreign"] == [] and i["scratch"] for i in infos), infos  # nothing of the other job in its HOME/TMP
    assert sorted(str(i["workers"]) for i in infos) == ["4", "None"] and all(
        i["legacy"] == i["workers"] for i in infos
    ), infos
    assert all(r["runner"]["slots"] == 2 and r["runner"]["parallel"] == 2 for r in got), [r["runner"] for r in got]
    assert all(r["runner"]["mem_peak_mb"] == 512 and r["runner"]["oom_kills"] == 0 for r in got)
    assert most.get("slots") == 2 and len(most.get("jobs") or []) == 2, most
    assert abs(got[0]["started"] - got[1]["started"]) >= 0.9  # START_GAP between the two starts


def test_one_slot_runs_jobs_one_after_the_other(tmp_path):
    """The rollback (`hub ci slots 1` -> the spool's slots.json): one job at a time, all CPUs each, as before."""
    got, most, out = _par_run(tmp_path, 1)
    infos = [json.loads(r["steps"][0]["detail"]) for r in got]
    assert all(r["ok"] for r in got), (got, out)
    assert not any(i["together"] for i in infos[:1]), infos  # the first ran alone (its barrier timed out)
    assert all(i["workers"] is None for i in infos) and all(r["runner"]["parallel"] == 1 for r in got)
    assert abs(infos[0]["t"] - infos[1]["t"]) > 1.5 and len(most.get("jobs") or []) <= 1


def test_slots_setting_order_and_memory_gate(tmp_path, monkeypatch):
    w = _mod("ciworker")
    monkeypatch.setenv("HUB_SPOOL", str(tmp_path))
    (tmp_path / "ci").mkdir()
    monkeypatch.delenv("HUB_CI_SLOTS", raising=False)
    assert w.slots() == w.SLOTS == 2
    monkeypatch.setenv("HUB_CI_SLOTS", "3")
    assert w.slots() == 3
    (tmp_path / "ci" / "slots.json").write_text(json.dumps({"slots": 1}))
    assert w.slots() == 1  # the hub's file wins: the brain's `hub ci slots N`, no restart
    for bad in ('{"slots": 9}', '{"slots": true}', "not json", "[]"):
        (tmp_path / "ci" / "slots.json").write_text(bad)
        assert w.slots() == 3, bad  # unusable file: the env, then the default
    cg = tmp_path / "cg"
    cg.mkdir()
    w.CGROUP = str(cg)
    assert w.mem_room() is True  # nothing known: no gate
    (cg / "memory.max").write_text(str(4 << 30))
    (cg / "memory.stat").write_text(f"anon {3 << 30}\nshmem {1 << 28}\n")
    assert w.mem_used() == (3 << 30) + (1 << 28) and w.mem_room() is False
    (cg / "memory.stat").write_text(f"anon {2 << 30}\nshmem 0\n")
    assert w.mem_room() is True
    (cg / "cpu.max").write_text("800000 100000\n")
    assert w.cpus() == 8 and w.workers(1) is None and w.workers(2) == 4 and w.workers(4) == 2 and w.workers(8) == 2


TREE_DRIVER = r"""
import json, os, subprocess, sys, time
a = sys.argv[1:]
rep = a[a.index("--report") + 1]
work = os.path.dirname(rep)
root = os.path.dirname(work)
pids = os.path.join(root, "pids.json")
if not os.path.exists(pids):  # the first job: a test Postgres-like child in its own session, one in its own group
    s = subprocess.Popen(["sleep", "300"], start_new_session=True)
    g = subprocess.Popen(["sleep", "300"], process_group=0)
    json.dump([s.pid, g.pid], open(pids + ".tmp", "w"))
    os.replace(pids + ".tmp", pids)
    time.sleep(120)
step = {"name": "probe", "ok": True, "detail": "", "secs": 0.1}
json.dump({"ok": True, "steps": [step], "secs": 0.1}, open(rep, "w"))
"""


def _alive(pid: int) -> bool:
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return st[st.rindex(")") + 2] not in "ZX"


def _until(cond, secs: float = 30.0) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return bool(cond())


def _tree_runner(tmp_path):
    rel = make_release(tmp_path)
    (rel / "app" / "ci" / "assistant.py").write_text(TREE_DRIVER)
    _tc_git(rel)
    spool = tmp_path / "spool"
    for d in ("jobs", "run", "results"):
        (spool / "ci" / d).mkdir(parents=True)
    head, tree, bundle = git_repo(tmp_path / "repo")
    env = {
        "PATH": os.environ["PATH"],
        "HUB_SPOOL": str(spool),
        "HUB_RELEASE": rel.name,
        "HUB_CI_WORK": str(tmp_path / "work"),
        "HUB_CI_CGROUP": str(tmp_path / "no-cgroup"),
        "HUB_CI_START_GAP": "0",
    }

    def queue(jid: str, created: float) -> None:
        shutil.copy(bundle, spool / "ci" / "jobs" / f"{jid}.bundle")
        job = {"id": jid, "commit": head, "tree": tree, "branch": "re-land", "created": created, "schema": "t_x"}
        (spool / "ci" / "jobs" / f"{jid}.json").write_text(json.dumps(job))

    def start() -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-I", "-B", str(rel / "app" / "ciworker.py")],
            env=env,
            cwd=rel,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    return spool, tmp_path / "work" / "pids.json", queue, start


def test_superseded_running_job_stops_with_its_whole_tree(tmp_path):
    """A re-land queues a newer job of the same branch: the older running job stops at once, its driver's session,
    its own process groups and a child that left the session (as pg_ctl's Postgres does) all die; its result says
    superseded (the land side's RUNNER_SUPERSEDED: no shared blocker) and the newer job runs."""
    spool, pids, queue, start = _tree_runner(tmp_path)
    queue("a" * 16, time.time() - 5)
    p = start()
    try:
        assert _until(pids.exists), p
        kids = json.loads(pids.read_text())
        assert all(_alive(k) for k in kids)
        queue("b" * 16, time.time())
        res = spool / "ci" / "results"
        assert _until(lambda: (res / ("a" * 16 + ".json")).exists() and (res / ("b" * 16 + ".json")).exists(), 60)
    finally:
        p.terminate()
        out = p.communicate(timeout=30)[0].decode()
    old = json.loads((res / ("a" * 16 + ".json")).read_text())
    new = json.loads((res / ("b" * 16 + ".json")).read_text())
    assert old["ok"] is False and old["error"].startswith("skipped: superseded by job " + "b" * 16), (old, out)
    assert new["ok"] is True, (new, out)
    assert _until(lambda: not any(_alive(k) for k in kids), 10), kids


def test_restart_kills_the_running_tree_and_requeues_the_job(tmp_path):
    """SIGTERM (a hub deploy restarts the runner): the running job's whole tree dies within the loader's 7 s grace,
    no result, the job stays in run/; the next start requeues it (recover) and it runs to a result."""
    spool, pids, queue, start = _tree_runner(tmp_path)
    queue("c" * 16, time.time())
    p = start()
    try:
        assert _until(pids.exists), p
        kids = json.loads(pids.read_text())
        t0 = time.monotonic()
        p.terminate()
        out = p.communicate(timeout=30)[0].decode()
        assert time.monotonic() - t0 < 7, out
    finally:
        if p.poll() is None:
            p.kill()
    assert _until(lambda: not any(_alive(k) for k in kids), 10), (kids, out)
    assert (spool / "ci" / "run" / ("c" * 16 + ".json")).exists() and not list((spool / "ci" / "results").iterdir())
    p = start()
    try:
        assert _until(lambda: (spool / "ci" / "results" / ("c" * 16 + ".json")).exists(), 60)
    finally:
        p.terminate()
        out = p.communicate(timeout=30)[0].decode()
    assert json.loads((spool / "ci" / "results" / ("c" * 16 + ".json")).read_text())["ok"] is True, out
    assert '"event": "ci_requeued"' in out


def test_start_kills_orphans_of_an_earlier_runner(tmp_path):
    """kill_orphans(root) at start: a process whose environment names a job folder under the work root dies; one
    that does not is left alone."""
    w = _mod("ciworker")
    root = tmp_path / "work"
    orphan = subprocess.Popen(["sleep", "300"], env={**os.environ, "TMPDIR": str(root / "ci-old1" / "tmp")})
    other = subprocess.Popen(["sleep", "300"], env={**os.environ, "TMPDIR": str(tmp_path / "elsewhere")})
    try:
        assert w.kill_orphans(str(root)) >= 1
        assert orphan.wait(timeout=10) == -9
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()
