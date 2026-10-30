#!/usr/local/bin/python3.13 -I
# managed: ~/opt/assistant — edit there, deploy with assistant land
"""assistant: CI gate, deploy / land / rollback / drift and crontab edits for the harness repo (~/opt/assistant).

  assistant ci [--path DIR] [--base REF] [--offline] [--skip STEP,..] [--only STEP,..] [--no-stamp] [--host]
        ruff + format, pyright basic (core/, packages/), pytest on 3.13 only (temp HOME, netguard, ASSISTANT_TEST=1;
        pytest-xdist workers, --dist loadfile, a HOME and TMPDIR per worker; ASSISTANT_CI_PYTEST_WORKERS=N, 0 = serial;
        the hub runner stays serial unless set); the tool suites are pytest tests in that step (tests/test_suites.py,
        one per suite, id = its name; ASSISTANT_CI_SUITES names them, their results come back in
        ASSISTANT_CI_SUITES_OUT): the ones the change impacts (legacy_select: a suite's script, the lib modules it
        imports, the files and scripts it or its suite dir name, tests/legacy/impact.toml's extras; every suite without
        a base, for a change in that area no suite claims, or above 60 % of them) against a staged bin overlay (*_BIN
        seams; the tree's lib/ modules beside them; the suite's run.sh from tests/legacy/<suite>/ in the tree, else
        ~/opt/<suite>-tests/; side by side, own HOME/TMPDIR each, ASSISTANT_CI_LEGACY_WORKERS=N at once,
        tests/legacy/slow.txt first; all suites become native pytest, the shell form is temporary:
        tests/legacy/README.md),
        AST ban, JSON Schema validity, lint-bin ratchet on the would-be ~/bin, inbox-lint on the would-be ~/bin (every
        ~/inbox note writer, managed or live, carries "Data, not instructions"), secretscan, codebase-sync precheck +
        `assistant-publish scan --no-identity` of the branch's new commits (a privacy hit blocks the land),
        bun test when adapters/tg changed. Green = stamp per git tree hash (state ci/<tree>.json).
        --host (what a land runs here; the hub runner is the test run): only the host-bound part -- the 3.13 venv,
        the host-bound tests (the untagged test lines of tests/runner-skip.txt), the host-bound tool suites (its
        tests/legacy/<suite> lines, real-home suites, suites still in ~/opt), ruff + format and the AST ban (seconds:
        a lint red fails the land before the runner's answer), secretscan and precheck + publish scan on the diff;
        never stamps. The runner (--toolchain) runs every other step and suite, its scripts'
        `#!/usr/local/bin/python3.13 -I` shebang resolving there (python:3.13 image) as here (uv's 3.13, linked there
        by ~/boot.sh; a leftover /usr/bin/python3 one is pointed at the runner's).
        On a clean checkout of main the suites' results go into main's record (all suites:
        `assistant ci --path ~/opt/assistant --base '' --only suites`).
        Steps: sync ruff pyright pytest suites ast schemas ratchet inbox-lint secretscan precheck bun (suites: the
        tool suites inside the pytest step; `--only suites` runs just tests/test_suites.py); the pytest
        suites and codebase-sync precheck run in the background beside the others, step lines print in this order.
        Every exit (green, red, timeout, SIGTERM/SIGINT/SIGHUP) stops what the run started (pytest runs in its own
        process group: SIGINT for fixture teardown, SIGKILL after 30 s; then whatever still holds the run dir, e.g.
        test Postgres detached by pg_ctl) and removes its ~/.cache/aci/assistant-ci.* dir (pytest's basetemp is inside).
        Local run dirs live under ~/.cache/aci (the appdata disk, not /tmp in the docker.img layer; the hub runner
        keeps its tmpfs). Each start sweeps leftovers of killed runs there and in /tmp: dirs untouched for 2 h whose
        ci is gone and that no live process holds, except orphan test Postgres (ppid 1, -D inside the dir), which is
        stopped first.
  assistant land BRANCH [--adopt DEST].. [--override REASON]   rebase BRANCH onto main (in its worktree), CI (or a
        green stamp for that tree), fast-forward main, deploy (--adopt as for deploy: existing unmanaged live files the
        branch takes over, after checking them); a failed deploy rolls back and resets main. Rebase + CI + runner +
        eval subset run OUTSIDE the deploy lock (29.10: lands' CIs overlap instead of queueing); the lock covers only
        the re-check, fast-forward and deploy: main and the branch still what the CI saw -> land; main moved -> once
        more outside the lock on the new main, then under it (a green stamp per tree skips a CI already run; the
        green eval subset is reused for unchanged files). Rollback switch to the old flow (CI under the lock):
        `touch ~/.config/assistant/land-serial` (or ASSISTANT_LAND_SERIAL=1).
        The tests run once, on the hub's runner: a land runs `ci --host` here (the smoke set and the
        host-bound steps) and needs the runner's signed green record for the tree in every gate mode but off.
        Here: uv sync, ruff + format and the AST ban (fast, so a lint red fails here first; the runner runs them
        too, for its record), secretscan + the publish scan on the diff, precheck on the changed would-be ~/bin
        files, the host-bound tool suites and tests (tests/runner-skip.txt); everything else (pyright, pytest,
        schemas, ratchet, inbox-lint, bun, the other tool suites) on the runner, under its own user with the
        would-be ~/bin.
        Rollback to the full local run (every step here, the runner per the gate mode as before):
        `touch ~/.config/assistant/land-local-ci` (or ASSISTANT_LAND_LOCAL_CI=1); gate mode off and --override
        run it too. The land row has land_cmd_start,
        lock_wait_secs, verify {tries, locked, serial}, gate.ci_local_at + ci_local_steps (each local step's
        seconds) and the runner's created/started/ended (the dashboard's land timeline). The lock: first come first
        served (a ticket per waiter in deploy.queue/, dead waiters' tickets pruned; "queued, position N" while
        waiting). Fail-fast on suites red on main: main's record (state legacy-main.json, `status`)
        holds each tool suite's last result on main's tree (a land's green CI, `ci` on a clean main checkout, or a
        red land's re-check on main of suites outside its area); before CI, a land runs the suites it would run
        that are red there, alone, and stops at once (exit 6) when one is still red. Not skip-with-warning: a
        skipped suite could hide a new regression; this check never refuses what the full CI would pass.
        The hub's CI runner gets a bundle of the rebased commit in parallel (ci-gate below): shadow = its signed
        record and the eval subset's case list are logged; enforce = no fast-forward without a signed green record
        for the tree and a green eval subset (`evals run --changed --paths`, as evalbot). The subset runs only
        when an eval case `covers` a changed path (evals' own selection, dry run first); otherwise the land has no
        eval step and says "evals: skipped (no covered paths)". --override REASON (the
        main session only, never a subagent) lands without them, logged with the reason; local CI still runs.
        After a land: `assistant-publish push --cron --bg` (a snapshot of main to its GitHub repo, scanned first),
        and `board poke landed BRANCH` (Board v2: the items whose done-when probes name the branch
        or one of its pre-rebase commits).
  assistant land BRANCH [--wait | --no-wait] [--timeout S]   with the land queue on (~/.config/assistant/land-queue
        holding "on"; ASSISTANT_LAND_QUEUE=0|1 wins; off = the land above; rollback: rm that file): verify, then
        land. Here only the quick checks (rebase onto main, fail-fast, ruff + format, the AST ban, secretscan:
        seconds; a conflict or red ends it now, exit 5 / 6), then the branch is queued (landq/entries/qN.json; a
        queued entry of the same branch is superseded). --wait (the default everywhere: a subagent's shell and the
        main session's carry the same Claude Code environment, AI_AGENT ending in _agent in both, so nothing tells
        them apart) follows it to its end with the exit codes above (0 landed with main's commit, 5 conflict, 6
        red with the red line, 7 --timeout (3600 s) or the queue stalled: the entry stays queued); --no-wait (the
        main session) returns "queued qN". ONE worker (landq/worker.lock; started by land, land status, --wait and
        the cron line; exits when idle) runs two lanes: the batch lane takes up to 4 queued entries in order,
        rebased one onto the other on main into branch landq/b-N (a branch that conflicts with one before it
        waits for the next batch; one that conflicts with main is a conflict), and runs the land above on it as a
        child (`land landq/b-N --batch bN`): host CI beside ONE runner job, the eval subset, ONE fast-forward and
        deploy; the deploy row names the first member (its land_cmd_start, landq {id, batch, size, lane,
        queued_secs, members, sha}), one more land row and one board poke per other member, each with its own
        last commit on main; a member's branch moves to that commit when it still stands where it was queued.
        Main and ~/bin only ever get trees with a signed green record: the queue never reverts. The gated lane
        (gated paths, --adopt) lands one entry at a time beside it. A red batch splits in halves at the front of
        the queue (first half alone on main, then the second on top; a lone second half whose tree is the red
        batch's tree is red without a run); one member left is red: exit 6 to its waiter, else one
        ~/inbox/land-red-* note (kind land.red, at the beat) naming its batch-mates. Runner down or refusing:
        back to queued, retried after 5 min. A smoke failure after the deploy (rolled back as before): each
        member once alone. Crash: a new worker adopts a live batch land, else main at the batch tip = landed
        (missing rows written, an undeployed tip deployed once), main unchanged = the members go back to the
        front. --override skips the queue (the land above).
  assistant land status [qN | BRANCH] [--json] [--wait]   the queue: queued (position, lane), testing (batch,
        members, phase, seconds), landed (commit, time), red / conflict / error (exit, red line, batch-mates),
        superseded; starts the worker when something waits. With qN or BRANCH and --wait: follow that entry
        (e.g. one queued with --no-wait) to its end, exit codes as --wait. A branch named status can't be landed.
  assistant ci --runner hub [--path DIR] [--base REF] [--wait S]   run CI on the hub's runner (ci container: no
        network, no secrets) for DIR's committed HEAD; prints the signed record; exit 0 = signed green
  assistant ci-gate status [--json] [--lands N] | shadow|enforce|enforce-gated|off --reason TEXT [--force]
        the land gate's mode; enforce-gated enforces only lands whose changed paths hit ops/ci/gated-paths.json
        (prompts, skills, tools, reflexes, workflows; read from main), shadow for the rest. status shows the scope,
        the overrides with their reasons, and the done-when share: gated lands that carried a green eval subset
        (~/.config/assistant/ci-gate; default shadow), logged in deploys.jsonl; enforce refuses before 5 signed
        green lands unless --force; off = no runner jobs at all (the rollback)
  assistant legacy-main [--after BRANCH] [--no-note]   every tool suite on a detached checkout of main into main's
        record (how post-land), niced, one at a time (a run checks main again when it moved); a land whose suites
        were a selection starts it detached (log: state legacy-main.log); skipped above load 30 (ci-nightly covers
        main). A suite green before and red now -> one ~/inbox/ci-nightly-<epoch>.md note.
  assistant ci-status [--json] [--clear KEY|all]
        shared CI blockers (core.ciblock): what stops every land (a suite red on main, the runner refusing a submit
        or giving no signed record outside the branch's own red), each raised at once as ONE ci.blocked agenda event
        (~/inbox/ci-blocked-*, delivery now, `events ack`), again at most every 2 h while still seen; cleared when
        the suite is green on main / a land gets a signed green record (or --clear). Plus the 24 h blocked time;
        exit 3 while any blocker is open
  assistant deploy [--rev REV] [--adopt DEST].. [--force]
        install the managed files of a main-checkout commit (ops/deploy/manifest.toml): staged in
        ~/.local/state/assistant/stage, atomic rename into place; core -> ~/bin/lib/core.releases/<rel>/core via an
        atomic symlink (last 3 kept); smoke command per path, a failure rolls everything back; refuses on drift
        (live hash != last deployed) and on unmanaged files in the way (--adopt DEST after checking them).
  assistant rollback [--to REL] [--force]   undo the last deploy (bytes + modes), or re-install release REL
  assistant deploy --target hub [--rev REV] [--plan] [--wait S] [--wait-calls MIN] [--force]
        the claude-hub release (hubkit): build ~/.local/share/claude-hub/releases/<rel> from a git archive, render the
        install kit (values from the profile), flip releases/CURRENT, wait up to S s (default 90) for /health to run
        it; a release that does not come up restores the previous CURRENT (exit 4). Hub unreachable (not installed
        yet): the release stays staged as CURRENT and the owner's plan is printed (`hub kit card`, `hub kit paste`).
        --plan prints what would happen and changes nothing. Never restarts a container (the loader follows CURRENT).
        Call guard: the switch restarts the voice relay (claude-hub-voice's loader follows CURRENT) and would drop a
        live call, so CURRENT flips only while `voice-relay status` says live 0: a live call is waited out for up to
        MIN minutes (default 10; polled before the deploy lock and again right before the flip), then exit 9 with
        CURRENT unchanged. Status unreadable (relay down or held, hub away): deploys anyway, said and logged ("voice"
        in the deploy record). --force skips the guard and writes CURRENT with the ":now" attempt suffix, so the
        voice loader switches at once too (a live call drops).
  assistant rollback --target hub [--to REL] [--plan] [--wait S]   CURRENT back to the previous hub release (or REL);
        never waits for a call (":now" attempt: the voice loader switches at once)
  assistant drift [--json]                  managed files whose live bytes/mode/link differ (exit 3 if any)
  assistant status [--json]                 release, managed files, drift, lock, recent deploys, metric counts
  assistant cron list | add NAME 'SCHEDULE' 'COMMAND' | rm NAME
        edit ~/crontab under the deploy lock (lines tagged `# assistant:NAME`), validate, atomic mv, `crontab ~/crontab`
  assistant cron install [--features a,b | --features all] [--dry-run]
        a fresh ~/crontab from the registry (core/schedules.toml): the lines of the enabled features, each tagged.
        Features: --features (switch or registry feature names; all = every switch's features), else the profile's
        `features` switches set true ({switch: bool}, mapped by core.schedules.FEATURE_SWITCHES), else core only
        (CORE_FEATURES; core is always in). Jobs marked owner_only in the registry are never scheduled. Keeps an
        installed crontab; adds a PATH line when the file has none. Lines already tagged stay as they are
        (idempotent); --dry-run prints the lines and writes nothing
  assistant uninstall [--dry-run | --yes]
        remove what the deploy put under $HOME: the files of the deployed release (~/.local/state/assistant/
        deployed.json) whose bytes are still the deployed ones (a changed file is kept and named), the core link and
        its releases, the folders left empty (with the *.pyc in their __pycache__); and the ~/crontab lines tagged
        `# assistant:` (`crontab ~/crontab`).
        Never the repo, ~/workspace, the rest of ~/.claude (settings, skills, agents: `assistant init`'s), the
        profile, ~/.secrets or ~/.local/state. --dry-run lists it and changes nothing; the removal needs --yes (ask the
        user first)
  assistant init [--force] [--dry-run] [--json]
        a new user's starter files from the repo's templates/ (core/starter.py): ~/workspace/CLAUDE.md and its
        workspace files, ~/.claude/settings.json (the example's hooks), skills and agents, each only while the
        profile's feature switches it needs are on (templates/manifest.toml; `profile-get --features`); profile
        placeholders filled in. Existing files are kept (an existing settings.json gets the starter version beside
        it as settings.starter.json); --force overwrites them, keeping <file>.bak. Idempotent.
  assistant lint-bin [--check | --update] [--json] [--dir DIR]
        ratchet over ~/bin: files defining load_json / save_json|atomic_write / write_note|write_inbox, using urlopen,
        using flock; --check fails when a count rises above ops/lint/bin-baseline.json; --update lowers it
  assistant --version

Log: ~/.local/state/assistant/deploys.jsonl. Every deploy / rollback / cron edit runs brain-commit before and after.
Exit: 0 ok · 1 error · 2 usage, or git conflict markers left in manifest.toml / ops/lint/*.json (file:line on
stderr) · 3 drift or refused · 4 smoke failed (rolled back), or the hub release did not come up (CURRENT restored) ·
5 rebase conflict · 6 CI failed (or a suite red on main still red) · 7 lock timeout · 8 owner approval pending (hub
protected paths) · 9 a voice call stayed live past --wait-calls (deploy --target hub; CURRENT unchanged).
A stdout reader that quits early (`assistant status | head`) costs no traceback and never cuts a deploy short.
Env: ASSISTANT_REPO (repo, default ~/opt/assistant) · ASSISTANT_LAND_QUEUE (0|1: the land queue's switch) ·
ASSISTANT_LOCK_WAIT (s, 900; per queue position: the clock
restarts when a waiter moves up) · ASSISTANT_CI_LEGACY_OUT (ci writes its per-suite suite results there; land sets
it) · ASSISTANT_TEST=1 (refuse the
real home) · test seams honoured only with ASSISTANT_TEST=1: ASSISTANT_BRAIN_COMMIT_BIN, ASSISTANT_CRONTAB_BIN,
ASSISTANT_LAND_CI (command run instead of `ci` by land), ASSISTANT_LANDQ_CHECK (instead of the queue's quick
checks; unset in test mode = skipped), ASSISTANT_LANDQ_TICK (the worker's and waiter's poll, s),
ASSISTANT_PUBLISH_BIN (started after a land),
ASSISTANT_BOARD_MARK_BIN (the `board` a land pokes),
ASSISTANT_LIVE_BIN (the bin dir the inbox-lint step stages over, default ~/bin), ASSISTANT_INBOX_LINT
(inbox_lint.py; its allowlist.txt is read from beside it), ASSISTANT_VOICE_RELAY_BIN (the call guard's
`voice-relay`; unset in test mode = status unreadable), ASSISTANT_CALL_POLL (s between the guard's status reads, 15).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path


def _find_core() -> None:
    here = os.path.dirname(os.path.realpath(__file__))
    for cand in (os.path.join(here, "lib"), os.path.dirname(here), os.path.expanduser("~/opt/assistant")):
        if os.path.isfile(os.path.join(cand, "core", "paths.py")):
            sys.path.insert(0, cand)
            if os.path.isdir(os.path.join(cand, "packages")):  # the repo: hubkit lives in packages/
                sys.path.insert(1, os.path.join(cand, "packages"))
            return
    raise SystemExit("assistant: core not found (~/bin/lib/core or the repo)")


_find_core()
from core import clock, paths  # noqa: E402

HEADER = "# managed: ~/opt/assistant — edit there, deploy with assistant land"
KEEP = 3
CI_STEPS = (
    "sync",
    "ruff",
    "pyright",
    "pytest",
    "suites",
    "ast",
    "schemas",
    "ratchet",
    "inbox-lint",
    "secretscan",
    "precheck",
    "bun",
)
INBOX_LINT = ("opt", "inbox-lint-tests", "inbox_lint.py")  # under the real home; allowlist.txt beside it
INBOX_LINT_TREE = ("tests", "legacy", "inbox-lint", "inbox_lint.py")  # the tree's copy wins (the runner has no ~/opt)
BANNED_DEFS = {"load_json", "save_json", "atomic_write", "write_note", "write_inbox"}
RATCHET = ("load_json", "save_atomic", "note_helpers", "urlopen", "flock")
PY_VERSIONS = (("3.13", "3.13", ".venv-3.13"),)  # 3.13 only: the runner's interpreter too
# `ci --host`: the runner runs these, nothing host-bound. A step moves only after a probe job ran it green there
# (packages/hub/ciworker.py PROBE_FILE): add it here and drop it from ciworker.LOCAL_STEPS together. ratchet and
# inbox-lint run over the would-be ~/bin the runner builds from the tree into its job HOME (_runner_home); bun on the
# toolchain's vendored node_modules. legacy and pytest: `ci --host` runs only the host-bound suites and tests
# (tests/runner-skip.txt lines, ~/opt suites, real-home ones), the runner the rest. ruff and ast run on both sides:
# seconds here (the tree's venv ruff, same lock as the runner's), so a lint red fails a land before the runner's
# answer instead of after it (6 of 15 runner reds on one day were ruff or ast).
HOST_ONLY_SKIP = {"pyright", "schemas", "ratchet", "inbox-lint", "bun"}
RUNNER_SKIP = ("tests", "runner-skip.txt")
LEGACY_SLOW = 5.0  # a legacy suite this slow (s) or red: its time goes into the step record ("suites")
CI_TMP_PREFIX = "assistant-ci."
CI_OWNER = ".owner"  # {"pid", "start"} of the ci process that made the run dir: while it lives, no sweep touches it
CI_SWEEP_AGE = 2 * 3600.0  # a run dir untouched this long, its ci gone, is a leftover (a whole CI run takes minutes)
CI_ROOT = (".cache", "aci")  # local run dirs: under the real home (appdata disk), not /tmp (the docker.img layer);
# short on purpose: test Postgres puts its socket 5 levels down (AF_UNIX paths stay < 108 bytes)
PYTEST_TIMEOUT = 900.0
PYTEST_GRACE = 30.0  # SIGINT -> SIGKILL for the pytest process group
# pytest-xdist workers: ASSISTANT_CI_PYTEST_WORKERS overrides (0 = serial).
# The hub runner (--toolchain) stays serial unless set: its ci container's /tmp is a 1 GB tmpfs.
PYTEST_WORKERS = 8
PYTEST_DIST = "loadfile"  # a test file stays on one worker: its module fixtures (test Postgres, hubd) start once
LAND_CI_TIMEOUT = 3600.0


class Fail(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code
        self.shared = False  # a shared blocker (runner down or refusing), not the tree's own red


def stdout_gone() -> None:
    """The reader of stdout went away (`assistant status | head`): send further output to /dev/null, so the command
    finishes its work (a deploy is never cut short by a closed pipe) and exits without a BrokenPipeError traceback."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(fd, sys.stdout.fileno())
        os.close(fd)
    except (OSError, ValueError):
        pass


