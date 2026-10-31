"""loop.health: the self-review's step (h), a whole-setup health pass over every log and trace source.

  run(write=True, now=None, only=None, budget_s=HEALTH_BUDGET) -> {"findings", "readings", "clusters", "errors",
      "secs", "phoenix"}
  markdown(res, new_ids=()) -> the report section "(h) Setup health"
  signal() -> the latest pass (latest.json, <= 26 h old) for the loop review (loop.signals "health")

One collector per source (inventory: ~/reports/self-review/sources.md) turns the last 24 h into metrics, error
signatures and deterministic anomalies against a 7-day baseline: the source's own history where it keeps one (land
log, Phoenix, traces, agent-watch, hub runs, SLO and events windows), else the pass's own daily history
(~/.local/state/loop/health/history.jsonl: per day and source, metrics and signature counts only). A signature never
seen in the 7 days before is "new" (one clustered finding per source). Anomalies that start at a known time name the
land or deploy before them ("since a change"). Findings: key, source, metric, now, base, sev 1-3, msg, fix, and for
Phoenix ones a trace link + the span they are annotated on.

Privacy: counts, ids, rule names, error signatures (exception class @ file:function, a fixed phrase vocabulary) and
code paths only; never message, mail, chat or document text, transcripts (traces.db counts only), the vault's data
or logs (status verbs only), or hook log commands (rule names only). Every string that leaves a collector passes
secretscan's redact. The LLM never sees anything from here except the clustered signatures (self-review ranks them).
Read-only everywhere except Phoenix write-backs (span annotations, datasets: loop.phoenix) and its own state.

Switches: ~/.config/self-review/health-off (or SELF_REVIEW_HEALTH=off) skips the pass (one command);
HEALTH_SOURCES=a,b runs only those collectors; ~/.config/self-review/phoenix-writes-off stops Phoenix write-backs.
Tests: LOOP_HEALTH_FIXTURE=<json> returns that result instead of collecting (the self-review suite's seam).
"""

from __future__ import annotations

import collections
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sqlite3
import statistics
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core import paths

from . import common

HEALTH_BUDGET = 300  # seconds for all collectors together (the 03:30 run stays under ~10 min)
DAY = 86400
FRESH_H = 26
HIST_DAYS = 30
TAIL = 65536  # bytes read from the end of a log
DENY_PATH = re.compile(r"vault|secure|approval|secret|\.env|token|credential", re.I)


# ------------------------------------------------------------------------------------------------ helpers
def home(*p: str) -> Path:
    return paths.home().joinpath(*p)


def state_dir() -> Path:
    return common.state_dir() / "health"


def switch_off() -> str | None:
    if os.environ.get("SELF_REVIEW_HEALTH", "").strip().lower() == "off":
        return "SELF_REVIEW_HEALTH=off"
    if home(".config", "self-review", "health-off").exists():
        return "~/.config/self-review/health-off"
    return None


_SCRUB: Callable[[str], str] | None = None


def scrub(s: Any) -> str:
    """secretscan's redact (the module, else the CLI), then $HOME-relative paths."""
    global _SCRUB
    s = str(s)
    if _SCRUB is None:
        fn: Callable[[str], str] = _mask
        try:
            import sys

            for d in (str(paths.bin_dir()), str(paths.bin_dir("lib"))):
                if d not in sys.path:
                    sys.path.append(d)
            import secretscan  # type: ignore

            fn = secretscan.redact
        except Exception:
            if os.access(str(paths.bin_dir("secretscan")), os.X_OK):
                fn = _cli_redact
        _SCRUB = fn
    s = s.replace(str(paths.home()), "~")
    try:
        return _SCRUB(s)
    except Exception:
        return _mask(s)


def _cli_redact(x: str) -> str:
    rc, out, _ = common.run([str(paths.bin_dir("secretscan")), "--redact"], timeout=20, stdin=x)
    return out.rstrip("\n") if rc == 0 else _mask(x)


def _mask(s: str) -> str:
    return re.sub(r"(?=[\w+-]*\d)(?=[\w+-]*[A-Za-z])[\w+-]{32,}={0,2}", "***", s)


def jsonl(p: Path, since: float = 0) -> list[dict[str, Any]]:
    out = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and float(d.get("ts") or 0) >= since:
                    out.append(d)
    except OSError:
        pass
    return out


def tail(p: Path, n: int = TAIL) -> str:
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def hhmm(ts: float | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, common.tz()).strftime("%H:%M %d.%m")


def run_json(argv: list[str], timeout: float = 60) -> Any:
    d, err = common.run_json([common.bin_path(argv[0]), *argv[1:]], timeout=timeout)
    if err:
        raise RuntimeError(err)
    return d


def fmtn(x: Any) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.1f}".rstrip("0").rstrip(".")
    return str(x)


# ------------------------------------------------------------------------------------------------ signatures
EXC_RE = re.compile(
    r"\b((?:[A-Za-z_]\w*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exception|Exceeded|Refused|Timeout|Interrupt|"
    r"Failure|Denied|Expired))\b"
)
FRAME_RE = re.compile(r'File "([^"]+)", line \d+, in ([\w<>]+)')
ERRISH = re.compile(r"error|fail|traceback|exception|fatal|refused|denied|timed out|timeout|killed|panic", re.I)
PHRASES = [
    ("timed out", re.compile(r"timed? ?out|timeout", re.I)),
    ("connection refused", re.compile(r"connection refused|ECONNREFUSED", re.I)),
    ("connection reset", re.compile(r"connection reset|ECONNRESET|broken pipe", re.I)),
    ("name resolution", re.compile(r"name or service not known|temporary failure in name resolution|ENOTFOUND", re.I)),
    ("permission denied", re.compile(r"permission denied|EACCES|operation not permitted", re.I)),
    ("no such file", re.compile(r"no such file|ENOENT", re.I)),
    ("http 429", re.compile(r"\b429\b|too many requests", re.I)),
    ("http 5xx", re.compile(r"(?:HTTP|status|error)\D{0,12}5\d\d\b", re.I)),
    ("http 4xx", re.compile(r"(?:HTTP|status|error)\D{0,12}4\d\d\b", re.I)),
    ("database locked", re.compile(r"database is locked|lock(?:ed)? (?:held|busy)", re.I)),
    ("disk full", re.compile(r"no space left", re.I)),
    ("killed", re.compile(r"\bkilled\b|out of memory|\boom\b", re.I)),
    ("exit code", re.compile(r"\bexit(?:ed)?(?: with)?(?: code| status)? [1-9]\d*\b|\brc=[1-9]", re.I)),
]


ZERO_BEFORE = re.compile(r"(?:\b0|\bno|\bzero|\bwithout)\s+(?:\w+\s+)?$", re.I)
ZERO_AFTER = re.compile(
    r"^\w*\)?\s*[:=]?\s*(?:0|none|false|no(?!\s+(?:such|space|route|module|permission|access)))\b", re.I
)


def zero_only(line: str) -> bool:
    """Every error word of the line is a zero count ("0 errors", "failed=0", "no timeout"): a summary, not an error."""
    hits = list(ERRISH.finditer(line))
    return bool(hits) and all(
        ZERO_BEFORE.search(line[max(0, h.start() - 24) : h.start()]) or ZERO_AFTER.search(line[h.end() : h.end() + 12])
        for h in hits
    )


