"""core.watchjobs: the watch jobs the hub runs on schedules, a view of the schedule registry (core.schedules,
class W) plus `runnable(name)` for any registry job. Stdlib only.

Each job is its cron line split into its parts: the schedule, the command (argv; a leading ~/ = $HOME; no shell),
where its output goes (a log under ~/.local/state, or nowhere), the state folders the line created first, a time
limit, and `match`: a piece of the cron line's command that finds that line in ~/crontab (two jobs may share one
line: it goes only when both run on the hub). The hub runs a job as job.tick (DBOS schedule "watch:<name>") through the
gateway tool workflow.job, which runs `argv` here, in the brain container, with cron's environment.

  JOBS                      name -> Job
  argv(job, home)           the command with ~/ expanded
  env(name, home)           cron's environment (crontab PATH, HOME, LOGNAME, SHELL, LANG, TZ) + HUB_JOB=name and
                            USAGE_JOB=name (cron-fail's: a text the job sends is credited to job.<name> in the
                            usage store)
  cron_lines(text)          {name: [(line number, line)]}: the ~/crontab lines each job's `match` finds (comments
                            and blank lines skipped)
  cron_match(cron, ts, tz)  does the 5-field cron fire in ts's minute (wall time in tz)
  run_live(name, home, src) one registry job here as workflow.job's live branch runs it -> exit code
  local_tz()                the container's IANA zone name (TZ, else the /etc/localtime link libc and cron use,
                            else /etc/timezone), else "UTC"
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo

from core import schedules


@dataclass(frozen=True)
class Job:
    cron: str
    argv: tuple[str, ...]
    log: str | None  # under ~/.local/state; None = output discarded (as the cron line's >/dev/null)
    match: str
    timeout_s: int = 300
    dirs: tuple[str, ...] = ()  # state folders (under ~/.local/state) the cron line made first
    name: str = ""
    err: str | None = "out"  # "out" = with stdout · a path under ~/.local/state · None = ~/.local/state/jobs/NAME.err
    after: int = 0  # failure streak that emits job.failed (cron-fail's --after); 0 = no failure events
    ok: tuple[int, ...] = ()  # exit codes that count as success (cron-fail's --ok)
    queue: str | None = None  # the DBOS queue (core.schedules.QUEUES)
    env: tuple[tuple[str, str], ...] = ()
    line: str = ""  # the crontab tag of its cron line

    def shown(self) -> str:
        return " ".join(self.argv) + (f" >> ~/.local/state/{self.log}" if self.log else " > /dev/null")


# pieces of each watch job's cron line: they find an untagged line (crontabs without tags, tests)
MATCH = {"watch": "bin/watch run >>", "media-ready": "bin/media ready-watch", "damp-watch": "bin/damp-watch run",
         "window-watch": "bin/window-watch run", "shop-watch": "bin/shop watch --cron",
         "train-watch": "bin/train-watch check", "flight-watch": "bin/flight-watch check",
         "drive-watch": "bin/drive-watch check", "strike-watch": "bin/strike-watch check", "fx-watch": "bin/fx-watch run",  # noqa: E501
         "ha-health-watch": "bin/ha-health-watch --cron"}  # fmt: skip


def job_of(e: schedules.Entry) -> Job:
    return Job(cron=e.cron, argv=e.argv, log=e.out, match=MATCH.get(e.name, f"# assistant:{e.line}"),
               timeout_s=e.timeout, dirs=e.dirs, name=e.name, err=e.err, after=e.after, ok=e.ok, queue=e.queue_name,
               env=e.env, line=e.line)  # fmt: skip


def runnable(name: str) -> Job:
    """Any registry job a schedule may run here: KeyError when unknown, ValueError(why) when it can't move yet."""
    e = schedules.load().entry(name)
    if e is None:
        raise KeyError(name)
    why = e.movable()
    if why:
        raise ValueError(why)
    return job_of(e)