def say(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        stdout_gone()


def run(cmd, cwd=None, env=None, timeout=None, check=False, text=True, input=None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, capture_output=True, text=text, input=input)
    if check and r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise Fail(f"{' '.join(map(str, cmd))[:120]}: exit {r.returncode}: {' | '.join(tail)[:300]}")
    return r


def git(repo, *args, check=True) -> str:
    return run(["git", "-C", str(repo), *args], check=check).stdout.strip()


def test_seam(name: str) -> str | None:
    return os.environ.get(name) if paths.test_mode() else None


# ---------------------------------------------------------------- state
def st(*parts: str) -> Path:
    return paths.state(*parts)


def read_json(p: Path, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", dir=p.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def log(rec: dict) -> None:
    p = st("deploys.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": round(clock.now(), 3), **rec}
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def history() -> list[dict]:
    out = []
    try:
        with open(st("deploys.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


QUEUE_DIR = "deploy.queue"  # FIFO tickets for the deploy lock: <n>.ticket, n from .seq (taken under a short flock)


def _queue_dir() -> Path:
    return st(QUEUE_DIR)


def _ticket_take(what: str) -> Path:
    """A ticket at the back of the queue: the next number from .seq and the waiter's pid + start time (a dead or
    reused pid prunes it). The number is taken and the ticket written under .seq's flock, so numbers never collide."""
    q = _queue_dir()
    q.mkdir(parents=True, exist_ok=True)
    fd = os.open(q / ".seq", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # held for a read and two small writes
        try:
            n = int(os.read(fd, 64).decode().strip() or 0) + 1
        except ValueError:
            n = 1 + max([_ticket_no(f) for f in q.glob("*.ticket")] + [0])
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(n).encode())
        me = _proc_stat(os.getpid())
        t = q / f"{n}.ticket"
        tfd, tmp = tempfile.mkstemp(prefix=f".{n}.", suffix=".tmp", dir=q)
        with os.fdopen(tfd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "start": me[2] if me else "", "what": what, "since": clock.now()}, f)
        os.replace(tmp, t)
        return t
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


_PHASE: dict = {}  # this process's land: {"branch", "rerun", "start"} while land() runs (the queue's phase record)
PHASES = (
    "rebase",
    "fail-fast",
    "quick checks",
    "queued",
    "CI local",
    "CI host + runner",
    "runner",
    "evals",
    "waiting for lock",
)


def _land_phase(phase: str, **kw) -> None:
    """The running land's phase into deploy.queue/land-<pid>.phase ({pid, start, what, phase, since, land_since,
    rerun, why}): the dashboard's land queue shows it (cifeed) instead of a bare "starting". Best effort, never fails a
    land; land() removes it at the end."""
    if not _PHASE.get("branch"):
        return
    me = _proc_stat(os.getpid())
    rec = {
        "pid": os.getpid(),
        "start": me[2] if me else "",
        "what": f"land {_PHASE['branch']}",
        "phase": phase,
        "since": clock.now(),
        "land_since": _PHASE.get("start"),
        "rerun": _PHASE.get("rerun", 0),
        **kw,
    }
    with contextlib.suppress(OSError):
        q = _queue_dir()
        q.mkdir(parents=True, exist_ok=True)
        write_json(q / f"land-{os.getpid()}.phase", rec)


def _land_phase_drop() -> None:
    with contextlib.suppress(OSError):
        (_queue_dir() / f"land-{os.getpid()}.phase").unlink()
    _PHASE.clear()


def _ticket_no(p: Path) -> int:
    try:
        return int(p.name.split(".", 1)[0])
    except ValueError:
        return 0


def _ticket_alive(d) -> bool:
    try:
        return _alive({"pid": int(d["pid"]), "start": str(d["start"])})
    except (KeyError, TypeError, ValueError):
        return False


def lock_queue(prune: bool = True) -> list[dict]:
    """The live tickets in order (position 1 = next to get the lock; the holder has none). prune: remove the tickets
    of dead waiters (a killed land, a reused pid) and unreadable ones as they are found."""
    q = _queue_dir()
    out = []
    try:
        files = sorted((f for f in q.glob("*.ticket") if _ticket_no(f) > 0), key=_ticket_no)
    except OSError:
        return out
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            d = None
        if isinstance(d, dict) and _ticket_alive(d):
            out.append({"path": f, "no": _ticket_no(f), **d})
        elif prune:
            with contextlib.suppress(FileNotFoundError):
                f.unlink()
    return out


class Lock:
    """The one deploy lock (land / deploy / rollback / cron). Re-entrant inside one process. First come, first
    served: each waiter takes a numbered ticket in deploy.queue/ and only the front ticket tries the flock; tickets of
    dead waiters are pruned by whoever looks. The wait budget (ASSISTANT_LOCK_WAIT, 900 s) is per queue position: it
    restarts each time the waiter moves up, so exit 7 means the queue stood still that long."""

    held = 0

    def __init__(self, what: str):
        self.what = what
        self.fd = None

    def __enter__(self):
        if Lock.held:
            Lock.held += 1
            return self
        p = st("deploy.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        wait = float(os.environ.get("ASSISTANT_LOCK_WAIT") or 900)
        mine: Path | None = None
        try:
            mine = _ticket_take(self.what)
            end = time.monotonic() + wait
            shown = None
            while True:
                if not mine.exists():  # removed by hand: back of the queue
                    mine = _ticket_take(self.what)
                live = [t["path"] for t in lock_queue()]
                pos = live.index(mine) + 1 if mine in live else len(live) + 1
                if pos == 1:
                    try:
                        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        pass
                if pos != shown:
                    if shown is not None and pos < shown:
                        end = time.monotonic() + wait  # moved up: a fresh budget for the new position
                    shown = pos
                    say(f"queued, position {pos} ({self.what}); lock: {lock_holder() or 'free'}")
                if time.monotonic() >= end:
                    raise Fail(
                        f"deploy lock busy: queue position {pos} unchanged for {wait:.0f} s "
                        f"({lock_holder() or 'unknown holder'})",
                        7,
                    )
                time.sleep(0.2)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        finally:
            if mine is not None:
                with contextlib.suppress(FileNotFoundError):
                    mine.unlink()
        os.ftruncate(self.fd, 0)
        os.write(self.fd, json.dumps({"pid": os.getpid(), "what": self.what, "since": clock.now()}).encode())
        Lock.held = 1
        return self

    def __exit__(self, *exc):
        Lock.held -= 1
        if Lock.held == 0 and self.fd is not None:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        return False


def lock_holder() -> str:
    try:
        fd = os.open(st("deploy.lock"), os.O_RDONLY)
    except FileNotFoundError:
        return ""
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # probe: free if we get it
            fcntl.flock(fd, fcntl.LOCK_UN)
            return ""
        except BlockingIOError:
            raw = os.read(fd, 4096).decode(errors="replace")
            try:
                h = json.loads(raw)
                return (
                    f"pid {h.get('pid')} {h.get('what')} since {time.strftime('%H:%M:%S', time.localtime(h['since']))}"
                )
            except (ValueError, KeyError, TypeError):
                return "held"
    finally:
        os.close(fd)


# ---------------------------------------------------------------- manifest + rendering
MANIFEST = ("ops", "deploy", "manifest.toml")
# Config files the CLI parses itself (TOML/JSON): a merge conflict left in one is refused up front with file:line.
CONFIG_FILES = (MANIFEST, ("ops", "lint", "ast-allow.json"), ("ops", "lint", "bin-baseline.json"))
CONFLICT = re.compile(r"(?:<{7}|>{7}|\|{7})(?: |$)|={7}$")


def conflict_lines(p: Path) -> list[int]:
    """1-based numbers of the lines in p that are git conflict markers (empty when p is missing or clean)."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [i for i, ln in enumerate(text.splitlines(), 1) if CONFLICT.match(ln)]


def refuse_conflicts(root: Path, files=CONFIG_FILES) -> None:
    """Fail (exit 2) naming file:line of every git conflict marker left in the given repo files."""
    hits = [f"{root.joinpath(*rel)}:{n}" for rel in files for n in conflict_lines(root.joinpath(*rel))]
    if hits:
        more = f" (+{len(hits) - 6} more)" if len(hits) > 6 else ""
        raise Fail(f"git conflict markers at {', '.join(hits[:6])}{more}: resolve the merge, then rerun", 2)


def load_manifest(root: Path) -> dict:
    p = root.joinpath(*MANIFEST)
    refuse_conflicts(root, (MANIFEST,))
    try:
        with open(p, "rb") as f:
            m = tomllib.load(f)
    except FileNotFoundError:
        raise Fail(f"no manifest at {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise Fail(f"{p}: invalid TOML: {e}") from None
    files = m.get("file", [])
    seen = set()
    for e in files:
        for k in ("src", "dest"):
            if not isinstance(e.get(k), str) or not e[k]:
                raise Fail(f"manifest: entry without {k}: {e}")
        if e["dest"].startswith("/") or ".." in e["dest"].split("/") or e["dest"] in seen:
            raise Fail(f"manifest: bad or duplicate dest {e['dest']}")
        seen.add(e["dest"])
        e.setdefault("mode", "0755" if e["dest"].startswith("bin/") else "0644")
        e.setdefault("header", True)
        int(e["mode"], 8)
        if not (root / e["src"]).is_file():
            raise Fail(f"manifest: missing src {e['src']}")
    core = m.get("core")
    if core:
        if not (root / core.get("src", "")).is_dir() or not core.get("dest"):
            raise Fail("manifest: core needs an existing src dir and a dest")
    return {"file": files, "core": core}


def render(data: bytes, header: bool) -> bytes:
    if not header:
        return data
    lines = data.split(b"\n")
    if any(HEADER.encode() in ln for ln in lines[:5]):
        return data
    at = 1 if lines and lines[0].startswith(b"#!") else 0
    lines.insert(at, HEADER.encode())
    return b"\n".join(lines)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_state(p: Path) -> dict | None:
    try:
        s = os.lstat(p)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(s.st_mode):
        return {"link": os.readlink(p)}
    if not stat.S_ISREG(s.st_mode):
        return {"other": True}
    return {"sha256": sha256(p.read_bytes()), "mode": f"{stat.S_IMODE(s.st_mode):04o}"}


def tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for dp, dns, fns in os.walk(d):
        dns[:] = sorted(x for x in dns if x != "__pycache__")
        for fn in sorted(fns):
            fp = Path(dp) / fn
            h.update(str(fp.relative_to(d)).encode() + b"\0")
            h.update(fp.read_bytes() + b"\0")
            h.update(f"{stat.S_IMODE(fp.stat().st_mode):o}".encode() + b"\n")
    return h.hexdigest()


def install_bytes(data: bytes, dest: Path, mode: int) -> None:
    """Write via a staged file in the state dir, then an atomic rename into place (same filesystem), else a
    sibling temp file + rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    sdir = st("stage")
    sdir.mkdir(parents=True, exist_ok=True)
    tmp_dir = sdir if os.stat(sdir).st_dev == os.stat(dest.parent).st_dev else dest.parent
    fd, tmp = tempfile.mkstemp(prefix=".assistant-", dir=tmp_dir)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def swap_link(link: Path, target: str | None) -> None:
    """Atomically point link at target (relative); None removes it. Refuses a real directory in the way."""
    if link.exists() and not link.is_symlink():
        raise Fail(f"{link} is a real file/dir, not a managed symlink: refusing", 3)
    if target is None:
        if link.is_symlink():
            link.unlink()
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.parent / f".{link.name}.{os.getpid()}.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


# ---------------------------------------------------------------- drift
def drift_report(deployed: dict) -> list[dict]:
    home = paths.home()
    out = []
    for dest, want in sorted(deployed.get("files", {}).items()):
        have = file_state(home / dest)
        if have is None:
            out.append({"dest": dest, "why": "missing"})
        elif have.get("sha256") != want["sha256"]:
            out.append({"dest": dest, "why": "modified" if "sha256" in have else "replaced"})
        elif have.get("mode") != want["mode"]:
            out.append({"dest": dest, "why": f"mode {have.get('mode')} != {want['mode']}"})
    core = deployed.get("core")
    if core:
        link = home / core["dest"]
        tgt = os.readlink(link) if link.is_symlink() else None
        if tgt != core["target"]:
            out.append({"dest": core["dest"], "why": "core link moved" if tgt else "core link missing"})
        elif tree_hash(link.parent / tgt) != core["tree"]:
            out.append({"dest": core["dest"], "why": "core release modified"})
    return out


# ---------------------------------------------------------------- deploy / rollback
def brain(msg: str) -> None:
    b = test_seam("ASSISTANT_BRAIN_COMMIT_BIN")
    if b is None:
        if paths.test_mode():
            return
        b = str(paths.bin_dir("brain-commit"))
        if not os.access(b, os.X_OK):
            return
    try:
        r = run([b, msg], timeout=180)
        if r.returncode != 0:
            say(f"  warning: brain-commit exit {r.returncode} ({msg})")
    except subprocess.TimeoutExpired:
        say(f"  warning: brain-commit timed out ({msg})")


def snapshot(rel_dir: Path, dests: list[str], core_dest: str | None, deployed: dict) -> dict:
    """Record the live state of dests (bytes + mode, or absent) before an install, for an exact undo."""
    home = paths.home()
    bdir = rel_dir / "before"
    if bdir.exists():
        shutil.rmtree(bdir)
    bdir.mkdir(parents=True)
    snap = {"files": {}, "core_target": None, "core_dest": core_dest, "prev_deployed": deployed}
    for dest in dests:
        p = home / dest
        s = file_state(p)
        if s is None:
            snap["files"][dest] = {"absent": True}
        elif "sha256" in s:
            q = bdir / "files" / dest
            q.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, q)
            snap["files"][dest] = s
        else:
            raise Fail(f"{dest}: live path is not a regular file: refusing", 3)
    if core_dest:
        link = home / core_dest
        snap["core_target"] = os.readlink(link) if link.is_symlink() else None
    write_json(rel_dir / "before.json", snap)
    return snap


def restore(rel_dir: Path) -> None:
    home = paths.home()
    snap = read_json(rel_dir / "before.json", None)
    if snap is None:
        raise Fail(f"no undo snapshot in {rel_dir}")
    if snap.get("core_dest"):
        swap_link(home / snap["core_dest"], snap["core_target"])
    for dest, s in snap["files"].items():
        p = home / dest
        if s.get("absent"):
            if p.is_file() or p.is_symlink():
                p.unlink()
        else:
            install_bytes((rel_dir / "before" / "files" / dest).read_bytes(), p, int(s["mode"], 8))
    write_json(st("deployed.json"), snap["prev_deployed"])


def smoke(entries: list[tuple[str, str]]) -> list[str]:
    home = paths.home()
    bad = []
    env = dict(os.environ)
    for dest, cmd in entries:
        c = cmd.format(
            dest=shlex.quote(str(home / dest)), home=shlex.quote(str(home)), lib=shlex.quote(str(home / "bin" / "lib"))
        )
        try:
            r = run(["sh", "-c", c], env=env, timeout=60)
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        if not ok:
            bad.append(dest)
    return bad


def activate(
    rid: str,
    rel_dir: Path,
    man: dict,
    files: dict,
    core_tree: str | None,
    adopt: set,
    force: bool,
    op: str,
    extra: dict,
) -> dict:
    """Install release rid (rendered files in rel_dir/files, core already in core.releases/rid) with undo +
    smoke. Caller holds the lock."""
    home = paths.home()
    t0 = clock.now()
    deployed = read_json(st("deployed.json"), {})
    drift = drift_report(deployed)
    if drift and not force:
        log({"op": op, "release": rid, "ok": False, "refused": "drift", "drift": drift, **extra})
        raise Fail(
            "drift, refusing ("
            + ", ".join(f"{d['dest']}: {d['why']}" for d in drift)
            + "); fix or redeploy with --force after checking",
            3,
        )
    managed = set(deployed.get("files", {}))
    for dest in files:
        if (home / dest).exists() and dest not in managed and dest not in adopt:
            log({"op": op, "release": rid, "ok": False, "refused": "unmanaged", "dest": dest, **extra})
            raise Fail(f"{dest} exists and is not managed: check it, then deploy with --adopt {dest}", 3)
    core = man.get("core")
    brain(f"before assistant {op} {rid}")
    snapshot(rel_dir, sorted(files), core["dest"] if core else None, deployed)
    new = {"release": rid, "sha": extra.get("sha"), "ts": round(clock.now(), 3), "files": {}, "core": None}
    try:
        if core:
            link = home / core["dest"]
            target = f"{link.name}.releases/{rid}/{link.name}"
            swap_link(link, target)
            new["core"] = {"dest": core["dest"], "target": target, "tree": core_tree}
        for dest, meta in sorted(files.items()):
            data = (rel_dir / "files" / dest).read_bytes()
            install_bytes(data, home / dest, int(meta["mode"], 8))
            new["files"][dest] = {"sha256": sha256(data), "mode": meta["mode"]}
        for dest in deployed.get("files", {}):
            if dest not in new["files"]:
                say(f"  note: {dest} left the manifest; left in place, no longer managed")
        write_json(st("deployed.json"), new)
        checks = [(core["dest"], core["smoke"])] if core and core.get("smoke") else []
        checks += [(d, m["smoke"]) for d, m in sorted(files.items()) if m.get("smoke")]
        bad = smoke(checks)
    except Exception:
        restore(rel_dir)
        log({"op": op, "release": rid, "ok": False, "rolled_back": True, "why": "install error", **extra})
        raise
    if bad:
        restore(rel_dir)
        log(
            {
                "op": op,
                "release": rid,
                "ok": False,
                "rolled_back": True,
                "why": "smoke",
                "failed": bad,
                "start": t0,
                "end": clock.now(),
                **extra,
            }
        )
        raise Fail("smoke failed for " + ", ".join(bad) + ": rolled back", 4)
    meta = read_json(rel_dir / "meta.json", {})
    meta["activated"] = clock.now()
    write_json(rel_dir / "meta.json", meta)
    log(
        {
            "op": op,
            "release": rid,
            "ok": True,
            "files": len(files),
            "core": bool(core),
            "start": t0,
            "end": clock.now(),
            **extra,
        }
    )
    prune(rid)
    brain(f"after assistant {op} {rid}")
    return new


def prune(active: str) -> None:
    rels = st("releases")
    if not rels.is_dir():
        return
    metas = []
    for d in rels.iterdir():
        m = read_json(d / "meta.json", {})
        metas.append((m.get("activated") or m.get("built") or 0, d.name))
    metas.sort(reverse=True)
    keep = {n for _, n in metas[:KEEP]} | {active}
    deployed = read_json(st("deployed.json"), {})
    if deployed.get("release"):
        keep.add(deployed["release"])
    core = deployed.get("core")
    for _, name in metas:
        if name in keep:
            continue
        shutil.rmtree(rels / name, ignore_errors=True)
        if core:
            link = paths.home() / core["dest"]
            shutil.rmtree(link.parent / f"{link.name}.releases" / name, ignore_errors=True)


def build_release(rid: str, root: Path, man: dict) -> tuple[Path, dict, str | None]:
    rel_dir = st("releases", rid)
    if (rel_dir / "files").exists():
        shutil.rmtree(rel_dir / "files")
    files = {}
    for e in man["file"]:
        data = render((root / e["src"]).read_bytes(), bool(e["header"]))
        q = rel_dir / "files" / e["dest"]
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(data)
        os.chmod(q, int(e["mode"], 8))
        files[e["dest"]] = {"mode": e["mode"], "smoke": e.get("smoke", ""), "src": e["src"]}
    core_tree = None
    core = man.get("core")
    if core:
        link = paths.home() / core["dest"]
        rdir = link.parent / f"{link.name}.releases" / rid
        sroot = st("stage", f"{rid}-core")
        if sroot.exists():
            shutil.rmtree(sroot)
        shutil.copytree(root / core["src"], sroot / link.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (sroot / "MANAGED").write_text(HEADER + "\n")
        core_tree = tree_hash(sroot / link.name)
        if rdir.exists():
            if (rdir / link.name).is_dir() and tree_hash(rdir / link.name) == core_tree:
                shutil.rmtree(sroot)
            else:
                shutil.rmtree(rdir)
        if not rdir.exists():
            rdir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(sroot, rdir)  # stage/<rid>-core/{core,MANAGED} -> core.releases/<rid>/
    write_json(
        rel_dir / "meta.json",
        {"release": rid, "built": clock.now(), "files": files, "core": core, "core_tree": core_tree},
    )
    return rel_dir, files, core_tree


def deploy(rev: str | None = None, adopt=(), force: bool = False, op: str = "deploy", extra=None) -> dict:
    repo = paths.repo()
    sha = git(repo, "rev-parse", "--verify", (rev or "HEAD") + "^{commit}")
    rid = sha[:12]
    with Lock(f"{op} {rid}"):
        stage = st("stage", rid)
        if stage.exists():
            shutil.rmtree(stage)
        src = stage / "src"
        src.mkdir(parents=True)
        arch = run(["git", "-C", str(repo), "archive", "--format=tar", sha], text=False, check=True).stdout
        run(["tar", "-x", "-C", str(src)], input=arch, text=False, check=True)
        man = load_manifest(src)
        rel_dir, files, core_tree = build_release(rid, src, man)
        shutil.rmtree(stage, ignore_errors=True)
        new = activate(rid, rel_dir, man, files, core_tree, set(adopt), force, op, {"sha": sha, **(extra or {})})
    say(f"deployed {rid}: {len(new['files'])} file(s)" + (", core" if new.get("core") else ""))
    return new


def rollback(to: str | None = None, force: bool = False) -> None:
    with Lock(f"rollback {to or 'last'}"):
        deployed = read_json(st("deployed.json"), {})
        if to is None:
            cur = deployed.get("release")
            if not cur:
                raise Fail("nothing deployed, nothing to roll back", 3)
            drift = drift_report(deployed)
            if drift and not force:
                raise Fail("drift, refusing rollback (" + ", ".join(d["dest"] for d in drift) + "); --force", 3)
            rel_dir = st("releases", cur)
            brain(f"before assistant rollback {cur}")
            restore(rel_dir)
            back = read_json(st("deployed.json"), {}).get("release")
            log({"op": "rollback", "release": cur, "to": back, "ok": True})
            brain(f"after assistant rollback {cur}")
            say(f"rolled back {cur} -> {back or 'nothing (pre-first-deploy state)'}")
            return
        matches = [d for d in (st("releases").iterdir() if st("releases").is_dir() else []) if d.name.startswith(to)]
        if len(matches) != 1:
            raise Fail(f"release {to}: {'ambiguous' if matches else 'not kept'} (assistant status lists kept ones)", 3)
        rel_dir = matches[0]
        meta = read_json(rel_dir / "meta.json", None)
        if not meta or not (rel_dir / "files").is_dir():
            raise Fail(f"release {rel_dir.name} is incomplete", 3)
        man = {"file": [], "core": meta.get("core")}
        if man["core"]:
            link = paths.home() / man["core"]["dest"]
            if not (link.parent / f"{link.name}.releases" / rel_dir.name / link.name).is_dir():
                raise Fail(f"core of release {rel_dir.name} was pruned", 3)
        activate(
            rel_dir.name,
            rel_dir,
            man,
            meta["files"],
            meta.get("core_tree"),
            set(),
            force,
            "rollback",
            {"to": rel_dir.name},
        )
        say(f"re-installed release {rel_dir.name}")


# ---------------------------------------------------------------- hub target
HUB_WAIT = 90.0
CALL_WAIT_MIN = 10.0  # deploy --target hub: minutes a live voice call may hold the switch (--wait-calls)
CALL_POLL = 15.0  # s between `voice-relay status` reads while a call is live (test seam ASSISTANT_CALL_POLL)
CALL_ASK = 30.0  # one status read (voice-hub: /gw/health 3 s + voice.status 15 s)


def voice_live() -> tuple[int | None, str]:
    """Live calls on the voice relay by `voice-relay status` (voice-hub's voice.status through the hub gateway; exit 2
    when the relay is down or held) -> (live, "") or (None, why the status could not be read)."""
    exe = test_seam("ASSISTANT_VOICE_RELAY_BIN")
    if exe is None:
        if paths.test_mode():
            return None, "test mode without ASSISTANT_VOICE_RELAY_BIN"
        exe = str(paths.bin_dir("voice-relay"))
    if not os.access(exe, os.X_OK):
        return None, "no voice-relay"
    try:
        r = run([exe, "status"], timeout=CALL_ASK)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"voice-relay status: {type(e).__name__}"
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:]
        return None, f"voice-relay status: exit {r.returncode}" + (f", {tail[0][:160]}" if tail else "")
    try:
        d = json.loads(r.stdout)
    except ValueError:
        d = None
    n = d.get("live") if isinstance(d, dict) else None
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        return None, "voice-relay status: no live count"
    return n, ""


class CallGuard:
    """deploy --target hub waits for live voice calls to end before CURRENT flips (the switch restarts the relay and
    would drop the call). wait(): live 0 -> go; live > 0 -> poll until 0, else exit 9 at the deadline (CURRENT
    unchanged); unreadable -> go, said once. rec = the deploy record's "voice" field. Rollback and --force skip it."""

    def __init__(self, minutes: float) -> None:
        self.minutes = max(0.0, minutes)
        self.end = time.monotonic() + self.minutes * 60
        self.poll = max(0.05, float(test_seam("ASSISTANT_CALL_POLL") or CALL_POLL))
        self.waited = 0.0
        self.rec: dict = {}
        self._said: set[str] = set()

    def _once(self, msg: str) -> None:
        if msg not in self._said:
            self._said.add(msg)
            say(msg)

    def plan(self) -> str:
        n, why = voice_live()
        if n is None:
            return f"voice: status unreadable ({why}): the deploy would go ahead"
        if n == 0:
            return "voice: no live call"
        return f"voice: {n} live call(s): the deploy would wait up to {self.minutes:g} min for them to end (exit 9)"

    def wait(self, rid: str) -> None:
        live_seen = False
        while True:
            n, why = voice_live()
            if n is None:
                self._once(f"voice: status unreadable ({why}): deploying anyway")
                self.rec = {"live": None, "unreadable": why, "waited_s": round(self.waited, 1)}
                return
            if n == 0:
                if live_seen:
                    say("voice: the call is over: deploying")
                self.rec = {"live": 0, "waited_s": round(self.waited, 1)}
                return
            live_seen = True
            left = self.end - time.monotonic()
            if left <= 0:
                self.rec = {"live": n, "waited_s": round(self.waited, 1)}
                log({"op": "deploy", "target": "hub", "release": rid, "ok": False, "why": "voice call live",
                     "voice": self.rec})  # fmt: skip
                raise Fail(
                    f"hub release {rid}: {n} voice call(s) still live after {self.minutes:g} min; CURRENT unchanged. "
                    "Deploy again after the call, with a longer --wait-calls, or --force (drops the call)",
                    9,
                )
            self._once(f"voice: {n} live call(s): waiting up to {self.minutes:g} min for the end (--force drops it)")
            nap = min(self.poll, left)
            time.sleep(nap)
            self.waited += nap


def _hub_wait(rid: str, wait: float) -> tuple[bool, str]:
    """Poll /health until it runs rid with every check ok (-> True), the loader marks CURRENT's attempt bad
    (-> False at once) or the time is up: then True only when rid runs (checks degraded: said, not fatal)."""
    from hubkit import client as hc
    from hubkit import release as hr
    from hubkit.cli import judge

    end = time.monotonic() + max(0.0, wait)
    while True:
        code, h, err = hc.health(timeout=4.0)
        cur = hr.read_current()
        rc, probs = judge(code, h, cur)
        runs = bool(h) and h.get("release") == rid  # type: ignore[union-attr]
        if runs and rc == 0:
            return True, "ok"
        if any("marked bad" in p for p in probs):
            return False, "; ".join(probs)
        if time.monotonic() >= end:
            if runs:
                return True, "runs, degraded: " + "; ".join(probs)
            return False, "; ".join(probs) or err or "no answer"
        time.sleep(min(2.0, max(0.1, end - time.monotonic())))


def _hub_plan_lines(kinfo: dict) -> list[str]:
    return [
        "plan:",
        f"  1. owner: `hub kit card` (his step card), then `hub kit paste install` ({kinfo['paste_lines']} lines) "
        "in Unraid > Terminal",
        "  2. me: `hub health --wait 120`, then `hub status` (kit installed = rendered)",
    ]


def hub_switch(
    rid: str, src: Path | None, sha: str, plan_only: bool, wait: float, op: str, calls: CallGuard | None = None
) -> int:
    """Shared by deploy and rollback --target hub: src = a source tree (deploy) or None (rollback to a kept rid).
    calls = the call guard (deploy); None (rollback, deploy --force) = CURRENT gets the ":now" attempt: the voice
    loader switches at once, a live call drops."""
    from hubkit import HubError
    from hubkit import client as hc
    from hubkit import kit as hk
    from hubkit import release as hr

    try:
        prev = hr.read_current()
        code, h, err = hc.health(timeout=4.0)
        reach = f"reachable, runs {(h or {}).get('release') or '-'}" if code is not None else f"unreachable ({err})"
        if plan_only:
            built = hr.verify(rid) is None
            add = sorted(hr.missing(src, rid)) if built and src is not None else []
            say(f"plan ({op} --target hub; nothing changed):")
            say(
                f"  release {rid}: "
                + ("kept already" if built else "build from " + sha[:12])
                + (f"; adds {', '.join(add)} (built while its switch was off)" if add else "")
            )
            if src is not None:
                new: dict = hk.build(src, sha)["info"]  # type: ignore[assignment]
                old = hk.info()
                say(
                    f"  kit {new['kit']} ({new['paste_lines']}-line paste, profile {new['profile']})"
                    + ("" if old and old.get("kit") == new["kit"] else " · differs from the rendered kit: paste")
                )
            say(f"  CURRENT: {prev[0] if prev else '-'} -> {rid}")
            if code is None:
                say(f"  hub {reach}: stage only, then the owner's paste")
            else:
                say(f"  hub {reach}: wait up to {wait:g} s for /health; failure restores {prev[0] if prev else '-'}")
            why_not = "rollback" if op == "rollback" else "--force"
            say("  " + (calls.plan() if calls else f"voice: not checked ({why_not}): the relay switches at once"))
            return 0
        if src is not None:
            add = sorted(hr.missing(src, rid)) if hr.verify(rid) is None else []
            hr.build(src, rid, sha)
            if add:
                say(f"release {rid}: kept; {', '.join(add)} added (it was built while the switch was off)")
            kinfo = hk.render(src, sha)
        elif hr.verify(rid) is not None:
            raise Fail(f"hub release {rid}: {hr.verify(rid)}", 3)
        else:
            kinfo = hk.info()
        if code is not None:
            _protected_check(rid, h, op)
        if calls is not None:
            calls.wait(rid)  # right before the flip: a call may have started while the release was built
        line = hr.write_current(rid, now=calls is None)
        rec = {"op": op, "target": "hub", "release": rid, "prev": prev[0] if prev else None, "sha": sha}
        rec["voice"] = calls.rec if calls is not None else {"skipped": "rollback" if op == "rollback" else "force"}
        if kinfo and kinfo.get("defaulted"):
            say(f"warning: profile values defaulted ({', '.join(kinfo['defaulted'])}): set them before the paste")
        if code is None:
            write_json(st("hub.json"), {"release": rid, "line": line, "prev": prev[1] if prev else None})
            log({**rec, "ok": True, "staged": True, "kit": (kinfo or {}).get("kit")})
            say(
                f"hub: not reachable at {hc.url()} ({err}): release {rid} staged as CURRENT; "
                "it loads when the stack runs"
            )
            if kinfo:
                say(f"kit {kinfo['kit']} rendered ({kinfo['paste_lines']}-line paste, profile {kinfo['profile']})")
                for ln in _hub_plan_lines(kinfo):
                    say(ln)
            return 0
        ok, why = _hub_wait(rid, wait)
        if not ok:
            hr.restore_current(prev[1] if prev else None)
            log({**rec, "ok": False, "why": why})
            raise Fail(f"hub release {rid} did not come up ({why}); CURRENT restored to {prev[0] if prev else '-'}", 4)
        write_json(st("hub.json"), {"release": rid, "line": line, "prev": prev[1] if prev else None})
        _, h2, _ = hc.health(timeout=4.0)
        keep = {rid} | ({prev[0]} if prev else set()) | {x for x in [(h2 or {}).get("last_good")] if x}
        gone = hr.prune(keep)
        log({**rec, "ok": True, "kit": (kinfo or {}).get("kit"), "pruned": gone})
        say(
            f"hub: {rid} live ({why}) · carries {' '.join(sorted(hr.entries(rid))) or '-'}"
            + (f"; pruned {len(gone)} old release(s)" if gone else "")
        )
        if kinfo and h2 and h2.get("kit") not in (None, "unknown", kinfo.get("kit")):
            say("kit changed since the install: the owner's paste (`hub kit card`, `hub kit paste install`) applies it")
        return 0
    except HubError as e:
        raise Fail(f"hub: {e}", e.code) from None


def deploy_hub(rev: str | None, plan_only: bool, wait: float, wait_calls: float | None = CALL_WAIT_MIN) -> int:
    """wait_calls = minutes a live voice call may hold the switch; None (--force) = no call guard."""
    repo = paths.repo()
    sha = git(repo, "rev-parse", "--verify", "--end-of-options", (rev or "HEAD") + "^{commit}")
    rid = sha[:12]
    calls = None if wait_calls is None else CallGuard(wait_calls)
    if calls is not None and not plan_only:
        calls.wait(rid)  # a call is waited out here, without holding the deploy lock
    with Lock(f"deploy hub {rid}"):
        stage = st("stage", f"hub-{rid}")
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        try:
            arch = run(["git", "-C", str(repo), "archive", "--format=tar", sha], text=False, check=True).stdout
            run(["tar", "-x", "-C", str(stage)], input=arch, text=False, check=True)
            return hub_switch(rid, stage, sha, plan_only, wait, "deploy", calls)
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def rollback_hub(to: str | None, plan_only: bool, wait: float) -> int:
    from hubkit import release as hr

    with Lock(f"rollback hub {to or 'last'}"):
        if to:
            m = [r for r in hr.kept() if r.startswith(to)]
            if len(m) != 1:
                raise Fail(f"hub release {to}: {'ambiguous' if m else 'not kept'} (hub status lists kept ones)", 3)
            rid = m[0]
        else:
            prev = (read_json(st("hub.json"), {}).get("prev") or "").split()
            if not prev:
                raise Fail("no previous hub release recorded (rollback --target hub --to REL)", 3)
            rid = prev[0]
        meta = read_json(Path(hr.releases_dir(rid, "release.json")), {})
        return hub_switch(rid, None, str(meta.get("sha") or rid), plan_only, wait, "rollback")


# ---------------------------------------------------------------- land
def worktree_of(repo: Path, branch: str) -> Path | None:
    cur = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            cur = Path(line[9:])
        elif line == f"branch refs/heads/{branch}":
            return cur
    return None


def dirty(wt: Path) -> bool:
    return bool(git(wt, "status", "--porcelain", "--untracked-files=no"))


# ---------------------------------------------------------------- legacy suites known red on main (land fail-fast)
# Main's record (state legacy-main.json): per suite {ok, detail, sha, ts, how} from the last run on main's tree:
# how = "ci" (`assistant ci` on a clean main checkout), "land" (the suites a land's CI ran green; that tree is main
# now) or "main-check" (a land whose CI went red on a suite outside its area re-runs that suite on main).
# Before CI, a land runs the suites it would run that main's record has red, alone; still red = fail at once (exit 6).
# Chosen over skip-with-warning: a skipped suite could hide a new regression and its tree would stamp green. A
# preflight that fails is a subset of CI run earlier (same tree, same suites), so it never refuses a land CI would
# pass; a suite now green (fixed by the branch or on main) goes on to the full CI.
LEGACY_MAIN = "legacy-main.json"
LEGACY_MAIN_CHECK_MAX = 3  # suites a red land re-runs on main (each up to its 240 s timeout)


def _read_json_safe(p: Path) -> dict:
    try:
        d = read_json(p, {})
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def legacy_main() -> dict:
    s = _read_json_safe(st(LEGACY_MAIN)).get("suites")
    return {k: v for k, v in s.items() if isinstance(v, dict)} if isinstance(s, dict) else {}


def legacy_main_record(results: dict, sha: str, how: str) -> None:
    """Per-suite results of a run on main's tree (commit sha) into main's record; other suites keep theirs."""
    if not results:
        return
    suites = legacy_main()
    now = round(clock.now(), 3)
    for s, r in results.items():
        suites[s] = {
            "ok": bool(r.get("ok")),
            "detail": str(r.get("detail") or "")[:300],
            "src": r.get("src"),
            "sha": sha,
            "ts": now,
            "how": how,
        }
    try:
        write_json(st(LEGACY_MAIN), {"suites": suites})
    except OSError as e:  # a hint for later lands, never a reason to fail this run
        say(f"suites: main's record not written ({type(e).__name__})")
    green = [f"{k}:{s}" for s, r in results.items() if r.get("ok") for k in ("suite", "legacy")]
    if green:
        _ci_cleared(green, why=f"green on main ({how})")


def legacy_red() -> dict:
    return {s: r for s, r in legacy_main().items() if not r.get("ok")}


def _legacy_report(tree: Path, head: str, tree_id: str, leg: dict, dirty0: bool, sel: dict | None = None) -> None:
    """ci's per-suite legacy results and its selection (mode, why; legacy_select): to ASSISTANT_CI_LEGACY_OUT (land
    reads it), and into main's record when the run is on a clean checkout of main itself."""
    out = os.environ.get("ASSISTANT_CI_LEGACY_OUT")
    if out:
        try:
            pick = {k: (sel or {}).get(k) for k in ("mode", "why", "of")}
            write_json(Path(out), {"tree": tree_id, "head": head, "suites": leg, "selection": pick})
        except OSError as e:
            say(f"suites: result file not written ({type(e).__name__})")
    if leg and not dirty0:
        r = run(["git", "-C", str(tree), "rev-parse", "--verify", "-q", "refs/heads/main"])
        if r.returncode == 0 and r.stdout.strip() == head:
            legacy_main_record(leg, head, "ci")


def _legacy_run(tree: Path, changed: list[str] | None, suites: set[str]) -> dict:
    """Just these legacy suites on tree, outside a CI run (own run dir, reaped and removed)."""
    res: dict = {}
    tmp = Path(tempfile.mkdtemp(prefix="assistant-legacy."))
    try:
        _ci_legacy(tree, changed, tmp, only=suites, results=res)
    finally:
        try:
            _ci_reap(tmp)
        except Exception:  # noqa: BLE001 (the dir still goes)
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def _since(r: dict) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(float(r.get("ts") or 0)))


def _legacy_preflight(wt: Path, branch: str, base: str, tree: str) -> None:
    """Fail fast: the suites this land's CI would run that main's record has red run first, alone; any still red
    refuses the land now (exit 6) instead of after the full CI."""
    red = legacy_red()
    if not red:
        return
    try:
        man = load_manifest(wt)
    except Fail:
        return  # CI says so
    changed = changed_files(wt, base) or []
    pick: dict = {}
    sel = {e["suite"]: e for e in _legacy_selected(man, changed, wt, base, pick)}
    pre = sorted(set(red) & set(sel))
    if not pre:
        return
    say(f"fail-fast: {', '.join(pre)} red on main; running {'it' if len(pre) == 1 else 'them'} before the full CI ...")
    res = _legacy_run(wt, changed, set(pre))
    still = [s for s in pre if not (res.get(s) or {}).get("ok")]
    if not still:
        say(f"fail-fast: {', '.join(pre)} green here; full CI")
        return
    why = []
    for s in still:
        m, e = red[s], sel[s]
        area = (
            "touches its area"
            if _legacy_touches(e, changed)
            else f"doesn't touch {e['src']} or tests/legacy/{s}/ (it runs: {pick.get('mode')}, {pick.get('why')})"
        )
        why.append(
            f"{s}: red on main since {_since(m)} ({str(m.get('sha') or '?')[:12]}: {m.get('detail') or '?'}), "
            f"still red here ({(res.get(s) or {}).get('detail') or 'not run'}); this branch {area}"
        )
    for s, w in zip(still, why, strict=True):
        _ci_blocked(f"suite:{s}", branch, f"fail-fast: {w}")
    log({"op": "land", "branch": branch, "ok": False, "why": "known red on main", "suites": still, "tree": tree})
    raise Fail(
        "fail-fast, full CI not run: "
        + " · ".join(why)
        + ". Fix the suite on main (or in this branch), then land again; `assistant status` shows main's record",
        6,
    )


def _legacy_after_red(repo: Path, wt: Path, base: str, out: dict, branch: str = "") -> str:
    """A red land CI with legacy suites red outside the branch's area: run those on main (this land holds the lock,
    so main stands still) and record them; the reply says whose failure it is."""
    fails = sorted(s for s, r in (out.get("suites") or {}).items() if isinstance(r, dict) and not r.get("ok"))
    if not fails:
        return ""
    try:
        ent = {e["suite"]: e for e in load_manifest(wt)["file"] if e.get("suite")}
    except Fail:
        return ""
    changed = changed_files(wt, base) or []
    outside = [s for s in fails if s in ent and not _legacy_touches(ent[s], changed)]
    if not outside:
        return ""
    if len(outside) > LEGACY_MAIN_CHECK_MAX:
        return f"; suites {', '.join(outside)} red outside this branch's area (too many to re-check on main)"
    say(f"suites {', '.join(outside)} red outside this branch's area: checking main ...")
    res = _legacy_run(repo, None, set(outside))
    legacy_main_record(res, git(repo, "rev-parse", "main"), "main-check")
    parts = []
    on_main = [s for s in outside if s in res and not res[s].get("ok")]
    for s in on_main:
        _ci_blocked(f"suite:{s}", branch, f"{s} red on main ({(res.get(s) or {}).get('detail') or '?'})")
    if on_main:
        parts.append(
            f"{', '.join(on_main)} red on main too (not this branch's doing; lands that run "
            f"{'it' if len(on_main) == 1 else 'them'} now stop at once until main's is fixed)"
        )
    green = [s for s in outside if (res.get(s) or {}).get("ok")]
    if green:
        parts.append(f"{', '.join(green)} green on main: this branch breaks {'it' if len(green) == 1 else 'them'}")
    return "; " + "; ".join(parts) if parts else ""


# ---------------------------------------------------------------- shared CI blockers (one agenda event each)
# What stops EVERY land (a suite red on main, the hub runner refusing a submit or giving no signed record for a reason
# outside the branch) goes to the main session at once: core.ciblock dedupes per blocker key (~2 h) and logs
# first-seen / cleared (ci-blockers.jsonl); a first sighting, or one 2 h after the last event, raises ONE ci.blocked
# event (delivery now; ~/inbox/ci-blocked-<epoch>.md, acked with `events ack`). Never fails or slows a land.
CI_BLOCKED_PREFIX = "ci-blocked"


def _ci_blocked(key: str, branch: str, text: str) -> None:
    try:
        from core import ciblock

        r = ciblock.seen(key, branch, text)
        if not r["emit"]:
            return
        _ci_blocked_note(key, branch, ciblock.one_line(text), r["first_seen"], r["count"])
    except Exception as e:  # noqa: BLE001 (an alert must never break the land)
        say(f"ci-blocked: alert not raised ({type(e).__name__})")


def _ci_blocked_note(key: str, branch: str, line: str, first: float, count: int) -> None:
    import core.notes as notes
    from core import ciblock

    body = notes.compose(
        f"CI blocked for every land: {line[:100]}",
        [
            f"Blocker: {line}",
            f"Branch that hit it: {branch} · first seen {ciblock.since(first)} · seen {count}x · key `{key}`",
            "Shared: lands that reach this stop until it is fixed (not one branch's own red).",
            "`assistant ci-status` lists the current shared blockers.",
        ],
        "fix the shared blocker (or hand it to one helper), then `events ack` this; while it is still seen it "
        "comes back at most every 2 h",
        source="assistant land",
    )
    payload = {"key": key[:200], "branch": branch[:120], "blocker": line, "first_seen": int(first), "count": count}
    try:
        from inbox import writer

        writer.new_note(CI_BLOCKED_PREFIX, body, source="assistant land", kind="ci.blocked", payload=payload)
    except Exception:  # noqa: BLE001 (no ledger here: the plain note still reaches the beat)
        notes.write_new(paths.inbox(), CI_BLOCKED_PREFIX, body)


def _ci_cleared(keys=None, prefix: str | None = None, why: str = "") -> None:
    try:
        from core import ciblock

        ciblock.clear(keys, prefix, why=why)
    except Exception as e:  # noqa: BLE001
        say(f"ci-blocked: clear not recorded ({type(e).__name__})")


# ciworker.skip_why's reason for a job a newer job of the same branch replaced: not a shared blocker
RUNNER_SUPERSEDED = "skipped: superseded by job"


def _runner_shared(submit_why: str, runner: dict) -> tuple[str, str] | None:
    """The runner's non-answer as a shared blocker (key, text), or None when it is this branch's own red (a finished
    run with failed steps and no runner error) or the job was superseded by a newer land of the same branch."""
    from core import ciblock

    state = str(runner.get("state") or "none")
    why = str(runner.get("why") or "")
    if state == "none":
        w = submit_why or "no job"
        return ciblock.key_for("runner-submit", w), f"hub runner refused the submit: {w}"
    if state == "done" and runner.get("ok") and not runner.get("signed_green"):
        return "runner:unsigned", "hub runner: green but no valid signature on its record"
    if state == "done" and not why:
        return None  # the branch's own failing steps
    if state == "done" and why.startswith(RUNNER_SUPERSEDED):
        return None  # a newer land of the same branch replaced this job: that land goes on, this one ends quietly
    return ciblock.key_for(
        f"runner:{state}", why
    ), f"hub runner gave no signed record ({state}{': ' + why if why else ''})"


def ci_status(as_json: bool, clear: str | None = None) -> int:
    """The shared CI blockers now (not cleared yet), main's legacy red record and the last 24 h's blocked time."""
    from core import ciblock

    if clear:
        gone = ciblock.clear(None if clear == "all" else [clear], why="ci-status --clear")
        say(f"cleared: {', '.join(gone) or 'nothing'}")
        return 0 if gone else 1
    cur = ciblock.current()
    st24 = ciblock.stats(24)
    red = legacy_red()
    if as_json:
        say(json.dumps({"blockers": cur, "legacy_red": red, "stats_24h": st24}, sort_keys=True))
        return 3 if cur else 0
    if not cur:
        say("shared CI blockers: none")
    else:
        say(f"shared CI blockers ({len(cur)}):")
        for e in cur:
            first, last = ciblock.since(e.get("first_seen") or 0), ciblock.since(e.get("last_seen") or 0)
            say(
                f"  {e.get('key')}: since {first}, last {last} ({e.get('branch') or '?'}, {e.get('count')}x)"
                f" — {e.get('text')}"
            )
    say("tool suites red on main: " + (", ".join(f"{s} (since {_since(r)})" for s, r in sorted(red.items())) or "none"))
    say(ciblock.summary_line(st24) + " in 24 h")
    return 3 if cur else 0


def _ci_current() -> list[dict]:
    try:
        from core import ciblock

        return ciblock.current()
    except Exception:  # noqa: BLE001 (status never fails over it)
        return []


LAND_PRE_TRIES = 2  # CI runs outside the lock this many times (main moved meanwhile: once more), then under it


def land_serial() -> bool:
    """The rollback switch to the old flow (CI under the deploy lock): ASSISTANT_LAND_SERIAL=1 or the file
    ~/.config/assistant/land-serial."""
    v = os.environ.get("ASSISTANT_LAND_SERIAL", "").strip().lower()
    if v:
        return v not in ("0", "no", "off", "false")
    return paths.config("land-serial").exists()


def land_local_ci() -> bool:
    """The rollback switch to the full local CI in a land (the runner then counts per the gate mode, as before):
    ASSISTANT_LAND_LOCAL_CI=1 or the file ~/.config/assistant/land-local-ci."""
    v = os.environ.get("ASSISTANT_LAND_LOCAL_CI", "").strip().lower()
    if v:
        return v not in ("0", "no", "off", "false")
    return paths.config("land-local-ci").exists()


def _land_ready(repo: Path, branch: str) -> None:
    if git(repo, "rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise Fail(f"{repo} is not on main", 3)
    if dirty(repo):
        raise Fail(f"{repo} has uncommitted changes", 3)
    git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")


def _rebase_on_main(repo: Path, wt: Path, branch: str) -> tuple[str, str, str]:
    """The branch's worktree rebased onto main's current commit -> (base, head, tree). A conflict aborts the rebase
    and fails with exit 5; uncommitted changes with exit 3. Shared by a land's verify pass and the queue's front."""
    if dirty(wt):
        raise Fail(f"{wt} has uncommitted changes: commit them first", 3)
    base = git(repo, "rev-parse", "main")  # read once: main may move while this runs
    _land_phase("rebase")
    r = run(["git", "-C", str(wt), "rebase", base])
    if r.returncode != 0:
        run(["git", "-C", str(wt), "rebase", "--abort"])
        log({"op": "land", "branch": branch, "ok": False, "why": "rebase conflict"})
        raise Fail(f"rebase onto main conflicts: in {wt} run `git rebase main`, resolve, commit, land again", 5)
    return base, git(wt, "rev-parse", "HEAD"), git(wt, "rev-parse", "HEAD^{tree}")


def _land_verify(repo: Path, wt: Path, branch: str, mode: str, override: str | None, prev: dict | None) -> dict:
    """Rebase the branch onto main's current commit and check that tree: a green stamp, else the fail-fast
    preflight and the CI; the runner job and its answer; the eval subset. Fail when anything is red. Runs outside
    the deploy lock (land re-checks under it that main and the branch are still what this saw), or under it in the
    serial flow. prev: an earlier pass of this land (its green eval subset is reused for the same changed files)."""
    base, head, tree = _rebase_on_main(repo, wt, branch)
    stamp = read_json(st("ci", tree + ".json"), {})
    if not stamp.get("ok"):
        _land_phase("fail-fast")
        _legacy_preflight(wt, branch, base, tree)  # a suite red on main and still red here: exit 6 now
    # the runner is the test run unless the switch, gate mode off or an override says the full local run
    full_local = land_local_ci() or mode == "off" or override is not None
    gate: dict = {"mode": mode, "tests": "local" if full_local else "runner"}
    if override is not None:
        gate.update(override=override.strip()[:200], by=os.environ.get("AI_AGENT", "")[:40])
    # main moved during the last pass: that pass's tree (old main + this branch) is green, so the test selection is
    # against it: the runner sees only what main's new commits changed (impacted; full only if those touch a shared
    # or protected path), the local part the main delta + the branch's. The rebase, fail-fast, gates, evals and the
    # fast-forward check keep main as the base.
    sel_base = base
    green = full_local or bool((prev or {}).get("runner", {}).get("signed_green"))  # that pass's test run was green
    if prev and prev.get("head") and prev.get("tree") != tree and green:
        sel_base = prev["head"]
        gate["rerun_base"] = prev["head"][:12]
    job = None
    if mode != "off":
        job, why = _runner_submit(wt, head, tree, sel_base, branch)
        gate["submit"] = "ok" if job else why
    t_ci, w_ci = time.monotonic(), clock.now()
    legacy_out: dict = {}
    if not stamp.get("ok"):
        seam = test_seam("ASSISTANT_LAND_CI")
        cmd = (
            shlex.split(seam)
            if seam
            else [sys.executable, "-I", os.path.realpath(__file__), "ci", "--path", str(wt), "--base", sel_base]
            + ([] if full_local else ["--host"])
        )
        say(f"CI for {branch} ({tree[:12]}){'' if full_local else ' here: host-bound steps; tests on the runner'} ...")
        _land_phase("CI local" if full_local else "CI host + runner")
        ofd, opath = tempfile.mkstemp(prefix=".legacy-out.", suffix=".json", dir=st())
        os.close(ofd)
        try:
            rc = run_child(cmd, cwd=wt, timeout=LAND_CI_TIMEOUT, env=dict(os.environ, ASSISTANT_CI_LEGACY_OUT=opath))
            legacy_out = _read_json_safe(Path(opath))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(opath)
        if legacy_out.get("tree") not in (None, tree):
            legacy_out = {}
        if rc != 0:
            log({"op": "land", "branch": branch, "ok": False, "why": "ci", "tree": tree})
            with Lock(f"land {branch} (main check)"):  # main stands still while its suites re-run (re-entrant)
                more = _legacy_after_red(repo, wt, base, legacy_out, branch)
            raise Fail(f"CI failed for {branch}: fix in {wt}, commit, land again{more}", 6)
    gate["ci_local_secs"] = round(time.monotonic() - t_ci, 1)
    if not stamp.get("ok"):  # the CI ran here (a green stamp: none): when it started, each step's seconds
        gate["ci_local_at"] = round(w_ci, 3)
        steps = legacy_out.get("steps")
        if isinstance(steps, dict) and steps:
            gate["ci_local_steps"] = {str(k)[:40]: v for k, v in steps.items() if isinstance(v, (int, float))}
    changed = [x for x in git(wt, "diff", "--name-only", f"{base}...HEAD").splitlines() if x]
    root = _gate_root(wt, base)  # gated-paths.json as of base (main's checkout may be mid-merge outside the lock)
    try:
        gated = gated_categories(changed, root)
    finally:
        if root != wt:
            shutil.rmtree(root, ignore_errors=True)
    enforce = override is None and (mode == "enforce" or (mode == "enforce-gated" and bool(gated)))
    gate.update(
        gated=gated,
        changed_n=len(changed),
        effective="off" if mode == "off" else "enforce" if enforce else "shadow",
    )
    need = enforce or not full_local  # the runner's record is the test run
    _land_phase("runner")
    runner = _runner_result(job, tree, RUNNER_WAIT_ENFORCE if need else RUNNER_WAIT_SHADOW)
    blobs = git(wt, "ls-tree", "HEAD", "--", *changed) if changed else ""
    if prev and prev.get("evals_key") == (blobs, enforce) and (prev.get("gate") or {}).get("evals", {}).get("ok"):
        evals = {**prev["gate"]["evals"], "reused": True}  # same changed files, same content: no second spend
    else:
        if mode != "off" and override is None:
            _land_phase("evals")
        evals = _eval_subset(changed, enforce) if mode != "off" and override is None else {}
    say(_evals_line(evals))
    gate.update(runner=runner, evals=evals)
    if runner.get("signed_green"):
        _ci_cleared(prefix="runner", why=f"signed green record ({branch})")
    if need and not runner.get("signed_green"):
        shared = _runner_shared(str(gate.get("submit") or ""), runner)
        if shared:
            _ci_blocked(shared[0], branch, shared[1])
        log({"op": "land", "branch": branch, "ok": False, "why": "no signed record", "tree": tree, "gate": gate})
        red = f"; runner RED (job {runner.get('job')}): {runner['red']}" if runner.get("red") else ""
        err = Fail(
            f"no signed green CI record from the hub runner for {tree[:12]} ({runner.get('state') or 'none'}"
            f"{': ' + str(runner.get('why')) if runner.get('why') else ''}){red}: fix and land again; the main "
            "session may `land --override 'REASON'`; runner down: `touch ~/.config/assistant/land-local-ci` runs "
            "the tests here",
            6,
        )
        err.shared = bool(shared)  # the runner down or refusing, not this tree's red: the queue retries later
        raise err
    if enforce and not evals.get("ok"):
        log({"op": "land", "branch": branch, "ok": False, "why": "eval subset", "tree": tree, "gate": gate})
        raise Fail(f"eval subset failed for {branch} ({evals.get('detail') or evals.get('rc')}): fix, land again", 6)
    return {
        "base": base,
        "head": head,
        "tree": tree,
        "gate": gate,
        "runner": runner,
        "legacy_out": legacy_out,
        "evals_key": (blobs, enforce),
    }


def land(branch: str, adopt=(), override: str | None = None, batch: str | None = None) -> dict:
    """Rebase + CI outside the deploy lock (two lands' CIs overlap instead of queueing), then under the lock: main
    and the branch still what the CI saw -> fast-forward + deploy; main moved -> once more outside the lock, after
    that under it (the old serial flow; land_serial() forces it). batch: the land queue's worker lands a batch this
    way (branch landq/b-N): the deploy row names its first member (that member's own land_cmd_start, a landq block),
    one more land row and one board poke per other member, each with its own last commit on main.
    -> {"sha": main, "shas": {entry id: its last commit on main}}"""
    repo = paths.repo()
    if branch in ("main", "HEAD"):
        raise Fail("land a feature branch, not main", 2)
    bq = _landq_read("batches", batch) if batch else None
    if batch and not (bq and bq.get("members")):
        raise Fail(f"land queue: batch {batch} unknown", 2)
    first = bq["members"][0] if bq else None
    if override is not None:
        if _subagent():
            raise Fail("--override is the main session's: a subagent can't land past the CI gate", 2)
        if len(override.strip()) < 8:
            raise Fail("--override needs a reason (8+ characters)", 2)
    mode = gate_mode()
    serial = land_serial()
    t_cmd, w_cmd = time.monotonic(), clock.now()
    _land_ready(repo, branch)
    wt = worktree_of(repo, branch)
    temp = None
    shas: dict = {}
    partial = ""  # the land's suites were a selection: every suite on main afterwards (legacy_main_bg)
    if wt is None:
        temp = Path(tempfile.mkdtemp(prefix="land-", dir=st()))
        git(repo, "worktree", "add", "--quiet", str(temp / "wt"), branch)
        wt = temp / "wt"
    try:
        # the branch's commits before the rebase: a board line may still name one of them as READY
        pre = git(repo, "rev-list", "--max-count=30", f"main..{branch}", check=False).split()
        v: dict | None = None
        tries = 0
        _PHASE.update(branch=branch, rerun=0, start=w_cmd)
        while True:
            if not serial and tries < LAND_PRE_TRIES:
                tries += 1
                _PHASE["rerun"] = tries - 1
                v = _land_verify(repo, wt, branch, mode, override, v)
            _land_phase("waiting for lock")
            t_lock = time.monotonic()
            with Lock(f"land {branch}"):
                t0 = clock.now()
                lock_wait = round(time.monotonic() - t_lock, 1)
                _land_ready(repo, branch)
                main_now = git(repo, "rev-parse", "main")
                fresh = (
                    v is not None
                    and main_now == v["base"]
                    and git(repo, "rev-parse", f"refs/heads/{branch}") == v["head"]
                )
                locked = False
                if not fresh:
                    if v is not None and not serial and tries < LAND_PRE_TRIES:
                        say(f"main moved to {main_now[:12]} during the CI: once more on it, outside the lock")
                        continue  # leaves the lock
                    v = _land_verify(repo, wt, branch, mode, override, v)
                    locked = True
                old = git(repo, "rev-parse", "main")
                git(repo, "merge", "--ff-only", "--quiet", v["head"])
                if bq and first:
                    shas = _landq_shas(repo, old, bq)
                try:
                    deploy(
                        op="land",
                        adopt=adopt,
                        extra={
                            "branch": first["branch"] if first else branch,
                            "from": old[:12],
                            "land_start": t0,
                            "land_cmd_start": first["cmd_start"] if first else w_cmd,
                            **({"landq": _landq_rowinfo(bq, first, shas[first["id"]], old)} if bq and first else {}),
                            "lock_wait_secs": lock_wait,
                            "verify": {"tries": tries + locked, "locked": locked, "serial": serial},
                            "tree": v["tree"],
                            "shell_suites": len(shell_suites(wt)),
                            "gate": v["gate"],
                            "runner": v["runner"],
                            **_land_tests(v["legacy_out"].get("pytest"), v["runner"]),
                            **({"adopt": list(adopt)} if adopt else {}),
                        },
                    )
                except Fail:
                    git(repo, "reset", "--quiet", "--keep", old)
                    say(f"main reset to {old[:12]} (land of {branch} undone)")
                    raise
                if v["legacy_out"].get("suites"):  # the suites this CI ran are main's now
                    legacy_main_record(v["legacy_out"]["suites"], git(repo, "rev-parse", "main"), "land")
                if (v["legacy_out"].get("selection") or {}).get("mode") == "impacted":
                    partial = branch
                if bq:
                    _landq_member_rows(bq, shas)
                break
    finally:
        _land_phase_drop()
        if temp is not None:
            run(["git", "-C", str(repo), "worktree", "remove", "--force", str(temp / "wt")])
            shutil.rmtree(temp, ignore_errors=True)
    main_sha = git(repo, "rev-parse", "main")
    say(f"landed {branch} on main ({main_sha[:12]}) in {time.monotonic() - t_cmd:.0f} s")
    if bq:
        for m in bq["members"]:
            sha = shas.get(m["id"]) or main_sha
            board_mark(m["branch"], sha, [h for h in [m.get("head", ""), *m.get("pre", [])] if h])
    else:
        board_mark(branch, main_sha, pre)
    publish_bg()
    if partial:
        legacy_main_bg(partial)
    return {"sha": main_sha, "shas": shas if bq else {}}


def board_mark(branch: str, sha: str, pre: list[str]) -> None:
    """After a land: `board poke landed BRANCH --hash SHA --also PRE..` (Board v2): the board items whose done-when
    probes name the branch, the commit or a pre-rebase commit (kept as a land alias) are checked at once and close when
    all their probes pass. Never fails or slows a land (15 s cap)."""
    exe = test_seam("ASSISTANT_BOARD_MARK_BIN")
    if exe is None:
        if paths.test_mode():
            return
        exe = str(paths.real_home() / "bin" / "board")
    if not os.access(exe, os.X_OK):
        return
    also = [a for h in pre[:30] if h != sha for a in ("--also", h)]
    try:
        r = run([exe, "poke", "landed", branch, "--hash", sha, *also], timeout=15)
        if (r.stdout or "").strip():
            say(r.stdout.strip()[:300])
    except Exception as e:  # noqa: BLE001  (the land is done: report, never raise)
        say(f"board poke skipped: {type(e).__name__}")


def publish_bg() -> None:
    """After a land: `assistant-publish push --cron --bg` (a snapshot of main -> its GitHub repo, after the
    privacy scan; a block or failure is its own ~/inbox note). Detached; never fails or slows a land."""
    exe = test_seam("ASSISTANT_PUBLISH_BIN")
    if exe is None:
        if paths.test_mode():
            return
        exe = str(paths.real_home() / "bin" / "assistant-publish")
    if not os.access(exe, os.X_OK):
        return
    try:
        run([exe, "push", "--cron", "--bg"], timeout=30)
    except Exception as e:  # noqa: BLE001  (the land is done: report, never raise)
        say(f"assistant-publish not started: {type(e).__name__}")


# ---------------------------------------------------------------- land queue (verify, then land)
# Switch: ~/.config/assistant/land-queue (content "on"; ASSISTANT_LAND_QUEUE=0|1 wins); absent = the direct land above.
# On: `land BRANCH` runs the quick checks here (rebase onto main, the fail-fast preflight, ruff + format, the AST ban
# and secretscan: seconds), queues the branch (an entry in landq/entries/) and then waits for its end (--wait, the
# default: exit codes as a direct land) or returns at once (--no-wait: "queued qN"). ONE worker (flock on
# landq/worker.lock; started by land, `land status` and --wait, and the cron line; it exits when idle) tests what
# waits: the batch lane takes up to LANDQ_BATCH_MAX queued entries in order, rebased one onto the other on main into
# branch landq/b-N (one that conflicts with an earlier one waits for the next batch, built on main after this one),
# and runs today's land() on it as a child (`land landq/b-N --batch bN`): the host CI beside ONE runner job, the eval
# subset, then under the deploy lock the fast-forward and ONE deploy; a land row and a board poke per member. Main and
# ~/bin only ever get a tree with a signed green record (the gate as before), so nothing is ever reverted. The gated
# lane (gated paths, --adopt) runs one entry at a time beside it: a long eval subset never holds the batch lane.
# A red batch splits in halves at the front of the queue (never mixed with new entries): the first half alone on
# main, then the second on top; a single member left is red (exit 6 to its waiter, else one ~/inbox/land-red-* note
# naming its batch-mates); a second half of one branch whose tree is the red batch's tree is red without a run.
# Runner down or refusing (a shared blocker): back to queued, retried after LANDQ_RETRY. Smoke failure after the
# deploy (rolled back as before): each member once alone. Crash: entries and batch files are written atomically; a
# new worker adopts a live child, else main at the batch tip = landed (missing rows written, an undeployed tip
# deployed once), main unchanged = the members go back to the front of the queue.
LANDQ_BATCH_MAX = 4
LANDQ_WAIT = 3600.0  # --wait's default budget (s): queue + one batch with its eval subset
LANDQ_RETRY = 300.0  # runner down or refusing: entries retry after this
LANDQ_KEEP = 2 * 86400.0  # finished entries and batches are pruned after this
LANDQ_TICK = 1.0
LANDQ_STALL = 60.0  # --wait: no worker for this long although it was started = exit 7 (queue stalled)
LANDQ_DONE = ("landed", "red", "conflict", "superseded", "error")
LANDQ_QUICK_SKIP = "suites,pytest,pyright,schemas,ratchet,inbox-lint,precheck,bun"  # leaves sync ruff ast secretscan
LANDQ_RED_PREFIX = "land-red"


def land_queue() -> bool:
    """The land queue's switch: ASSISTANT_LAND_QUEUE=0|1, else the file ~/.config/assistant/land-queue holding
    "on" (or nothing). Off: `land BRANCH` lands directly; the worker still finishes what is queued."""
    v = os.environ.get("ASSISTANT_LAND_QUEUE", "").strip().lower()
    if v:
        return v not in ("0", "no", "off", "false")
    try:
        return paths.config("land-queue").read_text(encoding="utf-8").strip().lower() in ("", "on", "1", "yes", "true")
    except OSError:
        return False


def _lq(*parts: str) -> Path:
    return st("landq", *parts)


def _landq_tick() -> float:
    try:
        return float(test_seam("ASSISTANT_LANDQ_TICK") or LANDQ_TICK)
    except ValueError:
        return LANDQ_TICK


@contextlib.contextmanager
def _lq_lock():
    """The queue's short state lock: every read-modify-write of entries, front groups and ids."""
    p = _lq("state.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _landq_read(kind: str, rid: str | None) -> dict | None:
    if not rid or not re.fullmatch(r"[qb]\d{1,9}", rid):
        return None
    d = _read_json_safe(_lq(kind, rid + ".json"))
    return d if d.get("id") == rid else None


def _landq_write(kind: str, rec: dict) -> None:
    rec["updated"] = round(clock.now(), 3)
    write_json(_lq(kind, rec["id"] + ".json"), rec)


def _landq_all(kind: str) -> list[dict]:
    out = []
    try:
        files = list(_lq(kind).glob("*.json"))
    except OSError:
        return out
    for f in files:
        d = _read_json_safe(f)
        if d.get("id") == f.stem:
            out.append(d)
    return sorted(out, key=lambda d: int(d.get("n") or 0))


def _landq_next_id(kind: str) -> tuple[str, int]:
    """The next entry (q) or batch (b) id; under _lq_lock. Never below an existing file's number."""
    p = _lq("seq.json")
    d = _read_json_safe(p)
    have = [int(x.get("n") or 0) for x in _landq_all("entries" if kind == "q" else "batches")]
    n = max([int(d.get(kind) or 0), *have]) + 1
    d[kind] = n
    write_json(p, d)
    return f"{kind}{n}", n


def _landq_front() -> list[dict]:
    g = _read_json_safe(_lq("front.json")).get("groups")
    return [x for x in g if isinstance(x, dict) and x.get("members")] if isinstance(g, list) else []


def _landq_front_set(groups: list[dict]) -> None:
    write_json(_lq("front.json"), {"groups": groups})


def _landq_quick(wt: Path, branch: str, base: str, tree: str) -> None:
    """The queue's quick checks on the rebased branch: `ci` with only sync, ruff + format, the AST ban and
    secretscan (seconds); red = exit 6 now, before the branch is queued."""
    seam = test_seam("ASSISTANT_LANDQ_CHECK")
    if seam is None and paths.test_mode():
        say("quick checks: skipped (test mode without ASSISTANT_LANDQ_CHECK)")
        return
    cmd = (
        shlex.split(seam)
        if seam
        else [sys.executable, "-I", os.path.realpath(__file__), "ci", "--path", str(wt), "--base", base]
        + ["--skip", LANDQ_QUICK_SKIP, "--no-stamp"]
    )
    _land_phase("quick checks")
    say(f"quick checks for {branch} ({tree[:12]}): ruff + format, ast, secretscan ...")
    rc = run_child(cmd, cwd=wt, timeout=900)
    if rc != 0:
        log({"op": "land", "branch": branch, "ok": False, "why": "quick checks", "tree": tree})
        raise Fail(f"quick checks failed for {branch}: fix in {wt}, commit, land again", 6)


def _land_front(branch: str, adopt=(), wait: bool = True, timeout: float = LANDQ_WAIT) -> int:
    """`land BRANCH` with the queue on: the quick checks, then an entry in the queue; --wait follows it to its end."""
    repo = paths.repo()
    if branch in ("main", "HEAD") or branch.startswith("landq/"):
        raise Fail("land a feature branch, not main (or a queue batch)", 2)
    w_cmd = clock.now()
    _land_ready(repo, branch)
    wt = worktree_of(repo, branch)
    temp = None
    if wt is None:
        temp = Path(tempfile.mkdtemp(prefix="land-", dir=st()))
        git(repo, "worktree", "add", "--quiet", str(temp / "wt"), branch)
        wt = temp / "wt"
    _PHASE.update(branch=branch, rerun=0, start=w_cmd)
    try:
        pre = git(repo, "rev-list", "--max-count=30", f"main..{branch}", check=False).split()
        base, head, tree = _rebase_on_main(repo, wt, branch)
        if head == base:
            say(f"nothing to land: {branch} has no commits beyond main ({base[:12]})")
            return 0
        if not read_json(st("ci", tree + ".json"), {}).get("ok"):
            _land_phase("fail-fast")
            _legacy_preflight(wt, branch, base, tree)  # a suite red on main and still red here: exit 6 now
        _landq_quick(wt, branch, base, tree)
        changed = [x for x in git(wt, "diff", "--name-only", f"{base}...HEAD").splitlines() if x]
        root = _gate_root(wt, base)
        try:
            gated = gated_categories(changed, root)
        finally:
            if root != wt:
                shutil.rmtree(root, ignore_errors=True)
    finally:
        _land_phase_drop()
        if temp is not None:
            run(["git", "-C", str(repo), "worktree", "remove", "--force", str(temp / "wt")])
            shutil.rmtree(temp, ignore_errors=True)
    e = landq_add(
        branch, base=base, head=head, tree=tree, pre=pre, adopt=list(adopt), gated=gated, cmd_start=round(w_cmd, 3)
    )
    lane = ", gated lane" if e["lane"] == "gated" else ""
    say(f"queued {e['id']} (position {landq_position(e)}{lane}) after {clock.now() - w_cmd:.0f} s of quick checks")
    if not landq_kick():
        say("land queue: worker not started (`assistant land status` starts it)")
    if not wait:
        say(
            f"`assistant land status {e['id']}` shows it; a red or conflicting end with nobody waiting becomes one "
            "~/inbox/land-red-* note"
        )
        return 0
    return landq_wait(e["id"], timeout)


def landq_add(branch: str, **kw) -> dict:
    """A new queue entry for branch; a queued (not yet testing) entry of the same branch is superseded by it."""
    now = round(clock.now(), 3)
    with _lq_lock():
        eid, n = _landq_next_id("q")
        lane = "gated" if kw.get("gated") or kw.get("adopt") else "batch"
        e = {"id": eid, "n": n, "branch": branch, "state": "queued", "lane": lane, "since": now, "tries": 0, **kw}
        e["by"] = os.environ.get("AI_AGENT", "")[:40]
        old = [o for o in _landq_all("entries") if o.get("branch") == branch and o.get("state") == "queued"]
        for o in old:
            o.update(state="superseded", superseded_by=eid, ended=now)
            _landq_write("entries", o)
        if old:
            gone = {o["id"] for o in old}
            groups = [{**g, "members": [m for m in g["members"] if m not in gone]} for g in _landq_front()]
            _landq_front_set([g for g in groups if g["members"]])
        _landq_write("entries", e)
    for o in old:
        say(f"{o['id']} (the earlier land of {branch}, still queued) is superseded by {eid}")
    return e


def landq_position(e: dict) -> int:
    ahead = [o for o in _landq_all("entries") if o.get("state") == "queued" and o.get("lane") == e.get("lane")]
    return 1 + sum(1 for o in ahead if int(o.get("n") or 0) < int(e.get("n") or 0))


def _landq_worker_alive() -> bool:
    """A worker holds landq/worker.lock (probed, never waited for)."""
    p = _lq("worker.lock")
    try:
        fd = os.open(p, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _landq_pending() -> bool:
    return any(e.get("state") == "queued" for e in _landq_all("entries")) or any(
        b.get("state") in ("building", "testing") for b in _landq_all("batches")
    )


def landq_kick() -> bool:
    """Start the worker, detached (its own session, reparented to init), unless one runs. -> a worker runs."""
    if _landq_worker_alive():
        return True
    if not _landq_pending():
        return False
    _lq().mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-I", os.path.realpath(__file__), "land", "--worker"]
    pid = os.fork()
    if pid == 0:  # double fork: the worker is never this process's child (no zombie while a waiter lives on)
        code = 0
        try:
            with open(_lq("worker.log"), "a", encoding="utf-8") as f:
                subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, start_new_session=True
                )
        except BaseException:  # noqa: BLE001 (the fork's child must never return into the caller's code)
            code = 1
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


def landq_worker(cron: bool = False) -> int:
    """The one worker: runs both lanes until nothing is queued or testing, then exits. A second one exits at once."""
    p = _lq("worker.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            try:
                me = _proc_stat(os.getpid())
                write_json(_lq("worker.json"), {"pid": os.getpid(), "start": me[2] if me else "", "since": clock.now()})
                if not cron or _landq_pending():
                    _landq_run()
                _landq_prune()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
            if not _landq_pending():  # an entry queued between the last look and the unlock: go on
                return 0
    finally:
        os.close(fd)


def _landq_alive(b: dict) -> bool:
    try:
        return bool(b.get("pid")) and _alive({"pid": int(b["pid"]), "start": str(b.get("start") or "")})
    except (TypeError, ValueError):
        return False


def _landq_run() -> None:
    running: dict[str, tuple[dict, subprocess.Popen | None]] = {}
    for b in _landq_all("batches"):  # a worker before this one stopped: adopt or settle what it left
        if b.get("state") == "building":
            _landq_settle(b, None, {"requeue": True, "why": "the worker stopped while building it"})
        elif b.get("state") == "testing":
            if _landq_alive(b):
                running[str(b.get("lane") or "batch")] = (b, None)
                say(f"adopted {b['id']} (its land still runs, pid {b.get('pid')})")
            else:
                _landq_finish(b, None)
    while True:
        for lane, (b, proc) in list(running.items()):
            if (proc.poll() is not None) if proc else not _landq_alive(b):
                _landq_finish(_landq_read("batches", b["id"]) or b, proc.returncode if proc else None)
                del running[lane]
        for lane in ("batch", "gated"):
            if lane not in running:
                started = _landq_start(lane)
                if started:
                    running[lane] = started
        if not running and not any(e.get("state") == "queued" for e in _landq_all("entries")):
            return
        time.sleep(_landq_tick())


def _landq_pick(lane: str, now: float) -> tuple[list[dict], dict] | None:
    """The next group for a lane (under _lq_lock): the batch lane takes the front groups first (bisect halves,
    members back from a stopped land; never mixed with new entries), else up to LANDQ_BATCH_MAX queued entries in
    order; the gated lane one entry."""
    queued = {e["id"]: e for e in _landq_all("entries") if e.get("state") == "queued"}
    groups = _landq_front()
    if lane == "batch":
        while groups:
            g = groups.pop(0)
            ms = [queued[i] for i in g["members"] if i in queued]
            if ms:
                _landq_front_set(groups)
                return ms, g
        _landq_front_set(groups)
    infront = {i for g in groups for i in g["members"]}
    ready = [
        e
        for e in queued.values()
        if e.get("lane", "batch") == lane and e["id"] not in infront and float(e.get("not_before") or 0) <= now
    ]
    ready.sort(key=lambda e: int(e.get("n") or 0))
    take = ready[: LANDQ_BATCH_MAX if lane == "batch" else 1]
    return (take, {}) if take else None


def _landq_build(entries: list[dict]) -> dict:
    """Each entry's queued commits rebased onto main and then onto the one before it, in a temp worktree: -> members
    (each with its commit count and tip), deferred (conflicts with an entry before it: next batch), conflict (with
    main), empty (nothing beyond main), tip, tree and base (main)."""
    repo = paths.repo()
    base = git(repo, "rev-parse", "main")
    out: dict = {"members": [], "deferred": [], "conflict": [], "empty": [], "base": base}
    tmp = Path(tempfile.mkdtemp(prefix="landq-build-", dir=st()))
    wt = tmp / "wt"
    tip = base
    git(repo, "worktree", "add", "--quiet", "--detach", str(wt), base)
    try:
        for e in entries:
            head = str(e.get("head") or "")
            if not head or run(["git", "-C", str(wt), "cat-file", "-e", head + "^{commit}"]).returncode != 0:
                out["conflict"].append((e, "its queued commit is gone"))
                continue
            git(wt, "checkout", "--quiet", "--detach", head)
            r = run(["git", "-C", str(wt), "rebase", "--quiet", tip])
            if r.returncode != 0:
                run(["git", "-C", str(wt), "rebase", "--abort"])
                if out["members"]:
                    out["deferred"].append(e)
                else:
                    out["conflict"].append((e, f"conflicts with main ({tip[:12]})"))
                continue
            new = git(wt, "rev-parse", "HEAD")
            count = int(git(wt, "rev-list", "--count", f"{tip}..{new}") or 0)
            if count == 0:
                out["empty"].append(e)
                continue
            keep = ("id", "branch", "head", "pre", "cmd_start", "since", "adopt")
            out["members"].append({**{k: e.get(k) for k in keep}, "count": count, "tip": new})
            tip = new
        out.update(tip=tip, tree=git(repo, "rev-parse", f"{tip}^{{tree}}"))
    finally:
        run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)])
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def _landq_start(lane: str) -> tuple[dict, subprocess.Popen] | None:
    """Pick, build and start the next batch of a lane: -> (batch, its land process), or None (nothing to run)."""
    now = round(clock.now(), 3)
    with _lq_lock():
        picked = _landq_pick(lane, now)
        if not picked:
            return None
        entries, g = picked
        bid, n = _landq_next_id("b")
        b = {
            "id": bid,
            "n": n,
            "lane": lane,
            "state": "building",
            "since": now,
            "members": [{"id": e["id"], "branch": e["branch"]} for e in entries],
            "mates": list(g.get("mates") or []),
            "alone": bool(g.get("alone")),
            **({"red_tree": g["red_tree"], "red_why": g.get("red_why")} if g.get("red_tree") else {}),
        }
        for e in entries:
            e.update(state="testing", batch=bid)
            _landq_write("entries", e)
        _landq_write("batches", b)
    try:
        bu = _landq_build(entries)
    except (Fail, OSError, ValueError, subprocess.SubprocessError) as ex:
        _landq_settle(b, None, {"requeue": True, "retry": True, "why": f"build: {ex}"[:300]})
        return None
    told = []
    with _lq_lock():
        for e, why in bu["conflict"]:
            e.update(state="conflict", rc=5, ended=now, red=f"{why}: in its worktree run `git rebase main`, resolve, "
                     "commit, land again")  # fmt: skip
            told.append(e)
        for e in bu["deferred"]:
            e["defer"] = int(e.get("defer") or 0) + 1
            if e["defer"] > 2:
                e.update(state="conflict", rc=5, ended=now, red="conflicts with the branches queued before it, "
                         "batch after batch: rebase it on main once they landed, land again")  # fmt: skip
                told.append(e)
            else:
                e.update(state="queued", batch=None)
        for e in bu["empty"]:
            e.update(state="landed", sha=bu["base"], ended=now, note="nothing beyond main")
        for e in [x for x, _w in bu["conflict"]] + bu["deferred"] + bu["empty"]:
            _landq_write("entries", e)
        b.update(members=bu["members"], base=bu["base"], tip=bu.get("tip"), tree=bu.get("tree"))
        if not bu["members"]:
            b.update(state="done", outcome="empty", ended=now)
            _landq_write("batches", b)
    for e in told:
        _landq_tell(e)
    if not bu["members"]:
        return None
    if b.get("red_tree") and len(bu["members"]) == 1 and bu["tree"] == b["red_tree"]:
        _landq_settle(b, None, {"ok": False, "rc": 6, "why": f"{b.get('red_why') or 'red'} (its tree is the red "
                                "batch's tree: red without another run)"})  # fmt: skip
        return None
    repo = paths.repo()
    b["branch"] = f"landq/b-{n}"
    git(repo, "branch", "-f", b["branch"], b["tip"])
    adopt = [d for m in bu["members"] for d in (m.get("adopt") or [])] if lane == "gated" else []
    cmd = [sys.executable, "-I", os.path.realpath(__file__), "land", b["branch"], "--batch", bid]
    cmd += [x for d in adopt for x in ("--adopt", d)]
    _lq("logs").mkdir(parents=True, exist_ok=True)
    with open(_lq("logs", bid + ".log"), "a", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd, cwd=str(repo), stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, start_new_session=True
        )
    ps = _proc_stat(proc.pid)
    b.update(state="testing", pid=proc.pid, start=ps[2] if ps else "", started=round(clock.now(), 3))
    _landq_write("batches", b)
    names = ", ".join(f"{m['id']} {m['branch']}" for m in bu["members"])
    say(f"{bid}: testing {names} ({lane} lane) on main {bu['base'][:12]}")
    return b, proc


def _landq_finish(b: dict, rc: int | None) -> None:
    res = _read_json_safe(_lq("results", b["id"] + ".json"))
    _landq_settle(b, rc, res or _landq_recover(b, rc))


def _landq_settle(b: dict, rc: int | None, res: dict) -> None:
    """A batch's end into its entries: landed; back to the queue (runner down: after LANDQ_RETRY; a stopped land: at
    the front); red with more than one member: halves at the front; smoke failure with more than one: each alone;
    else red / conflict / error (the waiter's exit code, or one note)."""
    now = round(clock.now(), 3)
    ids = [m["id"] for m in b.get("members") or []]
    branches = [m["branch"] for m in b.get("members") or []]
    code = res.get("rc")
    why = str(res.get("why") or (f"its land exited {rc}" if rc is not None else "its land stopped"))[:1000]
    told = []
    with _lq_lock():
        es = [e for e in (_landq_read("entries", i) for i in ids) if e]
        groups = _landq_front()
        mates = sorted(set(b.get("mates") or []) | set(branches))
        multi = len(es) > 1
        if res.get("ok"):
            for e in es:
                e.update(state="landed", sha=(res.get("shas") or {}).get(e["id"]) or res.get("sha"), ended=now)
            outcome = "landed"
        elif res.get("requeue") or res.get("shared") or code == 7:
            later = res.get("shared") or res.get("retry")
            for e in es:
                e.update(state="queued", batch=None, tries=int(e.get("tries") or 0) + 1)
                e["not_before"] = now + LANDQ_RETRY if later else 0
            if any(int(e.get("tries") or 0) > 3 for e in es):
                for e in es:
                    e.update(state="error", rc=code or 1, ended=now, red=f"back in the queue 4 times: {why}")
                    told.append(e)
                outcome = "error"
            else:
                if res.get("requeue") and multi and b.get("lane") == "batch":
                    groups.insert(0, {"members": [e["id"] for e in es], "mates": list(b.get("mates") or [])})
                outcome = "requeued"
        elif multi and code == 6:
            k = (len(es) + 1) // 2
            first, second = [e["id"] for e in es[:k]], [e["id"] for e in es[k:]]
            groups[:0] = [
                {"members": first, "mates": mates},
                {"members": second, "mates": mates, "red_tree": b.get("tree"), "red_why": why},
            ]
            for e in es:
                e.update(state="queued", batch=None)
            outcome = "split"
        elif multi and code == 4:
            groups[:0] = [{"members": [e["id"]], "alone": True} for e in es]
            for e in es:
                e.update(state="queued", batch=None)
            outcome = "each alone"
        elif multi and code == 5 and max(int(e.get("tries") or 0) for e in es) < 3:
            groups.insert(0, {"members": [e["id"] for e in es], "mates": list(b.get("mates") or [])})
            for e in es:  # main moved (the gated lane) and the batch conflicts with it: rebuilt on the new main
                e.update(state="queued", batch=None, tries=int(e.get("tries") or 0) + 1)
            outcome = "rebuild"
        else:
            state = {5: "conflict", 6: "red", 4: "red"}.get(code or 0, "error")
            for e in es:
                e.update(
                    state=state,
                    rc=code if isinstance(code, int) and code else (rc or 1),
                    red=why,
                    mates=[x for x in mates if x != e["branch"]],
                    ended=now,
                )
                told.append(e)
            outcome = state
        for e in es:
            _landq_write("entries", e)
        _landq_front_set(groups)
        b.update(state="done", outcome=outcome, ended=now, rc=code if code is not None else rc, why=why[:600])
        _landq_write("batches", b)
    if outcome == "landed":
        _landq_reset_branches(b, res)
    _landq_drop_branch(b)
    for e in told:
        _landq_tell(e)
    say(f"{b['id']}: {outcome} ({', '.join(branches)})")


def _landq_recover(b: dict, rc: int | None) -> dict:
    """A batch whose land ended without a result (killed, crashed): main at its tip = landed (rows written when
    missing, an undeployed tip deployed once); main unchanged = back to the queue."""
    if b.get("state") != "testing" or not b.get("tip"):
        return {"requeue": True, "why": "the worker stopped before its land started"}
    repo = paths.repo()
    row = next(
        (
            h
            for h in reversed(history())
            if h.get("op") == "land" and h.get("ok") and (h.get("landq") or {}).get("batch") == b["id"]
        ),
        None,
    )
    main = git(repo, "rev-parse", "main")
    tip = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{b.get('branch')}", check=False)
    if row is None and not (tip and main == tip and main != b.get("base")):
        return {"requeue": True, "why": f"its land stopped before the fast-forward (exit {rc})"}
    old = b.get("base") or main
    if row is not None and row.get("from"):
        old = git(repo, "rev-parse", "--verify", "-q", f"{row['from']}^{{commit}}", check=False) or old
    shas = _landq_shas(repo, old, b)
    if row is None and read_json(st("deployed.json"), {}).get("sha") != main:
        say(f"{b['id']}: main is at its tested tip ({main[:12]}) but not deployed: deploying it once")
        first = b["members"][0]
        try:
            deploy(
                op="land",
                extra={
                    "branch": first["branch"],
                    "from": old[:12],
                    "land_start": clock.now(),
                    "land_cmd_start": first.get("cmd_start"),
                    "tree": b.get("tree"),
                    "recovered": "deploy",
                    "landq": _landq_rowinfo(b, first, shas.get(first["id"]) or main, old),
                },
            )
        except Fail as e:
            return {"ok": False, "rc": e.code, "why": f"main is at the tested batch tip but its deploy failed ({e}): "
                    "run `assistant deploy` by hand"}  # fmt: skip
    _landq_member_rows(b, shas)
    for m in b["members"]:
        board_mark(m["branch"], shas.get(m["id"]) or main, [h for h in [m.get("head", ""), *(m.get("pre") or [])] if h])
    return {"ok": True, "rc": 0, "sha": main, "shas": shas, "recovered": True}


def _landq_shas(repo: Path, old: str, b: dict) -> dict:
    """Each member's last commit on main (the batch's commits in member order, by their counts); main for all when
    the counts don't add up (a commit dropped in a later rebase)."""
    main = git(repo, "rev-parse", "main")
    commits = git(repo, "rev-list", "--reverse", f"{old}..{main}", check=False).split()
    counts = [int(m.get("count") or 0) for m in b["members"]]
    if not commits or sum(counts) != len(commits) or 0 in counts:
        return {m["id"]: main for m in b["members"]}
    out, i = {}, 0
    for m, c in zip(b["members"], counts, strict=True):
        i += c
        out[m["id"]] = commits[i - 1]
    return out


def _landq_rowinfo(b: dict, m: dict, sha: str, start: str = "") -> dict:
    """A land row's landq block: the member's entry, its batch, the queue time and its own commits on main
    (start..sha: what the CI tab describes for this branch)."""
    return {
        "id": m["id"],
        "batch": b["id"],
        "size": len(b["members"]),
        "lane": b.get("lane") or "batch",
        "queued_secs": round(max(0.0, float(b.get("since") or 0) - float(m.get("since") or 0)), 1),
        "members": [x["branch"] for x in b["members"]],
        "sha": sha,
        **({"from": start[:12]} if start else {}),
    }


def _landq_member_rows(b: dict, shas: dict) -> None:
    """One land row per member that has none yet (the deploy row names the first), copied from the batch's deploy
    row with the member's branch, its own land_cmd_start and its last commit on main."""
    rows = [h for h in history() if h.get("op") == "land" and h.get("ok") and isinstance(h.get("landq"), dict)]
    main_row = next((h for h in reversed(rows) if h["landq"].get("batch") == b["id"]), None)
    if main_row is None:
        return
    have = {h["landq"].get("id") for h in rows}
    for i, m in enumerate(b["members"]):
        if m["id"] in have:
            continue
        rec = {k: v for k, v in main_row.items() if k != "ts"}
        sha = shas.get(m["id"]) or main_row.get("sha")
        start = str(main_row.get("from") or "") if i == 0 else shas.get(b["members"][i - 1]["id"]) or ""
        rec.update(
            branch=m["branch"],
            land_cmd_start=m.get("cmd_start"),
            landq={**_landq_rowinfo(b, m, sha, start), "member_of": main_row.get("branch")},
        )
        log(rec)


def _landq_reset_branches(b: dict, res: dict) -> None:
    """A member rebased into the batch (its commits on main are copies): its branch moves to its commit on main, so
    it counts as merged, unless it moved on since it was queued or its worktree has changes."""
    repo = paths.repo()
    for m in b.get("members") or []:
        sha = (res.get("shas") or {}).get(m["id"]) or res.get("sha")
        if not sha or sha == m.get("head"):
            continue
        try:
            if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{m['branch']}", check=False) != m.get("head"):
                continue
            wt = worktree_of(repo, m["branch"])
            if wt is None:
                run(["git", "-C", str(repo), "branch", "-f", m["branch"], sha])
            elif not dirty(wt):
                run(["git", "-C", str(wt), "reset", "--quiet", "--keep", sha])
        except Fail:
            continue


def _landq_drop_branch(b: dict) -> None:
    br = b.get("branch")
    if not br:
        return
    repo = paths.repo()
    try:
        wt = worktree_of(repo, br)
    except Fail:
        wt = None
    if wt is not None:  # a land killed mid-run left its temp worktree
        run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)])
        if wt.parent.name.startswith("land-") and wt.parent.parent == st():
            shutil.rmtree(wt.parent, ignore_errors=True)
    run(["git", "-C", str(repo), "branch", "-D", br])


