#!/usr/bin/env python3
"""ciworker: the CI runner in the ci container. Stdlib only, one file; the loader runs it from a
release. No network (network_mode none), no secrets: it reads jobs from the spool it shares with the hub, runs
`assistant ci` on each and writes the result back; the hub signs results, never this process.

Job (HUB_SPOOL/ci/jobs/<id>.json + <id>.bundle, written by hubd) -> claimed by renaming the json into run/ -> the
bundle fetched into the mirror (<work>/mirror.git: every tree sent so far; a bundle is incremental against the seed it
names as prerequisite, hubkit.ci; missing -> the error "seed missing: ..." and the brain sends the full trees next)
-> a checkout (objects through the mirror's alternates) in a private work folder
(workroot(): <state>/work on the ci container's disk-backed state mount, so the
checkout, HOME, TMPDIR and the tests' Postgres clusters neither fill the 1 GB /tmp tmpfs nor count against the
container's memory; /tmp only when that mount is missing), checked out at the job's commit (its tree must match)
-> the release's own
CI driver (app/ci/assistant.py, the CI code of main when the hub release was built, never the commit's own) with
the CI toolchain (git, ruff, basedpyright + node, pytest and the test deps for CPython 3.13: releases/toolchains/<id>,
named and hashed in release.json; checked before first use) -> results/<id>.json. A job the runner was killed in
(a hub deploy restarts it) goes back into the queue at its old place on the next start, once (REQUEUE_MAX); cut off
again, it gets a failed result. A job's processes never outlive it: the driver runs in its own session, and kill_job
(SIGKILL to that session + every process whose environment names the job's work folder, so a test Postgres that
left the session too) runs when the job ends, on SIGTERM (a restart), when a newer job of its branch is queued or
running (superseded: its result says so, never a shared blocker) and, at start, kill_orphans for an earlier runner's.
Heartbeats every 5 s, also while a job runs (the loader restarts a ci
child whose heartbeat is older than 30 s).

Slots (29.10: ~90 % of a land's wall was queue wait on one serial runner): up to slots() jobs run at once, each in
its own thread with its own work folder (checkout, HOME, TMPDIR, nss passwd, report); only the git mirror is shared
and its fetch + checkout run one job at a time (_git). slots() = the spool's ci/slots.json {"slots": N} (hubci POST
/ci/slots, the brain's `hub ci slots N|default`; read before every claim, no restart), else HUB_CI_SLOTS, else SLOTS;
1..SLOTS_MAX. Rollback: `hub ci slots 1` (one job at a time, as before). A further job starts only while the
container's unreclaimable memory (cgroup anon + shmem) leaves MEM_RESERVE_MB below its limit and START_GAP s after
the last start (the jobs' pyright peaks apart), and gets
cpus // (jobs running) pytest and legacy workers (a lone job keeps the driver's own count, all CPUs). The result's
"runner" says what the job had: slots, workers, parallel (most jobs at once while it ran), mem_peak_mb (the
container's anon + shmem peak while it ran), oom_kills (the cgroup's oom kills while it ran).

The toolchain also brings bun (bin/), Postgres (pg/, tests/hubstore/hubfix.py finds it through
ASSISTANT_CI_TOOLCHAIN) and the pinned hub wheels (hub-vendor/, HUB_VENDOR_CACHE).

Runner steps: ruff, pyright, pytest-3.13, ast, schemas: the land's test run. The steps that need the brain's home,
its network or its secrets stay in the brain's `ci --host` (sync, legacy, ratchet, inbox-lint, secretscan, precheck,
bun and the smoke set).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

EVERY = 5.0
JOB_TIMEOUT = 1500.0
QUEUE_MAX_AGE = 1800.0  # a job queued longer has no waiter: a land waits up to 1800 s (RUNNER_WAIT_ENFORCE)
# The steps a land runs on the brain (they need its secrets, profile or live home: `assistant ci --host`); this runner
# runs every other one. A step moves here only after a probe job ran it green in this runner.
LOCAL_STEPS = "sync,secretscan,precheck"
# Probe jobs: a job whose branch starts with PROBE_PREFIX (never landed) may list LOCAL_STEPS names in its tree's
# PROBE_FILE, one per line; those run here for that job only. A land's job never does: its branch is the landed one.
PROBE_PREFIX = "ci-probe"
PROBE_FILE = ("tests", "runner-probe-steps.txt")


def skip_steps(job: dict, repo: str) -> str:
    """LOCAL_STEPS minus the steps a probe job's tree asks this runner to run (only names in LOCAL_STEPS count)."""
    if not str(job.get("branch") or "").startswith(PROBE_PREFIX):
        return LOCAL_STEPS
    try:
        with open(os.path.join(repo, *PROBE_FILE), encoding="utf-8") as f:
            want = {ln.split("#", 1)[0].strip() for ln in f}
    except OSError:
        return LOCAL_STEPS
    return ",".join(s for s in LOCAL_STEPS.split(",") if s not in want)