def code_path(f: str) -> str:
    parts = f.replace("\\", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 1 else parts[0]


def signatures(text: str) -> collections.Counter[str]:
    """Error signatures of a log: "Class @ dir/file.py:function" for exceptions (the frame before them), else the
    first matching phrase of a fixed vocabulary, else "other error line". Never the line's own words."""
    c: collections.Counter[str] = collections.Counter()
    frame = None
    for line in text.splitlines()[-5000:]:
        m = FRAME_RE.search(line)
        if m:
            frame = f"{code_path(m.group(1))}:{m.group(2)}"
            continue
        if not ERRISH.search(line) or line.lstrip().startswith("Traceback") or zero_only(line):
            continue
        e = EXC_RE.search(line)
        if e:
            c[e.group(1).split(".")[-1] + (f" @ {frame}" if frame else "")] += 1
            frame = None
            continue
        c[next((name for name, rx in PHRASES if rx.search(line)), "other error line")] += 1
    return c


def norm_sig(s: str) -> str:
    s = re.sub(r"[0-9a-f]{8,}", "H", s)
    s = re.sub(r"\d+", "N", s)
    return " ".join(s.split())[:120]


# ------------------------------------------------------------------------------------------------ history / context
def load_history() -> list[dict[str, Any]]:
    return [h for h in jsonl(state_dir() / "history.jsonl") if isinstance(h.get("source"), str)]


class Ctx:
    def __init__(self, src: str, now: float, hist: list[dict[str, Any]]):
        self.src, self.now = src, now
        today = datetime.fromtimestamp(now, common.tz()).date()
        lo = (today - timedelta(days=7)).isoformat()
        self.hist = [h for h in hist if h["source"] == src and lo <= str(h.get("day")) < today.isoformat()]

    def base(self, metric: str) -> float | None:
        v = [
            float(h["metrics"][metric])
            for h in self.hist
            if isinstance((h.get("metrics") or {}).get(metric), (int, float))
        ]
        return round(statistics.median(v), 2) if v else None

    def seen(self) -> set[str] | None:
        if not self.hist:
            return None
        return {s for h in self.hist for s in (h.get("sigs") or {})}


def F(src: str, key: str, metric: str, now: Any, base: Any, sev: int, msg: str, fix: str, **kw: Any) -> dict[str, Any]:
    return dict(kw, key=f"{src}:{key}", source=src, metric=metric, now=now, base=base, sev=sev, msg=msg, fix=fix)


def rose(now: float | None, base: float | None, floor: float, factor: float = 2.0) -> bool:
    """now is an anomaly against base: at least `floor`, and more than factor x base (+1 so a zero base needs more
    than one event)."""
    if now is None or now < floor:
        return False
    return base is None or now > factor * base + 1


_DEPLOYS: list[dict[str, Any]] | None = None


def deploys() -> list[dict[str, Any]]:
    global _DEPLOYS
    if _DEPLOYS is None:
        _DEPLOYS = jsonl(home(".local", "state", "assistant", "deploys.jsonl"), since=common.now() - 9 * DAY)
    return _DEPLOYS


def after_change(ts: float | None) -> str:
    """'after land BRANCH (sha) HH:MM': the last successful land or deploy in the 24 h before ts."""
    if not ts:
        return ""
    best = None
    for r in deploys():
        t = float(r.get("end") or r.get("ts") or 0)
        if r.get("ok") and r.get("op") in ("land", "deploy") and ts - DAY <= t <= ts:
            if best is None or t > float(best.get("end") or best.get("ts") or 0):
                best = r
    if not best:
        return ""
    what = best.get("branch") or best.get("target") or best["op"]
    sha = str(best.get("sha") or best.get("release") or "")[:7]
    return f"after {best['op']} {what}{f' ({sha})' if sha else ''} {hhmm(float(best.get('end') or best['ts']))}"


# ------------------------------------------------------------------------------------------------ collectors
def c_land(ctx: Ctx) -> dict[str, Any]:
    """The land/deploy log (deploys.jsonl), CI records (ci/*.json) and the shared CI blockers log
    (ci-blockers.jsonl, when the land path writes it)."""
    now = ctx.now
    rows = deploys()
    lands = sorted((r for r in rows if r.get("op") == "land"), key=lambda r: float(r.get("ts") or 0))
    dep = [r for r in rows if r.get("op") == "deploy"]

    def day_metrics(a: float, b: float) -> dict[str, float]:
        ls = [r for r in lands if a <= float(r["ts"]) < b]
        durs = sorted(
            float(r["end"]) - float(r.get("land_start") or r["start"])
            for r in ls
            if r.get("end") and (r.get("land_start") or r.get("start"))
        )
        return {
            "lands": len(ls),
            "land_fail": sum(1 for r in ls if not r.get("ok")),
            "deploy_fail": sum(1 for r in dep if a <= float(r["ts"]) < b and not r.get("ok")),
            "lock_wait_min": round(sum(float(r.get("lock_wait_secs") or 0) for r in ls) / 60, 1),
            "land_p95_min": round(durs[int(0.95 * (len(durs) - 1))] / 60, 1) if durs else 0.0,
        }

    m = day_metrics(now - DAY, now)
    days = [day_metrics(now - (i + 1) * DAY, now - i * DAY) for i in range(1, 8)]
    base: dict[str, Any] = {k: round(statistics.median(d[k] for d in days), 2) for k in m}
    sigs: collections.Counter[str] = collections.Counter()
    for r in lands:
        if float(r["ts"]) >= now - DAY and not r.get("ok"):
            sub = str(((r.get("gate") or {}).get("submit")) or "")
            e = EXC_RE.search(sub)
            sigs[f"land {r.get('why') or '?'}" + (f": {e.group(1)}" if e else "")] += 1
    # shared red: >= 3 failed lands in a row on >= 2 branches (one branch's own red is not a blocker)
    runs, cur = [], []
    for r in [r for r in lands if float(r["ts"]) >= now - DAY]:
        if not r.get("ok"):
            cur.append(r)
            continue
        if cur:
            runs.append((cur, float(r["ts"])))
        cur = []
    if cur:
        runs.append((cur, now))
    shared = [(c, end) for c, end in runs if len(c) >= 3 and len({x.get("branch") for x in c}) >= 2]
    # blocked time is reported once, from the shared-blocker log below (and the loop's "CI blocked" metric from the
    # same log); the shared-red runs here only keep a common cause from also reading as a per-branch fail rate.
    # (29.10: a deploys.jsonl-derived blocked_min here showed the same blocked time a second time.)
    out: list[dict[str, Any]] = []
    # the shared-blocker log of the land path (core.ciblock), when present
    cb = jsonl(home(".local", "state", "assistant", "ci-blockers.jsonl"), since=now - 8 * DAY)
    if cb:
        spans: dict[str, list[float]] = {}
        for r in sorted(cb, key=lambda r: float(r["ts"])):
            k = str(r.get("key") or "?")
            if r.get("op") == "seen" and k not in spans:
                spans[k] = [float(r["ts"]), now]
            elif r.get("op") == "cleared" and k in spans:
                spans[k][1] = float(r["ts"])
                if spans[k][1] < now - DAY:
                    spans.pop(k)
        open_now = [k for k, (a, b) in spans.items() if b >= now]
        mins = sum(max(0.0, b - max(a, now - DAY)) for a, b in spans.values()) / 60
        m["ci_blockers"], m["ci_blocked_min"] = len(spans), round(mins, 1)
        for k in open_now:
            a = spans[k][0]
            out.append(
                F(
                    "land",
                    f"ci-blocker:{norm_sig(k)}",
                    "ci_blocked_min",
                    round((now - a) / 60),
                    0,
                    3,
                    f"shared CI blocker still open: {k} since {hhmm(a)}"
                    + (f" ({after_change(a)})" if after_change(a) else ""),
                    "`assistant ci-status`: fix it on main, then `assistant ci-status --clear KEY`",
                    first_ts=a,
                )
            )
    red = 0
    for p in home(".local", "state", "assistant", "ci").glob("*.json"):
        if mtime(p) < now - DAY:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        red += isinstance(d, dict) and d.get("ok") is False
    m["ci_red"] = red
    if rose(m["land_fail"], base["land_fail"], 4) and not shared:
        out.append(
            F(
                "land",
                "fail-rate",
                "land_fail",
                m["land_fail"],
                base["land_fail"],
                2,
                f"{m['land_fail']} of {m['lands']} lands failed in 24 h (7-day median {fmtn(base['land_fail'])}/day)",
                "the land log's why per branch: a common cause (runner, gate, suite) is a shared fix",
            )
        )
    if m["deploy_fail"] and m["deploy_fail"] > (base["deploy_fail"] or 0):
        out.append(
            F(
                "land",
                "deploy-fail",
                "deploy_fail",
                m["deploy_fail"],
                base["deploy_fail"],
                2,
                f"{m['deploy_fail']} deploy(s) failed in 24 h",
                "`assistant status`, `assistant drift`",
            )
        )
    if rose(m["lock_wait_min"], base["lock_wait_min"], 15):
        out.append(
            F(
                "land",
                "lock-wait",
                "lock_wait_min",
                m["lock_wait_min"],
                base["lock_wait_min"],
                1,
                f"lands waited {fmtn(m['lock_wait_min'])} min for the deploy lock in 24 h (median "
                f"{fmtn(base['lock_wait_min'])})",
                "serialise fewer lands at once or shorten the gate",
            )
        )
    return {
        "metrics": m,
        "base": base,
        "sigs": dict(sigs),
        "findings": out,
        "line": f"{m['lands']} lands, {m['land_fail']} failed, "
        f"land p95 {fmtn(m['land_p95_min'])} min (median {fmtn(base['land_p95_min'])})",
    }


JOB_RE = re.compile(r"^sched-(?:job|watch):(.+?)-\d{4}-\d{2}-\d{2}T")
# errored runs of this many jobs, each within OUTAGE_GAP of the next, are one outage (host, hub or brain), not N job
# faults: 28.10 18:30-18:50 (host load ~150, unclean reboot 18:55) failed 15 jobs and read as per-job findings
OUTAGE_JOBS = 4
OUTAGE_GAP = 15 * 60


def outages(errs: list[tuple[float, str]]) -> list[tuple[float, float, set[str], int]]:
    """(ts, job) errored runs -> [(first, last, jobs, runs)] for each chain spanning OUTAGE_JOBS jobs or more."""
    out: list[tuple[float, float, set[str], int]] = []
    chain: list[tuple[float, str]] = []
    for t, job in sorted(errs) + [(float("inf"), "")]:
        if chain and t - chain[-1][0] > OUTAGE_GAP:
            jobs = {j for _, j in chain}
            if len(jobs) >= OUTAGE_JOBS:
                out.append((chain[0][0], chain[-1][0], jobs, len(chain)))
            chain = []
        chain.append((t, job))
    return out


def cron_rows(since: float, now: float) -> list[dict[str, Any]]:
    """cron-fail's run log (core.cronruns; cron-back 29.10): one row per cron job run; [] when unreadable."""
    try:
        from core import cronruns

        return cronruns.runs(since, now)
    except Exception:  # noqa: BLE001  (a collector never fails the pass)
        return []


def c_hubjobs(ctx: Ctx) -> dict[str, Any]:
    """Job runs: the hub's workflow runs (errors with their class, catch-up, engine state), the cron jobs' runs from
    cron-fail's run log (failed runs count as errored runs, cron-back 29.10) and the keepers' failure streaks."""
    now = ctx.now
    rows = run_json(["hub", "wf", "ls", "--status", "ERROR", "--limit", "100", "--json"], timeout=60) or []
    per_now: collections.Counter[str] = collections.Counter()
    per_base: collections.Counter[str] = collections.Counter()
    first: dict[str, float] = {}
    errs: list[tuple[float, str]] = []
    sigs: collections.Counter[str] = collections.Counter()
    for r in rows if isinstance(rows, list) else []:
        t = float(r.get("created") or 0)
        m = JOB_RE.match(str(r.get("id") or ""))
        job = (r.get("output") or {}).get("job") if isinstance(r.get("output"), dict) else None
        job = job or (m.group(1) if m else str(r.get("name") or "?"))
        if t >= now - DAY:
            per_now[job] += 1
            errs.append((t, job))
            e = EXC_RE.search(str(r.get("error") or ""))
            sigs[f"{job}: {e.group(1) if e else 'error'}"] += 1
        elif t >= now - 8 * DAY:
            per_base[job] += 1
    cron_last: dict[str, bool] = {}
    cron_runs = 0
    for r in cron_rows(now - 8 * DAY, now):
        t, job = r["ts"], r["job"]
        if t >= now - DAY:
            cron_runs += 1
        cron_last[job] = bool(r["ok"])
        if r["ok"]:
            continue
        if t >= now - DAY:
            per_now[job] += 1
            errs.append((t, job))
            sigs[f"{job}: exit {r['rc']}"] += 1
        else:
            per_base[job] += 1
    st = run_json(["hub", "wf", "status", "--json"], timeout=60) or {}
    eng = st.get("engine") or {}
    cu = eng.get("catch_up") or {}
    bad_exit = 0
    last_ok: dict[str, bool] = {}
    for s in st.get("schedules") or []:
        o = ((s.get("runs") or [{}])[0] or {}).get("output")
        last_ok[str(s.get("job") or s.get("name"))] = isinstance(o, dict) and o.get("code") == "ok"
        for r in s.get("runs") or []:
            o = r.get("output") or {}
            if isinstance(o, dict) and o.get("code") not in (None, "ok"):
                bad_exit += 1
                sigs[f"{s.get('job') or s.get('name')}: code {o.get('code')}"] += 1
    last_ok.update(cron_last)
    shared = outages(errs)
    solo: collections.Counter[str] = collections.Counter()
    for t, job in errs:
        if not any(a <= t <= b for a, b, _, _ in shared):
            solo[job] += 1
            first[job] = min(first.get(job, t), t)
    m = {
        "outage_runs": sum(n for *_, n in shared),
        "run_errors": sum(per_now.values()),
        "catch_up_errors": len(cu.get("errors") or []),
        "catch_up_late": len(cu.get("late") or []),
        "last_run_bad": bad_exit,
        "cron_runs": cron_runs,
    }
    base = {"run_errors": round(sum(per_base.values()) / 7, 1)}
    out = []
    if eng.get("state") not in (None, "on"):
        out.append(
            F(
                "hubjobs",
                "engine",
                "engine_on",
                0,
                1,
                3,
                f"hub workflow engine is {eng.get('state')}"
                + (f" ({scrub(eng.get('why'))[:80]})" if eng.get("why") else ""),
                "`hub wf status`; `hub health`; keepers carry the daemons meanwhile",
            )
        )
    for a, b, jobs, n in shared:
        down = sorted(j for j in jobs if last_ok.get(j) is False)  # no schedule any more: not failing
        ch = after_change(a)
        out.append(
            F(
                "hubjobs",
                f"outage:{hhmm(a)}",
                "outage_runs",
                n,
                0,
                2 if down else 1,
                f"{n} errored hub runs across {len(jobs)} jobs {hhmm(a)}-{hhmm(b)}: one outage (host, hub or brain), "
                + (f"still failing: {', '.join(down[:4])}" if down else "every job ran ok since")
                + (f", {ch}" if ch else ""),
                "that window: `hub health`, server-health (load, reboot), the brain's worker.log; not the jobs' code",
                first_ts=a,
            )
        )
    for job, n in solo.most_common(8):
        b = per_base.get(job, 0) / 7
        if n >= 2 and rose(n, b, 2):
            ch = after_change(first[job])
            out.append(
                F(
                    "hubjobs",
                    f"job:{job}",
                    f"run_errors:{job}",
                    n,
                    round(b, 1),
                    2,
                    f"{'cron' if job in cron_last else 'hub'} job {job}: {n} errored runs in 24 h (7 d: "
                    f"{fmtn(round(b, 1))}/day), first "
                    f"{hhmm(first[job])}" + (f", {ch}" if ch else ""),
                    (
                        f"`cron-runs stats --job {job}`; ~/.local/state/cron-fail/{job}.err"
                        if job in cron_last
                        else f"`hub wf ls --name {job}`; the job's .err"
                    )
                    + " (secretscan --redact) and its code path",
                    first_ts=first[job],
                )
            )
    try:
        ks = run_json(["keepers", "status", "--json"], timeout=30) or []
    except RuntimeError:
        ks = []
    failing = [k for k in ks if isinstance(k, dict) and int(k.get("streak") or 0) >= max(1, int(k.get("after") or 1))]
    m["keepers_failing"] = len(failing)
    for k in failing:
        out.append(
            F(
                "hubjobs",
                f"keeper:{k.get('name')}",
                "keeper_streak",
                int(k.get("streak") or 0),
                0,
                2,
                f"keeper {k.get('name')} failed {k.get('streak')} runs in a row (rc {(k.get('last') or {}).get('rc')})",
                "`keepers status`; run its argv by hand once",
            )
        )
    return {
        "metrics": m,
        "base": base,
        "sigs": dict(sigs),
        "findings": out,
        "line": f"{m['run_errors']} errored runs ({cron_runs} cron runs; 7 d {fmtn(base['run_errors'])}/day), "
        f"catch-up errors {m['catch_up_errors']}, late {m['catch_up_late']}, keepers failing {len(failing)}",
    }


def c_joberr(ctx: Ctx) -> dict[str, Any]:
    """~/.local/state/jobs/*.err changed in 24 h (signatures from their tails) and job-failure notes by name."""
    now, sigs = ctx.now, collections.Counter()
    files = 0
    errs = [*home(".local", "state", "jobs").glob("*.err"), *home(".local", "state", "cron-fail").glob("*.err")]
    for p in sorted(errs):  # the hub executor's and cron-fail's (cron-back 29.10: the plain jobs run under cron-fail)
        if mtime(p) < now - DAY or DENY_PATH.search(p.name):
            continue
        try:
            if p.stat().st_size == 0:
                continue
        except OSError:
            continue
        files += 1
        for s, n in signatures(tail(p)).items():
            sigs[f"{p.stem}: {s}"] += n
    notes: collections.Counter[str] = collections.Counter()
    for p in home("inbox").glob("*.md"):
        mm = re.match(r"^(.+?)-(?:jobfail|failed)-", p.name)
        if mm and mtime(p) >= now - DAY:
            notes[mm.group(1)] += 1
    for job, n in notes.items():
        sigs[f"failure note {job}"] += n
    m = {"err_files": files, "fail_notes": sum(notes.values())}
    out = []
    for job, n in notes.items():
        if n >= 2:
            out.append(
                F(
                    "joberr",
                    f"notes:{job}",
                    f"fail_notes:{job}",
                    n,
                    ctx.base("fail_notes"),
                    2,
                    f"{n} failure notes for {job} in 24 h",
                    "read the job's .err through secretscan --redact; fix the job (not the note)",
                )
            )
    return {
        "metrics": m,
        "sigs": dict(sigs),
        "findings": out,
        "line": f"{files} job .err files changed, {m['fail_notes']} failure notes in 24 h",
    }


CRON_LOG_RE = re.compile(r"2?>>\s*\"?([^\s\";|&)]+)")
KNOWN_LOGS = [
    "agent-watch/cron.err",
    "test-all/cron.err",
    "gym/cron.err",
    "log-rotate.log",
    "tmp-tidy.log",
    "brain-backup.log",
    "self-review/cron.log",
]


def cron_logs() -> list[Path]:
    rc, out, _ = common.run(["crontab", "-l"], timeout=10)
    found: list[Path] = []
    h = str(paths.home())
    for ln in out.splitlines() if rc == 0 else []:
        if ln.lstrip().startswith("#"):
            continue
        for m in CRON_LOG_RE.finditer(ln):
            p = m.group(1).replace("$HOME", h).replace("${HOME}", h)
            if p.startswith("~/"):
                p = h + p[1:]
            if p.startswith("/") and "$" not in p and not p.startswith("/dev/"):
                found.append(Path(p))
    found += [home(".local", "state", *k.split("/")) for k in KNOWN_LOGS]
    out_l, seen = [], set()
    for p in found:
        if str(p) in seen or DENY_PATH.search(str(p)) or not str(p).startswith(h):
            continue
        seen.add(str(p))
        out_l.append(p)
    return out_l


def c_cronlogs(ctx: Ctx) -> dict[str, Any]:
    now, sigs, changed = ctx.now, collections.Counter(), 0
    logs = cron_logs()
    for p in logs:
        if mtime(p) < now - DAY:
            continue
        changed += 1
        for s, n in signatures(tail(p)).items():
            sigs[f"{p.name if p.parent.name in ('state', '') else p.parent.name + '/' + p.name}: {s}"] += n
    return {
        "metrics": {"logs": len(logs), "changed": changed, "error_lines": sum(sigs.values())},
        "sigs": dict(sigs),
        "findings": [],
        "line": f"{changed} of {len(logs)} cron logs changed, {sum(sigs.values())} error lines",
    }


def c_traces(ctx: Ctx) -> dict[str, Any]:
    """traces.db (counts only): tool-call error categories, hook blocks by hook name, his first-reply p95, cost,
    failed subagents. Baseline = the 7 days before (8-day report minus the last day)."""
    a = run_json(["traces", "report", "--days", "1", "--json"], timeout=150)
    b = run_json(["traces", "report", "--days", "8", "--json"], timeout=150)

    def cats(d: dict[str, Any]) -> dict[str, int]:
        return {str(k): int(v) for k, v in (d.get("error_counts") or {}).items() if isinstance(v, (int, float))}

    ca, cb = cats(a), cats(b)
    sigs, out = {}, []
    for k, n in ca.items():
        hk = re.match(r"^[^:]+:hook:([\w.-]+)$", k)
        sigs[f"hook {hk.group(1)}" if hk else k] = sigs.get(f"hook {hk.group(1)}" if hk else k, 0) + n
    for k, n in sorted(ca.items(), key=lambda x: -x[1])[:10]:
        base = max(0, cb.get(k, 0) - n) / 7
        if rose(n, base, 10, 3.0):
            hk = re.match(r"^[^:]+:hook:([\w.-]+)$", k)
            what = f"{hk.group(1)} hook blocks ({k.split(':')[0]})" if hk else f"tool errors {k}"
            out.append(
                F(
                    "traces",
                    f"cat:{k}",
                    f"errors:{k}",
                    n,
                    round(base, 1),
                    2 if hk else 1,
                    f"{what}: {n} in 24 h vs {fmtn(round(base, 1))}/day over 7 d",
                    "traces report: which callers (tool and category only); fix the caller, never weaken a guard",
                )
            )
    tools_b = max(0, int(b.get("tools") or 0) - int(a.get("tools") or 0))
    errs_b = max(0, int(b.get("errors") or 0) - int(a.get("errors") or 0))
    m = {
        "tools": a.get("tools"),
        "errors": a.get("errors"),
        "error_rate_pct": round(100 * float(a.get("error_rate") or 0), 2),
        "hook_blocks": a.get("hook_blocks"),
        "owner_turns": a.get("owner_turns"),
        "owner_first_p95_s": round(float(a.get("owner_first_p95") or 0) / 1000, 1),
        "cost": round(float(a.get("total_cost") or 0), 1),
        "agents_bad": a.get("agents_bad"),
    }
    base = {
        "error_rate_pct": round(100 * errs_b / tools_b, 2) if tools_b else None,
        "owner_first_p95_s": round(float(b.get("owner_first_p95") or 0) / 1000, 1),
        "cost": round(max(0.0, float(b.get("total_cost") or 0) - float(a.get("total_cost") or 0)) / 7, 1),
        "agents_bad": round(max(0, int(b.get("agents_bad") or 0) - int(a.get("agents_bad") or 0)) / 7, 1),
    }
    if rose(m["agents_bad"] or 0, base["agents_bad"], 3):
        out.append(
            F(
                "traces",
                "agents-bad",
                "agents_bad",
                m["agents_bad"],
                base["agents_bad"],
                2,
                f"{m['agents_bad']} subagent runs ended badly in 24 h (7 d {fmtn(base['agents_bad'])}/day; "
                f"{', '.join(f'{k} {v}' for k, v in (a.get('bad_by_status') or {}).items())})",
                "traces agents: which titles; agent-watch log for stalls",
            )
        )
    if (
        m["owner_first_p95_s"]
        and base["owner_first_p95_s"]
        and m["owner_first_p95_s"] > 40
        and m["owner_first_p95_s"] > 1.5 * base["owner_first_p95_s"]
    ):
        out.append(
            F(
                "traces",
                "first-reply",
                "owner_first_p95_s",
                m["owner_first_p95_s"],
                base["owner_first_p95_s"],
                2,
                f"first reply to him p95 {m['owner_first_p95_s']} s in 24 h vs {base['owner_first_p95_s']} s over 8 d",
                "traces report: slow owner turns; ack first, slow work in the background",
            )
        )
    return {
        "metrics": m,
        "base": base,
        "sigs": sigs,
        "findings": out,
        "line": f"{m['tools']} tool calls, {fmtn(m['error_rate_pct'])} % failed (7 d "
        f"{fmtn(base['error_rate_pct'])} %), hook blocks {m['hook_blocks']}, first reply p95 "
        f"{fmtn(m['owner_first_p95_s'])} s, bad agents {m['agents_bad']}",
    }


def c_phoenix(ctx: Ctx) -> dict[str, Any]:
    """Phoenix: per span name (24 h vs the 7 days before, per day): count, error spans, p50/p95, tokens; per tool
    p95; API-equivalent cost; trace p95; slowest traces. Error findings link the example span's trace."""
    from . import phoenix

    ok, why = phoenix.enabled()
    if not ok:
        raise RuntimeError(f"Phoenix off ({why})")
    s = phoenix.stats(ctx.now)
    ex = {e["name"]: e for e in s["errors"]}
    m: dict[str, Any] = {
        "spans": sum(v["n"] for v in s["names"].values()),
        "error_spans": sum(v["err"] for v in s["names"].values()),
        "trace_p95_s": round(s["trace_p95"]["now"] / 1000, 1),
        "cost": s["cost"]["now"],
        "traces": s["traces"]["now"],
    }
    base = {
        "error_spans": round(sum(v["base"]["err"] for v in s["names"].values()), 1),
        "trace_p95_s": round(s["trace_p95"]["base"] / 1000, 1),
        "cost": s["cost"]["base"],
        "traces": s["traces"]["base"],
    }
    sigs, out = {}, []
    for name, v in s["names"].items():
        if v["n"] >= 20:
            m[f"p95:{name}"], m[f"err:{name}"] = v["p95"], v["err"]
        if v["err"]:
            sigs[f"span ERROR {name}"] = v["err"]
        b = v["base"]
        rate, brate = v["err"] / max(1, v["n"]), (b["err"] / b["n"]) if b["n"] else 0.0
        if v["err"] >= 5 and rate >= 0.05 and rate > 2 * brate:
            e = ex.get(name) or {}
            first = e.get("first_ts")
            ch = after_change(first)
            lk = phoenix.link(e.get("trace_id"), e.get("node"))
            out.append(
                F(
                    "phoenix",
                    f"err:{name}",
                    f"error % of {name}",
                    round(100 * rate, 1),
                    round(100 * brate, 1),
                    3 if rate >= 0.25 else 2,
                    f"{name}: {v['err']} of {v['n']} spans failed in 24 h ({100 * rate:.0f} %; 7 d "
                    f"{100 * brate:.0f} %, {fmtn(b['err'])}/day)"
                    + (f"; {ch}" if ch else "")
                    + (f" · trace {lk}" if lk else ""),
                    "open the linked trace (span names, codes, timings); fix the caller or the tool; the "
                    "Phoenix annotation 'setup-health' marks it",
                    link=lk,
                    span_id=e.get("span_id"),
                    trace_id=e.get("trace_id"),
                )
            )
        if v["n"] >= 20 and b["n7"] >= 20 and v["p95"] >= 1000 and b["p95"] and v["p95"] >= 2 * b["p95"]:
            out.append(
                F(
                    "phoenix",
                    f"p95:{name}",
                    f"p95 of {name}",
                    v["p95"],
                    b["p95"],
                    1,
                    f"{name}: p95 {phoenix.ms_s(v['p95'])} in 24 h vs {phoenix.ms_s(b['p95'])} over 7 d "
                    f"({v['n']} spans)",
                    "Phoenix: filter the span name, sort by latency; the slowest parent",
                )
            )
    for tool, v in s["tools"].items():
        b = v["base"]
        if v["n"] >= 20 and b["n7"] >= 20 and v["p95"] >= 5000 and b["p95"] and v["p95"] >= 2 * b["p95"]:
            out.append(
                F(
                    "phoenix",
                    f"tool:{tool}",
                    f"p95 of tool {tool}",
                    v["p95"],
                    b["p95"],
                    1,
                    f"tool {tool}: p95 {phoenix.ms_s(v['p95'])} in 24 h vs {phoenix.ms_s(b['p95'])} over 7 d",
                    "Phoenix: claude_code.tool spans of that tool, slowest first",
                )
            )
    if m["cost"] >= 1.5 * (base["cost"] or 0) + 20:
        out.append(
            F(
                "phoenix",
                "cost",
                "API-equivalent cost/day",
                m["cost"],
                base["cost"],
                1,
                f"API-equivalent cost {m['cost']:.0f}/day vs {base['cost']:.0f} over 7 d (Phoenix)",
                "traces report: top cost drivers (agents by cost, beats)",
            )
        )
    slow = [dict(t, link=phoenix.link(t["trace_id"])) for t in s["slow"][:3]]
    return {
        "metrics": m,
        "base": base,
        "sigs": sigs,
        "findings": out,
        "slow": slow,
        "errors": s["errors"],
        "tools": {k: {"n": v["n"], "p95": v["p95"], "base_p95": v["base"]["p95"]} for k, v in s["tools"].items()},
        "line": f"{m['traces']} traces, {m['spans']} spans, {m['error_spans']} error spans (7 d "
        f"{fmtn(base['error_spans'])}/day), trace p95 {fmtn(m['trace_p95_s'])} s (7 d "
        f"{fmtn(base['trace_p95_s'])} s), cost {m['cost']:.0f}/day (7 d {base['cost']:.0f})",
    }


AW_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \S+ (\w+) -> (\w+)\s*$")


def c_agents(ctx: Ctx) -> dict[str, Any]:
    now = ctx.now
    cnt_now: collections.Counter[str] = collections.Counter()
    cnt_base: collections.Counter[str] = collections.Counter()
    for ln in tail(home(".local", "state", "agent-watch", "agent-watch.log"), 1 << 20).splitlines():
        mm = AW_RE.match(ln)
        if not mm:
            continue
        try:
            t = datetime.strptime(mm.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=common.tz()).timestamp()
        except ValueError:
            continue
        if t >= now - DAY:
            cnt_now[mm.group(3)] += 1
        elif t >= now - 8 * DAY:
            cnt_base[mm.group(3)] += 1
    bad = sum(cnt_now[k] for k in ("failed", "stalled", "killed"))
    bb = round(sum(cnt_base[k] for k in ("failed", "stalled", "killed")) / 7, 1)
    m = {
        "done": cnt_now["done"],
        "failed": cnt_now["failed"],
        "stalled": cnt_now["stalled"],
        "killed": cnt_now["killed"],
        "bad": bad,
    }
    out = []
    if rose(bad, bb, 3):
        out.append(
            F(
                "agents",
                "bad",
                "bad",
                bad,
                bb,
                2,
                f"{bad} subagents failed, stalled or were killed in 24 h ({cnt_now['failed']} failed, "
                f"{cnt_now['stalled']} stalled, {cnt_now['killed']} killed; 7 d {fmtn(bb)}/day)",
                "traces agents (titles) + agent-watch log: a shared cause (permission prompt, hung command)",
            )
        )
    return {
        "metrics": m,
        "base": {"bad": bb, "done": round(cnt_base["done"] / 7, 1)},
        "sigs": {},
        "findings": out,
        "line": f"{cnt_now['done']} done, {bad} failed/stalled/killed (7 d {fmtn(bb)}/day)",
    }


# the ledger's unacked statuses (core.events.STATUSES); an updated event goes back to "new", so there is no
# "updated" status: asking for one made `events list` fail every night
UNACKED = ("new", "listed")


def c_events(ctx: Ctx) -> dict[str, Any]:
    """The event ledger: emit errors by kind, restarts, stale events, unacked events and their ages (kinds and
    times only, never subjects)."""
    now = ctx.now
    d = run_json(["events", "metrics", "--days", "1", "--json"], timeout=90) or {}
    w, m3 = d.get("writers") or {}, d.get("m3") or {}
    sigs = {
        f"emit error {k}": int(v)
        for k, v in (w.get("emit_errors_by_kind") or {}).items()
        if isinstance(v, (int, float)) and v
    }
    ages: list[tuple[float, str]] = []
    errs = []
    for st in UNACKED:
        try:
            rows = run_json(["events", "list", "--status", st, "--limit", "100", "--json"], timeout=30) or []
        except RuntimeError as e:
            errs.append(f"{st}: {str(e)[:80]}")
            continue
        ages += [((now - float(r.get("ts") or now)) / 3600, str(r.get("kind"))) for r in rows if isinstance(r, dict)]
    old = [(a, k) for a, k in ages if a > 24]
    rs = m3.get("restarts")
    m = {
        "emit_errors": int(w.get("emit_errors") or 0),
        "unacked": len(ages),
        "unacked_24h": len(old),
        "oldest_h": round(max((a for a, _ in ages), default=0), 1),
        "restarts": len(rs) if isinstance(rs, list) else int(rs or 0),
        "stale_new": int(m3.get("stale_new") or 0),
    }
    out = []
    if old:
        kinds = collections.Counter(k for _, k in old)
        out.append(
            F(
                "events",
                "unacked",
                "unacked_24h",
                len(old),
                ctx.base("unacked_24h"),
                1,
                f"{len(old)} events unacked for more than 24 h (oldest {m['oldest_h']:.0f} h; "
                + ", ".join(f"{k} {n}" for k, n in kinds.most_common(4))
                + ")",
                "events agenda: handle or ack each; a writer that never gets acked needs fixing",
            )
        )
    if rose(m["emit_errors"], ctx.base("emit_errors"), 3):
        out.append(
            F(
                "events",
                "emit-errors",
                "emit_errors",
                m["emit_errors"],
                ctx.base("emit_errors"),
                2,
                f"{m['emit_errors']} event emit errors in 24 h ("
                + ", ".join(f"{k} {v}" for k, v in (w.get("emit_errors_by_kind") or {}).items())
                + ")",
                "~/.local/state/assistant/emit-errors.log (kinds, codes); fix the writer's path to the ledger",
            )
        )
    if m["restarts"] >= 2:
        out.append(
            F(
                "events",
                "restarts",
                "restarts",
                m["restarts"],
                ctx.base("restarts"),
                2,
                f"{m['restarts']} session restarts in 24 h",
                "restart-cause notes; PROBLEMS.md if it repeats",
            )
        )
    if errs:
        out.append(
            F(
                "events",
                "list-broken",
                "events list",
                len(errs),
                0,
                1,
                f"`events list --json` failed for {len(errs)} status(es)",
                "events list --json: the traceback",
            )
        )
    return {
        "metrics": m,
        "sigs": sigs,
        "findings": out,
        "line": f"{m['unacked']} unacked ({m['unacked_24h']} > 24 h, oldest {fmtn(m['oldest_h'])} h), emit errors "
        f"{m['emit_errors']}, restarts {m['restarts']}",
    }


def c_slo(ctx: Ctx) -> dict[str, Any]:
    a = run_json(["slo", "--days", "1", "--json"], timeout=90) or {}
    b = run_json(["slo", "--days", "7", "--json"], timeout=90) or {}
    tb = {t.get("id"): t for t in b.get("tiles") or []}
    m, out = {}, []
    for t in a.get("tiles") or []:
        tid = t.get("id")
        m[str(tid)] = t.get("value")
        bt = tb.get(tid) or {}
        if t.get("status") == "miss" and bt.get("status") != "miss":
            out.append(
                F(
                    "slo",
                    f"miss:{tid}",
                    str(t.get("label") or tid),
                    t.get("value"),
                    bt.get("value"),
                    2,
                    f"SLO {t.get('label') or tid}: {fmtn(t.get('value'))} {t.get('unit') or ''} in 24 h, "
                    f"target {t.get('op') or ''} {fmtn(t.get('target'))} (7 d {fmtn(bt.get('value'))}, n "
                    f"{t.get('n')})",
                    f"slo --only {tid} --json: its source ({t.get('source') or '?'})",
                )
            )
    sm = a.get("summary") or {}
    return {
        "metrics": m,
        "sigs": {},
        "findings": out,
        "line": f"{sm.get('ok', 0)} ok, {sm.get('miss', 0)} miss, {sm.get('no_data', 0)} no data (24 h)",
    }


def c_evals(ctx: Ctx) -> dict[str, Any]:
    rows = run_json(["evals", "history", "-n", "40", "--json"], timeout=60) or []
    now = ctx.now
    a = [r for r in rows if float(r.get("ts") or 0) >= now - DAY]
    b = [r for r in rows if now - 8 * DAY <= float(r.get("ts") or 0) < now - DAY]
    m = {
        "runs": len(a),
        "errored": sum(int(r.get("errored") or 0) for r in a),
        "critical_fail": sum(int(r.get("critical_fail") or 0) for r in a),
    }
    bb = round(sum(int(r.get("errored") or 0) for r in b) / 7, 1)
    out = []
    if rose(m["errored"], bb, 2):
        out.append(
            F(
                "evals",
                "errored",
                "errored",
                m["errored"],
                bb,
                1,
                f"{m['errored']} eval cases errored (infra, not a score) in 24 h (7 d {fmtn(bb)}/day)",
                "evals report --failures: runner/model errors; ~/.local/state/jobs/evals.err",
            )
        )
    return {
        "metrics": m,
        "base": {"errored": bb},
        "sigs": {},
        "findings": out,
        "line": f"{m['runs']} runs, {m['errored']} errored cases, {m['critical_fail']} critical fails (24 h)",
    }


def c_hub(ctx: Ctx) -> dict[str, Any]:
    """`hub health --json`: checks, gateway api errors/p95, CI runner queue, egress counters (counts only: the
    bindings' endpoints and secret names are never read)."""
    d = run_json(["hub", "health", "--json"], timeout=90) or {}
    h = d.get("health") or {}
    eg, api, rn = h.get("egress") or {}, h.get("api") or {}, h.get("ci_runner") or {}
    checks = {k: v for k, v in (h.get("checks") or {}).items()}
    m = {
        "api_errors": api.get("errors"),
        "api_p95_ms": api.get("p95_ms"),
        "runner_queued": rn.get("queued"),
        "runner_unsigned": rn.get("unsigned"),
        "bad_releases": len(h.get("bad") or []),
        **{f"egress_{k}": eg.get(k) for k in ("denied", "errors", "refused", "redacted")},
    }
    out = []
    for k, v in checks.items():
        if v != "ok":
            out.append(
                F(
                    "hub",
                    f"check:{k}",
                    f"check {k}",
                    0,
                    1,
                    3,
                    f"hub check {k}: {scrub(v)[:60]}",
                    "`hub health`; `hub status`",
                )
            )
    for k in ("errors", "refused", "denied"):
        v = eg.get(k)
        if isinstance(v, (int, float)) and rose(v, ctx.base(f"egress_{k}"), 1):
            out.append(
                F(
                    "hub",
                    f"egress:{k}",
                    f"egress_{k}",
                    v,
                    ctx.base(f"egress_{k}"),
                    2,
                    f"egress injector {k}: {v} (7-day median {fmtn(ctx.base(f'egress_{k}'))})",
                    "`hub egress status` (counts per handle; never print the bindings' secrets)",
                )
            )
    if (rn.get("unsigned") or 0) > 0:
        out.append(
            F(
                "hub",
                "runner-unsigned",
                "runner_unsigned",
                rn.get("unsigned"),
                0,
                2,
                f"CI runner: {rn.get('unsigned')} unsigned result(s)",
                "`hub health`; lands wait on signed records",
            )
        )
    if ctx.base("runner_queued") is not None and rose(rn.get("queued") or 0, ctx.base("runner_queued"), 8):
        out.append(
            F(
                "hub",
                "runner-queue",
                "runner_queued",
                rn.get("queued"),
                ctx.base("runner_queued"),
                1,
                f"CI runner queue {rn.get('queued')} (median {fmtn(ctx.base('runner_queued'))})",
                "lands wait: one slow or stuck job on the runner?",
            )
        )
    return {
        "metrics": m,
        "sigs": {},
        "findings": out,
        "line": f"checks {', '.join(f'{k} {v}' for k, v in checks.items()) or '?'}; api errors "
        f"{api.get('errors')}, p95 {fmtn(api.get('p95_ms'))} ms; runner queued {rn.get('queued')}; "
        f"egress errors {eg.get('errors')}, refused {eg.get('refused')}",
    }


def c_server(ctx: Ctx) -> dict[str, Any]:
    """Docker/disk on the server through the read-only `server.health` tool (one allowlisted SSH verb): counts by
    level and the summary numbers; finding messages (disk names) stay out."""
    d = run_json(["tool", "call", "server.health", "{}", "--json"], timeout=120) or {}
    data = d.get("data") or {}
    sm = data.get("summary") or {}
    lv = collections.Counter(f"{f.get('level')}/{f.get('status')}" for f in data.get("findings") or [])
    down = int(sm.get("containers") or 0) - int(sm.get("containers_running") or 0)
    m = {
        "new": int(data.get("new") or 0),
        "findings": sum(lv.values()),
        "array_pct": sm.get("array_pct"),
        "containers_down": down,
        "vms_down": int(sm.get("vms") or 0) - int(sm.get("vms_running") or 0),
    }
    out = []
    if m["new"]:
        out.append(
            F(
                "server",
                "new",
                "new findings",
                m["new"],
                0,
                1,
                f"server health: {m['new']} new finding(s) ({', '.join(f'{k} {v}' for k, v in lv.items())})",
                "skill server-health (`server.health`)",
            )
        )
    if ctx.base("containers_down") is not None and rose(down, ctx.base("containers_down"), 1, 1.0):
        out.append(
            F(
                "server",
                "containers-down",
                "containers_down",
                down,
                ctx.base("containers_down"),
                1,
                f"{down} containers not running (7-day median {fmtn(ctx.base('containers_down'))})",
                "`server.containers`: which ones; intended stops stay as they are",
            )
        )
    if isinstance(sm.get("array_pct"), (int, float)) and sm["array_pct"] >= 90:
        out.append(
            F(
                "server",
                "array",
                "array_pct",
                sm["array_pct"],
                ctx.base("array_pct"),
                1,
                f"array {sm['array_pct']:.0f} % full",
                "skill unraid-capacity",
            )
        )
    return {
        "metrics": m,
        "sigs": {},
        "findings": out,
        "line": f"{m['findings']} findings ({m['new']} new), array {fmtn(sm.get('array_pct'))} %, containers down "
        f"{down}, VMs down {m['vms_down']}",
    }


RISK_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) (HIGH|MEDIUM|LOW) (\w+) rule=([\w.-]+)")