def _landq_tell(e: dict) -> None:
    """A red, conflicting or failed entry: its live waiter prints it (exit code); nobody waiting -> one note."""
    w = _read_json_safe(_lq("waits", e["id"] + ".json"))
    try:
        if w and _alive({"pid": int(w["pid"]), "start": str(w.get("start") or "")}):
            return
    except (KeyError, TypeError, ValueError):
        pass
    try:
        _landq_note(e)
    except Exception as ex:  # noqa: BLE001 (the queue goes on; `land status` still shows it)
        say(f"land-red note not written ({type(ex).__name__})")


def _landq_note(e: dict) -> None:
    import core.notes as notes

    what = {"red": "red", "conflict": "conflicts", "error": "failed"}.get(str(e.get("state")), str(e.get("state")))
    red = " ".join(str(e.get("red") or "?").split())
    mates = [str(m) for m in e.get("mates") or []]
    lines = [
        f"Branch: {e['branch']} · queue entry {e['id']} · batch {e.get('batch') or '-'} · exit {e.get('rc')}",
        f"Why: {red[:500]}",
    ]
    if mates:
        lines.append(f"Batch-mates: {', '.join(mates[:8])} (a flaky red names them too; landing it again is the fix)")
    lines.append("Main and ~/bin don't have it: the queue only lands trees with a signed green record.")
    body = notes.compose(
        f"Land queue: {e['branch']} {what} ({e['id']})",
        lines,
        "fix it in its worktree (or hand it to the helper that made it), then `assistant land BRANCH` again; "
        "`assistant land status` shows the queue",
        source="assistant land",
    )
    payload = {
        "entry": e["id"],
        "branch": str(e["branch"])[:120],
        "state": str(e.get("state")),
        "red": red[:240],
        "batch": str(e.get("batch") or "")[:20],
        "mates": [m[:120] for m in mates[:8]],
    }
    try:
        from inbox import writer

        writer.new_note(LANDQ_RED_PREFIX, body, source="assistant land", kind="land.red", payload=payload)
    except Exception:  # noqa: BLE001 (no ledger here: the plain note still reaches the beat)
        notes.write_new(paths.inbox(), LANDQ_RED_PREFIX, body)