RELEASE = os.environ.get("HUB_RELEASE", "")
SUPERSEDED = "skipped: superseded by job"  # ops/assistant.py RUNNER_SUPERSEDED: never a shared blocker
HERE = os.path.dirname(os.path.abspath(__file__))
REL_DIR = os.path.dirname(HERE)
_stop = threading.Event()
_tc: dict = {"path": None, "id": None, "ok": None, "why": ""}
SLOTS = 2  # jobs at once unless the spool's ci/slots.json or HUB_CI_SLOTS says otherwise (1 = the old serial runner)
SLOTS_MAX = 4
SLOTS_FILE = "slots.json"  # in HUB_SPOOL/ci, written by the hub (hubci r_slots_set), read here
# A further job waits while the container's anon + shmem is within this of memory.max: one full job peaked at
# ~2.2 GB anon with 8 pytest workers (brain emulation 29.10); the 8 GB limit (4 GB before 29.10) holds 4 jobs.
MEM_RESERVE_MB = 1300
# ... and not within START_GAP s of the last start: two jobs starting together ran both pyrights (~1 GB each) at
# once beside the first pytest workers (3.4 GB anon at that moment in the emulation, ~2.7 GB at most otherwise).
START_GAP = float(os.environ.get("HUB_CI_START_GAP") or 30.0)
CGROUP = os.environ.get("HUB_CI_CGROUP", "/sys/fs/cgroup")
_lock = threading.Lock()  # _running and each job's sample fields
_git = threading.Lock()  # the shared mirror: one fetch + checkout at a time
_tc_lock = threading.Lock()
_running: dict[str, dict] = {}  # job id -> {"t0", "parallel", "mem_peak", "oom0"}


def _sig(*_a) -> None:
    _stop.set()


def spool(*parts: str) -> str:
    return os.path.join(os.environ.get("HUB_SPOOL", "/var/spool/hub"), *parts)


def _atomic(path: str, data: bytes, mode: int = 0o640) -> None:
    # per thread, not just per process: main() and the beater thread both beat at start; one shared tmp name let
    # one thread truncate the other's file after its rename, so a reader saw an empty heartbeat
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def beat() -> None:
    hb = os.environ.get("HUB_HEARTBEAT", "")
    if hb:
        try:
            with open(hb, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())) + "\n")
        except OSError:
            pass
    if os.path.isdir(spool()):
        with _lock:
            jobs = sorted(_running)
        body = {
            "release": RELEASE,
            "ts": round(time.time(), 3),
            "busy": jobs[0] if jobs else None,  # one job id, as before slots (readers of the old field)
            "jobs": jobs,
            "slots": slots(),
            "toolchain": _tc["id"],
            "toolchain_ok": _tc["ok"],
        }
        try:
            _atomic(spool("ci-heartbeat.json"), json.dumps(body).encode())
        except OSError:
            pass


def _beater() -> None:
    while not _stop.is_set():
        beat()
        _stop.wait(EVERY)