def c_hooks(ctx: Ctx) -> dict[str, Any]:
    """Hook decisions, rule names only: the risk check's log (level, action, rule; never the why or the command),
    the no-approval hook's deny count. Vault-guard blocks come as counts per hook from traces."""
    now = ctx.now
    sigs: collections.Counter[str] = collections.Counter()
    high: collections.Counter[str] = collections.Counter()
    for ln in tail(home(".local", "state", "risk-check.log"), 1 << 20).splitlines():
        mm = RISK_RE.match(ln)
        if not mm:
            continue
        try:
            t = datetime.fromisoformat(mm.group(1)).replace(tzinfo=common.tz()).timestamp()
        except ValueError:
            continue
        if t >= now - DAY:
            sigs[f"risk {mm.group(2)} {mm.group(3)} {mm.group(4)}"] += 1
            if mm.group(2) == "HIGH":
                high[mm.group(4)] += 1
    denies = 0
    for ln in tail(home(".local", "state", "no-approval.log"), 1 << 20).splitlines():
        try:
            t = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=common.tz()).timestamp()
        except ValueError:
            continue
        denies += t >= now - DAY
    if denies:
        sigs["no-approval deny"] = denies
    m = {
        "risk_high": sum(high.values()),
        "risk_medium": sum(v for k, v in sigs.items() if " MEDIUM " in k),
        "no_approval": denies,
    }
    out = []
    for rule, n in high.items():
        if n >= 3:
            out.append(
                F(
                    "hooks",
                    f"risk:{rule}",
                    f"risk HIGH {rule}",
                    n,
                    ctx.base("risk_high"),
                    1,
                    f"risk-check rule {rule} blocked {n} commands in 24 h",
                    "which skill or script keeps trying it (risk-ok log, rule names); fix that caller",
                )
            )
    if rose(denies, ctx.base("no_approval"), 5):
        out.append(
            F(
                "hooks",
                "no-approval",
                "no_approval",
                denies,
                ctx.base("no_approval"),
                1,
                f"{denies} Bash approval prompts denied in 24 h (median {fmtn(ctx.base('no_approval'))})",
                "rewrite the commands that need approval (literal rm paths)",
            )
        )
    return {
        "metrics": m,
        "sigs": dict(sigs),
        "findings": out,
        "line": f"risk-check HIGH {m['risk_high']}, MEDIUM {m['risk_medium']}; no-approval denies {denies}",
    }