def _landq_phase(b: dict | None) -> tuple[str, float | None]:
    """A testing batch's phase: its land's phase record (deploy.queue/land-<pid>.phase), else "starting"."""
    if not b or not b.get("pid"):
        return "starting", None
    d = _read_json_safe(_queue_dir() / f"land-{b['pid']}.phase")
    if d.get("phase") and str(d.get("start")) == str(b.get("start")):
        return str(d["phase"]), d.get("since")
    return "starting", b.get("started")


def _landq_view(e: dict, bs: dict) -> dict:
    now = clock.now()
    b = bs.get(e.get("batch") or "")
    st_ = e.get("state")
    v = {k: e.get(k) for k in ("id", "branch", "state", "lane", "batch", "since", "ended", "sha", "rc", "red", "mates")}
    if st_ == "queued":
        v["position"] = landq_position(e)
        nb = float(e.get("not_before") or 0)
        wait = f", retry after {time.strftime('%H:%M', time.localtime(nb))}" if nb > now else ""
        detail = f"position {v['position']}{', gated lane' if e.get('lane') == 'gated' else ''}{wait}"
    elif st_ == "testing" and b:
        phase, since = _landq_phase(b)
        v.update(phase=phase, members=[m["branch"] for m in b.get("members") or []])
        secs = now - float(since or b.get("since") or now)
        detail = f"batch {b['id']} ({len(v['members'])}: {', '.join(v['members'])}) · {phase} {secs:.0f} s"
    elif st_ == "landed":
        at = time.strftime("%H:%M", time.localtime(float(e.get("ended") or 0)))
        detail = f"{str(e.get('sha') or '')[:12]} at {at}"
    elif st_ == "superseded":
        detail = f"by {e.get('superseded_by')}"
    else:
        red = " ".join(str(e.get("red") or "").split())
        detail = f"exit {e.get('rc')} · {red[:160]}" + (f" (batch-mates: {', '.join(e.get('mates') or [])})"
                                                         if e.get("mates") else "")  # fmt: skip
    v["detail"] = detail
    v["line"] = f"{e['id']:5} {str(e.get('branch'))[:40]:40} {st_:10} {detail}"
    return v


def _landq_line(e: dict) -> str:
    """The waiter's progress line, printed when it changes: the state, position, batch or phase (no running
    seconds, so one line per step, not one per poll)."""
    b = _landq_read("batches", e.get("batch")) if e.get("batch") else None
    if e.get("state") == "testing" and b:
        members = ", ".join(m["branch"] for m in b.get("members") or [])
        return f"{e['id']} {e['branch']}: testing in batch {b['id']} ({members}): {_landq_phase(b)[0]}"
    v = _landq_view(e, {b["id"]: b} if b else {})
    return f"{e['id']} {e['branch']}: {e.get('state')}, {v['detail']}"


def landq_wait(eid: str, timeout: float = LANDQ_WAIT) -> int:
    """Follow one entry to its end: exit 0 landed (prints main's commit), 5 conflict, 6 red (the red line), 7 the
    budget ran out or the queue stalled (the entry stays queued); a newer land of the branch is followed."""
    end = time.monotonic() + timeout
    me = _proc_stat(os.getpid())
    shown, nowork = "", None

    def mark(i: str) -> None:
        write_json(_lq("waits", i + ".json"), {"pid": os.getpid(), "start": me[2] if me else "", "since": clock.now()})

    mark(eid)
    try:
        while True:
            e = _landq_read("entries", eid)
            if e is None:
                raise Fail(f"land queue: entry {eid} is gone", 1)
            if e.get("state") == "superseded" and e.get("superseded_by"):
                say(f"{eid} was superseded by {e['superseded_by']} (a newer land of {e['branch']}): following that")
                with contextlib.suppress(OSError):
                    _lq("waits", eid + ".json").unlink()
                eid = str(e["superseded_by"])
                mark(eid)
                continue
            if e.get("state") in LANDQ_DONE:
                return _landq_outcome(e)
            line = _landq_line(e)
            if line and line != shown:
                say(line)
                shown = line
            if landq_kick() or _landq_worker_alive():
                nowork = None
            elif nowork is None:
                nowork = time.monotonic()
            elif time.monotonic() - nowork > LANDQ_STALL:
                raise Fail(f"land queue stalled: no worker runs ({eid} {e.get('state')}); `assistant land status`", 7)
            if time.monotonic() >= end:
                raise Fail(
                    f"{eid} ({e['branch']}) still {e.get('state')} after {timeout:.0f} s: it stays in the queue "
                    f"(`assistant land status {eid}`)",
                    7,
                )
            time.sleep(_landq_tick())
    finally:
        with contextlib.suppress(OSError):
            _lq("waits", eid + ".json").unlink()


def _landq_outcome(e: dict) -> int:
    if e.get("state") == "landed":
        secs = float(e.get("ended") or clock.now()) - float(e.get("cmd_start") or e.get("since") or 0)
        say(
            f"landed {e['branch']} on main ({str(e.get('sha') or '')[:12]}) in {secs:.0f} s "
            f"(queue {e['id']}, batch {e.get('batch') or '-'})"
        )
        return 0
    code = int(e.get("rc") or {"conflict": 5, "red": 6}.get(str(e.get("state")), 1))
    mates = e.get("mates") or []
    raise Fail(
        f"{e['id']} {e['branch']} {e.get('state')}: {e.get('red') or '?'}"
        + (f" (batch-mates: {', '.join(mates)})" if mates else ""),
        code,
    )


def landq_status(ref: str | None, as_json: bool) -> int:
    """The queue: queued (position), testing (batch, members, phase, seconds), landed, red, conflict, superseded;
    starts the worker when something waits and none runs."""
    if _landq_pending():
        landq_kick()
    es = _landq_all("entries")
    bs = {b["id"]: b for b in _landq_all("batches")}
    if ref:
        sel = [e for e in es if e["id"] == ref] or [e for e in es if e.get("branch") == ref][-1:]
        if not sel:
            raise Fail(f"land queue: no entry {ref} (`assistant land status` lists them)", 3)
    else:
        live = [e for e in es if e.get("state") in ("queued", "testing")]
        sel = live + [e for e in es if e.get("state") not in ("queued", "testing")][-8:]
    rows = [_landq_view(e, bs) for e in sel]
    worker = _landq_worker_alive()
    if as_json:
        say(json.dumps({"switch": land_queue(), "worker": worker, "entries": rows, "front": _landq_front()},
                       sort_keys=True))  # fmt: skip
        return 0
    nq = sum(1 for e in es if e.get("state") == "queued")
    nt = sum(1 for e in es if e.get("state") == "testing")
    say(f"land queue: {'on' if land_queue() else 'off'} · worker {'running' if worker else 'idle'} · {nq} queued · "
        f"{nt} testing")  # fmt: skip
    for r in rows:
        say("  " + r["line"])
    return 0


def _landq_prune() -> None:
    cut = clock.now() - LANDQ_KEEP
    for e in _landq_all("entries"):
        if e.get("state") in LANDQ_DONE and float(e.get("ended") or e.get("updated") or 0) < cut:
            with contextlib.suppress(OSError):
                _lq("entries", e["id"] + ".json").unlink()
    for b in _landq_all("batches"):
        if b.get("state") == "done" and float(b.get("ended") or b.get("updated") or 0) < cut:
            for p in (
                _lq("batches", b["id"] + ".json"),
                _lq("results", b["id"] + ".json"),
                _lq("logs", b["id"] + ".log"),
            ):
                with contextlib.suppress(OSError):
                    p.unlink()


def _landq_child(branch: str, bid: str, adopt=()) -> int:
    """`land landq/b-N --batch bN` (the worker's child): today's land() on the batch branch; its outcome goes to
    landq/results/bN.json for the worker (exit codes as a direct land)."""
    try:
        r = land(branch, adopt, None, batch=bid)
    except Fail as e:
        why = re.sub(re.escape(str(st())) + r"/land-[^/\s]+/wt", "its worktree", str(e))  # the batch's temp checkout
        write_json(
            _lq("results", bid + ".json"),
            {"ok": False, "rc": e.code, "why": why[:1000], "shared": e.shared, "at": clock.now()},
        )
        raise
    write_json(
        _lq("results", bid + ".json"), {"ok": True, "rc": 0, "sha": r["sha"], "shas": r["shas"], "at": clock.now()}
    )
    return 0


# ---------------------------------------------------------------- hub CI runner + land gate
GATE_MODES = ("shadow", "enforce", "enforce-gated", "off")
GATED_FILE = ("ops", "ci", "gated-paths.json")
GATED_DEFAULT = {  # used when main has no ops/ci/gated-paths.json (that file is the one place to edit)
    "prompts": ["adapters/*/prompts/*", "*/prompts/*", "*.prompt.md", "hooks/agenda.sh"],
    "skills": ["skills/*", "*/skills/*", "*SKILL.md"],
    "tools": ["packages/tools/*", "schemas/tools.toml", "schemas/tools.typed.json"],
    "reflexes": ["packages/loop/reflex.py", "scripts/reflex", "*/reflexes/*"],
    "workflows": ["packages/hub/hubwf.py", "packages/hub/hubwfapi.py", "packages/hubkit/wf.py",
                  "packages/tools/domains/workflow.py", "core/watchjobs.py", "core/schedules.toml", "scripts/eventwf",
                  "*/workflows/*"],
    "gate": ["ops/ci/gated-paths.json"],
}  # fmt: skip
GATE_MIN_SIGNED = 5
RUNNER_WAIT_SHADOW = 300.0
RUNNER_WAIT_ENFORCE = 1800.0


def gate_file() -> Path:
    return paths.config("ci-gate")


def gate_mode() -> str:
    try:
        m = gate_file().read_text(encoding="utf-8").strip()
    except OSError:
        return "shadow"
    return m if m in GATE_MODES else "shadow"


def gated_globs(repo: Path | None = None) -> dict[str, list[str]]:
    """The gated path globs per category, from main's ops/ci/gated-paths.json (a branch can't loosen its own gate);
    unreadable or malformed -> GATED_DEFAULT."""
    try:
        d = json.loads((repo or paths.repo()).joinpath(*GATED_FILE).read_text(encoding="utf-8"))
        out = {k: [str(g) for g in v] for k, v in d.items() if not k.startswith("_") and isinstance(v, list)}
        return out or GATED_DEFAULT
    except (OSError, ValueError, AttributeError):
        return GATED_DEFAULT


def gated_categories(changed: list[str], repo: Path | None = None) -> list[str]:
    globs = gated_globs(repo)
    return sorted(c for c, gs in globs.items() if any(fnmatch.fnmatchcase(p, g) for p in changed for g in gs))


_CASES_RE = re.compile(r"(\d+) case\(s\) selected")


def _eval_cases(ev: dict) -> int | None:
    n = ev.get("cases")
    if isinstance(n, int):
        return n
    m = _CASES_RE.search(str(ev.get("detail") or ""))
    return int(m[1]) if m else None


def gate_metric(rows: list[dict], repo: Path | None = None) -> dict:
    """The gate's done-when share: of the lands on gated paths, how many carried a green eval subset (the subset ran,
    passed, >= 1 case). Older rows have no `gated`: their paths come from git (from..sha)."""
    repo = repo or paths.repo()
    globs = gated_globs(repo)
    cnt: dict[str, int] = {"lands": len(rows), "gated": 0, "green": 0, "dry_run_only": 0, "no_cases": 0, "failed": 0,
           "override": 0, "unknown": 0}  # fmt: skip
    for r in rows:
        g = r.get("gate") or {}
        cats = g.get("gated")
        if cats is None:
            frm, to = r.get("from"), r.get("sha") or r.get("release")
            res = run(["git", "-C", str(repo), "diff", "--name-only", f"{frm}..{to}"]) if frm and to else None
            if res is None or res.returncode != 0:
                cnt["unknown"] += 1
                continue
            ch = [x for x in res.stdout.splitlines() if x]
            cats = sorted(c for c, gs in globs.items() if any(fnmatch.fnmatchcase(p, x) for p in ch for x in gs))
        if not cats:
            continue
        cnt["gated"] += 1
        ev = g.get("evals") or {}
        n = _eval_cases(ev)
        if g.get("override"):
            cnt["override"] += 1
        elif ev.get("mode") == "run" and ev.get("ok") and (n or 0) >= 1:
            cnt["green"] += 1
        elif ev.get("mode") == "run" and not ev.get("ok"):
            cnt["failed"] += 1
        elif n == 0:
            cnt["no_cases"] += 1
        else:
            cnt["dry_run_only"] += 1
    return {**cnt, "share": round(cnt["green"] / cnt["gated"], 3) if cnt["gated"] else None}


def _subagent() -> bool:
    """AI_AGENT ends in "_agent": a Claude Code subagent's shell, and the main session's too in current Claude Code
    (every Bash shell gets the same environment, so nothing tells the two apart); --override then needs
    `env -u AI_AGENT`, and the land queue waits by default everywhere (the main session passes --no-wait)."""
    return os.environ.get("AI_AGENT", "").endswith("_agent")


def _runner_submit(wt: Path, head: str, tree: str, base: str | None, branch: str) -> tuple[dict | None, str]:
    seam = test_seam("ASSISTANT_LAND_RUNNER")
    if seam:
        return {"id": "seam", "seam": seam}, ""
    try:
        from hubkit import ci as hci

        return hci.submit(wt, head, tree, base, branch), ""
    except Exception as e:  # the runner is optional in shadow mode; enforce refuses without its record
        return None, f"{type(e).__name__}: {e}"[:200]


def _runner_result(job: dict | None, tree: str, wait: float) -> dict:
    """The runner's answer for job (waits up to `wait` s) -> {job, state, ok, signed_green, secs, steps, why}."""
    if job and job.get("seam"):
        r = run([*shlex.split(job["seam"]), tree], timeout=60)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {"state": "error", "why": "seam output"}
    if not job:
        return {"state": "none"}
    from hubkit import ci as hci

    try:
        ans = hci.wait(job["id"], wait)
    except Exception as e:
        return {"job": job.get("id"), "state": "error", "why": f"{type(e).__name__}: {e}"[:200]}
    rec = ans.get("record") or {}
    if ans.get("sig"):
        hci.cache(ans)
    walls = {"created": ans.get("created"), "started": rec.get("started"), "ended": rec.get("ended")}  # land timeline
    return {
        "job": ans.get("id"),
        "state": ans.get("state"),
        "ok": ans.get("ok"),
        "signed_green": hci.signed_green(ans),
        "valid": ans.get("valid"),
        "secs": ans.get("secs"),
        **{k: v for k, v in walls.items() if isinstance(v, (int, float))},
        "why": rec.get("error") or None,
        "steps": {s.get("name"): s.get("secs") for s in rec.get("steps") or [] if isinstance(s, dict)},
        "failed": [s.get("name") for s in rec.get("steps") or [] if isinstance(s, dict) and not s.get("ok")],
        "red": _runner_red(rec),
        **_runner_pytest(rec),
    }


def _runner_red(rec: dict, n: int = 5) -> str:
    """The red steps of a runner record in one line: each step's failing tests (pytest node ids) or red suites
    (legacy tails), else its detail; '' when none is red."""
    out = []
    for s in rec.get("steps") or []:
        if not isinstance(s, dict) or s.get("ok"):
            continue
        what = [str(x) for x in s.get("failed") or []] or [str(k) for k in s.get("tails") or {}]
        more = f" +{len(what) - n}" if len(what) > n else ""
        out.append(f"{s.get('name')}: " + (", ".join(what[:n]) + more if what else str(s.get("detail") or "")[:200]))
    return "; ".join(out)[:900]


def _runner_pytest(rec: dict) -> dict:
    """The signed record's pytest step: its selection, retry and quarantine (a release before them: none)."""
    for s in rec.get("steps") or []:
        if isinstance(s, dict) and str(s.get("name", "")).startswith("pytest"):
            return {k: s[k] for k in ("selection", "rerun", "quarantined") if s.get(k)}
    return {}


def _land_tests(local: dict | None, runner: dict) -> dict:
    """The land row's test record: selection impacted|full (full when any run was full), the counts per run, and
    the retries/quarantine; "stamp" when no CI ran here (a green stamp) and the runner said nothing."""
    loc = {n: x for n, x in (local or {}).items() if isinstance(x, dict)}
    runs = {f"local {n}": x for n, x in loc.items()}
    if runner.get("selection"):
        runs["runner"] = runner
    modes = {(x.get("selection") or {}).get("mode") for x in runs.values()} - {None}
    out: dict = {"selection": "full" if "full" in modes else "impacted" if modes else "stamp"}
    out["tests"] = {
        n: {k: (x.get("selection") or {}).get(k) for k in ("files", "of", "tests")} for n, x in runs.items()
    }
    for key in ("rerun", "quarantined"):
        got = {n: x[key] for n, x in runs.items() if x.get(key)}
        if got:
            out[key] = got
    return out


EVAL_LOCK_WAIT = 1800  # s a land's eval run waits for another run's lock (the call's own timeout is 3600 s)


def _eval_subset(changed: list[str], run_it: bool) -> dict:
    """The eval cases covering the changed repo paths (evals maps them through the deploy manifest and the cases'
    `covers`, as `evals run --changed` does). First the selection (--dry-run, no spend): no case covers the land's
    paths -> mode "skipped", no eval step (29.10: the subset ran on every land). Shadow: that case list is the
    result; enforce: run them (as evalbot, network) and require green. A selection that fails or can't be read
    falls back to the old path (shadow: its dry run; enforce: the run), never to a skip."""
    if not changed:
        return {"mode": "none", "ok": True, "cases": 0}
    seam = test_seam("ASSISTANT_LAND_EVALS")
    exe = shlex.split(seam) if seam else [str(paths.bin_dir("evals"))]
    cmd = exe + ["run", "--changed", "--paths", *changed, "--json", "--quiet"]
    sel = _evals_call(cmd + ["--dry-run"], "dry-run", 180)
    if sel.get("ok") and sel.get("cases") == 0:
        return {"mode": "skipped", "ok": True, "cases": 0, "detail": "no covered paths"}
    # another land's subset holds evals' run lock: wait for it (EVAL_LOCK_WAIT) instead of failing the land (29.10)
    return _evals_call(cmd + ["--lock-wait", str(EVAL_LOCK_WAIT)], "run", 3600) if run_it else sel


def _evals_call(cmd: list[str], mode: str, timeout: float) -> dict:
    try:
        r = run(cmd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"mode": mode, "ok": False, "detail": type(e).__name__}
    n = None
    try:
        d = json.loads(r.stdout or "null")
        cases = d.get("cases") if isinstance(d, dict) else d
        n = len(cases) if isinstance(cases, (list, dict)) else None
    except ValueError:
        pass
    last = (r.stdout.strip() or r.stderr.strip()).splitlines()[-1:] or [""]
    return {"mode": mode, "ok": r.returncode == 0, "rc": r.returncode, "cases": n, "detail": last[0][:160]}


def _evals_line(ev: dict) -> str:
    """The land output's one line on the eval step."""
    mode = ev.get("mode")
    if not ev:
        return "evals: off (gate off or override)"
    if mode == "skipped":
        return "evals: skipped (no covered paths)"
    if mode == "none":
        return "evals: skipped (no changed paths)"
    n = ev.get("cases")
    what = f"{n} covered case(s)" if isinstance(n, int) else "covered cases"
    state = "green" if ev.get("ok") else f"red ({ev.get('detail') or ev.get('rc')})"
    return f"evals: {'ran' if mode == 'run' else 'listed (shadow, not run)'} {what}, {state}" + (
        " (reused from the previous pass)" if ev.get("reused") else ""
    )


def ci_hub(tree: Path, base: str | None, wait: float, full: bool = False) -> int:
    """`assistant ci --runner hub`: the committed HEAD of tree on the hub's runner; prints the signed record. full:
    every pytest file there too (no base and no main in the bundle: the runner's ci has nothing to diff against)."""
    from hubkit import ci as hci

    tree = Path(os.path.realpath(tree))
    head = git(tree, "rev-parse", "HEAD")
    tid = git(tree, "rev-parse", "HEAD^{tree}")
    if base is None:
        r = run(["git", "-C", str(tree), "merge-base", "HEAD", "main"])
        base = r.stdout.strip() if r.returncode == 0 else None
    else:
        base = git(tree, "rev-parse", "--verify", base + "^{commit}")
    if git(tree, "status", "--porcelain", "--untracked-files=no"):
        say("note: uncommitted changes are not in the bundle (the runner tests the committed HEAD)")
    branch = git(tree, "rev-parse", "--abbrev-ref", "HEAD")
    try:
        name = branch if branch != "HEAD" else ""
        job = hci.submit(tree, head, tid, None if full else base, name, with_main=not full)
        say(f"runner job {job['id']} for {head[:12]} (tree {tid[:12]}) ...")
        ans = hci.wait(job["id"], wait)
    except Exception as e:
        raise Fail(f"hub runner: {type(e).__name__}: {e}", 4) from None
    rec = ans.get("record") or {}
    for s in rec.get("steps") or []:
        say(f"{'ok  ' if s.get('ok') else 'FAIL'} {str(s.get('name')):10} {s.get('secs')} s  {s.get('detail') or ''}")
        for suite, tail in sorted((s.get("tails") or {}).items()):
            say(f"     {suite}: {tail}")
    if rec.get("error"):
        say(f"error: {rec['error']}")
    if ans.get("sig"):
        hci.cache(ans)
    green = hci.signed_green(ans)
    say(
        f"runner {'green' if green else 'NOT green'} ({ans.get('state')}, {ans.get('secs')} s, signed "
        f"{'yes, valid' if ans.get('valid') else 'no'}, key {rec.get('key_id') or '-'}, toolchain "
        f"{(rec.get('runner') or {}).get('toolchain') or '-'})"
    )
    return 0 if green else 6


def ci_nightly(note: bool = True) -> int:
    """The nightly full suite (cron, `assistant ci-nightly`): every step and every pytest file (no selection) plus
    every legacy suite on a detached checkout of main's HEAD, offline, never stamps (main's legacy record is
    updated). Red, or a quarantine entry gone stale -> ONE ~/inbox/ci-nightly-<epoch>.md for the main session (data,
    never a message to him). Exit 0 when the run finished (green or red: the note is the report), 1 when it could
    not run."""
    repo = paths.repo()
    head = git(repo, "rev-parse", "main")
    d = Path(tempfile.mkdtemp(prefix="nightly-", dir=st()))
    wt, rep = d / "wt", d / "report.json"
    t0 = time.monotonic()
    try:
        git(repo, "worktree", "add", "--quiet", "--detach", str(wt), head)
        seam = test_seam("ASSISTANT_NIGHTLY_CI")
        cmd = shlex.split(seam) if seam else [sys.executable, "-I", os.path.realpath(__file__), "ci"]
        cmd += ["--path", str(wt), "--base", "", "--offline", "--no-stamp", "--full", "--report", str(rep)]
        rc = run_child(cmd, cwd=wt, timeout=LAND_CI_TIMEOUT)
        res = _read_json_safe(rep)
        _live, bad = quarantine(wt, None)
    finally:
        run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)])
        shutil.rmtree(d, ignore_errors=True)
    secs = round(time.monotonic() - t0)
    red = [s for s in res.get("steps") or [] if not s.get("ok")]
    log({"op": "ci-nightly", "ok": rc == 0, "rc": rc, "head": head[:12], "secs": secs, "red": [s["name"] for s in red]})
    say(f"ci-nightly {'green' if rc == 0 else 'RED'} for {head[:12]} in {secs} s")
    if not res:
        return 1
    if note and (rc != 0 or bad):
        import core.notes as notes

        body = [f"main {head[:12]}: {'red' if rc else 'green'} in {secs} s (full suite, every tool suite)."]
        for s in red[:8]:
            body.append(f"- {s['name']}: {str(s.get('detail') or '')[:200]}")
            body += [f"  - {i}" for i in (s.get("failed") or [])[:10]]
        if bad:
            body.append("Quarantine (tests/quarantine.txt):")
            body += [f"- {b}" for b in bad[:10]]
        text = notes.compose(
            "ci-nightly: main's full suite " + ("is red" if rc else "has stale quarantine entries"),
            body,
            "fix the red test on a branch and land it (a flake: deterministic waits, else a quarantine line with "
            "by= and expires=); rerun: `assistant ci-nightly`",
            source="assistant ci-nightly",
        )
        notes.write_new(paths.inbox(), "ci-nightly", text)
    return 0


LEGACY_MAIN_LOCK = "legacy-main.lock"  # one post-land main check at a time
LEGACY_MAIN_LAST = "legacy-main.last.json"  # main's sha the last one ran on
LEGACY_MAIN_LOG = "legacy-main.log"
LEGACY_MAIN_LOAD = 30.0  # host load1 above this: no post-land run (the nightly run covers main)
LEGACY_MAIN_ROUNDS = 3  # main moved during a run (another land): once more on the new main, this often at most


def legacy_main_bg(branch: str) -> None:
    """After a land whose CI ran only the impacted suites: `assistant legacy-main` detached (every suite on
    main, off the land's critical path), with the driver just deployed. Never fails or slows a land."""
    seam = test_seam("ASSISTANT_LEGACY_MAIN_BIN")
    if seam is None and paths.test_mode():
        return
    cmd = shlex.split(seam) if seam else [sys.executable, "-I", os.path.realpath(__file__), "legacy-main"]
    try:
        with open(st(LEGACY_MAIN_LOG), "ab") as out:
            subprocess.Popen(
                [*cmd, "--after", branch],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=st(),
            )
        say("suites: every tool suite on main in the background (`assistant legacy-main`, state legacy-main.log)")
    except OSError as e:  # the land is done: report, never raise
        say(f"suites on main not started: {type(e).__name__}")


def legacy_main_check(after: str = "", note: bool = True) -> int:
    """`assistant legacy-main`: every legacy suite on a detached checkout of main's HEAD (as ci-nightly runs them),
    into main's record (how "post-land"), niced. One at a time: a second start while one runs exits, and the running
    one checks main again when done (up to LEGACY_MAIN_ROUNDS runs). Skipped above LEGACY_MAIN_LOAD (the nightly run
    covers main) or when main's sha already ran. A suite green before and red now (the impact map missed it, or a
    flake) -> ONE ~/inbox/ci-nightly-<epoch>.md note (data for the main session). Exit 0 ran or nothing to do."""
    from core import locks

    with contextlib.suppress(OSError):
        os.nice(10)
    repo = paths.repo()
    with locks.hold(st(LEGACY_MAIN_LOCK), wait=0) as fd:
        if fd is None:
            say("suites on main: a run is going; it checks main again when done")
            return 0
        for _round in range(LEGACY_MAIN_ROUNDS):
            head = git(repo, "rev-parse", "main")
            if _read_json_safe(st(LEGACY_MAIN_LAST)).get("sha") == head:
                say(f"suites on main: {head[:12]} already checked")
                return 0
            load = os.getloadavg()[0]
            if load > LEGACY_MAIN_LOAD:
                log({"op": "legacy-main", "ok": True, "head": head[:12], "skipped": f"load {load:.0f}"})
                say(f"suites on main: load {load:.0f} > {LEGACY_MAIN_LOAD:.0f}, left to the nightly run")
                return 0
            before = legacy_main()
            d = Path(tempfile.mkdtemp(prefix="legacy-main-", dir=st()))
            t0 = time.monotonic()
            try:
                git(repo, "worktree", "add", "--quiet", "--detach", str(d / "wt"), head)
                names = {e["suite"] for e in load_manifest(d / "wt")["file"] if e.get("suite")}
                res = _legacy_run(d / "wt", None, names)
            finally:
                run(["git", "-C", str(repo), "worktree", "remove", "--force", str(d / "wt")])
                shutil.rmtree(d, ignore_errors=True)
            secs = round(time.monotonic() - t0)
            legacy_main_record(res, head, "post-land")
            write_json(st(LEGACY_MAIN_LAST), {"sha": head, "ts": round(clock.now(), 3), "after": after})
            red = sorted(s for s, r in res.items() if not r.get("ok"))
            new = [s for s in red if (before.get(s) or {}).get("ok", True)]
            log({"op": "legacy-main", "ok": not red, "head": head[:12], "secs": secs, "after": after, "red": red})
            say(f"suites on main: {len(res)} on {head[:12]} in {secs} s, red: {', '.join(red) or 'none'}")
            if note and new:
                import core.notes as notes

                body = [f"main {head[:12]} (after the land of {after or '?'}): {len(new)} suite(s) newly red."]
                body += [f"- {s}: {str(res[s].get('detail') or '')[:200]}" for s in new[:8]]
                text = notes.compose(
                    "tool suites: one went red on main after a land",
                    body,
                    "the land ran only the suites its change impacts (legacy_select): fix the suite on a branch and "
                    "land it; if the land's change caused it, add the missed file to tests/legacy/impact.toml; "
                    "rerun: `assistant legacy-main`",
                    source="assistant legacy-main",
                )
                notes.write_new(paths.inbox(), "ci-nightly", text)
                note = False
    return 0


def _land_rows(n: int = 50) -> list[dict]:
    return [r for r in history() if r.get("op") == "land" and r.get("ok")][-n:]


def ci_gate(action: str, reason: str, force: bool, as_json: bool, lands: int = 20) -> int:
    mode = gate_mode()
    rows = _land_rows()
    signed = [r for r in rows if (r.get("runner") or {}).get("signed_green")]
    if action == "status":
        last = [r for r in history() if r.get("op") == "ci-gate"][-1:]
        overrides = [
            {"branch": r.get("branch"), "release": r.get("release"), "reason": (r.get("gate") or {}).get("override"),
             "by": (r.get("gate") or {}).get("by"), "ts": r.get("ts")}
            for r in rows if (r.get("gate") or {}).get("override")
        ][-5:]  # fmt: skip
        out = {
            "mode": mode,
            "file": str(gate_file()),
            "signed_green_lands": len(signed),
            "needed": GATE_MIN_SIGNED,
            "lands_seen": len(rows),
            "last_change": last[0] if last else None,
            "scope": gated_globs(),
            "gated_metric": gate_metric(rows[-max(1, lands) :]),
            "overrides": overrides,
            "recent": [
                {
                    "branch": r.get("branch"),
                    "release": r.get("release"),
                    "runner": {k: (r.get("runner") or {}).get(k) for k in ("state", "signed_green", "secs")},
                    "ci_local_secs": (r.get("gate") or {}).get("ci_local_secs"),
                }
                for r in rows[-8:]
            ],
        }
        if as_json:
            say(json.dumps(out, indent=1))
        else:
            say(f"ci-gate: {mode} · signed green lands {len(signed)} (enforce needs {GATE_MIN_SIGNED})")
            gm = out["gated_metric"]
            say(
                f"  gated lands (last {gm['lands']}): {gm['gated']} · green subset {gm['green']}"
                f" ({'-' if gm['share'] is None else format(gm['share'], '.0%')}) · dry-run only {gm['dry_run_only']}"
                f" · no cases {gm['no_cases']} · failed {gm['failed']} · override {gm['override']}"
                + (f" · unknown {gm['unknown']}" if gm["unknown"] else "")
            )
            say(
                "  scope: "
                + " · ".join(f"{k} {len(v)}" for k, v in out["scope"].items())
                + " (ops/ci/gated-paths.json)"
            )
            for o in overrides:
                say(f"  override {o['release']} {o['branch']}: {o['reason']}")
            for x in out["recent"]:
                rr = x["runner"]
                say(
                    f"  {x['release']} {x['branch']}: runner {rr.get('state') or '-'} {rr.get('secs') or '-'} s"
                    f"{' signed' if rr.get('signed_green') else ''} · local CI {x['ci_local_secs'] or '-'} s"
                )
        return 0
    if len(reason.strip()) < 8:
        raise Fail("ci-gate: --reason needed (8+ characters; it goes into the deploy log)", 2)
    if action in ("enforce", "enforce-gated") and len(signed) < GATE_MIN_SIGNED and not force:
        raise Fail(
            f"ci-gate: only {len(signed)} signed green lands so far (enforce needs {GATE_MIN_SIGNED}; --force)", 3
        )
    f = gate_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".ci-gate.{os.getpid()}.tmp")
    tmp.write_text(action + "\n", encoding="utf-8")
    os.replace(tmp, f)
    log(
        {
            "op": "ci-gate",
            "ok": True,
            "mode": action,
            "prev": mode,
            "reason": reason.strip()[:200],
            "signed_green_lands": len(signed),
            "by": os.environ.get("AI_AGENT", "")[:40],
        }
    )
    say(f"ci-gate: {mode} -> {action} (logged)")
    return 0


def _protected_check(rid: str, h: dict | None, op: str) -> None:
    """A hub release whose protected set differs from the running one loads only once the hub accepts its digest
    (his ✅ on a hub approval, or the console line). Not accepted: request the approval (the hub mints it), send him
    its ✅ card through the outbox (_protected_card; the hub never sends one by itself), exit 8 with CURRENT
    unchanged."""
    from hubkit import release as hr
    from hubkit import settings

    from core import hub as ch

    meta = read_json(Path(hr.releases_dir(rid, "release.json")), {})
    new = (meta.get("protected") or {}).get("digest")
    cur = ((h or {}).get("protected") or {}).get("digest")
    if not new or not cur or new == cur:
        return
    try:
        got = ch.call("GET", f"/protected/{new}", timeout=10)
    except ch.HubError as e:
        raise Fail(f"hub: protected-set check failed ({e}); CURRENT unchanged", 4) from None
    if got.get("accepted"):
        say(f"protected paths changed; the hub accepts the set ({got.get('how')})")
        return
    names = ", ".join(meta["protected"].get("files") or [])
    try:
        ap = ch.call(
            "POST",
            "/approvals",
            {
                "rid": f"hubrel-{new[:32]}",
                "action_sha": got["action_sha"],
                "summary": f"Load hub release {rid}: protected code changed",
                "ref": f"hubrel:{new[:16]}",
                "ttl": 86_400,
            },
            timeout=10,
        )
    except ch.HubError as e:
        raise Fail(f"hub: approval request failed ({e}); CURRENT unchanged", 4) from None
    try:
        hub_root = settings()["hub_root"]
    except Exception:
        hub_root = "<hub appdata>"
    card = _protected_card(rid, new, len(meta["protected"].get("files") or []), str(ap.get("nonce") or ""))
    log(
        {
            "op": op,
            "target": "hub",
            "release": rid,
            "ok": False,
            "why": "protected: approval pending",
            "nonce": ap.get("nonce"),
            "digest": new,
            "card": card,
        }
    )
    raise Fail(
        f"hub release {rid} changes protected paths ({names}): needs the owner's ✅ on digest {new[:16]} "
        f"(hub approval {ap.get('nonce')}, expires in 24 h; decided only through the hub's tg caller; {card}). "
        f"Interim, his Unraid console: echo {new} >> {hub_root}/state/protected-approved . Then deploy again; "
        "CURRENT is unchanged",
        8,
    )