def _sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def toolchain() -> tuple[str | None, str]:
    """The release's CI toolchain, verified once per process (manifest sha from release.json, then every file)."""
    with _tc_lock:
        return _toolchain()


def _toolchain() -> tuple[str | None, str]:
    if _tc["ok"] is not None:
        return (_tc["path"] if _tc["ok"] else None), _tc["why"]
    try:
        with open(os.path.join(REL_DIR, "release.json"), encoding="utf-8") as f:
            ci = json.load(f).get("ci") or {}
        tid, msha = ci.get("toolchain"), ci.get("manifest_sha256")
        if not tid or not msha:
            raise ValueError("release names no CI toolchain")
        root = os.path.join(os.path.dirname(REL_DIR), "toolchains", tid)
        man = os.path.join(root, "manifest.json")
        if _sha(man) != msha:
            raise ValueError("toolchain manifest sha256 mismatch")
        with open(man, encoding="utf-8") as f:
            files = json.load(f)["files"]
        for rel, want in files.items():
            p = os.path.join(root, rel)
            if os.path.islink(p) or not os.path.isfile(p) or _sha(p) != want:
                raise ValueError(f"toolchain file {rel}: missing or changed")
        _tc.update(path=root, id=tid, ok=True, why="")
    except (OSError, ValueError, KeyError, TypeError) as e:
        _tc.update(ok=False, why=str(e)[:200] or type(e).__name__)
    return (_tc["path"] if _tc["ok"] else None), _tc["why"]


def workroot() -> str:
    """HUB_CI_WORK, else <the loader's state folder>/work when that folder is writable, else /tmp."""
    w = os.environ.get("HUB_CI_WORK", "")
    if not w:
        st = os.path.dirname(os.environ.get("HUB_LOADER_STATE", ""))
        w = os.path.join(st, "work") if st and os.access(st, os.W_OK) else "/tmp"
    try:
        os.makedirs(w, mode=0o700, exist_ok=True)
    except OSError:
        return "/tmp"
    return w


def sweep(root: str) -> int:
    """Work folders a killed runner left behind (the job itself removes its own); only at start, no job runs."""
    n = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(root, name)
        if name.startswith("ci-") and os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
            n += 1
    return n


def slots() -> int:
    """Jobs at once: the spool's ci/slots.json {"slots": N} (the hub writes it), else HUB_CI_SLOTS, else SLOTS;
    anything unreadable or out of 1..SLOTS_MAX falls through to the next."""
    try:
        with open(spool("ci", SLOTS_FILE), encoding="utf-8") as f:
            n = json.load(f).get("slots")
        if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= SLOTS_MAX:
            return n
    except (OSError, ValueError, AttributeError):
        pass
    raw = os.environ.get("HUB_CI_SLOTS", "").strip()
    if raw.isdigit() and 1 <= int(raw) <= SLOTS_MAX:
        return int(raw)
    return SLOTS