# The watch jobs (registry class W), from core/schedules.toml: their cron lines without the cron-fail
# wrapper (the executor turns failure streaks into job.failed / job.recovered events, core.jobfail).
JOBS: dict[str, Job] = {e.name: job_of(e) for e in schedules.load().jobs.values() if e.cls == "W"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CRON_PATH = "{home}/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"


def argv(job: Job, home: str) -> list[str]:
    return [os.path.join(home, a[2:]) if a.startswith("~/") else a for a in job.argv]


def env(name: str, home: str) -> dict[str, str]:
    user = os.environ.get("LOGNAME") or os.environ.get("USER") or os.path.basename(home.rstrip("/")) or "user"
    e = {
        "HOME": home,
        "LOGNAME": user,
        "USER": user,
        "SHELL": "/bin/sh",
        "PATH": CRON_PATH.format(home=home),
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "HUB_JOB": name,
        "USAGE_JOB": name,
    }
    if os.environ.get("TZ"):
        e["TZ"] = os.environ["TZ"]
    ent = schedules.load().entry(name)
    if ent is not None:
        e.update(dict(ent.env))
    return e


def cron_lines(text: str) -> dict[str, list[tuple[int, str]]]:
    """{watch job: [(line number, line)]}: its tagged line (`# assistant:<line>`), else an untagged line its
    `match` finds."""
    out: dict[str, list[tuple[int, str]]] = {n: [] for n in JOBS}
    for i, raw, _body, tag in schedules.active_lines(text):
        s = raw.strip()
        for n, j in JOBS.items():
            if (tag is not None and tag == j.line) or (tag is None and j.match in s):
                out[n].append((i, s))
    return out


def cron_match(cron: str, ts: float, tz: tzinfo) -> bool:
    """Does the 5-field cron fire in the minute of instant ts (wall time in tz)? Day-of-month/-week as cron ORs them."""
    w = datetime.fromtimestamp(ts, tz)
    f = cron.split()
    if len(f) != 5:
        return False
    vals = [schedules._values(f[0], 0, 59), schedules._values(f[1], 0, 23), schedules._values(f[2], 1, 31),
            schedules._values(f[3], 1, 12), schedules._values(f[4], 0, 7)]  # fmt: skip
    if any(v is None for v in vals):
        return False
    mi, hr, dom, mon, dow = vals
    dow = {0 if x == 7 else x for x in dow}  # type: ignore[union-attr]
    if w.minute not in mi or w.hour not in hr or w.month not in mon:  # type: ignore[operator]
        return False
    wd = (w.weekday() + 1) % 7
    if f[2] == "*" or f[4] == "*":
        return w.day in dom and wd in dow  # type: ignore[operator]
    return w.day in dom or wd in dow  # type: ignore[operator]


def run_live(name: str, home: str, src: str = "sentinel", extra_env: dict[str, str] | None = None) -> int:
    """One registry job here, exactly as workflow.job's live branch runs it (cron's env and log files, cron-fail's
    usage row under src and failure streak under the job's own name). -> exit code. hub-sentinel runs the
    critical hub jobs with this while the hub is down."""
    import subprocess
    import time

    from core import jobfail

    j = runnable(name)
    state = os.path.join(home, ".local", "state")
    err = j.err if j.err not in (None, "out") else (None if j.err == "out" else f"jobs/{name}.err")
    for d in j.dirs + tuple(os.path.dirname(x) for x in (j.log, err) if x):
        os.makedirs(os.path.join(state, d), exist_ok=True)
    t0 = time.monotonic()
    log = open(os.path.join(state, j.log), "ab") if j.log else None  # noqa: SIM115
    efile = open(os.path.join(state, err), "ab" if j.err else "wb") if err else None  # noqa: SIM115
    try:
        out = log if log is not None else subprocess.DEVNULL
        p = subprocess.run(argv(j, home), stdin=subprocess.DEVNULL, stdout=out,
                           stderr=efile if efile is not None else subprocess.STDOUT,
                           env={**env(name, home), **(extra_env or {})}, cwd=home, timeout=j.timeout_s,
                           check=False)  # fmt: skip
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except OSError as e:
        rc = 126 if isinstance(e, PermissionError) else 127
    finally:
        for f in (log, efile):
            if f is not None:
                f.close()
    jobfail.usage_run(name, rc, after=j.after, ok=j.ok, ms=(time.monotonic() - t0) * 1000, src=src)
    jobfail.record(name, rc, after=j.after, ok=j.ok, cmd=" ".join(j.argv), err=f"~/.local/state/{err}" if err else "")
    return rc


def local_tz(localtime: str = "/etc/localtime", timezone: str = "/etc/timezone") -> str:
    """TZ when set; else the zone /etc/localtime links to (what libc, cron and every process without TZ use), and
    /etc/timezone only when that is no zoneinfo link: the image's /etc/timezone can go stale (Etc/UTC beside a
    Europe/Zurich link), and cron's jobs and the tool worker run without TZ."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", tz or "") and tz:
        return tz
    try:
        link = os.path.realpath(localtime)
        if "/zoneinfo/" in link:
            return link.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open(timezone, encoding="utf-8") as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return "UTC"