PROTECTED_CARD_SENDER = "assistant-deploy"


def _protected_card(rid: str, digest: str, nfiles: int, nonce: str) -> str:
    """His ✅ card for a protected-set hub approval: one outbox send naming the nonce (the hub's tg checks it is
    pending, adds the ✅/❌ row and records the card), idem per nonce, so a re-run deploy (the hub replays the same
    approval for the same digest) never sends a second card. Held like any send in quiet hours. Returns what happened,
    for the exit line and the deploy log: never raises (the approval stands either way)."""
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        return "no card: the hub returned no nonce"
    from core import tg

    text = (
        f"🔐 Hub release {rid[:12]} changes protected code ({nfiles} files, digest {digest[:16]}).\n"
        "Load it? I deploy again after your ✅. Expires in 24 h."
    )
    body = {"text": text, "approval": {"nonce": nonce, "yes": "✅ Load it", "no": "❌ Not now"}}
    try:
        o = tg.send(PROTECTED_CARD_SENDER, "send", body, idem=f"hubrel-card:{nonce}", wait=10.0)
    except Exception as e:  # noqa: BLE001  (the exit line says so; the approval itself is minted)
        return f"card not sent ({type(e).__name__}): send one with buttons ap:{nonce}:y|n"
    if o.status == "sent":
        return "card sent"
    if o.status == "queued":
        return "card queued (held or retrying in the outbox)"
    if o.status == "skipped":
        return "card already sent for this approval"
    return f"card not sent ({o.status}: {o.error[:80]}): send one with buttons ap:{nonce}:y|n"


# ---------------------------------------------------------------- crontab
CRON_TAG = re.compile(r"#\s*assistant:([A-Za-z0-9_.-]+)\s*$")
CRON_FIELD = re.compile(r"^[0-9A-Za-z*/,\-]+$")
ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