def c_board(ctx: Ctx) -> dict[str, Any]:
    rows = run_json(["board-lint", "--json"], timeout=60) or []
    c = collections.Counter(str(r.get("check")) for r in rows if isinstance(r, dict) and not r.get("acked"))
    return {
        "metrics": {"open": sum(c.values()), **{f"check:{k}": v for k, v in c.items()}},
        "sigs": {},
        "findings": [],
        "line": f"{sum(c.values())} open board-lint findings"
        + (f" ({', '.join(f'{k} {v}' for k, v in c.items())})" if c else ""),
    }


BACKUP_HA_MAX_H = 36  # ha-backup-pull copies one a day (04:05)
BACKUP_HA_MIN = 3  # it keeps 14; fewer than this means rotation or the pull is broken
BACKUP_GIT_MAX_H = 48  # brain auto commits twice an hour
BACKUP_ARCHIVE_MAX_H = 48  # brain-backup uploads one archive a night (03:47), counted from the archive's day
BACKUP_VERIFY_DAYS = 35  # brain-backup --verify runs monthly
HA_TAR_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{4}_[A-Za-z0-9_\-]{4,64}__[A-Za-z0-9._\-]{0,48}\.tar")
ARCHIVE_RE = re.compile(r"claude-bot-(\d{4}-\d{2}-\d{2})\.tar\.gz")


