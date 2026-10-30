"""core.jobfail: cron-fail's failure-streak semantics for jobs the harness runs itself. Stdlib.

  record(name, rc, *, after, ok=(), cmd="", err="", home=None, now=None) -> "job.failed" | "job.recovered" | None

A run is good when rc is 0 or in `ok` (cron-fail's --ok). A failed run counts the streak; the after-th failed run in
a row emits ONE job.failed event: a typed row (payload: job, cmd, rc, runs, since, after, err) plus the note
~/inbox/<name>-jobfail-<epoch>.md for the beat, written by `events emit` (as cron-fail does). Later failures of the
same streak stay silent; a note deleted meanwhile keeps the streak silent until it ends. The first good run ends the
streak; when it had emitted, the note (if still there) gets one "Recovered" line and `events update` (recovered,
failed_runs), and one job.recovered row (digest, no note) follows. after 0 = count only, never an event.

Callers: the hub's job executor (tool workflow.job, live runs) and `keepers ensure`. State:
~/.local/state/jobs/<name>.streak.json (0600, one lock per job). Events: ~/bin/events, or $JOB_EVENTS (tests).
A failed emit still leaves the note (written here) and one line in ~/.local/state/assistant/emit-errors.log.

  usage_run(name, rc, *, after, ok=(), ms, src, home=None, now=None) -> bool

cron-fail's other half: one usage spool line per run (the store's `runs` table: run counts, failures, latency per
job), the same line cron-fail writes: {"ts", "src", "f": "job.<name>", "k": "run", "ch": "cron", "a": "cron", "ms",
"o": "ok" | "rc<N>"} appended to $USAGE_STATE/spool/<src>-YYYYMMDD.jsonl (default ~/.local/state/product; `usage
tick` folds it). Only for a job with failure handling (after > 0 = it ran under cron-fail, so its exit codes mean
ok/failed); after 0 writes nothing (a job like model-drift exits 1 for a finding). The callers call it once per
real run; src tells them apart in the spool (workflow.job: "hubjob", keepers: "keepers";
cron-fail: "cron"). USAGE=off = nothing. Never raises; False = not written.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from core import jsonstate, locks
from core import notes as notes

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HINT = "read the job's .err through secretscan --redact, fix the job, then ack"


def _home(home: str | os.PathLike[str] | None) -> Path:
    return Path(home) if home else Path(os.path.expanduser("~"))


def _events(home: Path) -> str:
    return os.environ.get("JOB_EVENTS") or str(home / "bin" / "events")


def _stamp(t: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _run(home: Path, args: list[str], body: str | None = None) -> int:
    try:
        p = subprocess.run([_events(home), *args], input=body, capture_output=True, text=True, timeout=20,
                           env={**os.environ, "HOME": str(home)}, check=False)  # fmt: skip
        return p.returncode
    except subprocess.TimeoutExpired:
        return 124  # as timeout(1): a slow ledger (28.10 18:46, host load ~150) read as 127, "events missing"
    except (OSError, subprocess.SubprocessError):
        return 127


def _emit_err(home: Path, what: str) -> None:
    try:
        d = home / ".local" / "state" / "assistant"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "emit-errors.log", "a", encoding="utf-8") as f:
            f.write(f"{int(time.time())} job.failed jobfail {what}\n")
    except OSError:
        pass


def usage_run(
    name: str,
    rc: int,
    *,
    after: int,
    ok: tuple[int, ...] | list[int] = (),
    ms: float,
    src: str,
    home: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> bool:
    if not after or os.environ.get("USAGE", "").lower() in ("off", "0", "no", "false"):
        return False
    if not NAME_RE.match(name) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", src):
        return False
    try:
        t = int(now if now is not None else time.time())
        state = os.environ.get("USAGE_STATE") or str(_home(home) / ".local" / "state" / "product")
        sd = Path(state) / "spool"
        sd.mkdir(parents=True, exist_ok=True, mode=0o700)
        good = rc == 0 or rc in tuple(ok)
        rec = {"ts": t, "src": src, "f": f"job.{name}", "k": "run", "ch": "cron", "a": "cron",
               "ms": max(0, int(ms)), "o": "ok" if good else f"rc{rc}"}  # fmt: skip
        line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        fd = os.open(sd / f"{src}-{time.strftime('%Y%m%d', time.localtime(t))}.jsonl",
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)  # fmt: skip
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except (OSError, ValueError, TypeError):
        return False


def state_dir(home: str | os.PathLike[str] | None = None) -> Path:
    return _home(home) / ".local" / "state" / "jobs"


def streak(name: str, home: str | os.PathLike[str] | None = None) -> dict:
    return jsonstate.load(state_dir(home) / f"{name}.streak.json", {}) or {}


def record(
    name: str,
    rc: int,
    *,
    after: int,
    ok: tuple[int, ...] | list[int] = (),
    cmd: str = "",
    err: str = "",
    home: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> str | None:
    if not NAME_RE.match(name):
        raise ValueError(f"jobfail: bad job name {name!r}")
    h = _home(home)
    t = int(now if now is not None else time.time())
    sd = state_dir(h)
    sd.mkdir(parents=True, exist_ok=True, mode=0o700)
    good = rc == 0 or rc in tuple(ok)
    fd = locks.acquire(sd / f"{name}.lock", wait=30)
    try:
        p = sd / f"{name}.streak.json"
        st = jsonstate.load(p, {}) or {}
        if good:
            if not st.get("since"):
                return None
            kind = None
            if st.get("emitted"):
                note = str(st.get("note") or "")
                n = int(st.get("count") or 0)
                np = h / "inbox" / note if note else None
                if np is not None and np.exists():
                    try:
                        with open(np, "a", encoding="utf-8") as f:
                            f.write(f"\nRecovered {_stamp(t)} after {n} failed run(s).\n")
                        _run(h, ["update", "--note", note, "--payload", json.dumps({"recovered": t, "failed_runs": n})])
                    except OSError:
                        pass
                payload = {"type": "job.recovered", "v": 1, "job": name, "runs": n, "since": int(st["since"]),
                           "recovered": t}  # fmt: skip
                erc = _run(h, ["emit", "--kind", "job.recovered", "--subject", f"{name} recovered after {n} failed run(s)",  # noqa: E501
                               "--source", "jobfail", "--delivery", "digest", "--no-note", "--payload",
                               json.dumps(payload), "--idem", f"job.recovered:{name}:{st['since']}"])  # fmt: skip
                if erc:
                    _emit_err(h, f"recovered emit exit {erc}")
                kind = "job.recovered"
            p.unlink(missing_ok=True)
            return kind
        st.setdefault("since", t)
        st["count"] = int(st.get("count") or 0) + 1
        st["rc"] = rc
        kind = None
        if after and st["count"] == after:
            note = f"{name}-jobfail-{t}.md"
            c1 = os.path.basename(cmd.split()[0]) if cmd.split() else "?"
            c1 = c1 if re.fullmatch(r"[A-Za-z0-9._+-]+", c1) else "?"
            body = notes.compose(
                f"{name} failed (hub job)",
                [f"Job `{name}` ({c1}) failed {st['count']} run(s) in a row since {_stamp(int(st['since']))}; last exit"
                 f" {rc}.", f"Its stderr: {err or '~/.local/state/jobs/' + name + '.err'} (secrets: read it through"
                 " `secretscan --redact`).", "One note per failure streak (core.jobfail); the first good run appends"
                 ' "Recovered" here.'],
                HINT,
            )  # fmt: skip
            payload = {"type": "job.failed", "v": 1, "job": name, "cmd": c1, "rc": rc, "runs": st["count"],
                       "since": int(st["since"]), "after": after}  # fmt: skip
            erc = _run(h, ["emit", "--kind", "job.failed", "--subject", f"{name} failed (hub job)", "--source",
                           "jobfail", "--next", HINT, "--note", note, "--body-file", "-", "--payload",
                           json.dumps(payload)], body)  # fmt: skip
            if not (h / "inbox" / note).exists():
                try:
                    notes.write(h / "inbox", note, body)
                except OSError:
                    note = ""
                _emit_err(h, f"emit exit {erc}, note written by jobfail")
            elif erc:
                _emit_err(h, f"emit exit {erc}")
            st["emitted"], st["note"] = True, note
            kind = "job.failed"
        jsonstate.save(p, st, mode=0o600)
        return kind
    finally:
        locks.release(fd)