def _cg(name: str) -> str:
    try:
        with open(os.path.join(CGROUP, name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _cg_stat(name: str, key: str) -> int | None:
    for ln in _cg(name).splitlines():
        k, _, v = ln.partition(" ")
        if k == key and v.strip().isdigit():
            return int(v)
    return None


def mem_used() -> int | None:
    """The container's unreclaimable memory now (cgroup anon + shmem: the tmpfs /tmp is shmem), None unknown."""
    anon = _cg_stat("memory.stat", "anon")
    return None if anon is None else anon + (_cg_stat("memory.stat", "shmem") or 0)


def mem_max() -> int | None:
    v = _cg("memory.max").strip()
    return int(v) if v.isdigit() else None


def mem_room() -> bool:
    """Room for one more job: unknown usage or no limit -> yes."""
    used, cap = mem_used(), mem_max()
    return used is None or cap is None or cap - used >= MEM_RESERVE_MB << 20


def cpus() -> int:
    """The container's CPU quota (cgroup cpu.max), else the affinity set."""
    q, _, per = _cg("cpu.max").strip().partition(" ")
    if q.isdigit() and per.strip().isdigit() and int(per) > 0:
        return max(1, int(q) // int(per))
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def workers(n_running: int) -> int | None:
    """pytest/legacy workers for a job that starts while n_running jobs (itself included) run: None = the driver's
    own count (a lone job: all CPUs), else the CPUs shared out, at least 2."""
    return None if n_running <= 1 else max(2, cpus() // n_running)


def _sample() -> None:
    """Every second while jobs run: the container's anon + shmem into each running job's peak."""
    while not _stop.is_set():
        if _running:
            used = mem_used()
            with _lock:
                for r in _running.values():
                    r["parallel"] = max(r["parallel"], len(_running))
                    if used is not None:
                        r["mem_peak"] = max(r["mem_peak"], used)
        _stop.wait(1.0)


def _env(tc: str, work: str) -> dict[str, str]:
    home = os.path.join(work, "home")
    os.makedirs(os.path.join(work, "tmp"), exist_ok=True)
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, ".gitconfig"), "w", encoding="utf-8") as f:
        f.write("[user]\n\tname = assistant-ci\n\temail = assistant-ci@localhost\n[safe]\n\tdirectory = *\n")
    env = {
        "PATH": f"{tc}/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": os.path.join(work, "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": os.environ.get("TZ", "UTC"),
        "PYTHONPATH": os.path.join(tc, "site"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_EXEC_PATH": os.path.join(tc, "libexec", "git-core"),
        "GIT_TEMPLATE_DIR": os.path.join(tc, "share", "git-core", "templates"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "LD_LIBRARY_PATH": os.path.join(tc, "lib"),
        "ASSISTANT_CI_TOOLCHAIN": tc,
        "ASSISTANT_CI_RUNNER": "1",
        "ASSISTANT_NETGUARD": "none",
    }
    if os.path.isdir(os.path.join(tc, "hub-vendor")):  # the pinned hub wheels for the hub-store tests
        env["HUB_VENDOR_CACHE"] = os.path.join(tc, "hub-vendor")
    return env


def _user(tc: str, work: str, home: str) -> dict[str, str]:
    """A user for this uid (the ci container's has no passwd entry): core.paths.real_home() reads
    ASSISTANT_REAL_HOME, and the toolchain's nss_wrapper gives pwd, getent and ~ a one-line passwd naming the job's
    HOME, as tests/hubstore/hubfix.py does for initdb. That HOME is the job's own and starts empty: the CI driver
    builds the would-be ~/bin into it from the checkout (assistant.py _runner_home), never a brain file."""
    out = {"ASSISTANT_REAL_HOME": home}
    try:
        import pwd

        pwd.getpwuid(os.getuid())
        return out
    except KeyError:
        pass
    # citool puts it beside Postgres (pg/lib), as hubfix finds it
    lib = next(
        (p for p in (os.path.join(tc, d, "libnss_wrapper.so") for d in ("pg/lib", "lib")) if os.path.isfile(p)), ""
    )
    if not lib:
        return out
    d = os.path.join(work, "nss")
    os.makedirs(d, exist_ok=True)
    uid, gid = os.getuid(), os.getgid()
    with open(os.path.join(d, "passwd"), "w", encoding="utf-8") as f:
        f.write(f"assistant-ci:x:{uid}:{gid}:assistant-ci:{home}:/bin/sh\n")
    with open(os.path.join(d, "group"), "w", encoding="utf-8") as f:
        f.write(f"assistant-ci:x:{gid}:\n")
    out.update(LD_PRELOAD=lib, NSS_WRAPPER_PASSWD=os.path.join(d, "passwd"), NSS_WRAPPER_GROUP=os.path.join(d, "group"))
    return out


def _run(cmd: list[str], env: dict, cwd: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
    )


def _mirror_fetch(env: dict, mirror: str, bundle: str) -> str:
    """The job's bundle into the runner's mirror (the next incremental bundle builds on it) -> "" or the error."""
    if not os.path.isdir(os.path.join(mirror, "objects")):
        _run(["git", "init", "--quiet", "--bare", mirror], env)
    heads = _run(["git", "-C", mirror, "bundle", "list-heads", bundle], env).stdout.split()
    specs = ["+refs/*:refs/*"] + (["+HEAD:refs/ci/bundle-head"] if "HEAD" in heads else [])  # a plain bundle too
    r = _run(["git", "-C", mirror, "fetch", "--quiet", bundle, *specs], env, timeout=300)
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["?"])[-1][:200]
        miss = "prerequisite" in r.stderr or "missing" in r.stderr.lower()
        return ("seed missing: " if miss else "fetch: ") + last
    return ""


def skip_why(job: dict, now: float | None = None) -> str:
    """Why a queued job is not worth a run: queued past QUEUE_MAX_AGE (no land waits that long), or a newer job of
    the same branch waits (that land rebased or re-ran: nobody waits for this one). "" = run it."""
    now = time.time() if now is None else now
    created = job.get("created")
    if isinstance(created, (int, float)) and now - created > QUEUE_MAX_AGE:
        return f"skipped: queued {int(now - created)} s (over {int(QUEUE_MAX_AGE)} s, nobody waits that long)"
    branch = job.get("branch") or ""
    if not branch:
        return ""
    jd = spool("ci", "jobs")
    try:
        names = [n for n in os.listdir(jd) if n.endswith(".json")]
    except OSError:
        return ""
    for n in names:
        try:
            with open(os.path.join(jd, n), encoding="utf-8") as f:
                other = json.load(f)
        except (OSError, ValueError):
            continue
        if (
            other.get("branch") == branch
            and other.get("id") != job.get("id")
            and (other.get("created") or 0) > (created or 0)
        ):
            return f"{SUPERSEDED} {str(other.get('id'))[:16]} of the same branch"
    return ""


def _newer(job: dict) -> str:
    """The id of a newer job of the same branch, queued or running (a re-land replaced this one), else ""."""
    branch, created = job.get("branch") or "", job.get("created") or 0
    if not branch:
        return ""
    for d in ("jobs", "run"):
        try:
            names = [n for n in os.listdir(spool("ci", d)) if n.endswith(".json")]
        except OSError:
            continue
        for n in names:
            try:
                with open(spool("ci", d, n), encoding="utf-8") as f:
                    other = json.load(f)
            except (OSError, ValueError):
                continue
            if (
                other.get("branch") == branch
                and other.get("id") != job.get("id")
                and (other.get("created") or 0) > created
            ):
                return str(other.get("id"))[:16]
    return ""


def _procs() -> list[tuple[int, int, bytes]]:
    """(pid, session id, environment) of every process this uid may read, this one left out."""
    me, out = os.getpid(), []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                st = f.read()
            sid = int(st[st.rindex(b")") + 2 :].split()[3])
            with open(f"/proc/{name}/environ", "rb") as f:
                env = f.read()
        except (OSError, ValueError, IndexError):
            continue
        out.append((int(name), sid, env))
    return out


def kill_procs(marker: str, sid: int = 0) -> int:
    """SIGKILL every process in session sid or whose environment names marker (a path) -> how many."""
    m, n = marker.encode(), 0
    for pid, s, env in _procs():
        if (sid and s == sid) or (m and m in env):
            try:
                os.kill(pid, signal.SIGKILL)
                n += 1
            except OSError:
                pass
    return n


def kill_job(work: str, sid: int = 0) -> int:
    """A job's whole process tree: its driver's session + everything carrying its work folder (HOME, TMPDIR)."""
    return kill_procs(work.rstrip("/") + "/", sid)


def kill_orphans(root: str) -> int:
    """At start (no job runs): what jobs of an earlier runner left running (the loader kills only this process)."""
    return kill_procs(os.path.join(root, "ci-"))


def skip_job(job: dict, bundle: str, why: str) -> dict:
    """A job not run: its bundle still goes into the mirror (a later incremental bundle's seed), result = not ok."""
    t0 = time.time()
    out = {
        "id": job["id"],
        "schema": job.get("schema"),
        "commit": job["commit"],
        "tree": job.get("tree"),
        "ok": False,
        "steps": [],
        "started": round(t0, 3),
        "error": why[:300],
        "runner": {"release": RELEASE, "toolchain": _tc["id"], "python": sys.version.split()[0]},
    }
    tc, _why = toolchain()
    if tc is not None:
        work = tempfile.mkdtemp(prefix="ci-", dir=workroot())
        try:
            with _git:
                err = _mirror_fetch(_env(tc, work), os.path.join(workroot(), "mirror.git"), bundle)
            if err:
                out["error"] = (why + "; mirror " + err)[:300]
        finally:
            shutil.rmtree(work, ignore_errors=True)
    out["ended"] = round(time.time(), 3)
    out["secs"] = round(out["ended"] - t0, 2)
    return out


def run_job(job: dict, bundle: str, n_workers: int | None = None) -> dict:
    """One job in its own work folder; n_workers (workers()) caps its pytest and legacy workers, None = the driver's."""
    t0 = time.time()
    out = {
        "id": job["id"],
        "schema": job.get("schema"),
        "commit": job["commit"],
        "tree": None,
        "ok": False,
        "steps": [],
        "started": round(t0, 3),
        "error": "",
        "runner": {"release": RELEASE, "toolchain": _tc["id"], "python": sys.version.split()[0]},
    }
    tc, why = toolchain()
    out["runner"]["toolchain"] = _tc["id"]
    if tc is None:
        out["error"] = f"no usable CI toolchain: {why}"
        return out
    work = tempfile.mkdtemp(prefix="ci-", dir=workroot())
    sid = 0
    try:
        env = _env(tc, work)
        repo = os.path.join(work, "repo")
        mirror = os.path.join(workroot(), "mirror.git")
        with _git:  # another job's fetch (or its auto-gc) never runs under this checkout
            err = _mirror_fetch(env, mirror, bundle)
            if err:
                out["error"] = err
                return out
            r = _run(["git", "init", "--quiet", repo], env)
            with open(os.path.join(repo, ".git", "objects", "info", "alternates"), "w", encoding="utf-8") as f:
                f.write(os.path.join(mirror, "objects") + "\n")
            r = _run(["git", "-C", repo, "checkout", "--quiet", "--detach", job["commit"]], env)
            if r.returncode != 0:
                out["error"] = "checkout: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200]
                return out
            tree = _run(["git", "-C", repo, "rev-parse", "HEAD^{tree}"], env).stdout.strip()
        out["tree"] = tree
        if tree != job.get("tree"):
            out["error"] = "the bundle's commit has another tree than the job names"
            return out
        env.update(_user(tc, work, env["HOME"]))  # a passwd entry + the would-be ~/bin in the job's HOME
        if n_workers:
            env.update(ASSISTANT_CI_PYTEST_WORKERS=str(n_workers), ASSISTANT_CI_LEGACY_WORKERS=str(n_workers))
        base = job.get("base")
        if base and _run(["git", "-C", repo, "cat-file", "-e", base + "^{commit}"], env).returncode != 0:
            base = None
        report = os.path.join(work, "report.json")
        cmd = [
            sys.executable,
            "-B",
            os.path.join(HERE, "ci", "assistant.py"),
            "ci",
            "--path",
            repo,
            "--skip",
            skip_steps(job, repo),
            "--no-stamp",
            "--report",
            report,
        ] + (["--base", base] if base else [])
        # its own session: the driver, pytest's process groups and whatever they start; test Postgres (pg_ctl
        # setsid) leaves it but keeps the job's HOME/TMPDIR in its environment: kill_job finds both
        p = subprocess.Popen(
            cmd,
            env=env,
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        sid = p.pid
        end = time.monotonic() + JOB_TIMEOUT
        while True:
            try:
                so, se = p.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                cut = "stop" if _stop.is_set() else (_running.get(job["id"]) or {}).get("cut") or _newer(job)
                if cut or time.monotonic() > end:
                    kill_job(work, sid)
                    p.communicate()
                    if cut == "stop":  # a runner restart: no result, the job stays in run/ and recover() requeues it
                        out["requeue"] = True
                    elif cut:
                        out["error"] = f"{SUPERSEDED} {cut} of the same branch (stopped while running)"
                    else:
                        out["error"] = f"timed out after {int(JOB_TIMEOUT)} s"
                    return out
        tail = (so + se).strip().splitlines()[-12:]
        try:
            with open(report, encoding="utf-8") as f:
                rep = json.load(f)
            out["steps"] = rep.get("steps") or []
            out["ok"] = bool(rep.get("ok")) and p.returncode == 0
        except (OSError, ValueError):
            out["error"] = "no CI report: " + " | ".join(tail)[-300:]
        return out
    finally:
        if sid:
            kill_job(work, sid)  # anything the driver left running (a test Postgres it did not stop)
        shutil.rmtree(work, ignore_errors=True)
        out["ended"] = round(time.time(), 3)
        out["secs"] = round(out["ended"] - t0, 2)


REQUEUE_MAX = 1  # restarts a job may be cut off by and still run again (a crash loop ends after this)


def recover() -> None:
    """Jobs a killed runner left in run/: back into the queue at their old place (mtime = created) while their
    bundle is there and they were requeued fewer than REQUEUE_MAX times (the land waiting on one just waits on);
    else a failed result each (the brain resubmits)."""
    rd = spool("ci", "run")
    if not os.path.isdir(rd):
        return
    for n in sorted(os.listdir(rd)):
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(rd, n), encoding="utf-8") as f:
                job = json.load(f)
            again = int(job.get("requeued") or 0)
            if again < REQUEUE_MAX and os.path.isfile(spool("ci", "jobs", n[:-5] + ".bundle")):
                dst = spool("ci", "jobs", n)
                _atomic(dst, json.dumps({**job, "requeued": again + 1}).encode())
                created = job.get("created")
                if isinstance(created, (int, float)):
                    os.utime(dst, (created, created))
                os.unlink(os.path.join(rd, n))
                print(json.dumps({"event": "ci_requeued", "id": job.get("id")}), flush=True)
                continue
            res = {
                "id": job["id"],
                "schema": job.get("schema"),
                "commit": job["commit"],
                "tree": job.get("tree"),
                "ok": False,
                "steps": [],
                "error": "the runner restarted during this job",
                "secs": 0,
            }
            _atomic(spool("ci", "results", n), json.dumps(res).encode())
        except (OSError, ValueError, KeyError):
            pass
        for p in (os.path.join(rd, n), spool("ci", "jobs", n[:-5] + ".bundle")):
            try:
                os.unlink(p)
            except OSError:
                pass


def next_job() -> tuple[dict, str] | None:
    jd = spool("ci", "jobs")
    try:
        names = sorted((os.stat(os.path.join(jd, n)).st_mtime, n) for n in os.listdir(jd) if n.endswith(".json"))
    except OSError:
        return None
    for _, n in names:
        src, dst = os.path.join(jd, n), spool("ci", "run", n)
        try:
            os.replace(src, dst)
            with open(dst, encoding="utf-8") as f:
                job = json.load(f)
        except (OSError, ValueError):
            continue
        return job, os.path.join(jd, n[:-5] + ".bundle")
    return None


def main() -> int:
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    for d in ("", "jobs", "run", "results"):
        try:
            os.makedirs(spool("ci", d), mode=0o2770, exist_ok=True)
            if os.stat(spool("ci", d)).st_uid == os.getuid():
                os.chmod(spool("ci", d), 0o2770)  # the umask drops group write: the hub (same group) needs it
        except OSError:
            pass
    beat()
    threading.Thread(target=_beater, daemon=True).start()
    print(json.dumps({"event": "ciworker_start", "release": RELEASE}), flush=True)
    recover()
    root = workroot()
    killed = kill_orphans(root)
    print(json.dumps({"event": "ci_work", "root": root, "killed": killed, "swept": sweep(root)}), flush=True)
    tc, why = toolchain()  # checked once at start: the heartbeat says whether jobs can run
    print(json.dumps({"event": "ci_toolchain", "id": _tc["id"], "ok": tc is not None, "why": why}), flush=True)
    beat()
    threading.Thread(target=_sample, daemon=True).start()
    threads: list[threading.Thread] = []
    last = 0.0  # monotonic time of the last start
    while not _stop.is_set():
        threads = [t for t in threads if t.is_alive()]
        if len(_running) >= slots() or (_running and (time.monotonic() - last < START_GAP or not mem_room())):
            _stop.wait(1.0)
            continue
        got = next_job()
        if got is None:
            _stop.wait(1.0)
            continue
        job, bundle = got
        last = time.monotonic()
        with _lock:
            br, cr = job.get("branch") or "", job.get("created") or 0
            for r in _running.values():  # a re-land's job starts: the older one of its branch stops now
                if br and r.get("branch") == br and (r.get("created") or 0) < cr:
                    r["cut"] = str(job["id"])[:16]
            _running[job["id"]] = {
                "t0": time.time(),
                "parallel": len(_running) + 1,
                "mem_peak": 0,
                "oom0": _ooms(),
                "branch": br,
                "created": cr,
            }
            n = len(_running)
        print(json.dumps({"event": "ci_job", "id": job.get("id"), "running": n}), flush=True)
        t = threading.Thread(target=_job_thread, args=(job, bundle, workers(n)), daemon=True)
        t.start()
        threads.append(t)
        beat()  # the hub sees the job count change now, not at the next 5 s beat
    for t in threads:  # SIGTERM: no new claim; each running job kills its tree within ~1 s and stays in run/
        t.join()
    return 0


def _ooms() -> int:
    return _cg_stat("memory.events", "oom_kill") or 0


def _job_thread(job: dict, bundle: str, n_workers: int | None) -> None:
    try:
        why = skip_why(job)
        res = skip_job(job, bundle, why) if why else run_job(job, bundle, n_workers)
    except Exception as e:  # noqa: BLE001 - a crash in one job's thread is that job's red result, never the runner's
        res = {
            "id": job["id"],
            "schema": job.get("schema"),
            "commit": job.get("commit"),
            "tree": job.get("tree"),
            "ok": False,
            "steps": [],
            "error": f"runner: {type(e).__name__}: {e}"[:300],
            "secs": 0,
        }
    if res.get("requeue"):  # cut off by a restart: recover() at the next start puts it back into the queue
        with _lock:
            _running.pop(job["id"], None)
        print(json.dumps({"event": "ci_cut", "id": job["id"]}), flush=True)
        return
    with _lock:
        r = _running.get(job["id"]) or {}
        res.setdefault("runner", {}).update(
            slots=slots(),
            workers=n_workers,
            parallel=r.get("parallel", 1),
            mem_peak_mb=(r.get("mem_peak") or 0) >> 20 or None,
            oom_kills=max(0, _ooms() - r.get("oom0", 0)),
        )
    try:
        _atomic(spool("ci", "results", job["id"] + ".json"), json.dumps(res).encode())
    finally:
        for p in (spool("ci", "run", job["id"] + ".json"), bundle):
            try:
                os.unlink(p)
            except OSError:
                pass
        with _lock:
            _running.pop(job["id"], None)
        beat()
    print(json.dumps({"event": "ci_done", "id": job["id"], "ok": res["ok"], "secs": res.get("secs")}), flush=True)


if __name__ == "__main__":
    sys.exit(main())