def _age_h(now: float, ts: float | None) -> float | None:
    return round((now - ts) / 3600, 1) if ts else None


def c_backups(ctx: Ctx) -> dict[str, Any]:
    """Backups the registry knows (schedules.toml feature "backup" + ha-backup-pull): the local HA backups (newest
    mtime, count), the brain git's last commit time, the newest nightly archive brain-backup logged (its name's date)
    and monthly restore-check failure notes. Names, mtimes, counts and commit times only: never a backup's content."""
    now, out = ctx.now, []
    d = home("backups", "ha")
    try:
        tars = [p for p in d.iterdir() if HA_TAR_RE.fullmatch(p.name)]
    except OSError:
        tars = []
    ha_new = max((mtime(p) for p in tars), default=0.0) or None
    ha_age = _age_h(now, ha_new)
    if ha_age is None or ha_age > BACKUP_HA_MAX_H:
        out.append(
            F(
                "backups",
                "ha:stale",
                "ha_age_h",
                ha_age,
                BACKUP_HA_MAX_H,
                3 if ha_age is None or ha_age > 3 * 24 else 2,
                "no HA backup in ~/backups/ha"
                if ha_age is None
                else f"newest HA backup in ~/backups/ha is {fmtn(ha_age)} h old ({hhmm(ha_new)}; "
                f"max {BACKUP_HA_MAX_H} h)",
                "`ha-backup-pull --status`; ~/.local/state/ha-backup-pull/cron.log; `cron-runs stats --job "
                "ha-backup-pull`; HA's own automatic backup",
            )
        )
    if tars and len(tars) < BACKUP_HA_MIN:
        out.append(
            F(
                "backups",
                "ha:few",
                "ha_count",
                len(tars),
                BACKUP_HA_MIN,
                2,
                f"only {len(tars)} HA backup(s) kept in ~/backups/ha (min {BACKUP_HA_MIN}, the pull keeps 14)",
                "`ha-backup-pull --status`; its --keep and free space on the appdata share",
            )
        )
    rc, o, _ = common.run(["git", "--git-dir", str(home(".brain.git")), "log", "-1", "--format=%ct"], timeout=30)
    git_ts = float(o.strip()) if rc == 0 and o.strip().isdigit() else None
    git_age = _age_h(now, git_ts)
    if git_age is None or git_age > BACKUP_GIT_MAX_H:
        out.append(
            F(
                "backups",
                "brain-git",
                "brain_git_age_h",
                git_age,
                BACKUP_GIT_MAX_H,
                3 if git_age is None else 2,
                "no readable commit in the brain git (~/.brain.git)"
                if git_age is None
                else f"brain git's last commit is {fmtn(git_age)} h old ({hhmm(git_ts)}; max {BACKUP_GIT_MAX_H} h)",
                '`brain auto` by hand once; `cron-runs stats --job brain-auto`; `brain-commit "msg"`',
            )
        )
    log = home(".local", "state", "brain-backup.log")
    days = sorted(set(ARCHIVE_RE.findall(tail(log))))  # archive names only, never other log text
    arc_ts = None
    if days:
        try:
            arc_ts = datetime.fromisoformat(days[-1]).replace(tzinfo=common.tz()).timestamp()
        except ValueError:
            arc_ts = None
    arc_age = _age_h(now, arc_ts)
    if arc_age is None or arc_age > BACKUP_ARCHIVE_MAX_H:
        out.append(
            F(
                "backups",
                "brain-archive",
                "brain_archive_age_h",
                arc_age,
                BACKUP_ARCHIVE_MAX_H,
                2,
                "brain-backup logged no archive name in ~/.local/state/brain-backup.log"
                if arc_age is None
                else f"newest brain-backup archive is from {days[-1]} ({fmtn(arc_age)} h since that day began; "
                f"max {BACKUP_ARCHIVE_MAX_H} h)",
                "`brain-backup --list`; the brain-backup log through secretscan --redact; `unraid-ssh mode`",
            )
        )
    verify = [
        p for p in home("inbox").glob("brain-backup-verify-failed-*.md") if mtime(p) >= now - BACKUP_VERIFY_DAYS * DAY
    ]
    if verify:
        out.append(
            F(
                "backups",
                "brain-verify",
                "verify_fail_notes",
                len(verify),
                0,
                2,
                f"{len(verify)} brain-backup restore check failure note(s) in {BACKUP_VERIFY_DAYS} d",
                "`brain-backup --verify` by hand; fix the archive's contents list, not the note",
            )
        )
    m = {
        "ha_count": len(tars),
        "ha_age_h": ha_age,
        "brain_git_age_h": git_age,
        "brain_archive_age_h": arc_age,
        "verify_fail_notes": len(verify),
    }
    return {
        "metrics": m,
        "sigs": {},
        "findings": out,
        "line": f"HA {len(tars)} kept, newest {fmtn(ha_age)} h; brain git {fmtn(git_age)} h; "
        f"brain archive {fmtn(arc_age)} h; verify failures {len(verify)}",
    }


def run_text(argv: list[str], timeout: float = 60, system: bool = False) -> str:
    """stdout of a command (an own ~/bin verb, or a system binary with system=True); a failure raises."""
    rc, out, _err = common.run([argv[0] if system else common.bin_path(argv[0]), *argv[1:]], timeout)
    if rc != 0:
        raise RuntimeError(f"{os.path.basename(argv[0])} exit {rc}")
    return out


def vault_repo() -> Path:
    v = os.environ.get("LOOP_VAULT_REPO", "")
    return Path(v) if v else paths.home() / "opt" / "claude-vault"


GH_RE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$")
VAULT_FAIL = ("failed", "error")


def _vault_repo_counts(repo: Path) -> tuple[int | None, int | None]:
    """(commits in 24 h, open issues) of the vault repo; None where it can't be read. The GitHub repo comes from the
    checkout's own remote (no name in this code)."""
    if not repo.is_dir():
        return None, None
    try:
        commits = len(
            run_text(["git", "-C", str(repo), "log", "--since=24 hours ago", "--format=%h"], 30, True).split()
        )
    except RuntimeError:
        commits = None
    issues = None
    try:
        m = GH_RE.search(run_text(["git", "-C", str(repo), "remote", "get-url", "origin"], 15, True).strip())
        if m:
            d, err = common.run_json(
                ["gh", "issue", "list", "-R", m[1], "--state", "open", "--json", "number", "--limit", "200"], 60
            )
            issues = len(d) if isinstance(d, list) and not err else None
    except RuntimeError:
        pass
    return commits, issues