def cron_line_ok(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#") or ENV_LINE.match(s):
        return True
    parts = s.split()
    if parts[0].startswith("@"):
        return (
            parts[0] in ("@reboot", "@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly")
            and len(parts) >= 2
        )
    return len(parts) >= 6 and all(CRON_FIELD.match(f) for f in parts[:5])


def cron_edit(action: str, name: str = "", sched: str = "", command: str = "", dry_run: bool = False) -> None:
    """add / rm one tagged line (list prints them). dry_run: say what would change; no lock, no write."""
    ct = Path(os.path.realpath(paths.home() / "crontab"))
    paths.guard(ct)
    with contextlib.nullcontext() if dry_run else Lock(f"cron {action} {name}"):
        old = ct.read_text(encoding="utf-8") if ct.exists() else ""
        lines = old.splitlines()
        mine = [i for i, ln in enumerate(lines) if (m := CRON_TAG.search(ln)) and m.group(1) == name]
        if action == "list":
            for ln in lines:
                if CRON_TAG.search(ln):
                    say(ln)
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
            raise Fail("cron NAME: 1-40 of [A-Za-z0-9_.-]", 2)
        if action == "add":
            if mine:
                raise Fail(f"cron entry {name} exists (rm it first)", 3)
            if "\n" in sched + command or not command.strip():
                raise Fail("cron add: one-line SCHEDULE and COMMAND needed", 2)
            line = f"{sched.strip()} {command.strip()}  # assistant:{name}"
            if not cron_line_ok(line):
                raise Fail(f"cron add: invalid line: {line}", 2)
            new_lines = lines + [line]
        else:
            if not mine:
                raise Fail(f"no cron entry {name}", 3)
            new_lines = [ln for i, ln in enumerate(lines) if i not in mine]
        bad = [ln for ln in new_lines if not cron_line_ok(ln)]
        if bad:
            raise Fail(f"crontab would hold {len(bad)} invalid line(s); unchanged", 3)
        if abs(len(new_lines) - len(lines)) != (1 if action == "add" else len(mine)) or (lines and not new_lines):
            raise Fail("crontab edit sanity check failed; unchanged", 3)
        if dry_run:
            gone = [lines[i] for i in mine] if action == "rm" else []
            for ln in gone or [new_lines[-1]]:
                say(f"cron {action} {name}: dry run, would {'remove' if gone else 'add'}: {ln}")
            return
        brain(f"before assistant cron {action} {name}")
        mode = stat.S_IMODE(ct.stat().st_mode) if ct.exists() else 0o644
        install_bytes(("\n".join(new_lines) + "\n").encode(), ct, mode)
        inst = test_seam("ASSISTANT_CRONTAB_BIN")
        if inst is None and paths.test_mode():
            inst = "true"
        r = run([inst or "crontab", str(ct)])
        if r.returncode != 0:
            install_bytes(old.encode(), ct, mode)
            raise Fail(f"crontab install failed (exit {r.returncode}); file restored", 1)
        log({"op": "cron", "action": action, "name": name, "ok": True})
        brain(f"after assistant cron {action} {name}")
        say(f"cron {action} {name}: ok")


def _profile_features() -> set[str] | None:
    """The profile's `features` switch via profile-get (~/bin, else the repo copy): None when the key is absent
    (unknown to profile-get, exit 2) or empty; a list of names or an object {name: bool} otherwise."""
    here = Path(__file__).resolve().parents[1] / "scripts" / "profile-get"  # a checkout's copy (tests, a fresh clone)
    tool = next(
        (t for t in (paths.bin_dir("profile-get"), paths.repo("scripts", "profile-get"), here) if t.exists()), None
    )
    if tool is None:
        return None
    r = run([str(tool), "features"], timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        v = json.loads(r.stdout)
    except ValueError:
        raise Fail(f"cron install: the profile's features is not JSON: {r.stdout.strip()[:60]}", 2) from None
    if isinstance(v, dict):  # {switch: bool}: only the switches set true schedule jobs
        return {str(k) for k, on in v.items() if on is True}
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return set(v)
    raise Fail("cron install: the profile's features must be a list of names or {name: true|false}", 2)


def cron_install(features: str | None, dry_run: bool) -> int:
    """Write the registry lines of the enabled features into ~/crontab, tagged, and install it (one write)."""
    from core import schedules

    try:
        reg = schedules.load()
    except schedules.RegistryError as e:
        raise Fail(f"cron install: registry: {e}", 3) from None
    known = {e.feature for e in reg.jobs.values()}
    on_cron = {e.feature for e in reg.jobs.values() if e.runs_on == "cron"}
    if features is not None:
        asked, src = {f.strip() for f in features.split(",") if f.strip()} - {"core"}, "--features"
    else:
        prof = _profile_features()
        asked, src = (prof, "profile") if prof else (set(), "default: core only")
    mapped, unknown = schedules.switch_features(asked, known)  # "all" = every switch, never an unmapped feature
    if unknown and features is not None:
        names = sorted(set(schedules.FEATURE_SWITCHES) | known)
        raise Fail(f"cron install: unknown feature(s) {', '.join(sorted(unknown))}; known: {', '.join(names)}", 2)
    if unknown:
        say(f"cron install: the profile's switch(es) {', '.join(sorted(unknown))} schedule nothing (ignored)")
    want = (mapped | set(schedules.CORE_FEATURES)) & on_cron
    tags, owner_only = schedules.install_tags(reg, want)
    ct = Path(os.path.realpath(paths.home() / "crontab"))
    paths.guard(ct)
    with contextlib.nullcontext() if dry_run else Lock("cron install"):
        inst = test_seam("ASSISTANT_CRONTAB_BIN")
        if inst is None and paths.test_mode():
            inst = "true"
        if ct.exists():
            old = ct.read_text(encoding="utf-8")
        else:  # no ~/crontab yet: keep what the user's installed crontab already holds
            r = run([inst or "crontab", "-l"], timeout=30)
            old = r.stdout if r.returncode == 0 else ""
            if old.strip():
                say(f"cron install: no ~/crontab; starting from the installed crontab ({len(old.splitlines())} lines)")
        have = schedules.parity(old, reg)
        tagged = set(have.by_tag)
        add = [(tag, reg.lines[tag][0].cron_line.rstrip()) for tag in tags if tag not in tagged]
        lines = old.splitlines()
        head = []
        if not any(ln.startswith("PATH=") for ln in lines):
            h = str(paths.home())
            head = [f"PATH={h}/bin:{h}/.local/bin:/usr/local/bin:/usr/bin:/bin"]  # cron does not expand $HOME here
        new_lines = head + lines + [f"{body}  # assistant:{tag}" for tag, body in add]
        bad = [ln for ln in new_lines if not cron_line_ok(ln)]
        if bad:
            raise Fail(f"cron install: {len(bad)} invalid line(s), e.g. {bad[0][:80]}; unchanged", 3)
        say(f"cron install: features {', '.join(sorted(want)) or '(none)'} ({src}); "
            f"{len(add)} line(s) to add, {len(tagged)} already tagged")  # fmt: skip
        if owner_only:
            say(f"cron install: owner-only, never scheduled here: {', '.join(owner_only)}")
        for ln in head + [f"{body}  # assistant:{tag}" for tag, body in add]:
            say(f"  + {ln}")
        if dry_run or not (add or head):
            say("cron install: dry run, nothing written" if dry_run else "cron install: nothing to add")
            return 0
        brain("before assistant cron install")
        existed = ct.exists()
        mode = stat.S_IMODE(ct.stat().st_mode) if existed else 0o644
        install_bytes(("\n".join(new_lines) + "\n").encode(), ct, mode)
        r = run([inst or "crontab", str(ct)])
        if r.returncode != 0:
            install_bytes(old.encode(), ct, mode) if existed else ct.unlink()
            raise Fail(f"crontab install failed (exit {r.returncode}); file restored", 1)
        log({"op": "cron", "action": "install", "lines": len(add), "features": sorted(want), "ok": True})
        brain("after assistant cron install")
        say(f"cron install: {len(add)} line(s) added and installed; check with `assistant cron check`")
    return 0


def _sh_same(old: str, new: str, done: list) -> list[str]:
    """dash -n (parse only, nothing runs) accepts each tagged line's command before and after the tag."""
    bad = []
    a, b = old.split("\n"), new.split("\n")
    for n, _t in done:
        for label, line in (("old", a[n - 1]), ("new", b[n - 1])):
            cmd = line.split(None, 5)[5] if len(line.split(None, 5)) == 6 else ""
            r = run(["/bin/sh", "-n", "-c", cmd.replace("\\%", "%")])
            if r.returncode != 0:
                bad.append(f"line {n} ({label}): sh -n exit {r.returncode}")
    return bad


def cron_tag(dry: bool) -> int:
    """Tag each untagged ~/crontab line whose exact text is a registry cron_line with `  # assistant:<line>`.
    Proof before writing: comment-only (core.schedules.comment_only), every line still valid, dash parses both
    versions; a backup of the old file goes to ~/.local/state/assistant/crontab-backups/. Idempotent."""
    from core import schedules

    ct = Path(os.path.realpath(paths.home() / "crontab"))
    paths.guard(ct)
    with Lock("cron tag"):
        old = ct.read_text(encoding="utf-8") if ct.exists() else ""
        try:
            reg = schedules.load()
        except schedules.RegistryError as e:
            raise Fail(f"cron tag: registry: {e}", 3) from None
        new, done = schedules.tag_text(old, reg)
        left = schedules.parity(new, reg)
        if not done:
            say(f"cron tag: nothing to tag ({left.tagged}/{left.active} lines tagged, {len(left.untracked)} untracked)")
            return 0
        bad = schedules.comment_only(old, new)
        bad += [f"invalid line: {ln[:60]}" for ln in new.splitlines() if not cron_line_ok(ln)]
        bad += _sh_same(old, new, done)
        if bad:
            raise Fail("cron tag: not a comment-only edit, unchanged: " + "; ".join(bad[:5]), 3)
        for n, t in done:
            say(f"  ~/crontab:{n} += # assistant:{t}")
        if dry:
            say(f"cron tag: would tag {len(done)} line(s); then {len(left.untracked)} untracked (dry run)")
            return 0
        brain("before assistant cron tag")
        bdir = st("crontab-backups")
        bdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = bdir / f"crontab-{time.strftime('%Y%m%d-%H%M%S')}"
        install_bytes(old.encode(), backup, 0o600)
        mode = stat.S_IMODE(ct.stat().st_mode) if ct.exists() else 0o644
        install_bytes(new.encode(), ct, mode)
        inst = test_seam("ASSISTANT_CRONTAB_BIN")
        if inst is None and paths.test_mode():
            inst = "true"
        r = run([inst or "crontab", str(ct)])
        if r.returncode != 0:
            install_bytes(old.encode(), ct, mode)
            raise Fail(f"crontab install failed (exit {r.returncode}); file restored", 1)
        log({"op": "cron", "action": "tag", "lines": len(done), "ok": True, "backup": str(backup)})
        brain("after assistant cron tag")
        say(f"cron tag: {len(done)} line(s) tagged (comments only); backup {backup}; {len(left.untracked)} untracked")
    return 0


def cron_check(as_json: bool) -> int:
    """The crontab against the registry: exit 0 clean, 3 untracked / missing / changed lines."""
    from core import schedules

    ct = Path(os.path.realpath(paths.home() / "crontab"))
    try:
        reg = schedules.load()
    except schedules.RegistryError as e:
        raise Fail(f"cron check: registry: {e}", 3) from None
    p = schedules.parity(ct.read_text(encoding="utf-8") if ct.exists() else "", reg)
    if as_json:
        say(json.dumps({"active": p.active, "tagged": p.tagged, "untracked": [n for n, _ in p.untracked],
                        "missing": p.missing, "changed": [[n, t, w] for n, t, w in p.changed],
                        "jobs": len(reg.jobs), "lines": len(reg.lines)}))  # fmt: skip
    else:
        say(f"cron check: {p.active} active lines · {p.tagged} tagged · {len(p.untracked)} untracked · "
            f"{len(p.missing)} missing · {len(p.changed)} changed · registry {len(reg.lines)} lines / {len(reg.jobs)} jobs")  # fmt: skip  # noqa: E501
        for n, raw in p.untracked:
            say(f"  untracked ~/crontab:{n}: {raw[:100]}")
        for t in p.missing:
            say(f"  missing {t}" + (" (owner-only)" if all(e.owner_only for e in reg.lines[t]) else ""))
        for n, t, w in p.changed:
            say(f"  changed ~/crontab:{n} ({t}): {w}")
    return 0 if p.clean else 3


def uninstall(dry_run: bool, yes: bool) -> int:
    """Remove what the deploy put under $HOME and the registry's crontab lines; nothing else (see the usage)."""
    from core import schedules

    if not dry_run and not yes:
        raise Fail("uninstall: run it with --dry-run first, show the user the list, then --yes with their OK", 2)
    home = paths.home()
    deployed = read_json(st("deployed.json"), {})
    remove: list[Path] = []
    kept: list[tuple[str, str]] = []  # (dest, why)
    for dest, want in sorted((deployed.get("files") or {}).items()):
        p = paths.guard(home / dest)
        if home not in p.parents:
            raise Fail(f"uninstall: {dest}: not under the home folder; nothing removed", 3)
        have = file_state(p)
        if have is None:
            continue
        if have.get("sha256") == want.get("sha256"):
            remove.append(p)
        else:
            kept.append((dest, "changed since the deploy"))
    core = deployed.get("core") or {}
    link = rel_root = None
    if core.get("dest") and core.get("target"):
        link = paths.guard(home / core["dest"])
        tgt = os.readlink(link) if link.is_symlink() else None
        root = core["target"].split("/", 1)[0]  # core.releases: the deploy's own folder beside the link
        if tgt != core["target"] or not root.endswith(".releases"):
            kept.append((core["dest"], "the core link moved"))
            link = None
        elif (link.parent / root).is_dir():
            rel_root = link.parent / root
    ct = Path(os.path.realpath(home / "crontab"))
    paths.guard(ct)
    inst = test_seam("ASSISTANT_CRONTAB_BIN")
    if inst is None and paths.test_mode():
        inst = "true"
    if ct.exists():
        old = ct.read_text(encoding="utf-8")
    else:
        r = run([inst or "crontab", "-l"], timeout=30)
        old = r.stdout if r.returncode == 0 else ""
    lines = old.splitlines()
    tagged = [ln for ln in lines if schedules.TAG_RE.search(ln)]
    say(f"uninstall: {len(remove)} deployed file(s) to remove" + (", the core link and its releases" if link else "")
        + f"; {len(tagged)} crontab line(s) tagged # assistant: to remove; {len(kept)} kept")  # fmt: skip
    for p in remove:
        say(f"  - ~/{p.relative_to(home)}")
    if link:
        say(f"  - ~/{link.relative_to(home)}" + (f", ~/{rel_root.relative_to(home)}/" if rel_root else ""))
    for ln in tagged:
        say(f"  - crontab: {ln[:110]}")
    for dest, why in kept:
        say(f"  kept ~/{dest} ({why})")
    say(
        "uninstall: stays: ~/opt/assistant, ~/workspace, ~/.claude (but the deployed hooks), ~/.config, ~/.secrets, "
        "~/.local/state, ~/.profile"
    )
    if dry_run:
        say("uninstall: dry run, nothing changed")
        return 0
    with Lock("uninstall"):
        brain("before assistant uninstall")
        if tagged:
            new = "\n".join(ln for ln in lines if not schedules.TAG_RE.search(ln))
            new = new + "\n" if new.strip() else ""
            existed = ct.exists()
            mode = stat.S_IMODE(ct.stat().st_mode) if existed else 0o644
            install_bytes(new.encode(), ct, mode)
            r = run([inst or "crontab", str(ct)])
            if r.returncode != 0:
                install_bytes(old.encode(), ct, mode) if existed else ct.unlink()
                raise Fail(f"uninstall: crontab install failed (exit {r.returncode}); restored, nothing removed")
        dirs: set[Path] = set()
        for p in remove:
            p.unlink()
            dirs.update(q for q in p.parents if home in q.parents)
        if link:
            link.unlink()
            dirs.update(q for q in link.parents if home in q.parents)
            if rel_root:
                shutil.rmtree(rel_root)
        for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):  # only folders the removal left empty
            pc = d / "__pycache__"  # the bytecode of the removed modules (Python wrote it there, no user file)
            if pc.is_dir() and not pc.is_symlink():
                for f in pc.glob("*.pyc"):
                    f.unlink()
                with contextlib.suppress(OSError):
                    pc.rmdir()
            with contextlib.suppress(OSError):
                d.rmdir()
        write_json(st("deployed.json"), {})
        log({"op": "uninstall", "files": len(remove), "core": bool(link), "cron_lines": len(tagged),
             "kept": len(kept), "ok": True})  # fmt: skip
    say(f"uninstall: removed {len(remove)} file(s)" + (" and the core link" if link else "")
        + f", {len(tagged)} crontab line(s); `sh ~/opt/assistant/ops/install.sh` installs it again")  # fmt: skip
    return 0


# ---------------------------------------------------------------- lint (AST ban + ratchet)
def _is_python(path: Path, data: bytes) -> bool:
    return path.suffix == ".py" or (data.startswith(b"#!") and b"python" in data.split(b"\n", 1)[0])


def scan_source(path: Path, data: bytes) -> set[str]:
    """Ratchet categories a file hits: load_json, save_atomic, note_helpers, urlopen, flock."""
    hits: set[str] = set()
    text = data.decode("utf-8", errors="replace")
    if _is_python(path, data):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = n.name.lstrip("_")
                    if name == "load_json":
                        hits.add("load_json")
                    elif name in ("save_json", "atomic_write"):
                        hits.add("save_atomic")
                    elif name in ("write_note", "write_inbox"):
                        hits.add("note_helpers")
                elif (
                    isinstance(n, ast.Attribute)
                    and n.attr == "urlopen"
                    or isinstance(n, ast.Name)
                    and n.id == "urlopen"
                ):
                    hits.add("urlopen")
                elif isinstance(n, ast.ImportFrom) and any(a.name == "urlopen" for a in n.names):
                    hits.add("urlopen")
                elif isinstance(n, ast.ImportFrom) and n.module == "fcntl" and any(a.name == "flock" for a in n.names):
                    hits.add("flock")
                elif (
                    isinstance(n, ast.Attribute)
                    and n.attr == "flock"
                    and isinstance(n.value, ast.Name)
                    and n.value.id == "fcntl"
                ):
                    hits.add("flock")
            return hits
    if re.search(r"\bdef _?load_json\b", text):
        hits.add("load_json")
    if re.search(r"\bdef _?(save_json|atomic_write)\b", text):
        hits.add("save_atomic")
    if re.search(r"\bdef _?(write_note|write_inbox)\b", text):
        hits.add("note_helpers")
    if "urlopen" in text:
        hits.add("urlopen")
    if re.search(r"\bflock\b", text):
        hits.add("flock")
    return hits


def bin_files(bdir: Path):
    for dp, dns, fns in os.walk(bdir):
        dns[:] = sorted(d for d in dns if d != "__pycache__" and not d.endswith(".releases") and not d.startswith("."))
        for fn in sorted(fns):
            p = Path(dp) / fn
            if fn.startswith(".") or p.is_symlink() and p.is_dir():
                continue
            yield p


ALLOW_CAT = {
    "load_json": "load_json",
    "save_json": "save_atomic",
    "atomic_write": "save_atomic",
    "write_note": "note_helpers",
    "write_inbox": "note_helpers",
    "urlopen": "urlopen",
    "flock": "flock",
}


def ratchet_exempt(root: Path) -> dict[str, set[str]]:
    """Reviewed exceptions (ops/lint/ast-allow.json) mapped onto the managed bin/ files: {bin relpath: categories}."""
    allow = read_json(root / "ops" / "lint" / "ast-allow.json", {}).get("allow", {})
    try:
        man = load_manifest(root)
    except Fail:
        return {}
    return {
        e["dest"][4:]: {ALLOW_CAT[n] for n in allow[e["src"]] if n in ALLOW_CAT}
        for e in man["file"]
        if e["dest"].startswith("bin/") and e["src"] in allow
    }


def ratchet_counts(
    bdir: Path, overrides: dict[str, bytes] | None = None, exempt: dict[str, set[str]] | None = None
) -> dict:
    """Files per category under bdir (skipping core.releases/ and dot work copies); overrides = {relpath: bytes}
    replaces or adds files (the would-be ~/bin after a land); exempt = reviewed exceptions per relpath."""
    overrides = dict(overrides or {})
    exempt = exempt or {}
    files = {cat: [] for cat in RATCHET}
    seen = set()
    for p in bin_files(bdir):
        rel = str(p.relative_to(bdir))
        seen.add(rel)
        try:
            data = overrides.pop(rel) if rel in overrides else p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        for c in scan_source(p, data) - exempt.get(rel, set()):
            files[c].append(rel)
    for rel, data in overrides.items():
        for c in scan_source(Path(rel), data) - exempt.get(rel, set()):
            files[c].append(rel)
    return {"counts": {c: len(v) for c, v in files.items()}, "files": files}


def ast_ban(root: Path, rels: list[str], allow: dict) -> list[str]:
    out = []
    for rel in rels:
        if rel.startswith("core/") or rel.startswith("adapters/"):
            continue
        p = root / rel
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if not _is_python(p, data):
            continue
        try:
            tree = ast.parse(data.decode("utf-8", errors="replace"))
        except SyntaxError as e:
            out.append(f"{rel}:{e.lineno}: syntax error")
            continue
        ok = set(allow.get(rel, []))
        for n in ast.walk(tree):
            what = None
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.lstrip("_") in BANNED_DEFS:
                what = n.name.lstrip("_")
            elif isinstance(n, ast.Attribute) and n.attr == "urlopen" or isinstance(n, ast.Name) and n.id == "urlopen":
                what = "urlopen"
            elif isinstance(n, ast.ImportFrom) and any(a.name in ("urlopen", "flock") for a in n.names):
                what = "urlopen" if any(a.name == "urlopen" for a in n.names) else "flock"
            elif (
                isinstance(n, ast.Attribute)
                and n.attr == "flock"
                and isinstance(n.value, ast.Name)
                and n.value.id == "fcntl"
            ):
                what = "flock"
            if what and what not in ok:
                out.append(f"{rel}:{getattr(n, 'lineno', 0)}: {what} outside core/ (use core helpers)")
    return out


def lint_bin(mode: str, as_json: bool, bdir: Path | None) -> int:
    bdir = bdir or paths.bin_dir()
    res = ratchet_counts(bdir, exempt=ratchet_exempt(paths.repo()))
    base_p = paths.repo("ops", "lint", "bin-baseline.json")
    base = read_json(base_p, {})
    limits = base.get("counts", {})
    over = {c: (n, limits.get(c)) for c, n in res["counts"].items() if c in limits and n > limits[c]}
    if as_json:
        say(json.dumps({"counts": res["counts"], "baseline": limits, "over": over}, sort_keys=True))
    else:
        for c in RATCHET:
            n, lim = res["counts"][c], limits.get(c)
            say(f"{c:13} {n:4}" + (f"  (baseline {lim})" if lim is not None else "") + ("  OVER" if c in over else ""))
    if mode == "update":
        if over and not os.environ.get("ASSISTANT_RATCHET_RAISE"):
            say("refusing to raise the baseline (ASSISTANT_RATCHET_RAISE=1 to accept a rise)")
            return 3
        base["counts"] = res["counts"]
        base["updated"] = time.strftime("%Y-%m-%d", time.localtime(clock.now()))
        write_json(base_p, base)
        os.chmod(base_p, 0o644)
        say(f"baseline written: {base_p}")
        return 0
    if mode == "check":
        return 3 if over else 0
    return 0


# ---------------------------------------------------------------- CI
def _uv_env(base: dict) -> dict:
    env = dict(base)
    rh = paths.real_home()
    env.setdefault("UV_PYTHON_INSTALL_DIR", str(rh / ".local/share/uv/python"))
    env.setdefault("UV_CACHE_DIR", str(rh / ".cache/uv"))
    env["UV_NO_PROGRESS"] = "1"
    env.pop("VIRTUAL_ENV", None)
    return env


def _uv() -> str:
    for c in (shutil.which("uv"), str(paths.real_home() / ".local/bin/uv")):
        if c and os.access(c, os.X_OK):
            return c
    raise Fail("uv missing: run ops/ci/install-uv.sh")


def _netguard() -> str:
    """The LD_PRELOAD network guard; "" in the hub's CI runner (ASSISTANT_NETGUARD=none: it has no network)."""
    if os.environ.get("ASSISTANT_NETGUARD") == "none":
        return ""
    g = os.environ.get("ASSISTANT_NETGUARD") or str(paths.real_home() / "opt/test-all/netguard.so")
    if not os.path.isfile(g):
        raise Fail(f"netguard missing: {g}")
    return g


def _temp_home(root: Path) -> Path:
    """A test-all style HOME: links to bin, opt, plugins, caches and tools; empty inbox/config/state."""
    h = root / "home"
    rh = paths.real_home()
    for link in ("bin", "opt", "plugins", ".bun", ".cache", ".local/bin", "crontab", "boot.sh"):
        src = rh / link
        if src.exists():
            (h / link).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(src, h / link)
    for d in ("inbox", ".config", ".local/state"):
        (h / d).mkdir(parents=True, exist_ok=True)
    (h / ".gitconfig").write_text("[user]\n\tname = assistant-ci\n\temail = assistant-ci@localhost\n")
    return h


def _guarded_env(tmp: Path, home: Path, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    for k in ("ASSISTANT_REPO", "ASSISTANT_NOW", "VIRTUAL_ENV"):
        env.pop(k, None)
    (tmp / "tmp").mkdir(exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "ASSISTANT_TEST": "1",
            "USAGE_TEST": "1",
            "TMPDIR": str(tmp / "tmp"),
            "NETGUARD_LOG": str(tmp / "guard.log"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    g = _netguard()
    if g:
        env["LD_PRELOAD"] = g
    env.update(extra or {})
    return env


def _guard_hits(tmp: Path) -> list[str]:
    try:
        return [ln for ln in (tmp / "guard.log").read_text(errors="replace").splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------- CI run dirs: stop what a run started, sweep leftovers
class CiStopped(BaseException):
    """SIGTERM / SIGINT / SIGHUP to ci: unwinds like KeyboardInterrupt, so the run is reaped and its dir removed."""


def _on_stop(signum, _frame) -> None:
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):  # one-shot: a second signal must not cut the cleanup short
        signal.signal(s, signal.SIG_IGN)
    raise CiStopped(signum)


def _trap_stop() -> dict | None:
    """Route SIGTERM / SIGINT / SIGHUP to CiStopped; the previous handlers (None off the main thread)."""
    try:
        return {s: signal.signal(s, _on_stop) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    except ValueError:
        return None


def _restore_signals(prev: dict | None) -> None:
    for s, h in (prev or {}).items():
        signal.signal(s, h)


def _proc_stat(pid: int) -> tuple[str, int, str] | None:
    """(state, ppid, start time in clock ticks) from /proc/PID/stat; pid + start names one process across pid reuse."""
    try:
        s = Path(f"/proc/{pid}/stat").read_text()
        f = s[s.rindex(")") + 2 :].split()
        return f[0], int(f[1]), f[19]
    except (OSError, ValueError, IndexError):
        return None


def _alive(p: dict) -> bool:
    ps = _proc_stat(p["pid"])
    return ps is not None and ps[2] == p["start"] and ps[0] not in ("Z", "X")


def _procs() -> list[dict]:
    """This user's live processes except ci itself: pid, ppid, start, argv and what they hold (cwd, open files,
    environment values). A process that ends mid-read is skipped."""
    me, uid, out = os.getpid(), os.getuid(), []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        base = f"/proc/{name}"
        try:
            if os.stat(base).st_uid != uid:
                continue
            argv = [a.decode(errors="replace") for a in Path(base, "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        ps = _proc_stat(int(name))
        if ps is None or ps[0] in ("Z", "X"):
            continue
        held: list[str] = []
        try:
            held.append(os.readlink(base + "/cwd"))
        except OSError:
            pass
        try:
            fds = os.listdir(base + "/fd")
        except OSError:
            fds = []
        for fd in fds:
            try:
                held.append(os.readlink(f"{base}/fd/{fd}"))
            except OSError:
                pass
        try:
            env = Path(base, "environ").read_bytes().split(b"\0")
            held += [e.split(b"=", 1)[1].decode(errors="replace") for e in env if b"=" in e]
        except OSError:
            pass
        out.append({"pid": int(name), "ppid": ps[1], "start": ps[2], "argv": argv, "held": held})
    return out


def _names(p: dict, dirs: set[str], argv: bool) -> bool:
    """p holds a path inside one of dirs (cwd, an open file, an environment value; with argv=True also a mention in
    its command line): a bare path, `-D/d/pgdata`, `--basetemp=/d/x`, `/d (deleted)`."""
    pg = _pg_data(p["argv"])
    for h in p["held"] + (p["argv"] if argv else [pg] if pg else []):
        for d in dirs:
            i = h.find(d)
            while i >= 0:
                j = i + len(d)
                if j == len(h) or not (h[j].isalnum() or h[j] in "._-"):
                    return True
                i = h.find(d, i + 1)
    return False


def _pg_data(argv: list[str]) -> str | None:
    """The data dir of a Postgres postmaster (`postgres -D DIR …`), else None (backends rewrite their argv)."""
    if not argv or os.path.basename(argv[0]) != "postgres":
        return None
    for i, a in enumerate(argv):
        if a == "-D" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("-D") and len(a) > 2:
            return a[2:]
    return None


def _stop(first: list[dict], also: list[dict], grace: float) -> None:
    """Postmasters in `first` get SIGQUIT (immediate shutdown: its backends go with it), the others SIGTERM; after
    grace s, SIGKILL to whatever of first + also still runs. Only the very process (pid + start time) is signalled."""

    def sig(p: dict, s: int) -> None:
        if _alive(p):
            try:
                os.kill(p["pid"], s)
            except OSError:
                pass

    for p in first:
        sig(p, signal.SIGQUIT if _pg_data(p["argv"]) else signal.SIGTERM)
    everyone = first + also
    for wait, s in ((grace, signal.SIGKILL), (5.0, None)):
        end = time.monotonic() + wait
        while time.monotonic() < end and any(_alive(p) for p in everyone):
            time.sleep(0.05)
        for p in everyone if s else ():
            sig(p, s)


def _brief(argv: list[str]) -> str:
    return " ".join(argv)[:80]


def _ci_reap(run_dir: Path, grace: float = 10.0) -> list[str]:
    """Stop every process still in this run's own dir (its cwd, an open file or an environment value there, or a
    postmaster's -D): a pytest ended by its timeout or a signal leaves test Postgres (pg_ctl detaches it), hubd or
    helpers behind. A mere mention in a command line (a shell running `ls DIR`) and ci's own ancestors are never
    touched. One line each."""
    dirs = {str(run_dir), os.path.realpath(run_dir)}
    mine, pid = set(), os.getpid()
    while pid > 1 and pid not in mine:
        mine.add(pid)
        ps = _proc_stat(pid)
        pid = ps[1] if ps else 0
    hit = [p for p in _procs() if p["pid"] not in mine and _names(p, dirs, argv=False)]
    pg = {p["pid"] for p in hit if _pg_data(p["argv"])}
    first = [p for p in hit if p["ppid"] not in pg]
    if hit:
        _stop(first, [p for p in hit if p["ppid"] in pg], grace)
    return [f"stopped pid {p['pid']} ({_brief(p['argv'])})" for p in first]


def _owner_alive(d: Path) -> bool:
    o = read_json(d / CI_OWNER, {})
    try:
        return _alive({"pid": int(o["pid"]), "start": str(o["start"])})
    except (KeyError, TypeError, ValueError):
        return False


def _ci_root(toolchain: bool) -> str | None:
    """Where a local CI run makes its run dir: ASSISTANT_CI_TMPDIR, else ~/.cache/aci under the real home (0700);
    None (the system temp dir) on the hub runner or when that can't be made."""
    v = os.environ.get("ASSISTANT_CI_TMPDIR", "").strip()
    if v:
        return v
    if toolchain or paths.test_mode():
        return None
    try:
        d = paths.real_home().joinpath(*CI_ROOT)
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return str(d)
    except OSError:
        return None


def _ci_sweep(root: Path | None = None, age: float = CI_SWEEP_AGE, grace: float = 10.0) -> list[str]:
    """Leftovers of CI runs that were killed outright (SIGKILL, OOM, container stop): assistant-ci.* dirs in the
    temp dir, untouched (the dir and its entries) for `age` s, whose owner ci is gone and that no live process holds
    except orphan test Postgres (ppid 1, -D inside the dir) and its backends: those are stopped, then the dir goes.
    Any other process holding a dir keeps it (another builder's run). Never any other Postgres. One line each."""
    roots = [Path(root)] if root else list(dict.fromkeys(p for p in (_ci_root(False), tempfile.gettempdir()) if p))
    now, uid, stale = time.time(), os.getuid(), []
    cands = []
    for r in roots:
        try:
            cands += sorted(Path(r).glob(CI_TMP_PREFIX + "*"))
        except OSError:
            pass
    for d in cands:
        try:
            s = d.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != uid:
                continue
            newest = max([s.st_mtime] + [c.lstat().st_mtime for c in d.iterdir()])
        except OSError:
            continue
        if now - newest >= age and not _owner_alive(d):
            stale.append(d)
    if not stale:
        return []
    procs, out = _procs(), []
    for d in stale:
        real = os.path.realpath(d)
        users = [p for p in procs if _names(p, {str(d), real}, argv=True)]  # any mention keeps the dir
        orphans = []
        for p in users:
            pd = _pg_data(p["argv"])
            if p["ppid"] == 1 and pd and os.path.isabs(pd) and os.path.realpath(pd).startswith(real + "/"):
                orphans.append(p)
        opids = {p["pid"] for p in orphans}
        rest = [p for p in users if p["pid"] not in opids and p["ppid"] not in opids]
        if rest:
            out.append(f"kept {d}: in use by pid {', '.join(str(p['pid']) for p in rest[:5])}")
            continue
        if orphans:
            _stop(orphans, [p for p in users if p["ppid"] in opids], grace)
            if any(_alive(p) for p in orphans):
                out.append(f"kept {d}: orphan Postgres did not stop")
                continue
        shutil.rmtree(d, ignore_errors=True)
        pgs = ", ".join(str(p) for p in sorted(opids))
        out.append(f"removed {d}" + (f" (stopped orphan test Postgres pid {pgs})" if pgs else ""))
    return out


def run_group(cmd, cwd=None, env=None, timeout=None, grace: float = PYTEST_GRACE) -> subprocess.CompletedProcess:
    """run() for a step whose children outlive it unless stopped (pytest: test Postgres, hubd): its own process
    group; on a timeout or an exception (CiStopped) SIGINT to the group so pytest tears its fixtures down, SIGKILL
    to the group after grace s, then the exception goes on."""
    p = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )  # fmt: skip
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        for s, wait in ((signal.SIGINT, grace), (signal.SIGKILL, 10.0)):
            try:
                os.killpg(p.pid, s)
            except OSError:
                pass
            try:
                p.communicate(timeout=wait)
            except Exception:  # noqa: BLE001 (TimeoutExpired, or a pipe the first communicate left half-read)
                pass
        if p.poll() is None:
            p.kill()
            p.wait()
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


class BgGroup:
    """run_group() in two halves, for steps that run at once (both pytest suites beside the other steps): the
    command starts now in its own process group and a reader thread holds its output; past its timeout the reader
    sends SIGINT to the group (pytest tears its fixtures down), SIGKILL after grace s; wait() collects it (raises
    TimeoutExpired after a timeout); stop() does the same SIGINT -> SIGKILL for an exception in the caller (CiStopped,
    another step's error). secs: its own wall time. The caller waits on an Event, never Thread.join: a signal that
    interrupts join() (CiStopped) leaves 3.11's Thread marked stopped while its reader still runs."""

    def __init__(self, cmd, cwd=None, env=None, timeout=None, grace: float = PYTEST_GRACE):
        self.cmd, self.timeout, self.grace = cmd, timeout, grace
        self.out = self.err = ""
        self.timed_out = False
        self.t0 = time.monotonic()
        self.secs = 0.0
        self.done = threading.Event()
        self.p = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )  # fmt: skip
        self.th = threading.Thread(target=self._read, daemon=True)
        self.th.start()

    def _kill(self, s: int) -> None:
        try:
            os.killpg(self.p.pid, s)
        except OSError:
            pass

    def _read(self) -> None:
        try:
            try:
                self.out, self.err = self.p.communicate(timeout=self.timeout)
                return
            except subprocess.TimeoutExpired:
                self.timed_out = True
            for s, wait in ((signal.SIGINT, self.grace), (signal.SIGKILL, 10.0)):
                self._kill(s)
                try:
                    self.out, self.err = self.p.communicate(timeout=wait)
                    return
                except Exception:  # noqa: BLE001 (TimeoutExpired, or a pipe the first communicate left half-read)
                    pass
            if self.p.poll() is None:
                self.p.kill()
                self.p.wait()
        except Exception:  # noqa: BLE001 (a reader that dies must still let wait() return)
            pass
        finally:
            self.secs = time.monotonic() - self.t0
            self.done.set()

    def wait(self) -> subprocess.CompletedProcess:
        while not self.done.wait(1.0):
            pass
        if self.timed_out:
            raise subprocess.TimeoutExpired(self.cmd, self.timeout or 0, self.out, self.err)
        return subprocess.CompletedProcess(self.cmd, self.p.returncode, self.out, self.err)

    def stop(self) -> None:
        """SIGINT to the group, SIGKILL after grace s; returns once the reader is done (or 10 s after SIGKILL)."""
        if self.done.is_set():
            return
        for s, wait in ((signal.SIGINT, self.grace), (signal.SIGKILL, 10.0)):
            self._kill(s)
            if self.done.wait(wait):
                return
        if self.p.poll() is None:
            self.p.kill()


def _xdist_args(py: Path, env: dict, toolchain: bool, nfiles: int = 10**6, suites: int = 1) -> list[str]:
    """pytest-xdist flags for this interpreter (pytest_workers: host load, cgroup quota, file count); none when
    serial or xdist is not installed there (an older lock or toolchain)."""
    n = pytest_workers(nfiles, suites, toolchain)
    if n <= 1:
        return []
    if run([str(py), "-c", "import xdist"], env=env, timeout=60).returncode != 0:
        return []
    return ["-n", str(n), "--dist", PYTEST_DIST]


def run_child(cmd, cwd=None, timeout: float | None = None, grace: float = 120.0, env=None) -> int:
    """subprocess.run(cmd).returncode without its SIGKILL: on a timeout or an exception (^C, a signal) the child
    gets SIGTERM and grace s to clean up (ci reaps its test processes and removes its run dir), then SIGKILL."""
    p = subprocess.Popen(cmd, cwd=cwd, env=env)
    try:
        return p.wait(timeout=timeout)
    except BaseException:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(grace)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        raise


# ---------------------------------------------------------------- test impact selection, retry, quarantine (28.10)
# A land runs the pytest files its changed paths can reach (imports + path references, conservative) plus a smoke
# set; shared core code, CI config, gated paths and the protected set run everything. The rules live here (main's copy
# drives every land, and the hub runner runs the release's copy): a branch can't narrow its own selection.
# Narrowed 30.10 (79 % of 80 lands ran everything, mostly for files no test shares): a manifest change counts the srcs
# of the entries it changes as changed (its [core], or no readable base: everything); a runner-skip.txt change the
# test files its changed lines name (no diff: everything); a lint data file (ast-allow.json, bin-baseline.json) the
# tests that name it, as any data file (the lint steps that read it run on every land anyway); a conftest below tests/
# the tests under its dir (the root one: everything).
# Narrowed again 30.10 (58 % of the lands since the runner selection ran everything; the import graph itself is dense,
# so most of that stays): core modules and this driver go by the import graph like any module (core's own imports are
# followed too; a data file a module names by its bare file name, core/schedules.toml in core/schedules.py, is part of
# that module); an import resolves only to .py files and never a one-word stdlib name to a repo file (`import secrets`
# is not packages/hubkit/secrets.py); "assistant" alone is the project's directory word (~/.config/assistant), not
# this driver (its path, file name or bin/ name still are); a pyproject.toml change only in the lint tools' tables
# ([tool.ruff], [tool.basedpyright], [tool.pyright]) runs no extra test (those steps run on every land). The driver's
# own change never drives its own CI (main's copy here, the release's on the runner): a full run adds nothing for it.
# The nightly full suite and `assistant legacy-main` still run everything; gated paths keep their full run.
SELECT_FULL = (
    "core/__init__.py",  # `import core.x` runs it: no import names it (core is a top-level word)
    "ops/ci/*",
    "ops/deploy/*",  # but the manifest: SELECT_EXPAND
    "ops/lint/*.py",  # lint code (its data files: the tests naming them)
    "pyproject.toml",
    "uv.lock",
    "*/pyproject.toml",
    "*requirements*.lock",
    "tests/conftest.py",  # the root conftest; a deeper one is part of every test below its dir (select_tests)
    "tests/quarantine.txt",
    "schemas/*",  # shared contracts (typed tools, events, pg migrations)
    "packages/hubkit/release.py",  # what the runner and the hub run
    "packages/hubkit/citool.py",
    "packages/hub/ciworker.py",
    "packages/hub/hubci.py",
)
SELECT_PROTECTED = (  # hubverify.PROTECTED in repo terms (release app/* <- packages/hub/*, packages/tools/*.cedar)
    "packages/hub/hubverify.py",
    "packages/hub/hubsecrets.py",
    "packages/hub/hubapprovals.py",
    "packages/tools/*.cedar",
    "packages/hub/hubpolicy*",
    "packages/hub/policy/*",
    "packages/hub/hubredact*",
    "packages/hub/redact/*",
)
SELECT_SMOKE = (  # always run (fast, broad): core paths + notes, the CI driver's own rules, the hub client
    "tests/core/test_paths.py",
    "tests/core/test_notes.py",
    "tests/ops/test_ci_select.py",
    "tests/hub/test_h17_hubkit_feed.py",
)
SELECT_EXPAND = ("ops/deploy/manifest.toml", "tests/runner-skip.txt")  # what they change counts (_sel_expand)
SELECT_LINT_TABLES = ("ruff", "basedpyright", "pyright")  # pyproject [tool.*] tables only lint steps read
SELECT_FULL_SHARE = 0.6  # an impacted set above this share of the test files runs as the full suite
_SEL_TOP = {"core", "ops", "packages", "scripts", "tests", "docs", "schemas", "adapters", "hooks"}
# generic one-word names; "assistant" is the state/config/opt directory word too (SELECT_FULL's note)
_SEL_GENERIC = {"run", "main", "test", "tests", "index", "lib", "util", "utils", "config", "data", "base", "common",
                "types", "init", "cli", "app", "src", "bin", "tmp", "home", "state", "README", "readme", "x", "py",
                "json", "md", "sh", "ts", "toml", "txt", "a", "b", "c", "d", "f", "t", "assistant"}  # fmt: skip
_SEL_TOKEN = re.compile(r"[\w./*+@-]+")
_SEL_FILE_NAME = re.compile(r"[\w-][\w.-]*\.[A-Za-z0-9]{1,8}")  # "schedules.toml": a file name, no path
RERUN_MAX = 25  # more failures than this: a real break, no retry
QUARANTINE_FILE = ("tests", "quarantine.txt")
QUARANTINE_MAX_DAYS = 30


def _sel_keys(rel: str) -> set[str]:
    """The names a reference to repo file `rel` may use: path suffixes, the stem, dotted module names, and its
    ancestor dirs below the top level ("packages/hub", "hub"); generic one-word names are dropped (noise)."""
    parts = rel.split("/")
    keys = {"/".join(parts[i:]) for i in range(len(parts))}
    base = parts[-1]
    stem = base.rsplit(".", 1)[0] if "." in base[1:] else base
    keys.add(stem)
    if base.endswith(".py"):
        mod = parts[:-1] + ([] if base == "__init__.py" else [stem])
        keys |= {".".join(mod[i:]) for i in range(len(mod))}
    for j in range(2, len(parts)):  # dirs of two or more parts ("packages/hub"): one word ("hub") is too common
        d = parts[:j]
        keys |= {"/".join(d[i:]) for i in range(len(d) - 1)}
    return {k for k in keys if k and k not in _SEL_TOP and k not in _SEL_GENERIC and k != "__init__.py"}


def _sel_refs(path: Path, rel: str) -> tuple[set[str], set[str], set[str]]:
    """(imported dotted names, path-like tokens, bare words) of one file. Python: ast imports; a string constant
    that is one word is a token as it is ("remind" in ROOT / "scripts" / "remind"), prose and commands give only
    their path-like words (a "/" or "."); docstrings nothing. Other text (shell): path-like words, and bare words
    (the caller keeps those naming a script). Files outside tests/ contribute imports only."""
    try:
        st_ = path.stat()
    except OSError:
        return set(), set(), set()
    key = (str(path), rel, st_.st_mtime_ns, st_.st_size, st_.st_ino)
    if key not in _SEL_REFS:
        _SEL_REFS[key] = _sel_refs_read(path, rel)
    a, b, c = _SEL_REFS[key]
    return set(a), set(b), set(c)


_SEL_REFS: dict[tuple, tuple[set[str], set[str], set[str]]] = {}  # per file version: select_tests over one tree again


def _sel_refs_read(path: Path, rel: str) -> tuple[set[str], set[str], set[str]]:
    try:
        data = path.read_bytes()
    except OSError:
        return set(), set(), set()
    if len(data) > 2_000_000 or b"\0" in data[:4096]:
        return set(), set(), set()
    text = data.decode("utf-8", errors="replace")
    py = rel.endswith(".py") or text.startswith("#!") and "python" in text.split("\n", 1)[0]
    imps: set[str] = set()
    strs: list[str] = []
    words: set[str] = set()
    if py:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            pkg = rel.split("/")[:-1]
            docs = set()
            for n in ast.walk(tree):
                body = getattr(n, "body", None)
                if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                    docs.add(id(body[0].value))
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        p = a.name.split(".")
                        imps |= {".".join(p[: i + 1]) for i in range(len(p))}
                elif isinstance(n, ast.ImportFrom):
                    p = (n.module or "").split(".") if n.module else []
                    if n.level:
                        p = pkg[: len(pkg) - (n.level - 1)] + p
                    imps |= {".".join(p[: i + 1]) for i in range(len(p))}
                    imps |= {".".join([*p, a.name]) for a in n.names if a.name != "*"}
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                    v = n.value.strip()
                    if v and len(v) < 300 and not any(c.isspace() for c in v):
                        words.add(v.strip("/") if not v.startswith("*") else v)
                    elif len(v) < 4000:
                        strs.append(v)
        else:
            strs.append(text)
    else:
        strs.append(text)
    toks: set[str] = set()
    bare: set[str] = set()
    if not rel.startswith("tests/"):  # outside tests: imports, and the bare file names it names (select_tests)
        return {i for i in imps if i}, {w for w in words if _SEL_FILE_NAME.fullmatch(w)}, bare
    for s in strs:
        for t in _SEL_TOKEN.findall(s):
            t = t.strip("./") if not t.startswith("*") else t
            if not t or len(t) >= 300:
                continue
            if "/" in t or "." in t or "*" in t:
                toks.add(t)
            elif not py:
                bare.add(t)
    if py:
        toks |= {w for w in words if w}
        # ROOT / "packages" / "hub": a top-level word and another one-word literal name that dir
        toks |= {f"{a}/{b}" for a in words & _SEL_TOP for b in words if b not in _SEL_TOP and "/" not in b}
    return {i for i in imps if i}, toks, bare


def _sel_lookup(tok: str, index: dict[str, set[str]], files: list[str]) -> set[str]:
    """Repo files a token can name: any of its path suffixes is one of their keys; a glob token matches by fnmatch."""
    if "*" in tok:
        if tok in ("*", "**", "**/*"):
            return set()
        return {f for f in files if fnmatch.fnmatchcase(f, tok) or fnmatch.fnmatchcase(f.rsplit("/", 1)[-1], tok)}
    parts = [p for p in tok.split("/") if p]
    out: set[str] = set()
    for i in range(len(parts)):
        out |= index.get("/".join(parts[i:]), set())
    return out


def _test_files(tree: Path, tracked: list[str]) -> list[str]:
    """The pytest files `pytest` collects here (tests/**/test_*.py, not the legacy suites' run.sh helpers)."""
    return sorted(
        t
        for t in tracked
        if t.startswith("tests/")
        and not t.startswith("tests/legacy/")
        and t.rsplit("/", 1)[-1].startswith("test_")
        and t.endswith(".py")
    )


def select_tests(tree: Path, changed: list[str] | None, tracked: list[str], base: str | None = None) -> dict:
    """Which pytest files a run with these changed paths needs.
    -> {"mode": "full"|"impacted", "why", "files": [...] (impacted only), "of": n test files, "smoke": n}"""
    tests = _test_files(tree, tracked)
    out: dict = {"mode": "full", "of": len(tests)}
    forced = os.environ.get("ASSISTANT_CI_SELECT", "").strip()
    if forced == "full":
        return {**out, "why": "ASSISTANT_CI_SELECT=full"}
    if changed is None:
        return {**out, "why": "no base: everything"}
    if "pyproject.toml" in changed and _sel_lint_only(tree, base):
        changed = [c for c in changed if c != "pyproject.toml"]  # only the lint tools' tables: their steps cover it
    full = [
        c
        for c in changed
        if c not in SELECT_EXPAND and any(fnmatch.fnmatchcase(c, g) for g in SELECT_FULL + SELECT_PROTECTED)
    ]
    if full:
        more = f" (+{len(full) - 1})" if len(full) > 1 else ""
        return {**out, "why": f"shared/CI/protected path: {full[0]}{more}"}
    gated = gated_categories(changed, _gate_root(tree, base))
    if gated:
        return {**out, "why": "gated: " + ",".join(gated)}
    pyfiles = [t for t in tracked if (tree / t).is_file()]
    extra = _sel_expand(tree, changed, base, tests)
    if isinstance(extra, str):
        return {**out, "why": extra}
    index: dict[str, set[str]] = {}
    for f in sorted(set(pyfiles) | set(changed)):
        for k in _sel_keys(f):
            index.setdefault(k, set()).add(f)
    with contextlib.suppress(Fail, OSError, KeyError, TypeError):  # deployed names: "bin/assistant" is this driver
        for e in load_manifest(tree)["file"]:
            if e["dest"].startswith("bin/") and e["src"] in index.get(e["src"], ()):
                index.setdefault(e["dest"], set()).add(e["src"])
    sources = [
        t
        for t in pyfiles
        if not t.startswith("tests/legacy/")
        and (
            t.endswith(".py")
            or t.startswith(("scripts/", "hooks/"))
            or (t.startswith("tests/") and t.endswith((".sh", ".ts", ".js")))
        )
    ]
    deps: dict[str, set[str]] = {}
    cmds = {f.rsplit("/", 1)[-1] for f in pyfiles if f.startswith("scripts/") and f.count("/") == 1}
    for f in sources:
        imps, toks, bare = _sel_refs(tree / f, f)
        d = imps_files(imps, index)  # imports: followed transitively
        if not f.startswith("tests/"):  # a data file beside it that it names (Path(__file__).with_name("x.toml"))
            d |= {f"{f.rsplit('/', 1)[0]}/{t}" for t in toks if "/" in f} & set(pyfiles)
        else:
            # what a test or its helper names (a script it runs, a file it reads, a module it loads by path): one
            # hop, then only imports (mentions of mentions tie every file to every other through CLI words)
            for t in toks | {f"scripts/{w}" for w in bare & cmds}:
                d |= {x for x in _sel_lookup(t, index, pyfiles) if not x.rsplit("/", 1)[-1].startswith("test_")}
        d.discard(f)
        deps[f] = d
    conftests = sorted(t for t in tracked if t.endswith("conftest.py") and t.startswith("tests/"))
    named = set(changed) & set(SELECT_EXPAND)  # read by the root conftest: only the tests that name it themselves
    chg = (set(changed) - named) | extra
    picked: list[str] = []
    for t in tests:
        seen, todo = {t}, [t]
        for c in conftests:  # a directory's conftest (fixtures) is part of every test below it
            if t.startswith(c.rsplit("/", 1)[0] + "/"):
                seen.add(c)
                todo.append(c)
        while todo:
            for x in deps.get(todo.pop(), ()):
                if x not in seen:  # core too (its modules import only core and the stdlib)
                    seen.add(x)
                    todo.append(x)
        if seen & chg or deps.get(t, set()) & named:
            picked.append(t)
    if len(picked) > SELECT_FULL_SHARE * max(1, len(tests)):
        return {**out, "why": f"{len(picked)}/{len(tests)} test files impacted"}
    smoke = [s for s in SELECT_SMOKE if s in tests and s not in picked]
    return {
        "mode": "impacted",
        "why": f"{len(picked)} impacted + {len(smoke)} smoke",
        "files": sorted(picked) + smoke,
        "of": len(tests),
        "smoke": len(smoke),
    }


def _sel_lint_only(tree: Path, base: str | None) -> bool:
    """pyproject.toml differs from base's only in the lint tools' [tool.*] tables (or comments, formatting): no
    pytest file depends on those (ruff and pyright run on every land). No base or an unreadable side: False."""
    if not base:
        return False
    r = run(["git", "-C", str(tree), "show", f"{base}:pyproject.toml"])
    try:
        old, new = tomllib.loads(r.stdout), tomllib.loads((tree / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if r.returncode != 0:
        return False
    for d in (old, new):
        for k in SELECT_LINT_TABLES:
            d.get("tool", {}).pop(k, None)
    return old == new


def _sel_expand(tree: Path, changed: list[str], base: str | None, tests: list[str]) -> set[str] | str:
    """The files a manifest or runner-skip.txt change stands for (SELECT_EXPAND): the srcs of the manifest entries it
    changes, the test files its changed runner-skip lines name (a legacy suite line: legacy_select's). A reason
    string when it can't tell (no base or no readable base manifest, [core] changed): the caller runs everything."""
    out: set[str] = set()
    mpath = "/".join(MANIFEST)
    if mpath in changed:
        base_man = _leg_manifest_at(tree, base) if base else None
        if base_man is None:
            return "manifest changed, no readable base manifest"
        try:
            man = load_manifest(tree)
        except Exception as e:  # noqa: BLE001 - an unreadable manifest fails its own lint step; here: everything
            return f"manifest unreadable ({type(e).__name__})"
        if (base_man.get("core") or {}) != (man.get("core") or {}):
            return "manifest's [core] changed"
        old, new = {e["dest"]: e for e in base_man["file"]}, {e["dest"]: e for e in man["file"]}
        out |= {
            e["src"] for d in old.keys() | new.keys() if old.get(d) != new.get(d) for e in (old.get(d), new.get(d)) if e
        }
    skip = "/".join(RUNNER_SKIP)
    if skip in changed:
        lines = _sel_skip_lines(tree, base) if base else None
        if lines is None:
            return "runner-skip.txt changed, no diff to read"
        for ln in lines:
            path = ln.split("#", 1)[0].split()[0].split("::", 1)[0].strip("/") if ln.split("#", 1)[0].split() else ""
            if not path or path.startswith("tests/legacy/"):
                continue  # a comment, or a legacy suite (legacy_select picks those)
            hit = {t for t in tests if t == path or t.startswith(path + "/")}
            if not hit and (tree / path).exists():
                return f"runner-skip.txt line names no test file: {path}"
            out |= hit  # a deleted test's line: nothing left to run
    return out


def _sel_skip_lines(tree: Path, base: str) -> list[str] | None:
    """The lines a branch adds or removes in tests/runner-skip.txt (committed and uncommitted); None: no diff."""
    rel = "/".join(RUNNER_SKIP)
    r = run(["git", "-C", str(tree), "diff", f"{base}...HEAD", "--", rel])
    r2 = run(["git", "-C", str(tree), "diff", "HEAD", "--", rel])
    if r.returncode != 0 or r2.returncode != 0:
        return None
    return [ln[1:] for ln in (r.stdout + r2.stdout).splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]


def imps_files(imps: set[str], index: dict[str, set[str]]) -> set[str]:
    """The .py files a set of imported names resolves to (a test that imports another test file depends on it); a
    one-word stdlib name (`import secrets`, `import typing`) is the stdlib, never a repo file of that stem."""
    out: set[str] = set()
    for i in imps:
        if "." not in i and i in sys.stdlib_module_names:
            continue
        out |= {f for f in index.get(i, ()) if f.endswith(".py")}
    return out


def _gate_root(tree: Path, base: str | None) -> Path:
    """Where gated-paths.json is read from: the base commit's copy (main at land time) when there is one, so a
    branch can't loosen the paths that force its own full run; else the tree."""
    if base:
        r = run(["git", "-C", str(tree), "show", f"{base}:{'/'.join(GATED_FILE)}"])
        if r.returncode == 0:
            d = Path(tempfile.mkdtemp(prefix="gate-"))
            d.joinpath(*GATED_FILE).parent.mkdir(parents=True)
            d.joinpath(*GATED_FILE).write_text(r.stdout, encoding="utf-8")
            return d
    return tree


def quarantine(tree: Path, base: str | None, today: str | None = None) -> tuple[dict[str, dict], list[str]]:
    """Main's tests/quarantine.txt (the base commit's copy: a branch can't quarantine its own red test) ->
    ({node id: {by, expires, why}} still in force, problems). Line: `NODE_ID by=WHO expires=YYYY-MM-DD # why` (by =
    who answers for it); an entry without by= or expires=, expired, or more than QUARANTINE_MAX_DAYS out is not in
    force."""
    text = ""
    if base:
        r = run(["git", "-C", str(tree), "show", f"{base}:{'/'.join(QUARANTINE_FILE)}"])
        text = r.stdout if r.returncode == 0 else ""
    else:
        with contextlib.suppress(OSError):
            text = tree.joinpath(*QUARANTINE_FILE).read_text(encoding="utf-8")
    day = today or time.strftime("%Y-%m-%d", time.localtime(clock.now()))
    limit = time.strftime("%Y-%m-%d", time.localtime(clock.now() + QUARANTINE_MAX_DAYS * 86400))
    live: dict[str, dict] = {}
    bad: list[str] = []
    for ln in text.splitlines():
        body, _, why = ln.partition("#")
        words = body.split()
        if not words:
            continue
        kv = dict(w.split("=", 1) for w in words[1:] if "=" in w)
        exp, who = kv.get("expires", ""), kv.get("by", "")
        if not who or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp):
            bad.append(f"{words[0]}: needs by= and expires=YYYY-MM-DD")
        elif exp < day:
            bad.append(f"{words[0]}: expired {exp} (counts again)")
        elif exp > limit:
            bad.append(f"{words[0]}: expires {exp}, more than {QUARANTINE_MAX_DAYS} days out")
        else:
            live[words[0]] = {"by": who, "expires": exp, "why": why.strip()[:120]}
    return live, bad


def _quarantined(node: str, live: dict[str, dict]) -> bool:
    return node in live or node.split("[", 1)[0] in live or node.split("::", 1)[0] in live


def _host_cpus() -> float:
    """CPUs this process may use: the cgroup quota (the hub runner's compose limit) else the affinity set."""
    try:
        q, p = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if q != "max":
            return max(1.0, int(q) / int(p))
    except (OSError, ValueError):
        pass
    try:
        return float(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return float(os.cpu_count() or 1)


def _mem_cap() -> int | None:
    """Workers the cgroup memory limit allows (~400 MB each), None when unlimited."""
    try:
        v = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        return None if v == "max" else max(1, int(v) // (400 << 20))
    except (OSError, ValueError):
        return None


def pytest_workers(nfiles: int, suites: int, toolchain: bool) -> int:
    """xdist workers for one pytest suite: ASSISTANT_CI_PYTEST_WORKERS wins (0/1 = serial); else the CPUs left
    over the host's 1-min load, shared by the suites running at once (one since 29.10: 3.13 only), 2..PYTEST_WORKERS
    locally, up to the cgroup quota and memory on the hub runner; never more than the files (loadfile) unless set."""
    raw = os.environ.get("ASSISTANT_CI_PYTEST_WORKERS", "").strip()
    if raw.isdigit():  # an explicit count is taken as it is (tests ask for xdist on a one-file repo)
        n = int(raw)
        return n if n > 1 else 0
    else:
        cpus = _host_cpus()
        if toolchain:
            n = int(cpus)
        else:
            try:
                load = os.getloadavg()[0]
            except OSError:
                load = 0.0
            n = max(2, min(PYTEST_WORKERS, int((cpus - load) / max(1, suites))))
        cap = _mem_cap()
        if cap is not None:
            n = min(n, cap)
    n = min(n, nfiles)
    return n if n > 1 else 0


def changed_files(tree: Path, base: str | None) -> list[str] | None:
    """Paths changed vs base (committed, staged, unstaged, untracked); None = everything (no base)."""
    if not base:
        return None
    out = set(git(tree, "diff", "--name-only", f"{base}...HEAD").splitlines())
    out |= set(git(tree, "diff", "--name-only", "HEAD").splitlines())
    out |= set(git(tree, "ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(x for x in out if x)


def ci(
    tree: Path,
    base: str | None,
    offline: bool,
    skip: set,
    stamp: bool,
    toolchain: str | None = None,
    report: str | None = None,
    host: bool = False,
) -> int:
    """toolchain: the hub runner's tool folder (ASSISTANT_CI_TOOLCHAIN): ruff/basedpyright from its bin, pytest on
    this interpreter only (3.13), no uv sync. report: a JSON file with every step's result and seconds. host: a
    land's part here (the runner runs the rest): the smoke set, the host-bound legacy suites and steps; no stamp."""
    if host:
        skip = set(skip) | HOST_ONLY_SKIP
    tree = Path(os.path.realpath(tree))
    git(tree, "rev-parse", "--git-dir")
    refuse_conflicts(tree)
    head = git(tree, "rev-parse", "HEAD")
    tree_id = git(tree, "rev-parse", "HEAD^{tree}")  # the stamp's key: what the steps test (read now, not at the end)
    dirty0 = bool(git(tree, "status", "--porcelain"))
    if base is None:
        branch = git(tree, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main" and run(["git", "-C", str(tree), "rev-parse", "--verify", "-q", "main"]).returncode == 0:
            base = git(tree, "merge-base", "HEAD", "main")
        else:
            base = read_json(st("deployed.json"), {}).get("sha") or None
            rel = read_json(st("deployed.json"), {}).get("release")
            if rel and not base:
                r = run(["git", "-C", str(tree), "rev-parse", "--verify", "-q", rel + "^{commit}"])
                base = r.stdout.strip() if r.returncode == 0 else None
    changed = changed_files(tree, base)
    tracked = git(tree, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    tracked = [t for t in tracked if (tree / t).is_file()]
    try:
        for ln in _ci_sweep():
            say(f"sweep: {ln}")
    except Exception as e:  # noqa: BLE001 (housekeeping never fails a CI run)
        say(f"sweep: skipped ({type(e).__name__}: {e})"[:200])
    uv_env = _uv_env(os.environ)
    tc = Path(toolchain) if toolchain else None
    if tc is not None:
        try:
            say(_runner_home(tree) or "runner home: as found")
        except (Fail, OSError) as e:
            say(f"runner home: not built ({e})"[:200])
    t_start = mark = time.monotonic()
    py_versions = (("3.13", sys.executable, ""),) if tc else PY_VERSIONS
    # The step lines print in this order whatever order the steps finish in (the pytest suites run beside the
    # others); an error line, and at the end whatever finished, print at once.
    order = [
        s
        for s in ("sync", "ruff", "pyright", "pytest", *CI_STEPS[4:])
        if s not in skip and not (s == "sync" and tc is not None)
    ]
    if "pytest" in order:
        i = order.index("pytest")
        order[i : i + 1] = [f"pytest-{ver}" for ver, _py, _vdir in py_versions]
    done: dict[str, tuple[bool, str, float]] = {}
    shown: list[str] = []

    def show(final: bool = False) -> None:
        for name in order + [n for n in done if n not in order]:
            if name in shown:
                continue
            if name not in done:
                if final:
                    continue
                return
            ok, detail, _secs = done[name]
            shown.append(name)
            say(f"{'ok  ' if ok else 'FAIL'} {name:10} {detail}")

    def did(name, ok, detail="", secs=None):
        """secs: the step's own wall time (a background step); else the time since the previous step here."""
        nonlocal mark
        now = time.monotonic()
        done[name] = (ok, detail, round(now - mark if secs is None else secs, 2))
        mark = now
        show(final=name not in order)

    def venv(v):
        return tree / v / "bin"

    def tool(name):
        return tc / "bin" / name if tc else venv(".venv-3.13") / name

    def pyexe(vdir):
        return Path(sys.executable) if tc else venv(vdir) / "python"

    failed: dict[str, list[str]] = {}  # pytest node ids per step (the report and the runner's record carry them)
    extras: dict[str, dict] = {}  # per pytest step: selection, rerun, quarantined (the report and the record)
    sel: dict = {"mode": "full", "why": "pytest skipped"}
    st_where = "runner" if tc else "host" if host else "all"
    st_names: list[str] = []  # the tool suites this run's pytest runs (tests/test_suites.py)
    st_sel: dict = {}
    st_else = 0
    runs: dict[str, tuple[Path, dict]] = {}  # pytest-<ver> -> (its python, its env): the retry runs the same way
    stopped: int | None = None
    suites: dict[str, tuple[BgGroup, Path]] = {}  # pytest-<ver> -> its run, its dir
    precheck: tuple[BgGroup | None, Path, str] | None = None  # codebase-sync precheck, started early

    prev_signals = _trap_stop()
    tmp = Path(tempfile.mkdtemp(prefix=CI_TMP_PREFIX, dir=_ci_root(tc is not None)))
    try:
        me = _proc_stat(os.getpid())
        (tmp / CI_OWNER).write_text(json.dumps({"pid": os.getpid(), "start": me[2] if me else ""}) + "\n")
        if "sync" not in skip and tc is None:
            ok, det = True, []
            for ver, py, vdir in PY_VERSIONS:
                e = dict(uv_env, UV_PROJECT_ENVIRONMENT=str(tree / vdir))
                cmd = [_uv(), "sync", "--locked", "--python", py, "--no-python-downloads"] + (
                    ["--offline"] if offline else []
                )
                r = run(cmd, cwd=tree, env=e, timeout=600)
                if r.returncode != 0:
                    ok = False
                    det.append(f"{ver}: " + (r.stderr.strip().splitlines() or ["?"])[-1][:160])
            did("sync", ok, "; ".join(det) or "3.13 venv locked")
        if "suites" not in skip:
            # the tool suites are pytest tests (tests/test_suites.py): the ones this change impacts (legacy_select),
            # on this side (host-bound here, the rest on the runner), go to it through the environment
            st_names, st_else, st_sel = suite_plan(tree, changed, st_where, base if changed is not None else None)
            say(f"suites: {_suite_tag(st_sel, len(st_names), st_else, st_where)}; {len(shell_suites(tree))} still in "
                "shell form (tests/legacy: all become native pytest)")  # fmt: skip
        if "pytest" not in skip or st_names:
            # every interpreter's suite at once, each in its own process group with xdist workers; the steps below
            # run meanwhile, the suites are collected after them. The files: what the changed paths reach.
            if "pytest" in skip:  # `--only suites`: just the wrapper
                sel = {"mode": "suites", "why": "tool suites only", "of": 1, "files": []}
            else:
                sel = smoke_tests(tree) if host else select_tests(tree, changed, tracked, base)
            if st_names and sel["mode"] != "full" and SUITES_TEST not in sel["files"]:
                more = sel["mode"] == "smoke"  # the smoke set's count is its own files: the wrapper is one more
                sel = {**sel, "files": [*sel["files"], SUITES_TEST], "of": sel["of"] + more}
            say(f"pytest: {sel['mode']} ({sel['why']}; {len(sel.get('files') or []) or sel['of']}/{sel['of']} files)")
            nothing = sel["mode"] == "smoke" and not sel["files"]  # no host-bound test left: never the whole suite
            for ver, _py, _vdir in py_versions if nothing else ():
                did(f"pytest-{ver}", True, "no host-bound tests (tests/runner-skip.txt): the runner ran them", secs=0.0)
            for ver, _py, vdir in () if nothing else py_versions:
                t = tmp / f"py{ver}"  # short: see CI_ROOT
                t.mkdir()
                home = t / "home"
                home.mkdir()
                env = _guarded_env(t, home, {"ASSISTANT_CI_WORKER_DIRS": "1", SUITES_ENV: ",".join(st_names)})
                if st_names:
                    env.update({SUITES_OUT_ENV: str(t / "suites.json"), SUITES_DIR_ENV: str(tmp)})
                cmd = [str(pyexe(vdir)), "-m", "pytest", "--basetemp", str(t / "bt")]
                files = sel.get("files") if sel["mode"] in ("impacted", "smoke", "suites") else None
                nf = len(files) if files is not None else sel["of"]
                cmd += _xdist_args(pyexe(vdir), env, tc is not None, nf, len(py_versions))
                cmd += files or []
                runs[f"pytest-{ver}"] = (pyexe(vdir), env)
                suites[f"pytest-{ver}"] = (
                    BgGroup(
                        cmd,
                        cwd=tree,
                        env=env,
                        timeout=float(test_seam("ASSISTANT_CI_PYTEST_TIMEOUT") or PYTEST_TIMEOUT),
                    ),
                    t,
                )
        if "precheck" not in skip:
            precheck = _ci_precheck_start(tree, changed)
        mark = time.monotonic()
        py_dirs = [d for d in ("core", "ops", "scripts", "packages", "tests") if (tree / d).is_dir()]
        if "ruff" not in skip:
            r1 = run([str(tool("ruff")), "check", "--quiet", *py_dirs], cwd=tree)
            r2 = run([str(tool("ruff")), "format", "--check", "--quiet", *py_dirs], cwd=tree)
            det = (r1.stdout + r2.stdout).strip().splitlines()
            did(
                "ruff",
                r1.returncode == 0 and r2.returncode == 0,
                f"{len(det)} finding(s): " + " | ".join(det[:4]) if det else "clean",
            )
        if "pyright" not in skip:
            tdirs = [d for d in ("core", "packages") if (tree / d).is_dir() and any((tree / d).rglob("*.py"))]
            r = run(
                [str(tool("basedpyright")), "--pythonpath", str(pyexe(".venv-3.13")), *tdirs],
                cwd=tree,
                timeout=600,
            )
            last = (r.stdout.strip().splitlines() or [""])[-1]
            errs = [ln.strip() for ln in r.stdout.splitlines() if " - error" in ln]
            did("pyright", r.returncode == 0, (" | ".join(errs[:3]) or last)[:300])
        if "ast" not in skip:
            allow = read_json(tree / "ops" / "lint" / "ast-allow.json", {}).get("allow", {})
            hits = ast_ban(tree, tracked, allow)
            did("ast", not hits, " | ".join(hits[:5]) or "no local json/note helpers, urlopen or flock outside core/")
        lint_red = [s for s in ("ruff", "ast") if s in done and not done[s][0]]
        if host and lint_red:  # a land's part here: a lint red ends it now (the suites and scans stop unseen)
            raise Fail(f"{' + '.join(lint_red)} red: the rest of the host CI skipped; fix it, land again")
        if "schemas" not in skip:
            did(*_ci_schemas(tree, tracked, pyexe(".venv-3.13"), isolated=tc is None))
        if "ratchet" not in skip:
            did(*_ci_ratchet(tree))
        if "inbox-lint" not in skip:
            did(*_ci_inbox_lint(tree, tmp / "inbox-lint"))
        if "secretscan" not in skip:
            # the branch's changed files when there is a base (29.10: the tree's other files passed when they landed;
            # seconds instead of the whole tree), else every tracked file
            scan = [t for t in tracked if t in set(changed)] if changed is not None else tracked
            ss = str(paths.bin_dir("secretscan"))
            bad = 0
            for i in range(0, len(scan), 200):
                r = run([ss, "-q", "--file", *[str(tree / t) for t in scan[i : i + 200]]], timeout=300)
                bad += r.returncode != 0
            what = f"{len(scan)} changed file(s)" if changed is not None else f"{len(scan)} file(s)"
            did("secretscan", bad == 0, what + (" — HITS (run secretscan --file on the tree)" if bad else ""))
        if "precheck" not in skip:
            res = _ci_precheck(tree, base, precheck)
            g0 = precheck[0] if precheck else None
            did(*res, secs=time.monotonic() - g0.t0 if g0 else None)  # from its start, not from the last step
        if "bun" not in skip:
            did(*_ci_bun(tree, changed, tmp))
        for name, (g, t) in suites.items():
            r = g.wait()
            left = _ci_reap(t)  # what the tests left running (test Postgres, hubd) stops now
            hits = _guard_hits(t)
            summ = (r.stdout.strip().splitlines() or [""])[-1]
            fails = [ln for ln in r.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))]
            rc, secs, note = r.returncode, g.secs, ""
            if rc == 5 and sel["mode"] in ("impacted", "smoke", "suites"):
                rc = 0  # every selected file is host-bound and skipped here (runner-skip.txt): nothing to run
            ex: dict = {"selection": {k: sel[k] for k in ("mode", "why", "of") if k in sel}}
            ex["selection"].update(files=len(sel.get("files") or []) or sel["of"], tests=_pytest_count(summ))
            ids = _node_ids(fails)
            if rc in (1, 2) and not hits and ids and len(ids) <= RERUN_MAX:
                # one retry of only the failed tests, serial (load and ordering are the usual flake causes)
                py0, env0 = runs[name]
                t1 = time.monotonic()
                r2 = run_group([str(py0), "-m", "pytest", "--basetemp", str(t / "bt-rerun"), *ids], tree, env0,
                               timeout=float(test_seam("ASSISTANT_CI_PYTEST_TIMEOUT") or PYTEST_TIMEOUT))  # fmt: skip
                left += _ci_reap(t)
                hits = _guard_hits(t)
                secs += time.monotonic() - t1
                fails2 = [ln for ln in r2.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))]
                still = _node_ids(fails2) if r2.returncode != 0 else []
                if r2.returncode != 0 and not still:
                    still = ids  # red without a FAILED line (crash, timeout): all count
                ex["rerun"] = {"ids": ids[:RERUN_MAX], "ok": r2.returncode == 0, "still": still[:RERUN_MAX]}
                note = f"; rerun of {len(ids)}: " + (
                    "green (flaky: " + ", ".join(i.rsplit("::", 1)[-1] for i in ids[:3]) + ")"
                    if r2.returncode == 0
                    else f"{len(still)} still red"
                )
                rc, ids, fails = (0, [], []) if r2.returncode == 0 else (1, still, fails2 or fails)
            if rc == 1 and ids and not hits:
                live, _bad = quarantine(tree, base)
                q = [i for i in ids if _quarantined(i, live)]
                if q:
                    ex["quarantined"] = q[:RERUN_MAX]
                    note += f"; quarantined {len(q)}"
                    ids = [i for i in ids if i not in q]
                    fails = [ln for ln in fails if _node_ids([ln])[:1] and _node_ids([ln])[0] in ids]
                    if not ids:
                        rc = 0
            if st_names:  # the suites' own results: the step record, land's result file, main's record
                leg = (_read_json_safe(t / "suites.json").get("suites") or {}) if (t / "suites.json").is_file() else {}
                leg = {k: v for k, v in leg.items() if k in st_names and isinstance(v, dict)}
                gone = [x for x in st_names if x not in leg]
                if gone and rc == 0:
                    rc = 1  # green without a result for every selected suite: the wrapper did not run them
                slow = {s: [int(bool(x.get("ok"))), x.get("secs", 0)] for s, x in leg.items()
                        if not x.get("ok") or x.get("secs", 0) >= LEGACY_SLOW}  # fmt: skip
                if slow:
                    ex["suites"] = slow
                tails = legacy_tails(leg)
                if tails:  # a red suite's stderr tail: the runner's record is all its waiter sees
                    ex["tails"] = tails
                ex["selection"]["suites"] = {**{k: st_sel.get(k) for k in ("mode", "why", "of")}, "ran": len(leg)}
                red_s = sorted(s for s, x in leg.items() if not x.get("ok"))
                note += f"; suites {len(leg)}/{len(st_names)} ran" + (f", red: {', '.join(red_s)}" if red_s else "")
                note += f", no result: {', '.join(gone[:5])}" if gone else ""
                _legacy_report(tree, head, tree_id, leg, dirty0, st_sel)
            failed[name] = ids[:400]
            extras[name] = ex
            did(
                name,
                rc == 0 and not hits,
                summ[:120]
                + note
                + (f"; {len(hits)} blocked connection(s)" if hits else "")
                + (f"; stopped {len(left)} leftover process(es)" if left else "")
                + ("; " + " | ".join(fails[:3]) if fails else ""),
                secs=secs,
            )
    except Fail as e:
        did("error", False, str(e))
    except subprocess.TimeoutExpired as e:
        did("timeout", False, str(e)[:200])
    except CiStopped as e:
        stopped = int(e.args[0]) if e.args else int(signal.SIGTERM)
        did("stopped", False, f"{signal.Signals(stopped).name} during the run")
    finally:
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP) if prev_signals else ():
            signal.signal(s, signal.SIG_IGN)  # the cleanup runs to the end
        show(final=True)
        try:
            for g, _t in suites.values():  # a suite still running after an error or a signal: SIGINT, then SIGKILL
                g.stop()
            if precheck is not None:
                if precheck[0] is not None:
                    precheck[0].stop()
                shutil.rmtree(precheck[1], ignore_errors=True)
            for ln in _ci_reap(tmp):
                say(f"cleanup: {ln}")
        except Exception as e:  # noqa: BLE001 (the run dir still goes)
            say(f"cleanup: reap failed ({type(e).__name__}: {e})"[:200])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            _restore_signals(prev_signals)
    results = [(n, *done[n]) for n in shown]  # the order they printed in
    ok = all(r[1] for r in results)
    why_not = "uncommitted changes" if dirty0 else _ci_unchanged(tree, head, tree_id)
    clean = not why_not
    if report:
        steps = [
            {
                "name": n,
                "ok": o,
                "detail": d[:300],
                "secs": s,
                **({"failed": failed[n]} if failed.get(n) else {}),
                **extras.get(n, {}),
            }
            for n, o, d, s in results
        ]
        rep_obj = {
            "ok": ok,
            "head": head,
            "tree": tree_id,
            "clean": clean,
            "steps": steps,
            "secs": round(time.monotonic() - t_start, 2),
        }
        Path(report).write_text(json.dumps(rep_obj, indent=1) + "\n", encoding="utf-8")
    _pytest_out(tree_id, {k: v for k, v in extras.items() if k.startswith("pytest")})
    _land_out(tree_id, steps={n: s for n, _o, _d, s in results})
    if ok and stamp and clean and not skip:
        write_json(
            st("ci", tree_id + ".json"), {"ok": True, "ts": clock.now(), "head": head, "steps": [r[0] for r in results]}
        )
    say(
        f"CI {'green' if ok else 'RED'} for {head[:12]} (tree {tree_id[:12]})"
        + (
            ""
            if ok and stamp and clean and not skip
            else " — not stamped" + (f" ({why_not})" if why_not else "") + (" (steps skipped)" if skip else "")
        )
    )
    if stopped is not None:
        return 128 + stopped
    return 0 if ok else 6


def _node_ids(lines: list[str]) -> list[str]:
    """`FAILED tests/x.py::t[p] - why` / `ERROR tests/x.py` -> the node ids, in order, once each."""
    out: list[str] = []
    for ln in lines:
        w = ln.split(" - ")[0].split()
        if len(w) >= 2 and w[1].startswith("tests/") and w[1] not in out:
            out.append(w[1])
    return out


def _pytest_count(summary: str) -> int | None:
    """Tests that ran (passed + failed + errors) from pytest's last line, None when it has no counts."""
    got = dict((k, int(n)) for n, k in re.findall(r"(\d+) (passed|failed|errors?|xfailed|xpassed)", summary))
    return sum(got.values()) if got else None


def _pytest_out(tree_id: str, extras: dict) -> None:
    """The pytest steps' selection/rerun/quarantine into land's ASSISTANT_CI_LEGACY_OUT file (key "pytest")."""
    if extras:
        _land_out(tree_id, pytest=extras)


def _land_out(tree_id: str, **kv) -> None:
    """kv into land's ASSISTANT_CI_LEGACY_OUT file: "pytest" (above), "steps" (each step's seconds: the CI tab)."""
    out = os.environ.get("ASSISTANT_CI_LEGACY_OUT")
    if not out or not kv:
        return
    d = _read_json_safe(Path(out))
    if d.get("tree") not in (None, tree_id):
        d = {}
    d.update(tree=tree_id, **kv)
    with contextlib.suppress(OSError):
        write_json(Path(out), d)


def _ci_unchanged(tree: Path, head: str, tree_id: str) -> str:
    """Empty when the checkout is still what the CI run started on (same HEAD and tree, nothing uncommitted or
    untracked), else why not: a commit or an edit during the run must not stamp a tree the steps never saw."""
    if git(tree, "rev-parse", "HEAD") != head or git(tree, "rev-parse", "HEAD^{tree}") != tree_id:
        return "HEAD moved during the run"
    if git(tree, "status", "--porcelain"):
        return "uncommitted changes"
    return ""


# Would-be ~/bin files staged in the overlay whether or not their own suite runs: the staged scripts import them from
# their own dir, as deployed (secretscan: codebase-sync, recall, tg-cmd, evals, flowd, agentfeed's dash-data, ...)
OVERLAY_ALWAYS = ("bin/secretscan.py",)


def _overlay_stage(tree: Path, e: dict, q: Path, reshebang: bool) -> None:
    """One manifest entry's file into the overlay at q, rendered as it deploys (the runner: its python shebang)."""
    data = render((tree / e["src"]).read_bytes(), bool(e["header"]))
    q.write_bytes(_reshebang(data) if reshebang else data)
    os.chmod(q, int(e["mode"], 8))


def _overlay_lib(tree: Path, man: dict, lib: Path, live: Path) -> None:
    """The rest of the would-be ~/bin/lib next to the staged scripts (most moved scripts import from their own lib/
    dir): the manifest's other bin/lib/ files rendered from the tree, then a link to every other top-level entry of
    the live ~/bin/lib (unmanaged helpers such as assistant_profile or usage). core and inbox stay the tree's."""
    for e in man["file"]:
        if not e["dest"].startswith("bin/lib/"):
            continue
        rel = Path(e["dest"][len("bin/lib/") :])
        if (lib / rel.parts[0]).is_symlink():  # core and inbox come whole from the tree; never write through a link
            continue
        q = lib / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(render((tree / e["src"]).read_bytes(), bool(e["header"])))
        os.chmod(q, int(e["mode"], 8))
    try:
        names = sorted(os.listdir(live))
    except OSError:
        return
    for name in names:
        q = lib / name
        if name == "__pycache__" or name.startswith(".") or name.endswith(".releases") or q.is_symlink() or q.exists():
            continue
        real = os.path.realpath(live / name)
        if os.path.exists(real):
            os.symlink(real, q)


# ---------------------------------------------------------------- legacy suite selection (impact map)
# A CI run with a base runs the legacy suites its change can affect, as pytest's impacted mode picks test files. A
# suite's files (legacy_impact): its script; what that script and the suite dir import from the would-be ~/bin/lib
# (core, inbox, the manifest's bin/lib files), followed through their own imports, and the data files beside those
# modules that they name; the managed scripts and tree files the script or the suite dir name (one hop: their imports,
# not their mentions; on the runner a suite's ~/bin is the tree's); the suite dir; the extra globs in
# tests/legacy/impact.toml (shell or indirect deps). Every suite runs (the safe fallback) for: no base, a change to
# the map's extras or the manifest's [core], a manifest unreadable at the base, a changed file in that area no suite
# claims (other than a module nobody imports or a script nobody runs), or more than LEGACY_FULL_SHARE of the suites.
# A manifest change selects the suites of the entries it changes (their srcs count as changed); a runner-skip.txt
# change the suites its changed lines name. The driver and the runner's toolchain (ops/assistant.py, citool, ciworker)
# force nothing: a land's CI runs the deployed driver and the release's toolchain, not the tree's, so all suites there
# would test the old ones; the post-land main check (legacy-main: every suite on main with the just-deployed driver,
# in the background) and `ci-nightly` run every suite.
LEGACY_IMPACT_FILE = ("tests", "legacy", "impact.toml")
LEGACY_ALL = ("tests/legacy/impact.toml",)  # the map's extras: a change re-checks every suite
LEGACY_NEUTRAL = ("tests/legacy/slow.txt", "tests/legacy/README.md", "tests/legacy/conftest.py")  # no suite reads them
LEGACY_FULL_SHARE = 0.6  # an impacted set above this share of the suites runs every suite
LEGACY_TOKEN_MAX = 3  # a path-like word that resolves to more tree files than this names none of them
_LEG_IMPORT = re.compile(
    r"(?:^|[\s;\"'(])(?:from\s+([A-Za-z_][\w.]*)\s+import\b|import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*))"
    r"|\s-m\s+([A-Za-z_][\w.]*)"
)
_LEG_REFS: dict[tuple, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {}


def _leg_dotted(rel: str) -> str:
    """bin/lib-relative path -> module name: memrows/store.py -> memrows.store, memrows/__init__.py -> memrows."""
    parts = rel[: -len(".py")].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _leg_refs(path: Path, pkg: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """(imported dotted names, path-like tokens, words) of one file; pkg: its package (relative imports). Python: ast
    imports, its one-word string constants as words, path-like words and `import x` / `-m x` in its other strings
    (python -c, heredocs); docstrings nothing. Other text (shell, data): path-like words, bare words, imports."""
    try:
        s = path.stat()
    except OSError:
        return frozenset(), frozenset(), frozenset()
    key = (str(path), pkg, s.st_mtime_ns, s.st_size)
    if key in _LEG_REFS:
        return _LEG_REFS[key]
    imps: set[str] = set()
    toks: set[str] = set()
    words: set[str] = set()
    try:
        data = path.read_bytes() if s.st_size <= 2_000_000 else b""
    except OSError:
        data = b""
    text = "" if b"\0" in data[:4096] else data.decode("utf-8", errors="replace")
    tree = None
    if text and (path.suffix == ".py" or text.startswith("#!") and "python" in text.split("\n", 1)[0]):
        with contextlib.suppress(SyntaxError, ValueError):
            tree = ast.parse(text)
    strs: list[str] = []
    if tree is not None:
        docs = set()
        for n in ast.walk(tree):
            body = getattr(n, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                docs.add(id(body[0].value))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    p = a.name.split(".")
                    imps |= {".".join(p[: i + 1]) for i in range(len(p))}
            elif isinstance(n, ast.ImportFrom):
                p = n.module.split(".") if n.module else []
                if n.level:
                    p = list(pkg[: max(0, len(pkg) - (n.level - 1))]) + p
                imps |= {".".join(p[: i + 1]) for i in range(len(p))}
                imps |= {".".join([*p, a.name]) for a in n.names if a.name != "*"}
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                v = n.value.strip()
                if v and len(v) < 300 and not any(c.isspace() for c in v):
                    words.add(v.strip("/"))
                if len(v) < 200_000:
                    strs.append(v)
    elif text:
        strs.append(text)
    for t in strs:
        for m in _LEG_IMPORT.finditer(t):
            for g in (m.group(1), m.group(3), *(m.group(2) or "").split(",")):
                p = (g or "").strip().split(".")
                imps |= {".".join(p[: i + 1]) for i in range(len(p)) if p[0]}
        for w in _SEL_TOKEN.findall(t):
            w = w.strip("./")
            if not w or len(w) >= 300:
                continue
            if "/" in w or "." in w:
                toks.add(w)
            elif tree is None:
                words.add(w)
    out = (frozenset(i for i in imps if i), frozenset(toks), frozenset(words))
    _LEG_REFS[key] = out
    return out


def _leg_lookup(tok: str, by_base: dict[str, list[tuple[list[str], str]]]) -> set[str]:
    """The files a path-like word names: same file name, the longest run of matching trailing path parts wins."""
    parts = [p for p in tok.split("/") if p]
    best, out = 0, set()
    for fp, f in by_base.get(parts[-1], []) if parts else []:
        k = 0
        while k < min(len(fp), len(parts)) and fp[-1 - k] == parts[-1 - k]:
            k += 1
        if fp[0] == "bin" and k < 2:  # a dest (bin/tg) only as bin/<name>: "opt/assistant" names no bin/assistant
            continue
        if k > best:
            best, out = k, {f}
        elif k == best:
            out.add(f)
    return out


_LEG_LOADS = re.compile(rb"SourceFileLoader|spec_from_file_location|run_path\(|load_source\(")


def _leg_loads(path: Path) -> bool:
    """The file loads another file as a module by its path (its imports count, not only its bytes)."""
    try:
        with open(path, "rb") as f:
            return bool(_LEG_LOADS.search(f.read(2_000_000)))
    except OSError:
        return False


def _leg_files(tree: Path) -> list[str]:
    """The tree's files (tracked + untracked, not ignored); a tree that is no git checkout: every file under it."""
    r = run(["git", "-C", str(tree), "ls-files", "--cached", "--others", "--exclude-standard"])
    if r.returncode == 0:
        return [f for f in r.stdout.splitlines() if f and (tree / f).is_file()]
    out = []
    for dp, dns, fns in os.walk(tree):
        dns[:] = [d for d in dns if d not in (".git", "__pycache__", ".venv", "node_modules")]
        out += [os.path.relpath(os.path.join(dp, f), tree) for f in fns]
    return sorted(out)


def _leg_manifest_at(tree: Path, base: str) -> dict | None:
    """The manifest's [[file]] entries at commit base (with load_manifest's defaults); None when unreadable."""
    r = run(["git", "-C", str(tree), "show", f"{base}:{'/'.join(MANIFEST)}"])
    if r.returncode != 0:
        return None
    try:
        m = tomllib.loads(r.stdout)
    except tomllib.TOMLDecodeError:
        return None
    files = [e for e in m.get("file", []) if isinstance(e, dict) and e.get("src") and e.get("dest")]
    for e in files:
        e.setdefault("mode", "0755" if str(e["dest"]).startswith("bin/") else "0644")
        e.setdefault("header", True)
    return {"file": files, "core": m.get("core")}


def legacy_impact(tree: Path, man: dict, base_man: dict | None = None, changed: list[str] = ()) -> dict:
    """The impact map: {"deps": {suite: files it exercises}, "extras": {suite: globs}, "mods": module files,
    "scripts": script srcs, "core": core's src dir}. base_man/changed add the base's entries and deleted files, so a
    suite that still imports a deleted module or names a removed script claims it."""
    entries = man["file"] + (base_man["file"] if base_man else [])
    core_src = str((man.get("core") or {}).get("src") or "core").strip("/")
    files = _leg_files(tree)
    every = sorted(set(files) | set(changed))
    mods: dict[str, str] = {}  # dotted name -> repo path, as the would-be ~/bin/lib resolves it
    for f in every:
        if f.endswith(".py") and f.startswith(core_src + "/"):
            mods[_leg_dotted("core" + f[len(core_src) :])] = f
        elif f.endswith(".py") and f.startswith("packages/inbox/"):
            mods[_leg_dotted(f[len("packages/") :])] = f
    for e in entries:
        if e["dest"].startswith("bin/lib/") and e["dest"].endswith(".py"):
            mods.setdefault(_leg_dotted(e["dest"][len("bin/lib/") :]), e["src"])
    pkg_of = {p: tuple(n.split(".") if p.endswith("__init__.py") else n.split(".")[:-1]) for n, p in mods.items()}
    scripts = {e["dest"][4:]: e["src"] for e in entries if e["dest"].startswith("bin/") and "/" not in e["dest"][4:]}
    # a bare word naming a script: not the driver's name (every path under ~/opt/assistant or state/assistant)
    script_names = {n for n in scripts if n not in _SEL_GENERIC and n != "assistant"}
    src_scripts = set(scripts.values())
    by_base: dict[str, list[tuple[list[str], str]]] = {}  # file name -> (path parts as named, the repo file)
    for f in every:
        by_base.setdefault(f.rsplit("/", 1)[-1], []).append((f.split("/"), f))
    for e in entries:  # a dest names its src (bin/lib/remindwhen.py, bin/tg), with its dir (not "x/tg" alone)
        by_base.setdefault(e["dest"].rsplit("/", 1)[-1], []).append((e["dest"].split("/"), e["src"]))
    # data files by their dir in the would-be ~/bin/lib (a module opens what lies beside it there): core's and
    # inbox's non-.py files, the manifest's non-.py bin/lib files (schemas/kinds.toml -> inbox/kinds.toml)
    beside: dict[str, dict[str, str]] = {}
    for f in files:
        if f.endswith(".py"):
            continue
        rel = (
            "core" + f[len(core_src) :]
            if f.startswith(core_src + "/")
            else f[9:]
            if f.startswith("packages/inbox/")
            else ""
        )
        if "/" in rel:
            beside.setdefault(rel.rsplit("/", 1)[0], {})[rel.rsplit("/", 1)[1]] = f
    for e in entries:
        if e["dest"].startswith("bin/lib/") and not e["dest"].endswith(".py") and e["dest"].count("/") > 2:
            d, b = e["dest"][8:].rsplit("/", 1)
            beside.setdefault(d, {})[b] = e["src"]
    libdir = {p: "/".join(n.split(".") if p.endswith("__init__.py") else n.split(".")[:-1]) for n, p in mods.items()}

    def closure(starts: set[str]) -> set[str]:
        seen, todo = set(starts), list(starts)
        while todo:
            f = todo.pop()
            if not (tree / f).is_file():
                continue
            imps, toks, words = _leg_refs(tree / f, pkg_of.get(f, ()))
            nxt = {mods[i] for i in imps if i in mods}
            if libdir.get(f):  # a module's data files: names of files in its own lib dir
                near = beside.get(libdir[f], {})
                nxt |= {near[b] for b in {w.rsplit("/", 1)[-1] for w in words | toks} if b in near}
            for x in nxt - seen:
                seen.add(x)
                todo.append(x)
        return seen

    try:
        extra = tomllib.loads(tree.joinpath(*LEGACY_IMPACT_FILE).read_text(encoding="utf-8")).get("suites") or {}
    except (OSError, tomllib.TOMLDecodeError):
        extra = {}
    rh = paths.real_home()
    deps: dict[str, set[str]] = {}
    for e in man["file"]:
        suite = e.get("suite")
        if not suite:
            continue
        sdir = f"tests/legacy/{suite}/"
        own = [f for f in files if f.startswith(sdir)]
        roots = [(tree / f, f) for f in [e["src"], *own]]
        if not own:  # a suite still in ~/opt: its files are no part of the tree, what they name is
            od = rh / "opt" / f"{suite}-tests"
            if od.is_dir():
                roots += [(Path(dp) / f, "") for dp, _d, fns in os.walk(od) for f in fns if "__pycache__" not in dp]
        hop: set[str] = set()
        loads = False  # loads a script as a module (SourceFileLoader & co.): the scripts it names bring their imports
        for p, _rel in roots:
            _imps, toks, words = _leg_refs(p, ())
            for t in toks:  # a glob or a name many files share (run.sh, __init__.py) names none of them
                hit = set() if "*" in t else _leg_lookup(t, by_base)
                hop |= hit if len(hit) <= LEGACY_TOKEN_MAX else set()
            hop |= {scripts[w] for w in words & script_names}
            loads = loads or _leg_loads(p)
        starts = {e["src"], *own} | {mods[i] for p, _r in roots for i in _leg_refs(p, ())[0] if i in mods}
        ran = set() if loads else hop & src_scripts  # a script it runs: that file only (its suite covers its imports)
        fixed = [g for g in extra.get(suite) or [] if isinstance(g, str) and not any(c in g for c in "*?[")]
        deps[suite] = closure(starts) | closure(hop - ran) | ran | set(own) | closure(set(fixed))
    return {
        "deps": deps,
        "extras": {s: [str(g) for g in v] for s, v in extra.items() if isinstance(v, list)},
        "mods": set(mods.values()),
        "scripts": set(scripts.values()),
        "core": core_src,
    }


def _leg_skip_suites(tree: Path, base: str) -> set[str] | None:
    """The suites named on the lines a branch changes in tests/runner-skip.txt (host or runner side); None: no diff."""
    r = run(["git", "-C", str(tree), "diff", f"{base}...HEAD", "--", "/".join(RUNNER_SKIP)])
    r2 = run(["git", "-C", str(tree), "diff", "HEAD", "--", "/".join(RUNNER_SKIP)])
    if r.returncode != 0 or r2.returncode != 0:
        return None
    lines = [ln for ln in (r.stdout + r2.stdout).splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    return {m for ln in lines for m in re.findall(r"tests/legacy/([\w.+-]+)", ln)}


def legacy_select(tree: Path, man: dict, changed: list[str] | None, base: str | None = None) -> dict:
    """Which legacy suites a run with these changed paths needs (see the block comment above).
    -> {"mode": "all"|"impacted", "why", "of": n suites, "suites": [...] (impacted only)}"""
    names = [e["suite"] for e in man["file"] if e.get("suite")]
    out: dict = {"mode": "all", "of": len(names)}
    if os.environ.get("ASSISTANT_CI_SELECT", "").strip() == "full":
        return {**out, "why": "ASSISTANT_CI_SELECT=full"}
    if changed is None:
        return {**out, "why": "no base: everything"}
    hit = [c for c in changed if any(fnmatch.fnmatchcase(c, g) for g in LEGACY_ALL)]
    if hit:
        return {**out, "why": f"impact map changed: {hit[0]}" + (f" (+{len(hit) - 1})" if len(hit) > 1 else "")}
    chg, picked = set(changed) - set(LEGACY_NEUTRAL), set()
    base_man = None
    mpath = "/".join(MANIFEST)
    if mpath in chg:
        base_man = _leg_manifest_at(tree, base) if base else None
        if base_man is None:
            return {**out, "why": "manifest changed, no readable base manifest"}
        old, new = {e["dest"]: e for e in base_man["file"]}, {e["dest"]: e for e in man["file"]}
        for d in old.keys() | new.keys():
            if old.get(d) != new.get(d):
                for e in (old.get(d), new.get(d)):
                    if e:
                        chg.add(e["src"])
                        picked |= {e["suite"]} if e.get("suite") else set()
        if (base_man.get("core") or {}) != (man.get("core") or {}):
            return {**out, "why": "manifest's [core] changed"}
        chg.discard(mpath)
    skip = "/".join(RUNNER_SKIP)
    if skip in chg:
        named = _leg_skip_suites(tree, base) if base else None
        if named is None:
            return {**out, "why": "runner-skip.txt changed, no diff to read"}
        picked |= named
        chg.discard(skip)
    imp = legacy_impact(tree, man, base_man, sorted(chg))
    deps, extras = imp["deps"], imp["extras"]
    bin_srcs = {e["src"] for e in man["file"] + (base_man or {"file": []})["file"] if e["dest"].startswith("bin/")}
    unknown: list[str] = []
    for c in sorted(chg):
        parts = c.split("/")
        if c.startswith("tests/legacy/") and len(parts) > 3:
            picked |= {parts[2]}  # its suite's dir (a dir that is no suite, e.g. inbox-lint's: none)
            continue
        hits = {s for s, d in deps.items() if c in d} | {
            s for s, gs in extras.items() if any(fnmatch.fnmatchcase(c, g) for g in gs)
        }
        if hits:
            picked |= hits
            continue
        area = c.startswith((imp["core"] + "/", "packages/inbox/", "tests/legacy/")) or c in bin_srcs
        if area and c not in imp["mods"] and c not in imp["scripts"]:
            unknown.append(c)  # a data file, harness or lib file no suite claims: it may be read by any of them
    picked &= set(names)
    if unknown:
        more = f" (+{len(unknown) - 1})" if len(unknown) > 1 else ""
        return {**out, "why": f"no suite claims {unknown[0]}{more}"}
    if len(picked) > LEGACY_FULL_SHARE * max(1, len(names)):
        return {**out, "why": f"{len(picked)}/{len(names)} suites impacted"}
    first = next((c for c in sorted(chg) if any(c in d for d in deps.values())), "")
    return {
        "mode": "impacted",
        "why": f"{len(picked)} impacted" + (f" (e.g. by {first})" if picked and first else ""),
        "of": len(names),
        "suites": sorted(picked),
    }


def _legacy_selected(
    man: dict, changed: list[str] | None, tree: Path | None = None, base: str | None = None, sel: dict | None = None
) -> list[dict]:
    """The manifest entries whose legacy suite a CI run with these changed paths runs (legacy_select on tree; no
    tree: every suite). sel: filled with the selection."""
    s = legacy_select(tree, man, changed, base) if tree is not None else {"mode": "all", "why": "no tree"}
    if sel is not None:
        sel.update(s)
    keep = None if s["mode"] == "all" else set(s["suites"])
    return [e for e in man["file"] if e.get("suite") and (keep is None or e["suite"] in keep)]


def _legacy_touches(e: dict, changed: list[str]) -> bool:
    """A branch touches a suite's area: its managed script or its folded suite dir (tests/legacy/<suite>/)."""
    return any(c == e["src"] or c.startswith(f"tests/legacy/{e['suite']}/") for c in changed)


def _under_dir(p: Path, root: Path) -> bool:
    rp, rr = p.resolve(), root.resolve()
    return rp == rr or rr in rp.parents


LEGACY_TIMEOUT = 240.0  # one legacy suite (s), as test-all runs it
LEGACY_WORKERS = 8  # legacy suites at once at most (ASSISTANT_CI_LEGACY_WORKERS overrides; 1 = serial)
LEGACY_SLOW_FILE = ("tests", "legacy", "slow.txt")  # suites that start first (the longest ones), slowest first


def legacy_workers(n: int) -> int:
    """Legacy suites at once: ASSISTANT_CI_LEGACY_WORKERS wins; else the hub runner's CPU quota, here the CPUs left
    over the 1-min load (at least 2), capped at LEGACY_WORKERS and the suite count. Mostly shell, stubs and sleeps."""
    raw = os.environ.get("ASSISTANT_CI_LEGACY_WORKERS", "").strip()
    if raw.isdigit():
        w = max(1, int(raw))
    else:
        cpus = _host_cpus()
        if os.environ.get("ASSISTANT_CI_TOOLCHAIN"):
            w = int(cpus)
        else:
            try:
                load = os.getloadavg()[0]
            except OSError:
                load = 0.0
            w = max(2, int(cpus - load))
        w = min(w, LEGACY_WORKERS)
    return max(1, min(w, n))


SUITES_TEST = "tests/test_suites.py"  # the pytest wrapper: one test per tool suite, id = the suite's name
SUITES_ENV = "ASSISTANT_CI_SUITES"  # the suites it runs: a comma list or "all"; "" none; unset: its tests skip
SUITES_OUT_ENV = "ASSISTANT_CI_SUITES_OUT"  # their per-suite results ({"suites": {...}}), merged into this file
SUITES_DIR_ENV = "ASSISTANT_CI_SUITES_DIR"  # their run dirs go here: the CI run dir (short paths, reaped with it)


def _real_home_suites() -> set[str]:
    """~/opt/test-all/real-home.txt: the <suite>-tests that run on the real home (one at a time)."""
    out = set()
    try:
        for ln in (paths.real_home() / "opt/test-all/real-home.txt").read_text().splitlines():
            if ln.strip() and not ln.startswith("#"):
                out.add(ln.split("#")[0].strip())
    except OSError:
        pass
    return out


def suite_plan(tree: Path, changed: list[str] | None, where: str = "all", base: str | None = None):
    """The tool suites a CI run with these changed paths runs on this side (where: all | runner | host; the other
    side runs the rest), in manifest order -> (names, how many the other side runs, the selection: legacy_select)."""
    man = load_manifest(tree)
    picked: dict = {}
    hb = host_suites(tree, _real_home_suites()) if where != "all" else set()
    names, elsewhere = [], 0
    for e in _legacy_selected(man, changed, tree, base, picked):
        if where != "all" and (e["suite"] in hb) != (where == "host"):
            elsewhere += 1
            continue
        names.append(e["suite"])
    return names, elsewhere, picked


def shell_suites(tree: Path) -> list[str]:
    """The tool suites still in shell form: a tests/legacy/<suite>/ with a run.sh or run.py, and every manifest suite
    (one still under ~/opt too). All become native pytest (tests/legacy/README.md): this count goes to 0."""
    d = tree / "tests" / "legacy"
    out = {x.name for x in d.iterdir() if (x / "run.sh").is_file() or (x / "run.py").is_file()} if d.is_dir() else set()
    try:
        out |= {e["suite"] for e in load_manifest(tree)["file"] if e.get("suite")}
    except Fail:
        pass
    return sorted(out)


def _suite_tag(picked: dict, n: int, elsewhere: int = 0, where: str = "all") -> str:
    """A suite selection in one line: "impacted 3/151 (why); 9 on the runner"."""
    side = {"runner": "the host", "host": "the runner"}.get(where, "")
    more = f"; {elsewhere} on {side}" if elsewhere and side else ""
    return f"{picked.get('mode')} {n}/{picked.get('of')} ({picked.get('why')}){more}"


def _legacy_slow_first(tree: Path) -> dict[str, int]:
    """tests/legacy/slow.txt -> {suite: rank}: one suite per line (slowest first), # comments."""
    try:
        text = tree.joinpath(*LEGACY_SLOW_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    names = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
    return {n: i for i, n in enumerate(x for x in names if x)}


def _ci_legacy(
    tree: Path,
    changed: list[str] | None,
    tmp: Path,
    only: set[str] | None = None,
    results: dict | None = None,
    where: str = "all",
    base: str | None = None,
    sel: dict | None = None,
) -> tuple[str, bool, str]:
    """only: of the selected suites, run just these. results: filled per suite with {"ok", "detail", "src"}.
    where: all; runner = not the host-bound suites (host_suites()); host = only those. base: the commit changed is
    relative to (legacy_select reads the manifest and runner-skip.txt there). sel: filled with the selection."""
    try:
        man = load_manifest(tree)
    except Fail as e:
        return ("legacy", False, str(e))
    overlay = tmp / "overlay"
    (overlay / "lib").mkdir(parents=True)
    os.symlink(tree / "core", overlay / "lib" / "core")  # overlay scripts import the tree's core
    os.symlink(tree / "packages" / "inbox", overlay / "lib" / "inbox")  # ... and its ledger package (inbox.writer)
    ran, bad = [], []
    rh = paths.real_home()
    # ... and the rest of the would-be ~/bin/lib (a script imports its own lib/ first): the tree's managed lib modules
    # (botlib, memrows, loop, ...) rendered as they deploy, so a branch that changes a lib and its callers together is
    # tested as it deploys, not against the live lib; the live unmanaged helpers linked
    _overlay_lib(tree, man, overlay / "lib", rh / "bin" / "lib")
    real_home_suites = _real_home_suites()
    res = results if results is not None else {}
    hb = host_suites(tree, real_home_suites)
    elsewhere = 0
    reshebang = not os.path.exists("/usr/bin/python3")  # the runner: no system python for the scripts' shebang
    for e in man["file"]:
        if e["dest"] in OVERLAY_ALWAYS:
            _overlay_stage(tree, e, overlay / os.path.basename(e["dest"]), reshebang)
    jobs: list[dict] = []  # the suites to run, prepared in manifest order (their results keep that order)
    picked: dict = {}
    for e in _legacy_selected(man, changed, tree, base, picked):
        suite = e["suite"]
        if only is not None and suite not in only:
            continue
        if where != "all" and (suite in hb) != (where == "host"):
            elsewhere += 1  # the other side runs it
            continue
        sdir = tree / "tests" / "legacy" / suite  # a suite folded into the repo wins over ~/opt
        if not (sdir / "run.sh").is_file():
            sdir = rh / "opt" / f"{suite}-tests"
        if not (sdir / "run.sh").is_file():
            bad.append((suite, f"no suite at tests/legacy/{suite}/run.sh or ~/opt/{suite}-tests/run.sh"))
            res[suite] = {"ok": False, "detail": "no suite", "src": e["src"]}
            continue
        name = os.path.basename(e["dest"])
        q = overlay / name
        _overlay_stage(tree, e, q, reshebang)
        t = tmp / f"legacy-{suite}"
        t.mkdir()
        real = f"{suite}-tests" in real_home_suites
        home = rh if real else _temp_home(t)
        seam = e.get("seam") or re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_BIN"
        env = _guarded_env(t, home, {seam: str(q), "ASSISTANT_CI": "1", "ASSISTANT_CI_TREE": str(tree)})
        env.pop("ASSISTANT_TEST", None)  # legacy suites run as test-all runs them
        jobs.append({"suite": suite, "src": e["src"], "sdir": sdir, "t": t, "env": env, "real": real, "home": home})
    # Every suite in its own process group with its own HOME, TMPDIR and guard log, up to legacy_workers() at once
    # (serial: 993 s for all on the runner, 29.10); suites on the real home one at a time (they share it). Slow
    # suites start first (tests/legacy/slow.txt), so the longest one does not start last.
    first = _legacy_slow_first(tree)
    order = {j["suite"]: n for n, j in enumerate(jobs)}
    jobs.sort(key=lambda j: (j["suite"] not in first, first.get(j["suite"], 0)))
    nw = legacy_workers(len(jobs))
    pending, running = list(jobs), []
    try:
        while pending or running:
            i = 0
            while len(running) < nw and i < len(pending):
                j = pending[i]
                if j["real"] and any(r["real"] for r in running):
                    i += 1
                    continue
                pending.pop(i)
                env = j["env"]
                # a real-home suite assumes its mktemp dirs lie outside $HOME (scripts show home paths as ~/...):
                # with the run dir under ~/.cache/aci (CI_ROOT) they would not, so its TMPDIR stays in /tmp
                if j["home"] == rh and _under_dir(j["t"], rh):
                    j["rtmp"] = env["TMPDIR"] = tempfile.mkdtemp(prefix="aci-legacy-", dir="/tmp")
                cmd = ["timeout", "-k", "10", str(int(LEGACY_TIMEOUT)), "bash", "run.sh"]
                j["run"] = BgGroup(cmd, cwd=j["sdir"], env=env, timeout=LEGACY_TIMEOUT + 30, grace=10.0)
                running.append(j)
            ended = [r for r in running if r["run"].done.is_set()]
            if not ended:
                time.sleep(0.2)
                continue
            for j in ended:
                running.remove(j)
                if j.get("rtmp"):
                    shutil.rmtree(j["rtmp"], ignore_errors=True)
                try:
                    r = j["run"].wait()
                    rc, out, err = r.returncode, r.stdout or "", r.stderr or ""
                except subprocess.TimeoutExpired as te:
                    rc, out, err = 124, str(te.output or ""), str(te.stderr or "")
                j.update(rc=rc, out=out, err=err, secs=round(j["run"].secs, 1))
    finally:
        for j in running:  # an exception here (CiStopped): stop what still runs
            j["run"].stop()
            if j.get("rtmp"):
                shutil.rmtree(j["rtmp"], ignore_errors=True)
    for j in sorted(jobs, key=lambda j: order[j["suite"]]):  # results in manifest order, whatever ran first
        suite, rc, secs = j["suite"], j["rc"], j["secs"]
        hits = _guard_hits(j["t"])
        ran.append(suite)
        if rc != 0 or hits:
            tail = [ln for ln in j["out"].splitlines() if "FAIL" in ln][:3]
            why = (
                f"exit {rc}"
                + (" (timed out)" if rc == 124 else "")
                + (f", {len(hits)} blocked" if hits else "")
                + (" " + " | ".join(tail) if tail else "")
            )
            bad.append((suite, why))
            res[suite] = {"ok": False, "detail": why[:300], "src": j["src"], "secs": secs,
                          "tail": _legacy_tail(j["out"], j["err"]),
                          "fails": [ln.strip() for ln in j["out"].splitlines() if "FAIL" in ln][:20]}  # fmt: skip
        else:
            res[suite] = {"ok": True, "detail": "", "src": j["src"], "secs": secs}
    side = {"runner": "the host", "host": "the runner"}.get(where, "")
    more = f"; {elsewhere} on {side}" if elsewhere else ""
    if sel is not None:
        sel.update(picked)
    n_sel = picked["of"] if picked.get("mode") == "all" else len(picked.get("suites") or [])
    tag = _suite_tag(picked, n_sel)
    # the red suites' names first: a long FAIL tail must not push them out of the (truncated) detail
    red = f"{len(bad)} red: {', '.join(s for s, _ in bad)}; " + "; ".join(f"{s}: {w}" for s, w in bad) if bad else ""
    return (
        "legacy",
        not bad,
        red[:400]
        or (f"{tag}: ran {', '.join(ran)}" if ran else f"{tag}: none" if not elsewhere else f"{tag}: none here") + more,
    )


LEGACY_TAIL = 400  # a red suite's tail in the step record: the last stderr lines (a traceback's end), else stdout's
LEGACY_TAILS = 5500  # all tails together (hub/hubci.py signs a step extra up to 6000 JSON chars)


def _legacy_tail(out: str, err: str, n: int = 6) -> str:
    """A red suite's why: its last n non-empty stderr lines (where a missing tool or a traceback shows), plus its
    first FAIL line from stdout; stdout's last lines when stderr is empty."""
    e = [ln.strip() for ln in err.splitlines() if ln.strip()][-n:]
    o = [ln.strip() for ln in out.splitlines() if ln.strip()]
    fail = [ln for ln in o if "FAIL" in ln][:1]
    lines = (fail + e) if e else (fail + [ln for ln in o[-n:] if ln not in fail])
    return " | ".join(lines)[-LEGACY_TAIL:]


def legacy_tails(res: dict) -> dict:
    """{suite: tail} of the red suites, each cut so all fit in LEGACY_TAILS (the record's limit for one extra)."""
    red = {s: str(x.get("tail") or "") for s, x in res.items() if not x.get("ok") and x.get("tail")}
    if not red:
        return {}
    each = max(60, min(LEGACY_TAIL, LEGACY_TAILS // len(red) - 30))  # 30: the name, quotes and separators
    out = {s: t[-each:] for s, t in red.items()}
    while out and len(json.dumps(out)) > LEGACY_TAILS:
        out.pop(max(out, key=lambda k: len(out[k])))
    return out


def _reshebang(data: bytes) -> bytes:
    """A staged script's `#!/usr/bin/python3 [flags]` -> this interpreter (the runner has no /usr/bin/python3)."""
    if not data.startswith(b"#!/usr/bin/python3"):
        return data
    first, nl, rest = data.partition(b"\n")
    return b"#!" + sys.executable.encode() + first[len(b"#!/usr/bin/python3") :] + nl + rest


def _skip_lines(tree: Path) -> list[tuple[str, list[str]]]:
    """tests/runner-skip.txt -> [(path or node id, tags)]."""
    try:
        text = tree.joinpath(*RUNNER_SKIP).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for ln in text.splitlines():
        words = ln.split("#", 1)[0].split()
        if words:
            out.append((words[0], [w[1:] for w in words[1:] if w.startswith("@")]))
    return out


def host_suites(tree: Path, real_home: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """The legacy suites only this host can run: an untagged `tests/legacy/<suite>` line in tests/runner-skip.txt, a
    suite still under ~/opt (no tests/legacy/<suite>/run.sh in the tree) or a real-home one (~/opt/test-all)."""
    out = {p.split("/")[2] for p, tags in _skip_lines(tree) if p.startswith("tests/legacy/") and p.count("/") == 2
           and not tags}  # fmt: skip
    out |= {s[: -len("-tests")] for s in real_home if s.endswith("-tests")}
    try:
        man = load_manifest(tree)
    except Fail:
        return out
    legacy = tree / "tests" / "legacy"
    return out | {e["suite"] for e in man["file"] if e.get("suite") and not (legacy / e["suite"] / "run.sh").is_file()}


def smoke_tests(tree: Path) -> dict:
    """`ci --host`: the local smoke set = the host-bound tests: the untagged lines of tests/runner-skip.txt, legacy
    suite dirs aside."""
    files = [p for p, tags in _skip_lines(tree) if not tags and not p.startswith("tests/legacy/")
             and (tree / p.split("::", 1)[0]).is_file()]  # fmt: skip
    return {"mode": "smoke", "why": "host-bound tests, tests/runner-skip.txt", "of": len(files), "files": files}


def _precheck_home(tree: Path, root: Path) -> Path:
    """The would-be home after a land, for `codebase-sync precheck`: ~/bin mirrored with hard links (same
    filesystem as the state dir; the gate scans bin/, so managed files must be checked where they will live) with
    the tree's managed files rendered in; everything else codebase-sync reads is a symlink to the real home."""
    rh = paths.real_home()
    h = root / "home"
    h.mkdir(parents=True)
    for name in (".claude", "crontab", "boot.sh", "workspace", "plugins"):
        if (rh / name).exists():
            os.symlink(rh / name, h / name)
    (h / "opt").mkdir()
    for name in os.listdir(rh / "opt"):
        if name != "assistant":
            os.symlink(rh / "opt" / name, h / "opt" / name)
    src_bin = rh / "bin"
    for dp, dns, fns in os.walk(src_bin):
        out = h / "bin" / os.path.relpath(dp, src_bin)
        out.mkdir(parents=True, exist_ok=True)
        for d in list(dns):
            if os.path.islink(os.path.join(dp, d)) or d.endswith(".releases") or d == "__pycache__":
                dns.remove(d)
                if os.path.islink(os.path.join(dp, d)):
                    os.symlink(os.readlink(os.path.join(dp, d)), out / d)
        for f in fns:
            sp = os.path.join(dp, f)
            if os.path.islink(sp):
                os.symlink(os.readlink(sp), out / f)
            else:
                try:
                    os.link(sp, out / f)
                except OSError:
                    shutil.copy2(sp, out / f)
    for e in load_manifest(tree)["file"]:
        if e["dest"].startswith("bin/"):
            q = h / e["dest"]
            if q.is_symlink() or q.exists():
                q.unlink()
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_bytes(render((tree / e["src"]).read_bytes(), bool(e["header"])))
            os.chmod(q, int(e["mode"], 8))
    return h


PRECHECK_NONE = "no would-be ~/bin file changed"


def _precheck_only(tree: Path, changed: list[str] | None) -> list[str] | None:
    """The would-be ~/bin files (manifest dests) the change touches: what a diff-only precheck exports and checks
    (the rest of the home is the same before and after the land; the nightly codebase-sync checks it). None = the
    whole home: no base, or the manifest itself changed (renders, modes, dests)."""
    if changed is None or "/".join(MANIFEST) in changed or "scripts/codebase-sync" in changed:
        return None  # ... or the precheck's own tool changed
    ch = set(changed)
    return sorted(e["dest"] for e in load_manifest(tree)["file"] if e["dest"].startswith("bin/") and e["src"] in ch)


def _ci_precheck_start(tree: Path, changed: list[str] | None = None) -> tuple[BgGroup | None, Path, str]:
    """`codebase-sync precheck` on the would-be home, started in the background so it runs beside the other steps:
    (its run or None, the overlay root to remove, why it did not start; PRECHECK_NONE = nothing to check).
    changed: the branch's changed paths; only the would-be ~/bin files among them are exported and checked
    (CODEBASE_SYNC_ONLY: seconds); None = the whole would-be home (~100 s)."""
    root = st("ci-overlay", str(os.getpid()))
    try:
        if root.exists():
            shutil.rmtree(root)
        only = _precheck_only(tree, changed)
        if only == []:
            return None, root, PRECHECK_NONE
        h = _precheck_home(tree, root)
        env = dict(os.environ, CODEBASE_SYNC_HOME=str(h), CODEBASE_SYNC_ASSISTANT_SRC=str(tree))
        if only is not None:
            (root / "only.txt").write_text("".join(f"{h / d}\n" for d in only), encoding="utf-8")
            env["CODEBASE_SYNC_ONLY"] = str(root / "only.txt")
        cmd = [str(paths.real_home() / "bin" / "codebase-sync"), "precheck"]
        return BgGroup(cmd, env=env, timeout=600, grace=10.0), root, ""
    except Fail as e:
        shutil.rmtree(root, ignore_errors=True)
        return None, root, str(e)


def _ci_precheck(
    tree: Path, base: str | None = None, started: tuple[BgGroup | None, Path, str] | None = None
) -> tuple[str, bool, str]:
    g, root, why = started or _ci_precheck_start(tree)
    try:
        if g is None and why == PRECHECK_NONE:
            pok, pdetail = _ci_publish_scan(tree, base)
            return ("precheck", pok, f"{why}; {pdetail}")
        if g is None:
            return ("precheck", False, why)
        r = g.wait()
    finally:
        if g is not None:
            g.stop()
        shutil.rmtree(root, ignore_errors=True)
    lines = [ln.strip() for ln in (r.stdout + r.stderr).splitlines() if re.match(r"^ +\S+:\d+ ", ln)]
    ok = r.returncode == 0
    detail = " | ".join(lines[:4])[:300] or f"exit {r.returncode} (would-be ~/bin)"
    pok, pdetail = _ci_publish_scan(tree, base)
    return ("precheck", ok and pok, f"{detail}; {pdetail}" if pok or not ok else pdetail)


def _ci_publish_scan(tree: Path, base: str | None) -> tuple[bool, str]:
    """The branch's new commits (base..HEAD) through the tree's own `assistant-publish scan --no-identity`, so a
    privacy hit blocks the land, not only the publish after it (identities are mapped at publish time)."""
    if not base:
        return True, "publish scan: no base"
    n = run(["git", "-C", str(tree), "rev-list", "--count", f"{base}..HEAD"]).stdout.strip()
    if n in ("", "0"):
        return True, "publish scan: no new commits"
    exe = test_seam("ASSISTANT_PUBLISH_BIN")
    if exe is None:
        if paths.test_mode():
            return True, "publish scan: skipped (test)"
        exe = str(tree / "scripts" / "assistant-publish")
    try:
        r = run([exe, "scan", "--repo", str(tree), "--rev", f"{base}..HEAD", "--no-identity"], timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"publish scan could not run ({type(e).__name__})"
    if r.returncode == 0:
        return True, f"publish scan: {n} commit(s) clean"
    hits = [ln.strip() for ln in r.stdout.splitlines() if ln.startswith("  ") and not ln.startswith("  allowed ")]
    if r.returncode == 3:
        return False, (
            "publish scan BLOCKS (fix, or `assistant-publish allow HASH --reason ...`): " + " | ".join(hits[:4])
        )[:400]
    return (
        False,
        f"publish scan could not run (exit {r.returncode}): {(r.stderr.strip().splitlines() or [''])[-1][:200]}",
    )


def _ci_schemas(tree: Path, tracked: list[str], py: Path, isolated: bool = True) -> tuple[str, bool, str]:
    files = [t for t in tracked if t.startswith("schemas/") and t.endswith(".json")]
    if not files:
        return ("schemas", True, "none yet")
    code = (
        "import json,sys,jsonschema\nbad=[]\nfor p in sys.argv[1:]:\n"
        "    try:\n        d=json.load(open(p))\n"
        "        if isinstance(d,dict) and ('$schema' in d or p.endswith('.schema.json')):\n"
        "            jsonschema.validators.validator_for(d).check_schema(d)\n"
        "    except Exception as e:\n        bad.append(p.rsplit('/',1)[-1]+': '+str(e).splitlines()[0][:80])\n"
        "print(' | '.join(bad)); sys.exit(1 if bad else 0)\n"
    )
    r = run([str(py), "-I" if isolated else "-s", "-c", code, *[str(tree / f) for f in files]])
    if r.returncode != 0:
        return ("schemas", False, (r.stdout.strip() or (r.stderr.strip().splitlines() or ["?"])[-1])[:300])
    return ("schemas", True, f"{len(files)} valid")


def _ci_ratchet(tree: Path) -> tuple[str, bool, str]:
    try:
        man = load_manifest(tree)
    except Fail as e:
        return ("ratchet", False, str(e))
    base = read_json(tree / "ops" / "lint" / "bin-baseline.json", {}).get("counts")
    if not base:
        return ("ratchet", False, "ops/lint/bin-baseline.json missing")
    over = {
        e["dest"][4:]: render((tree / e["src"]).read_bytes(), bool(e["header"]))
        for e in man["file"]
        if e["dest"].startswith("bin/")
    }
    bdir = paths.real_home() / "bin"
    counts = ratchet_counts(bdir, over, ratchet_exempt(tree))["counts"]
    up = [f"{c} {counts[c]} > {base[c]}" for c in RATCHET if c in base and counts[c] > base[c]]
    return ("ratchet", not up, " | ".join(up) or " ".join(f"{c}={counts[c]}" for c in RATCHET))


def _runner_home(tree: Path) -> str:
    """The hub runner's real home (ciworker: ASSISTANT_REAL_HOME and nss_wrapper name the job's HOME; it has no brain
    home) starts empty: the would-be ~/bin goes into it, built from the tree alone (_stage_bin_links over no live bin:
    the manifest's bin/ files rendered, core linked), so the legacy suites' temp HOMEs, the ratchet and inbox-lint see
    what a deploy of this tree installs. The ci image's venvs and voices (ops/hub/ci/Dockerfile: /opt/ci-heavy/<name>)
    are linked in as ~/opt/<name>, where the scripts look for them. Never on a host with a real home. -> a status line,
    "" if nothing was done."""
    rh = paths.real_home()
    if not os.environ.get("ASSISTANT_CI_TOOLCHAIN") or not rh.is_dir() or (rh / "bin").exists():
        return ""
    if str(rh) != os.path.realpath(os.environ.get("HOME", "")):  # only the job's own HOME, as ciworker names it
        return ""
    managed, _ = _stage_bin_links(tree, rh / ".no-live-bin", rh / "bin")
    heavy = Path(os.environ.get("ASSISTANT_CI_HEAVY", "/opt/ci-heavy"))  # the ci image's venvs and voices (Dockerfile)
    opt = [d for d in sorted(heavy.glob("*")) if d.is_dir() and d.name != "requirements"] if heavy.is_dir() else []
    for d in opt:
        (rh / "opt").mkdir(exist_ok=True)
        with contextlib.suppress(FileExistsError):
            (rh / "opt" / d.name).symlink_to(d)
    more = f", ~/opt: {len(opt)} from the image" if opt else ""
    return f"runner home: would-be ~/bin built from the tree ({len(managed)} managed files{more})"


def _stage_bin_links(tree: Path, bdir: Path, out: Path) -> tuple[set[str], int]:
    """The would-be ~/bin for read-only lints, made of symlinks (nothing is copied): every live file links to its
    absolute real path, so relative links such as ~/bin/channel-check (../plugins/...) still resolve once moved;
    dangling links, __pycache__, dot dirs and *.releases are left out; the manifest's bin/ files are rendered from
    the tree on top and the [core] dest links to the tree's core. -> (managed paths relative to bin incl. the core
    dest, dangling links skipped)"""
    man = load_manifest(tree)
    core = man.get("core")
    core_rel = core["dest"][4:] if core and core["dest"].startswith("bin/") else None
    out.mkdir(parents=True)
    dangling = 0
    for dp, dns, fns in os.walk(bdir):  # dir symlinks are listed in dns and not descended into
        rel_dir = os.path.relpath(dp, bdir)
        o = out / rel_dir
        o.mkdir(parents=True, exist_ok=True)
        for d in list(dns):
            sp = os.path.join(dp, d)
            if d == "__pycache__" or d.startswith(".") or d.endswith(".releases") or os.path.islink(sp):
                dns.remove(d)
                if os.path.islink(sp) and os.path.normpath(os.path.join(rel_dir, d)) != core_rel:
                    real = os.path.realpath(sp)
                    if os.path.isdir(real):
                        os.symlink(real, o / d)
                    else:
                        dangling += 1
        for f in fns:
            real = os.path.realpath(os.path.join(dp, f))
            if os.path.exists(real):
                os.symlink(real, o / f)
            else:
                dangling += 1

    def place(rel: str) -> Path:
        """A fresh path in the stage for rel; never writes through a staged dir link into a live dir."""
        q = out
        for part in Path(rel).parts[:-1]:
            q = q / part
            if q.is_symlink():
                raise Fail(f"{rel} sits under a linked live dir ({part}); refusing to stage over it")
            q.mkdir(exist_ok=True)
        q = q / Path(rel).name
        if q.is_symlink() or q.exists():
            q.unlink()
        return q

    managed = set()
    for e in man["file"]:
        if e["dest"].startswith("bin/"):
            rel = e["dest"][4:]
            q = place(rel)
            q.write_bytes(render((tree / e["src"]).read_bytes(), bool(e["header"])))
            os.chmod(q, int(e["mode"], 8))
            managed.add(rel)
    if core_rel:
        os.symlink(tree / core["src"], place(core_rel))
        managed.add(core_rel)
    return managed, dangling


def _ci_inbox_lint(
    tree: Path, tmp: Path, bdir: Path | None = None, linter: Path | None = None
) -> tuple[str, bool, str]:
    """inbox-lint (~/opt/inbox-lint-tests) over the would-be ~/bin: live ~/bin with the tree's managed files on top.
    Fails closed when the linter is missing. The crontab is not the tree's; test-all's inbox-lint suite covers it."""
    rh = paths.real_home()
    bdir = bdir or Path(test_seam("ASSISTANT_LIVE_BIN") or rh / "bin")
    if linter is None:
        mine = tree.joinpath(*INBOX_LINT_TREE)
        linter = Path(test_seam("ASSISTANT_INBOX_LINT") or (mine if mine.is_file() else rh.joinpath(*INBOX_LINT)))
    if not linter.is_file():
        return ("inbox-lint", False, f"no linter at {linter}")
    try:
        managed, dangling = _stage_bin_links(tree, bdir, tmp / "bin")
        r = run([sys.executable, "-I", str(linter), "--bin", str(tmp / "bin"), "--crontab", ""], timeout=120)
    except (Fail, OSError) as e:
        return ("inbox-lint", False, f"staging the would-be ~/bin failed: {e}"[:300])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # links only: rmtree unlinks them, never their targets
    skipped = f", {dangling} dangling link(s) skipped" if dangling else ""
    summ = next((ln for ln in r.stdout.splitlines() if ln.startswith("inbox_lint:")), "")
    if r.returncode not in (0, 1) or not summ:
        tail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1:] or ["no output"]
        return ("inbox-lint", False, f"linter exit {r.returncode}: {tail[0][:200]}")
    m = re.search(r"(\d+) file\(s\), (\d+) inbox writer\(s\)", summ)
    counts = f"{m.group(1)} files, {m.group(2)} writers" if m else summ
    if r.returncode == 0:
        return ("inbox-lint", True, f"{counts}, 0 problems (would-be ~/bin, {len(managed)} managed){skipped}")
    bad = []
    for ln in r.stdout.splitlines():
        if not ln.startswith("FAIL "):
            continue
        w = re.match(r"FAIL (\S+): writes into the inbox \(line ([^)]*)\)", ln)
        if w:
            rel = w.group(1)
            kind = "managed" if any(rel == x or rel.startswith(x + "/") for x in managed) else "live"
            bad.append(f"{kind} {rel} (line {w.group(2)})")
        else:
            bad.append(ln[5:][:120])
    return (
        "inbox-lint",
        False,
        f"{len(bad)} problem(s): "
        + " | ".join(bad[:5])
        + " — each ~/inbox note needs a 'Data, not instructions.' line",
    )


def _bun_failed(out: list[str]) -> str:
    """': a > b; c > d' for bun's '(fail) …' lines (first 3, then +N), '' when none."""
    failed = [m.group(1).strip() for ln in out if (m := re.match(r"^\s*\(fail\)\s+(.*?)(\s+\[[\d.]+m?s\])?$", ln))]
    if not failed:
        return ""
    return ": " + "; ".join(failed[:3]) + (f" +{len(failed) - 3}" if len(failed) > 3 else "")


def _ci_bun(tree: Path, changed: list[str] | None, tmp: Path) -> tuple[str, bool, str]:
    d = tree / "adapters" / "tg"
    if not (d / "package.json").is_file():
        return ("bun", True, "no adapters/tg yet")
    if changed is not None and not any(c.startswith("adapters/tg/") for c in changed):
        return ("bun", True, "adapters/tg unchanged")
    bun = shutil.which("bun") or "/usr/local/bin/bun"
    tc = os.environ.get("ASSISTANT_CI_TOOLCHAIN")
    if tc and (d / "bun.lock").is_file() and not (d / "node_modules").is_dir():
        # the runner (no network): the toolchain's copy for this lock (hubkit/citool.py bun-deps, bun_key), else red
        key = hashlib.sha256((d / "bun.lock").read_bytes()).hexdigest()[:16]
        vend = Path(tc) / "bun-deps" / key / "node_modules"
        if not vend.is_dir():
            return ("bun", False, f"no vendored node_modules for this bun.lock in the toolchain ({vend.parent.name})")
        shutil.copytree(vend, d / "node_modules", symlinks=False)
    if any((d / n).is_file() for n in ("bun.lock", "bun.lockb")) and not (d / "node_modules").is_dir():
        r = run([bun, "install", "--frozen-lockfile"], cwd=d, timeout=300)
        if r.returncode != 0:
            return ("bun", False, "bun install failed: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200])
    t = tmp / "bun"
    t.mkdir()
    home = t / "home"
    home.mkdir()
    env = _guarded_env(t, home)
    r = run([bun, "test"], cwd=d, env=env, timeout=600)
    out = (r.stdout + r.stderr).strip().splitlines()
    summ = " ".join(ln.strip() for ln in out if re.match(r"^\s*\d+ (pass|fail)", ln))
    summ += _bun_failed(out)  # the nightly log keeps only this line
    hits = _guard_hits(t)
    return (
        "bun",
        r.returncode == 0 and not hits,
        (summ or (out[-1] if out else ""))[:400] + (f"; {len(hits)} blocked" if hits else ""),
    )


# ---------------------------------------------------------------- status
def status(as_json: bool) -> int:
    deployed = read_json(st("deployed.json"), {})
    drift = drift_report(deployed) if deployed else []
    hist = history()
    deploys = [h for h in hist if h.get("op") in ("deploy", "land")]
    metric = {
        "deploys_ok": sum(1 for h in deploys if h.get("ok")),
        "rollbacks": sum(1 for h in hist if h.get("op") == "rollback" or h.get("rolled_back")),
        "drift_refusals": sum(1 for h in hist if h.get("refused") == "drift"),
        "drift_now": len(drift),
    }
    kept = sorted(d.name for d in st("releases").iterdir()) if st("releases").is_dir() else []
    info = {
        "release": deployed.get("release"),
        "deployed": deployed.get("ts"),
        "files": sorted(deployed.get("files", {})),
        "core": deployed.get("core"),
        "drift": drift,
        "lock": lock_holder(),
        "queue": [
            {"pos": i + 1, "pid": t.get("pid"), "what": t.get("what"), "since": t.get("since")}
            for i, t in enumerate(lock_queue(prune=False))
        ],
        "legacy_red": legacy_red(),
        "ci_blockers": _ci_current(),
        "kept": kept,
        "metric": metric,
        "recent": hist[-5:],
    }
    if as_json:
        say(json.dumps(info, sort_keys=True))
        return 3 if drift else 0
    if not deployed:
        say("nothing deployed yet")
    else:
        when = time.strftime("%d.%m %H:%M", time.localtime(deployed["ts"]))
        say(
            f"release {deployed['release']} deployed {when}; {len(info['files'])} managed file(s)"
            + (f" + core ({deployed['core']['dest']})" if deployed.get("core") else "")
        )
    say("drift: " + (", ".join(f"{d['dest']} ({d['why']})" for d in drift) if drift else "none"))
    say(f"lock: {info['lock'] or 'free'} · kept releases: {', '.join(kept) or '-'}")
    if info["queue"]:
        say("queue: " + ", ".join(f"{q['pos']}. {q['what']} (pid {q['pid']})" for q in info["queue"]))
    say(
        "tool suites red on main: "
        + (
            ", ".join(f"{s} (since {_since(r)}, {r.get('how')})" for s, r in sorted(info["legacy_red"].items()))
            or "none"
        )
    )
    say(
        "shared CI blockers: "
        + (", ".join(f"{e.get('key')} ({e.get('branch') or '?'})" for e in info["ci_blockers"]) or "none")
        + " (`assistant ci-status`)"
    )
    say("metric: {deploys_ok} ok deploys, {rollbacks} rollbacks, {drift_refusals} drift refusals".format(**metric))
    for h in hist[-5:]:
        t = time.strftime("%d.%m %H:%M", time.localtime(h.get("ts", 0)))
        say(
            f"  {t} {h.get('op')} {h.get('release') or h.get('name') or h.get('branch') or ''} "
            f"{'ok' if h.get('ok') else 'FAILED ' + str(h.get('why') or h.get('refused') or '')}"
        )
    return 3 if drift else 0


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="assistant", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("ci")
    p.add_argument("--path", default=".")
    p.add_argument("--base")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--skip", default="")
    p.add_argument("--only", default="")
    p.add_argument("--no-stamp", action="store_true")
    p.add_argument("--runner", choices=("local", "hub"), default="local")
    p.add_argument("--wait", type=float, default=RUNNER_WAIT_ENFORCE)
    p.add_argument("--toolchain", default=os.environ.get("ASSISTANT_CI_TOOLCHAIN") or None)
    p.add_argument("--report")
    p.add_argument("--full", action="store_true", help="every pytest file (no impact selection)")
    p.add_argument("--host", action="store_true", help="a land's host-bound part (the runner runs the rest)")
    p = sub.add_parser("ci-nightly", help="the nightly full suite on main; red -> one ~/inbox note")
    p.add_argument("--no-note", action="store_true")
    p = sub.add_parser("legacy-main", help="every tool suite on main (after a land that ran a selection)")
    p.add_argument("--after", default="", help="the landed branch (for the record)")
    p.add_argument("--no-note", action="store_true")
    p = sub.add_parser("ci-status", help="shared CI blockers now (what stops every land) + 24 h blocked time")
    p.add_argument("--json", action="store_true")
    p.add_argument("--clear", metavar="KEY|all")
    p = sub.add_parser("land", help="land BRANCH, or `land status [qN|BRANCH]` (the land queue)")
    p.add_argument("branch", nargs="?")
    p.add_argument("ref", nargs="?", help="land status: one entry (qN) or branch")
    p.add_argument("--adopt", action="append", default=[])
    p.add_argument("--override")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--wait", dest="wait", action="store_true", default=None, help="queue on: follow it (default)")
    g.add_argument("--no-wait", dest="wait", action="store_false", help="queue on: return once queued")
    p.add_argument("--timeout", type=float, default=LANDQ_WAIT, help="--wait's budget in s")
    p.add_argument("--json", action="store_true", help="land status as JSON")
    p.add_argument("--batch", help=argparse.SUPPRESS)  # the queue worker's child
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--cron", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("ci-gate")
    p.add_argument("action", choices=("status",) + GATE_MODES)
    p.add_argument("--reason", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--lands", type=int, default=20, help="status: the gated-land share over the last N lands")
    p = sub.add_parser("deploy")
    p.add_argument("--rev")
    p.add_argument("--adopt", action="append", default=[])
    p.add_argument("--force", action="store_true")
    p.add_argument("--target", choices=("bin", "hub"), default="bin")
    p.add_argument("--plan", action="store_true")
    p.add_argument("--wait", type=float)
    p.add_argument("--wait-calls", type=float, metavar="MIN")
    p = sub.add_parser("rollback")
    p.add_argument("--to")
    p.add_argument("--force", action="store_true")
    p.add_argument("--target", choices=("bin", "hub"), default="bin")
    p.add_argument("--plan", action="store_true")
    p.add_argument("--wait", type=float)
    p = sub.add_parser("drift")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("cron")
    p.add_argument("action", choices=("list", "add", "rm", "tag", "check", "install"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--features")
    p.add_argument("--json", action="store_true")
    p.add_argument("name", nargs="?", default="")
    p.add_argument("schedule", nargs="?", default="")
    p.add_argument("command", nargs="?", default="")
    p = sub.add_parser("init", help="install the starter CLAUDE.md, settings, skills and agents (templates/)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("uninstall", help="remove the deployed files and the tagged crontab lines (--dry-run first)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--yes", action="store_true")
    p = sub.add_parser("lint-bin")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true")
    g.add_argument("--update", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir")
    a = ap.parse_args(argv)
    try:
        if a.version:
            d = read_json(st("deployed.json"), {})
            say(f"assistant (release {d.get('release') or 'none'})")
            return 0
        if a.cmd == "ci":
            skip = {s for s in a.skip.split(",") if s}
            only = {s for s in a.only.split(",") if s}
            if (skip | only) - set(CI_STEPS):
                raise Fail(f"unknown step(s): {', '.join(sorted((skip | only) - set(CI_STEPS)))}", 2)
            if only:
                skip |= set(CI_STEPS) - only
            if a.full:
                os.environ["ASSISTANT_CI_SELECT"] = "full"
            if a.runner == "hub":
                return ci_hub(Path(a.path), a.base, a.wait, full=a.full)
            return ci(Path(a.path), a.base, a.offline, skip, not a.no_stamp, a.toolchain, a.report, a.host)
        if a.cmd == "ci-nightly":
            return ci_nightly(not a.no_note)
        if a.cmd == "legacy-main":
            return legacy_main_check(a.after, not a.no_note)
        if a.cmd == "ci-status":
            return ci_status(a.json, a.clear)
        if a.cmd == "ci-gate":
            return ci_gate(a.action, a.reason, a.force, a.json, a.lands)
        if a.cmd == "land":
            return land_cmd(a)
        elif a.cmd in ("deploy", "rollback") and a.target == "hub":
            if getattr(a, "adopt", None):
                raise Fail("--adopt is for the bin target", 2)
            wait = HUB_WAIT if a.wait is None else a.wait
            if a.cmd == "deploy":
                wc = CALL_WAIT_MIN if a.wait_calls is None else a.wait_calls
                if wc < 0:
                    raise Fail("--wait-calls: minutes, 0 or more", 2)
                return deploy_hub(a.rev, a.plan, wait, None if a.force else wc)
            return rollback_hub(a.to, a.plan, wait)
        elif a.cmd in ("deploy", "rollback") and (
            a.plan or a.wait is not None or getattr(a, "wait_calls", None) is not None
        ):
            raise Fail("--plan, --wait and --wait-calls are for --target hub", 2)
        elif a.cmd == "deploy":
            deploy(a.rev, a.adopt, a.force)
        elif a.cmd == "rollback":
            rollback(a.to, a.force)
        elif a.cmd == "drift":
            d = drift_report(read_json(st("deployed.json"), {}))
            say(json.dumps(d) if a.json else ("\n".join(f"{x['dest']}: {x['why']}" for x in d) or "no drift"))
            return 3 if d else 0
        elif a.cmd == "status":
            return status(a.json)
        elif a.cmd == "cron" and a.action == "tag":
            return cron_tag(a.dry_run)
        elif a.cmd == "cron" and a.action == "check":
            return cron_check(a.json)
        elif a.cmd == "cron" and a.action == "install":
            return cron_install(a.features, a.dry_run)
        elif a.cmd == "cron":
            if a.action == "add" and not (a.schedule and a.command):
                raise Fail("usage: assistant cron add NAME 'SCHEDULE' 'COMMAND'", 2)
            cron_edit(a.action, a.name, a.schedule, a.command, dry_run=a.dry_run)
        elif a.cmd == "init":
            return init_cmd(a.force, a.dry_run, a.json)
        elif a.cmd == "uninstall":
            return uninstall(a.dry_run, a.yes)
        elif a.cmd == "lint-bin":
            return lint_bin(
                "check" if a.check else "update" if a.update else "show", a.json, Path(a.dir) if a.dir else None
            )
        else:
            ap.print_help()
            return 2
        return 0
    except Fail as e:
        print(f"assistant: {e}", file=sys.stderr)
        return e.code
    except paths.RealHomeRefused as e:
        print(f"assistant: {e}", file=sys.stderr)
        return 3


def land_cmd(a) -> int:
    """`assistant land ...`: the worker, `land status`, a batch child, the direct land (queue off, --override) or
    the queue's front."""
    if a.worker:
        return landq_worker(a.cron)
    if a.branch == "status":
        if a.wait and a.ref:  # follow a queued entry (one queued with --no-wait) to its end
            e = next((x for x in reversed(_landq_all("entries")) if a.ref in (x["id"], x.get("branch"))), None)
            if e is None:
                raise Fail(f"land queue: no entry {a.ref} (`assistant land status` lists them)", 3)
            return landq_wait(e["id"], a.timeout)
        return landq_status(a.ref, a.json)
    if not a.branch:
        raise Fail(
            "usage: assistant land BRANCH [--wait | --no-wait] | assistant land status [qN | BRANCH] [--json]", 2
        )
    if a.ref:
        raise Fail(f"land: unexpected argument {a.ref!r} (a branch named status can't be landed: rename it)", 2)
    if a.batch:
        return _landq_child(a.branch, a.batch, a.adopt)
    if a.override is not None or not land_queue():
        land(a.branch, a.adopt, a.override)
        return 0
    return _land_front(a.branch, a.adopt, a.wait is not False, a.timeout)


def _home_rel(p: Path, home: Path) -> str:
    return "~/" + str(p.relative_to(home)) if home in p.parents else str(p)


def init_cmd(force: bool, dry_run: bool, as_json: bool) -> int:
    """`assistant init`: core.starter over the repo's templates into $HOME."""
    from core import starter

    repo = paths.repo()
    if not (repo / "templates" / "manifest.toml").is_file():
        raise Fail(f"no templates/manifest.toml in {repo} (set ASSISTANT_REPO to the repo checkout)")
    home = paths.home()
    try:
        results = starter.run(repo, home, force=force, dry_run=dry_run)
        features = starter.profile_module(repo).features_on()
        if any(r.item.kind == "profile" and r.action == "would-install" for r in results):
            features = ["core", *starter.STARTER_ON]  # what the dry run planned with
    except (FileNotFoundError, ValueError) as e:
        raise Fail(f"init: {e}") from None
    except Exception as e:  # assistant_profile.ProfileError: a broken profile names the key, never the value
        if type(e).__name__ != "ProfileError":
            raise
        raise Fail(f"init: {e}") from None
    if as_json:
        say(
            json.dumps(
                {
                    "features": features,
                    "items": [
                        {
                            "action": r.action,
                            "kind": r.item.kind,
                            "name": r.item.name,
                            "dest": _home_rel(r.item.dest, home),
                            "note": r.note,
                        }
                        for r in results
                    ],
                }
            )
        )
        return 0
    for r in results:
        if r.action == "same":
            continue
        dest = _home_rel(r.item.dest, home)
        say(f"{r.action:<13} {r.item.kind:<9} {dest}" + (f"  ({r.note})" if r.note else ""))
    say(starter.summary(results, features))
    return 0


def flush_stdout() -> None:
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        stdout_gone()


def entry(argv=None) -> int:
    """main() with a reader that closes stdout early (`assistant status | head`): no traceback; the command's own
    exit code stands (say() keeps a closed pipe from cutting work short), a pipe that breaks outside say() exits 0."""
    try:
        rc = main(argv)
    except BrokenPipeError:
        stdout_gone()
        return 0
    except SystemExit:  # argparse --help / usage errors: its unflushed text must not fail at interpreter exit
        flush_stdout()
        raise
    flush_stdout()
    return rc


if __name__ == "__main__":
    sys.exit(entry())