def c_vault(ctx: Ctx) -> dict[str, Any]:
    """The privacy vault through its health verbs only (counts and durations; never its data, never a text or content
    flag): `vault status`, `vault metrics --days 1`, `vault alerts` (never to the inbox), `vault audit --days 1` as
    counts per event and state (no other audit field is read out), `vault admin logs` as error signatures only, and
    the vault repo (commits in 24 h, open issues). Route names are kept; accounts, chats and queries never."""
    st = run_json(["vault", "status", "--json"], timeout=60) or {}
    mt = run_json(["vault", "metrics", "--days", "1", "--json"], timeout=90) or {}
    llm, ml = st.get("llm") or {}, mt.get("llm") or {}
    routes = [r for r in mt.get("routes") or [] if isinstance(r, dict)]
    slow = max(routes, key=lambda r: float(r.get("p95_s") or 0), default={})
    wait = (ml.get("wait") or {}).get("live") or {}
    feeds = sum(1 for a in (st.get("gmail") or {}).get("accounts") or [] if isinstance(a, dict) and a.get("error"))
    feeds += 1 if (st.get("ha") or {}).get("error") else 0
    alerts = [x for x in run_text(["vault", "alerts"], 60).splitlines() if x.strip() and "no alerts" not in x.lower()]
    audit = [r for r in run_json(["vault", "audit", "--days", "1", "--json"], timeout=90) or [] if isinstance(r, dict)]
    failed = collections.Counter(str(r.get("event")) for r in audit if r.get("state") in VAULT_FAIL)
    try:
        sigs = signatures(run_text(["vault", "admin", "logs", "-n", "400"], 90))
    except RuntimeError:
        sigs = collections.Counter()
    commits, issues = _vault_repo_counts(vault_repo())
    m = {
        "calls": sum(int(r.get("calls") or 0) for r in routes),
        "errors": sum(int(r.get("errors") or 0) + int(r.get("timeouts") or 0) for r in routes),
        "slow_p95_s": slow.get("p95_s"),
        "llm_busy_pct": round(100 * float(ml.get("busy_share") or 0), 1),
        "wait_live_p95_s": wait.get("p95_s"),
        "llm_dropped": ml.get("dropped"),
        "backlog_max": mt.get("backlog_max"),
        "llm_healthy": 1 if llm.get("healthy") else 0,
        "llm_restarts": llm.get("restarts"),
        "feeds_error": feeds,
        "alerts": len(alerts),
        "audit_events": len(audit),
        "audit_failed": sum(failed.values()),
        "commits_24h": commits,
        "open_issues": issues,
    }
    out = []
    if alerts:
        out.append(
            F(
                "vault",
                "alerts",
                "alerts",
                len(alerts),
                0,
                2,
                f"vault alerts: {len(alerts)}",
                "skill vault (`vault alerts`)",
            )
        )
    if st and not llm.get("healthy"):
        out.append(
            F(
                "vault",
                "llm",
                "llm_healthy",
                0,
                1,
                2,
                f"vault LLM not healthy (state {scrub(llm.get('state'))[:24]})",
                "`vault status`; skill vault § troubleshooting",
            )
        )
    if rose(m["errors"], ctx.base("errors"), 3):
        out.append(
            F(
                "vault",
                "errors",
                "errors",
                m["errors"],
                ctx.base("errors"),
                1,
                f"vault route errors + timeouts {m['errors']} in 24 h (7-day median {fmtn(ctx.base('errors'))})",
                "`vault metrics --days 1` (routes); skill vault § troubleshooting",
            )
        )
    if rose(m["audit_failed"], ctx.base("audit_failed"), 3):
        top = ", ".join(f"{k} {v}" for k, v in failed.most_common(3))
        out.append(
            F(
                "vault",
                "audit-failed",
                "audit_failed",
                m["audit_failed"],
                ctx.base("audit_failed"),
                1,
                f"vault jobs failed {m['audit_failed']} in 24 h ({top})",
                "`vault audit --days 1 --json` counted per event (no other field)",
            )
        )
    w, wb = m["wait_live_p95_s"], ctx.base("wait_live_p95_s")
    if isinstance(w, (int, float)) and w >= 10 and wb is not None and w > 2 * wb:
        out.append(
            F(
                "vault",
                "wait",
                "wait_live_p95_s",
                w,
                wb,
                1,
                f"vault LLM wait (live) p95 {fmtn(w)} s (7-day median {fmtn(wb)} s)",
                "`vault metrics --days 1` (llm busy, queue); the scorecard's vault_wait_p95",
            )
        )
    if feeds:
        out.append(
            F("vault", "feeds", "feeds_error", feeds, 0, 1, f"vault feeds with an error: {feeds}", "`vault status`")
        )
    return {
        "metrics": m,
        "sigs": dict(sigs),
        "findings": out,
        "line": f"{m['calls']} calls, {m['errors']} errors, slowest route p95 {fmtn(m['slow_p95_s'])} s "
        f"({scrub(slow.get('route') or '-')[:40]}), LLM wait p95 {fmtn(w)} s, {m['alerts']} alerts, "
        f"{m['audit_failed']}/{m['audit_events']} audit failed, repo {fmtn(commits)} commits, "
        f"{fmtn(issues)} open issues",
    }


HA_WATCH_RE = re.compile(
    r"last run: (\S+?): \w+: (\d+) findings \((\d+) known-offline\), (\d+) new, (\d+) recovered, (\d+) pending"
)
REMINDER_LATE_S = 15 * 60


def _tool(name: str, args: str, errs: list[str]) -> dict[str, Any] | None:
    try:
        d = run_json(["tool", "call", name, args, "--json"], timeout=90) or {}
    except Exception as e:
        errs.append(f"{name} {type(e).__name__}")
        return None
    if not d.get("ok"):
        errs.append(f"{name} not ok")
        return None
    return d.get("data") or {}


def _ts(s: Any) -> float | None:
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=common.tz())).timestamp()


def c_parts(ctx: Ctx) -> dict[str, Any]:
    """Parts the other collectors see only as job failures, through read-only typed tools and status verbs: each
    of my own containers (claude-*) running (`server.docker`: names and states), the media apps (`media.health`), the
    voice relay (`voice.status`), the web agent (`web.stats` over 1 d), owner reminders past due (`reminders.list`:
    times only, the text is never read out) and the HA health watch's last run (`ha-health-watch status`: counts).
    The phone app's own sources come with its own change."""
    errs: list[str] = []
    m: dict[str, Any] = {}
    out = []
    dk = _tool("server.docker", "{}", errs)
    if dk is not None:
        own = {
            str(r.get("name")): str(r.get("state"))
            for r in dk.get("containers") or []
            if str(r.get("name")).startswith("claude")
        }
        down = sorted(n for n, s in own.items() if s != "running")
        m.update(own_containers=len(own), own_down=len(down))
        if down:
            out.append(
                F(
                    "parts",
                    "own-down",
                    "own_down",
                    len(down),
                    0,
                    2,
                    f"own container(s) not running: {', '.join(down)}",
                    "`server.docker`, `hub health`; the vault container is his to start",
                )
            )
    md = _tool("media.health", "{}", errs)
    if md is not None:
        m.update(media_problems=len(md.get("problems") or []), media_apps=len(md.get("checked") or []))
        if m["media_problems"]:
            out.append(
                F(
                    "parts",
                    "media",
                    "media_problems",
                    m["media_problems"],
                    ctx.base("media_problems"),
                    1,
                    f"media stack: {m['media_problems']} problem(s) in {m['media_apps']} apps",
                    "skill media-requests (`media.health`, `media.doctor`)",
                )
            )
    vo = _tool("voice.status", "{}", errs)
    if vo is not None:
        m["voice_up"] = 1 if vo.get("up") else 0
        if not vo.get("up"):
            out.append(F("parts", "voice", "voice_up", 0, 1, 2, "voice relay down", "`voice.status`, `hub health`"))
    wb = _tool("web.stats", '{"days": 1}', errs)
    if wb is not None:
        s = wb.get("stats") or {}
        m.update(web_tasks=s.get("tasks"), web_failed=int(s.get("failed") or 0) + int(s.get("expired") or 0))
        m["web_guard_blocks"] = s.get("guard_blocks")
        if rose(m["web_failed"], ctx.base("web_failed"), 2):
            out.append(
                F(
                    "parts",
                    "web",
                    "web_failed",
                    m["web_failed"],
                    ctx.base("web_failed"),
                    1,
                    f"web agent: {m['web_failed']} failed or expired task(s) in 24 h",
                    "skill web-agent (`web.stats`, `web.sessions`)",
                )
            )
    rm = _tool("reminders.list", "{}", errs)
    if rm is not None:
        ats = [_ts(i.get("at")) for i in rm.get("items") or [] if isinstance(i, dict)]
        late = sum(1 for t in ats if t is not None and t < ctx.now - REMINDER_LATE_S)
        m.update(reminders=len(ats), reminders_late=late)
        if late:
            out.append(
                F(
                    "parts",
                    "reminders-late",
                    "reminders_late",
                    late,
                    0,
                    2,
                    f"{late} reminder(s) more than 15 min past due and still pending",
                    "`reminders.list`, `hub wf status` (the reminder timers); skill reminders",
                )
            )
    try:
        hm = HA_WATCH_RE.search(run_text(["ha-health-watch", "status"], 60))
    except RuntimeError as e:
        errs.append(f"ha-health-watch {e}")
        hm = None
    if hm:
        last = _ts(hm[1])
        m.update(ha_findings=int(hm[2]), ha_known=int(hm[3]), ha_new=int(hm[4]), ha_pending=int(hm[6]))
        m["ha_age_h"] = round((ctx.now - last) / 3600, 1) if last else None
        if m["ha_new"]:
            out.append(
                F(
                    "parts",
                    "ha-new",
                    "ha_new",
                    m["ha_new"],
                    0,
                    1,
                    f"HA health watch: {m['ha_new']} new finding(s)",
                    "`ha-health-watch status`; skill home-assistant",
                )
            )
        if m["ha_age_h"] is not None and m["ha_age_h"] > FRESH_H:
            out.append(
                F(
                    "parts",
                    "ha-stale",
                    "ha_age_h",
                    m["ha_age_h"],
                    24,
                    1,
                    f"HA health watch last ran {fmtn(m['ha_age_h'])} h ago",
                    "its cron line (`cron-runs`), `ha-health-watch --dry-run`",
                )
            )
    if not m:
        raise RuntimeError("no part answered: " + ", ".join(errs[:4]))
    m["sources_failed"] = len(errs)
    return {
        "metrics": m,
        "sigs": {},
        "findings": out,
        "line": f"own containers down {fmtn(m.get('own_down'))}/{fmtn(m.get('own_containers'))}, media problems "
        f"{fmtn(m.get('media_problems'))}, voice up {fmtn(m.get('voice_up'))}, web failed {fmtn(m.get('web_failed'))}, "
        f"reminders late {fmtn(m.get('reminders_late'))}, HA new {fmtn(m.get('ha_new'))}"
        + (f"; not read: {', '.join(errs)}" if errs else ""),
    }


def c_coverage(ctx: Ctx) -> dict[str, Any]:
    """This setup's parts against the loop coverage map (packages/loop/coverage.toml, `loop coverage`): a registry
    member (job feature, container, tool domain, egress binding, skill, agent) no part claims, a part with no loop
    step, a step with no collector, or an agent-file marker that is gone."""
    from . import coverage

    rep = coverage.report()
    pr, n = rep["problems"], rep["count"]
    m = {
        "parts": len(rep["rows"]),
        "problems": n,
        "unmapped": sum(len(v) for v in pr["unmapped"].values()),
        "uncovered": len(pr["uncovered"]),
        "in_progress": sum(1 for r in rep["rows"] if r.get("status") == "in-progress"),
    }
    out = []
    if n:
        lines = coverage.problem_lines(pr)
        out.append(
            F(
                "coverage",
                "gaps",
                "problems",
                n,
                ctx.base("problems"),
                1,
                f"loop coverage: {n} problem(s): " + "; ".join(lines[:3]) + (" …" if len(lines) > 3 else ""),
                "claim it in packages/loop/coverage.toml and give the part a step (`loop coverage --gaps`)",
            )
        )
    return {
        "metrics": m,
        "sigs": {},
        "findings": out,
        "line": f"{m['parts']} parts, {n} coverage problem(s), {m['in_progress']} in progress",
    }


PHONE_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
PHONE_OFFLINE_H = 6


def _phone_ids(xs: Any) -> list[str]:
    """Self-test check ids as the app names them (a fixed vocabulary such as `vpn`, `fsi`); anything else is dropped."""
    return sorted({x for x in xs or [] if isinstance(x, str) and PHONE_ID.match(x)})


def _phone_mirror(sql: str, args: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    """Read-only SQL on the brain's mirror of the gateway DB (the allow-listed export: timings, frame types)."""
    from .scorecard import phone_mirror

    p = phone_mirror()
    if not p.exists():
        raise FileNotFoundError("phonegw-mirror.db")
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def c_phone(ctx: Ctx) -> dict[str, Any]:
    """The phone app: the Android app, its gateway in claude-hub-phone and phone-produce here. Counts, check ids and
    timings only (never coordinates, zone names, push, chat or note text): `phone status --json` (gateway uptime,
    self-test check ids, push p95 over 30 d, today's app budget), `hub phone status --json` (the loader's state), the
    brain's mirror of the gateway DB (push ack times and app connects for 24 h vs the 7 days before), crash notes in
    the event ledger (kind and time), phone-inbox cron runs (exit codes), phone.* gateway tool errors."""
    from core import cronruns

    from .scorecard import pct, src_push

    now = ctx.now
    out: list[dict[str, Any]] = []
    sigs: collections.Counter[str] = collections.Counter()
    m: dict[str, Any] = {}
    broken: list[str] = []

    def f(key: str, metric: str, v: Any, base: Any, sev: int, msg: str, fix: str) -> None:
        out.append(F("phone", key, metric, v, base, sev, msg, fix))

    # the gateway and the device
    try:
        st = run_json(["phone", "status", "--json"], timeout=40) or {}
    except RuntimeError:
        st = {}
    up = bool(st.get("ok"))
    m["gw_up"] = int(up)
    upt = st.get("uptime_s")
    if up and isinstance(upt, (int, float)):
        m["gw_uptime_h"] = round(upt / 3600, 1)
        m["gw_restarted_24h"] = int(upt < DAY)
        started = now - upt
        why = after_change(started)
        if upt < DAY and not why:
            f(
                "gw-restart",
                "gw_restarted_24h",
                1,
                ctx.base("gw_restarted_24h"),
                1,
                f"phone gateway restarted at {hhmm(started)} with no land or deploy before it",
                "`hub phone status`; the loader's exits are in claude-hub-phone's container log",
            )
    try:
        ld = (run_json(["hub", "phone", "status", "--json"], timeout=30) or {}).get("loader") or {}
    except RuntimeError:
        ld = {}
        broken.append("hub phone status")
    lstate = str(ld.get("state") or "?")
    m["loader_running"] = int(lstate == "running")
    if not up:
        f(
            "gw-down",
            "gw_up",
            0,
            1,
            2,
            f"phone gateway not answering `phone status` (loader {lstate})",
            "`hub phone status`, `phone status`; the app falls back to Telegram while it is down",
        )
    devs = [d for d in st.get("devices") or [] if isinstance(d, dict) and d.get("status") == "active"]
    red: set[str] = set()
    opt: set[str] = set()
    unknown: set[str] = set()
    green = total = 0
    st_at: list[float] = []
    offline_h: list[float] = []
    for d in devs:
        s = d.get("selftest") or {}
        if s:
            green += int(s.get("green") or 0)
            total += int(s.get("total") or 0)
            red |= set(_phone_ids(s.get("red")))
            opt |= set(_phone_ids(s.get("optional_red")))
            unknown |= set(_phone_ids(s.get("unknown")))
            if isinstance(s.get("at"), (int, float)):
                st_at.append(float(s["at"]))
        if not d.get("online") and isinstance(d.get("last_seen"), (int, float)):
            offline_h.append(max(0.0, (now - float(d["last_seen"])) / 3600))
    app = [d.get("app_budget", {}).get("app") or {} for d in devs if isinstance(d.get("app_budget"), dict)]
    m.update(
        {
            "devices": len(devs),
            "devices_offline": len(offline_h),
            "app_code": max((int(d.get("app_code") or 0) for d in devs), default=0),
            "selftest_green": green,
            "selftest_total": total,
            "selftest_red": len(red),
            "selftest_optional_red": len(opt),
            "selftest_unknown": len(unknown),
            "selftest_age_h": round((now - max(st_at)) / 3600, 1) if st_at else None,
            "push_p95_30d_s": max(
                (
                    round(float(d["push_p95_ms"]) / 1000, 1)
                    for d in devs
                    if isinstance(d.get("push_p95_ms"), (int, float))
                ),
                default=None,
            ),
            "reconnects_today": sum(int(a.get("reconnects_d") or 0) for a in app),
            "budget_over": sum(len(d.get("app_budget", {}).get("over") or []) for d in devs),
            **{f"red:{c}": 1 for c in red},
            **{f"optional_red:{c}": 1 for c in opt},
        }
    )
    for c in sorted(red):
        f(
            f"selftest:{c}",
            f"red:{c}",
            1,
            ctx.base(f"red:{c}"),
            1,
            f"phone self-test: required check {c} red ({green} of {total} green; the app sent it "
            f"{fmtn(m['selftest_age_h'])} h ago, it re-sends on a change)",
            "the app's Settings checklist (Setup); a check that is red by design is fixed in "
            "~/opt/phone-app/claude (setup/SetupChecks.kt)",
        )
    if offline_h and max(offline_h) >= PHONE_OFFLINE_H:
        f(
            "offline",
            "devices_offline",
            len(offline_h),
            ctx.base("devices_offline"),
            1,
            f"phone app offline for {max(offline_h):.0f} h",
            "`phone status` (route, keepalive); pushes wait in the queue and fall back to Telegram",
        )

    # pushes and connects from the mirror (it keeps 8 days: the source's own 7-day baseline)
    try:
        pushes = src_push(now - 8 * DAY, now)
        hellos = [
            float(t) for (t,) in _phone_mirror("SELECT at FROM inbound WHERE t = 'hello' AND at >= ?", (now - 8 * DAY,))
        ]
        fetched = _phone_mirror("SELECT v FROM mirror WHERE k = 'fetched'", ())
    except (OSError, sqlite3.Error, ValueError) as e:
        pushes, hellos, fetched = [], [], []
        broken.append(f"mirror {type(e).__name__}")
    p24 = [x for t, x in pushes if t >= now - DAY]
    p7 = [x for t, x in pushes if now - 8 * DAY <= t < now - DAY]
    q24, q7 = pct(p24, 0.95), pct(p7, 0.95)
    m["pushes_acked"] = len(p24)
    m["push_p95_s"] = round(q24, 1) if q24 is not None else None
    m["push_p95_base_s"] = round(q7, 1) if q7 is not None else None
    if q24 is not None and len(p24) >= 3 and q24 >= 30 and (q7 is None or q24 > 2 * q7):
        f(
            "push-p95",
            "push_p95_s",
            m["push_p95_s"],
            m["push_p95_base_s"],
            1,
            f"phone push delivery p95 {fmtn(m['push_p95_s'])} s over {len(p24)} pushes (7 days before "
            f"{fmtn(m['push_p95_base_s'])} s)",
            "`phone status` (pending, keepalive, route), `phone pushes`; scorecard push_p95",
        )
    days = [sum(1 for t in hellos if now - (k + 1) * DAY <= t < now - k * DAY) for k in range(1, 8)]
    m["connects"] = sum(1 for t in hellos if t >= now - DAY)
    m["connects_base"] = round(statistics.median(days), 1) if hellos else None
    if rose(m["connects"], m["connects_base"], 30):
        f(
            "reconnects",
            "connects",
            m["connects"],
            m["connects_base"],
            1,
            f"phone app connected {m['connects']} times in 24 h (daily median {fmtn(m['connects_base'])})",
            "`phone status` (route, keepalive, idle probes, app budget): a flapping route costs battery",
        )
    if fetched:
        try:
            age = (now - float(fetched[0][0])) / 60
        except (TypeError, ValueError):
            age = None
        m["mirror_age_min"] = round(age, 1) if age is not None else None
        if age is not None and age > 30:
            f(
                "mirror-stale",
                "mirror_age_min",
                round(age),
                None,
                1,
                f"phone gateway mirror {age:.0f} min old (refreshed once a minute by `hub phone inbox`)",
                "`hub phone inbox --once`; usage, board and dash-data read the mirror",
            )

    # app crashes (the gateway's crash notes in the ledger: at most 5 a day per device; ANRs are not reported)
    try:
        rows = run_json(["events", "list", "--all", "--kind", "note.phone-crash", "--limit", "100", "--json"], 30) or []
    except RuntimeError:
        rows = []
        broken.append("events list")
    ts = [float(r.get("ts") or 0) for r in rows if isinstance(r, dict)]
    m["crashes"] = sum(1 for t in ts if t >= now - DAY)
    m["crashes_7d"] = sum(1 for t in ts if now - 8 * DAY <= t < now - DAY)
    if m["crashes"]:
        f(
            "crashes",
            "crashes",
            m["crashes"],
            m["crashes_7d"],
            2 if m["crashes"] >= 3 else 1,
            f"phone app crashed {m['crashes']} time(s) in 24 h (7 days before: {m['crashes_7d']})",
            "the note.phone-crash events (exception class and code path); fix in ~/opt/phone-app/claude",
        )

    # phone-inbox (cron, every minute: the brain's side of the app's taps, chat, notes and the mirror)
    runs = [r for r in cronruns.runs(now - 8 * DAY, now) if r["job"] == "phone-inbox"]
    r24 = [r for r in runs if r["ts"] >= now - DAY]
    m["inbox_runs"] = len(r24)
    m["inbox_failed"] = sum(1 for r in r24 if not r["ok"])
    sigs.update(f"phone-inbox exit {r['rc']}" for r in r24 if not r["ok"])
    if rose(m["inbox_failed"], ctx.base("inbox_failed"), 10):
        f(
            "inbox-fail",
            "inbox_failed",
            m["inbox_failed"],
            ctx.base("inbox_failed"),
            1,
            f"phone-inbox failed {m['inbox_failed']} of {len(r24)} runs in 24 h "
            f"(median {fmtn(ctx.base('inbox_failed'))})",
            "`cron-runs stats --job phone-inbox`; exit 3 = hub unreachable (a hub deploy minute is expected)",
        )

    # the phone.* tools through the hub gateway (phone.lookup runs phone-produce and the gateway's other lookups)
    try:
        tstat = (run_json(["tool", "stats", "--days", "1", "--json"], timeout=60) or {}).get("tools") or {}
    except (RuntimeError, AttributeError):
        tstat = {}
        broken.append("tool stats")
    tools = {k: v for k, v in tstat.items() if str(k).startswith("phone.") and isinstance(v, dict)}
    terr = {k: sum(int(n) for n in (v.get("errors") or {}).values()) for k, v in tools.items()}
    m["tool_calls"] = sum(int(v.get("calls") or 0) for v in tools.values())
    m["tool_errors"] = sum(terr.values())
    for k, v in tools.items():
        for code, n in (v.get("errors") or {}).items():
            sigs[f"{k} {code}"] += int(n)
    if rose(m["tool_errors"], ctx.base("tool_errors"), 5):
        f(
            "tool-errors",
            "tool_errors",
            m["tool_errors"],
            ctx.base("tool_errors"),
            1,
            f"phone.* tool errors {m['tool_errors']} in 24 h ("
            + ", ".join(f"{k} {n}" for k, n in terr.items() if n)
            + ")",
            "`tool stats --days 1`; phone.lookup = phone-produce and the gateway's lookups",
        )
    if broken:
        f(
            "source-broken",
            "sources",
            len(broken),
            0,
            1,
            f"phone collector: {len(broken)} source(s) unreadable ({', '.join(broken)})",
            "run the named command by hand",
        )
    line = (
        f"gateway {'up ' + fmtn(m.get('gw_uptime_h')) + ' h' if up else 'down'} (loader {lstate}); "
        f"self-test {green}/{total} green"
        + (f", red {', '.join(sorted(red))}" if red else "")
        + (f", optional red {len(opt)}" if opt else "")
        + f"; push p95 {fmtn(m['push_p95_s'])} s (n {len(p24)}, 30 d {fmtn(m['push_p95_30d_s'])} s); "
        f"connects {m['connects']} (median {fmtn(m['connects_base'])}); crashes {m['crashes']}; "
        f"phone-inbox failed {m['inbox_failed']}/{len(r24)}; phone.* errors {m['tool_errors']}"
    )
    return {"metrics": m, "sigs": dict(sigs), "findings": out, "line": line}


COLLECTORS: dict[str, tuple[Callable[[Ctx], dict[str, Any]], str]] = {
    "land": (c_land, "land/deploy log, CI records, shared CI blockers"),
    "hubjobs": (c_hubjobs, "hub workflow runs, catch-up, keepers"),
    "joberr": (c_joberr, "job .err files, failure notes"),
    "cronlogs": (c_cronlogs, "crontab logs"),
    "traces": (c_traces, "traces.db (tool errors, hook blocks, first reply, agents)"),
    "phoenix": (c_phoenix, "Phoenix spans (latency, errors, tokens, cost, slow traces)"),
    "agents": (c_agents, "agent-watch transitions"),
    "events": (c_events, "event ledger (unacked, emit errors, restarts)"),
    "slo": (c_slo, "SLO tiles 24 h vs 7 d"),
    "evals": (c_evals, "evals history (infra errors)"),
    "hub": (c_hub, "hub health (checks, gateway, runner, egress)"),
    "server": (c_server, "server health (docker, disks; read-only verb)"),
    "hooks": (c_hooks, "hook decisions (rule names only)"),
    "board": (c_board, "board-lint"),
    "backups": (c_backups, "backup freshness: HA backups, brain git, brain archive, restore check (names/mtimes)"),
    "vault": (c_vault, "vault health counts (status, metrics, alerts, audit per event, log signatures, repo)"),
    "parts": (c_parts, "part checks (own containers, media, voice, web agent, reminders late, HA health watch)"),
    "coverage": (c_coverage, "loop coverage map vs the live registries"),
    "phone": (c_phone, "phone app: gateway, self-test, pushes, connects, crashes, phone-inbox, phone.* tools"),
}


# ------------------------------------------------------------------------------------------------ the pass
def fid(key: str) -> str:
    """The finding id: the same as the self-review's (sha1 of "sh|<key>"), so `self-review ack ID`, the report and
    the Phoenix annotation's identifier name one thing."""
    return hashlib.sha1(f"sh|{key}".encode()).hexdigest()[:8]


def _clean(f: dict[str, Any]) -> dict[str, Any]:
    f = dict(f)
    for k in ("msg", "fix"):
        f[k] = scrub(f.get(k) or "")
    f["id"] = fid(f["key"])
    return f


def run(
    write: bool = True, now: float | None = None, only: list[str] | None = None, budget_s: float = HEALTH_BUDGET
) -> dict[str, Any]:
    fx = os.environ.get("LOOP_HEALTH_FIXTURE", "")
    if fx:
        res = json.loads(Path(fx).read_text(encoding="utf-8"))
        res["findings"] = [_clean(f) for f in res.get("findings") or []]
        if write:
            _save_latest(res, common.now())
        return res
    global _DEPLOYS
    _DEPLOYS = None
    t0 = time.monotonic()
    now = now if now is not None else common.now()
    only = only or [x for x in os.environ.get("HEALTH_SOURCES", "").split(",") if x] or list(COLLECTORS)
    hist = load_history()
    readings: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_one, name, Ctx(name, now, hist)): name for name in only if name in COLLECTORS}
        done, pending = cf.wait(futs, timeout=budget_s)
        for fu in done:
            name = futs[fu]
            r, err = fu.result()
            if err:
                errors[name] = err
            elif r is not None:
                readings[name] = r
        for fu in pending:
            errors[futs[fu]] = f"over the {budget_s:.0f} s budget"
            fu.cancel()
    findings: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    for name in only:
        r = readings.get(name)
        if not r:
            continue
        findings += r.get("findings") or []
        ctx = Ctx(name, now, hist)
        seen = ctx.seen()
        sigs: dict[str, int] = {}
        for k, v in (r.get("sigs") or {}).items():
            if v:  # signatures that differ only in digits ("exit 3", "exit 124") fold into one: their counts add up
                s = norm_sig(scrub(k))
                sigs[s] = sigs.get(s, 0) + int(v)
        r["sigs"] = sigs
        new = sorted((s for s in sigs if seen is not None and s not in seen), key=lambda s: -sigs[s])
        for s, n in sigs.items():
            b = [int((h.get("sigs") or {}).get(s, 0)) for h in ctx.hist]
            clusters.append(
                {"source": name, "sig": s, "n": n, "base": round(sum(b) / 7, 1) if b else None, "new": s in new}
            )
        if new:
            h = hashlib.sha256("\n".join(new).encode()).hexdigest()[:6]
            findings.append(
                F(
                    name,
                    f"newsig:{h}",
                    "new error signatures",
                    len(new),
                    0,
                    1,
                    f"{len(new)} new error signature(s) in {name} (not seen in 7 d): "
                    + "; ".join(f"{s} ×{sigs[s]}" for s in new[:4])
                    + (" …" if len(new) > 4 else ""),
                    "the signature's code path; ranked by the local LLM in the report",
                )
            )
    findings = sorted((_clean(f) for f in findings), key=lambda f: (-f["sev"], f["source"], f["key"]))
    clusters.sort(key=lambda c: (not c["new"], -c["n"]))
    res = {
        "at": now,
        "findings": findings,
        "readings": {k: _public(v) for k, v in readings.items()},
        "clusters": clusters[:40],
        "errors": errors,
        "secs": round(time.monotonic() - t0, 1),
        "phoenix": [],
    }
    if write:
        _save_history(now, readings, hist)
        _save_latest(res, now)
    return res


def _one(name: str, ctx: Ctx) -> tuple[dict[str, Any] | None, str | None]:
    t = time.monotonic()
    try:
        r = COLLECTORS[name][0](ctx)
        r["secs"] = round(time.monotonic() - t, 1)
        r["line"] = scrub(r.get("line") or "")
        return r, None
    except Exception as e:  # a broken source never stops the pass
        return None, scrub(f"{type(e).__name__}: {str(e)[:160]}")


def _public(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("metrics", "base", "line", "secs", "slow", "tools", "errors")
    return {k: r[k] for k in keep if k in r}


def _save_history(now: float, readings: dict[str, dict[str, Any]], hist: list[dict[str, Any]]) -> None:
    day = datetime.fromtimestamp(now, common.tz()).date()
    lo = (day - timedelta(days=HIST_DAYS)).isoformat()
    rows = [
        h for h in hist if str(h.get("day")) >= lo and not (h.get("day") == day.isoformat() and h["source"] in readings)
    ]
    for name, r in readings.items():
        mtr = {k: v for k, v in (r.get("metrics") or {}).items() if isinstance(v, (int, float))}
        rows.append({"day": day.isoformat(), "ts": now, "source": name, "metrics": mtr, "sigs": r.get("sigs") or {}})
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True, mode=common.STATE_MODE)
    tmp = d / "history.jsonl.tmp"
    tmp.write_text("".join(json.dumps(h, sort_keys=True) + "\n" for h in rows), encoding="utf-8")
    os.chmod(tmp, common.FILE_MODE)
    os.replace(tmp, d / "history.jsonl")


def _save_latest(res: dict[str, Any], now: float) -> None:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True, mode=common.STATE_MODE)
    common.save(
        "health/latest.json", {"at": now, "findings": res.get("findings") or [], "errors": res.get("errors") or {}}
    )


def signal() -> dict[str, Any] | None:
    d = common.load("health/latest.json", None)
    if not isinstance(d, dict) or common.now() - float(d.get("at") or 0) > FRESH_H * 3600:
        return None
    return signal_from(d)


def signal_from(res: dict[str, Any]) -> dict[str, Any]:
    """The loop's view of a pass: the findings' metric fields (no readings, no clusters)."""
    keep = ("id", "key", "source", "metric", "now", "base", "sev", "msg", "fix", "link")
    return {
        "at": res.get("at"),
        "findings": [{k: f.get(k) for k in keep} for f in res.get("findings") or []][:12],
        "errors": res.get("errors") or {},
    }


def phoenix_writeback(res: dict[str, Any], new_ids: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Findings that carry a span -> span annotations; the day's failing spans and slowest traces -> datasets."""
    from . import phoenix

    if not phoenix.enabled_writes():
        return []
    L = []
    fs = [dict(f, new=f["id"] in new_ids) for f in res.get("findings") or []]
    try:
        n = phoenix.annotate(fs)
        if n:
            L.append(f"{n} span annotation(s) '{phoenix.ANNOTATION}' written")
    except Exception as e:
        L.append(f"annotations failed: {type(e).__name__}")
    ph = (res.get("readings") or {}).get("phoenix") or {}
    day = common.today().isoformat()
    by_name = {f.get("span_id"): f["id"] for f in fs if f.get("span_id")}
    rows = [
        {
            "span_id": e["span_id"],
            "input": {"span_name": e["name"]},
            "output": {"status": "ERROR"},
            "meta": {"trace_id": e["trace_id"], "day": day, "finding": by_name.get(e["span_id"])},
        }
        for e in ph.get("errors") or []
    ][:50]
    slow = [
        {
            "span_id": t["trace_id"],
            "input": {"root": t.get("root")},
            "output": {"ms": t.get("ms")},
            "meta": {"trace_id": t["trace_id"], "day": day},
        }
        for t in ph.get("slow") or []
    ]
    for name, desc, rr in (
        (phoenix.DS_FAILING, "Example error spans, one per span name and day (setup health)", rows),
        (phoenix.DS_SLOW, "Slowest traces of the day (setup health)", slow),
    ):
        if not rr:
            continue
        try:
            if name == phoenix.DS_SLOW:  # traces, not spans: no span link
                n = _append_traces(phoenix, name, desc, rr)
            else:
                n = phoenix.dataset_append(name, desc, rr)
            if n:
                L.append(f"{n} example(s) appended to dataset {name}")
        except Exception as e:
            L.append(f"dataset {name} failed: {type(e).__name__}")
    return L


def _append_traces(phoenix: Any, name: str, desc: str, rows: list[dict[str, Any]]) -> int:
    st = phoenix._state()
    sent = st.setdefault("sent", {}).setdefault(name, [])
    new = [r for r in rows if r["meta"]["trace_id"] not in sent]
    if not new:
        return 0
    phoenix._upload(name, desc, [r["input"] for r in new], [r["output"] for r in new], [r["meta"] for r in new])
    st["sent"][name] = (sent + [r["meta"]["trace_id"] for r in new])[-3000:]
    phoenix._save(st)
    return len(new)


# ------------------------------------------------------------------------------------------------ report
SEV = {3: "high", 2: "med", 1: "low"}


def markdown(
    res: dict[str, Any],
    new_ids: set[str] | frozenset[str] = frozenset(),
    ranked: list[str] | None = None,
    phx_lines: list[str] | None = None,
) -> str:
    L = ["## (h) Setup health: every log and trace source, 24 h vs 7 d", ""]
    fs = res.get("findings") or []
    L.append(
        f"{len(fs)} anomalies ({sum(1 for f in fs if f['id'] in new_ids)} new); "
        f"{len(res.get('readings') or {})} sources read in {res.get('secs')} s"
        + (
            f"; unavailable: {', '.join(f'{k} ({v})' for k, v in (res.get('errors') or {}).items())}"
            if res.get("errors")
            else ""
        )
        + ". Sources: ~/reports/self-review/sources.md."
    )
    L += ["", "Anomalies (metric first):"] if fs else ["", "Anomalies: none"]
    for f in fs:
        L.append(
            f"- `{f['id']}`{' **new**' if f['id'] in new_ids else ''} [{SEV.get(f['sev'], f['sev'])}] "
            f"{f['source']}: {f['msg']}. Fix: {f['fix']}."
        )
    L += ["", "Per source (24 h; baseline = the 7 days before):"]
    for k, r in (res.get("readings") or {}).items():
        L.append(f"- {k}: {r.get('line') or '-'} ({r.get('secs')} s)")
    ph = (res.get("readings") or {}).get("phoenix") or {}
    if ph.get("slow"):
        L += ["", "Slowest traces (Phoenix):"]
        for t in ph["slow"]:
            L.append(f"- {t.get('root') or 'trace'} {_ms(t.get('ms'))}: {t.get('link') or t['trace_id']}")
    cl = res.get("clusters") or []
    if cl:
        L += ["", "Error signatures (clustered; new first):"]
        for c in cl[:12]:
            was = f" (7 d {fmtn(c['base'])}/day)" if c["base"] is not None else ""
            L.append(f"- {c['source']}: {c['sig']} ×{c['n']}" + (" (new)" if c["new"] else was))
    if ranked:
        L += ["", "Ranked by the local LLM (signatures only):"] + [f"- {x}" for x in ranked]
    if phx_lines:
        L += ["", "Phoenix write-back: " + "; ".join(phx_lines)]
    return "\n".join(L)


def _ms(ms: Any) -> str:
    if not isinstance(ms, (int, float)):
        return "?"
    return f"{ms / 60000:.0f} min" if ms >= 120000 else f"{ms / 1000:.1f} s"
