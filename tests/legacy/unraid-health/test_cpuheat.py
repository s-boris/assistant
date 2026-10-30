#!/usr/bin/env python3
"""Offline tests for unraid-health's CPU heat line (the last 24 h of cputemp-log): report line, WARN on
package throttling above the minutes limit or a high p95, hysteresis while active, data gaps, and no line
without cputemp-log. Writes synthetic cputemp CSVs into a temp CPUTEMP_STATE and runs the real cputemp-log
(CPUTEMP_LOG_BIN overrides it) on a frozen clock with --input, so it never touches SSH, the real logger state
or Telegram. UH_BIN overrides the unraid-health path (to test a copy before installing it)."""
import atexit, datetime as dt, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

UH = os.environ.get("UH_BIN", "/home/node/bin/unraid-health")
CT = os.environ.get("CPUTEMP_LOG_BIN", "/home/node/bin/cputemp-log")
HERE = Path(__file__).resolve().parent
FIX = (HERE / "fixtures/collect-2026-10-12.txt").read_text()
NOW = int(re.search(r"^now=(\d+)", FIX, re.M).group(1)) + 60
TZ = ZoneInfo("Europe/Zurich")
FREEZE = ("import sys, time, runpy; t = float(sys.argv[1]); time.time = lambda: t; p = sys.argv[2]; "
          "sys.argv = [p] + sys.argv[3:]; runpy.run_path(p, run_name='__main__')")
T = Path(tempfile.mkdtemp(prefix="uh-cpuheat."))
atexit.register(shutil.rmtree, T, True)
COLS = "ts,dt_s,pkg_c,core_max_c,pkg_w,pcore_mhz,pkg_thr_n,pkg_thr_ms,core_thr_n,core_thr_ms_max,win"
P = F = 0


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1; print(f"ok   - {name}")
    else:
        F += 1; print(f"FAIL - {name}\n       got: {str(got)[:700]}")


def csvs(state, hours=8.0, thr_min=5.0, hot_share=0.0, offset_h=0.0, step=60):
    """cputemp rows every `step` s over the `hours` before NOW - offset_h; thr_min package-throttled minutes
    spread evenly; hot_share of the rows at 99 °C (else 70 °C)."""
    if state.exists():
        shutil.rmtree(state)
    state.mkdir(parents=True)
    n = int(hours * 3600 // step)
    end = NOW - int(offset_h * 3600)
    per = thr_min * 60000 / max(n, 1)
    hot_every = int(1 / hot_share) if hot_share else 0
    by_day = {}
    for i in range(n):
        t = dt.datetime.fromtimestamp(end - (n - 1 - i) * step, TZ)
        c = 99.0 if hot_every and i % hot_every == 0 else 70.0
        row = f"{t:%Y-%m-%d %H:%M:%S},{step:.1f},{c - 2:.1f},{c:.1f},60.0,3400,1,{per:.0f},1,{per:.0f},other"
        by_day.setdefault(t.date().isoformat(), []).append(row)
    for day, rows in by_day.items():
        (state / f"{day}.csv").write_text(COLS + "\n" + "\n".join(rows) + "\n")


def run(sd, tag, state, i, bin_=CT):
    env = dict(os.environ, UNRAID_HEALTH_TG="/bin/true", UNRAID_HEALTH_TZ="Europe/Zurich", CPUTEMP_STATE=str(state),
               UNRAID_HEALTH_CPUTEMP=str(bin_), UNRAID_HEALTH_ACK_FILE=str(T / "no-ack.json"),
               UNRAID_HEALTH_HDDHEAT_LOG=str(T / "no-hddheat.log"))
    env.pop("UNRAID_HEALTH_NO_SEND", None)
    f = T / f"{tag}.txt"
    f.write_text(FIX)
    r = subprocess.run([sys.executable, "-I", "-c", FREEZE, str(NOW + 30 * i), UH, "--json", "--input", str(f),
                        "--state-dir", str(sd), "--reports-dir", str(sd / "reports"), "--inbox-dir", str(sd / "inbox")],
                       capture_output=True, text=True, env=env, timeout=120)
    try:
        res = json.loads(r.stdout[r.stdout.find("{"):])
    except Exception:
        print(r.stdout[-1500:], r.stderr[-1500:]); raise
    rep = next(iter((sd / "reports").glob("*.md")), None)
    res["_report"] = rep.read_text() if rep else ""
    res["_notes"] = "".join(p.read_text() for p in sorted((sd / "inbox").glob("*"))) if (sd / "inbox").exists() else ""
    res["_rc"] = r.returncode
    return res


def heat_line(res):
    return next((x for x in res["_report"].splitlines() if x.startswith("- CPU heat, last 24 h")), "")


def active(sd):
    return "sys:cpu_throttle" in json.loads((sd / "state.json").read_text()).get("active", {})


def clear_inbox(sd):
    if (sd / "inbox").exists():
        shutil.rmtree(sd / "inbox")


sd, st = T / "s1", T / "ct"
# 1. calm day (5 min throttled, p95 70): baseline run, line in the report, no WARN
csvs(st, hours=8, thr_min=5)
r = run(sd, "calm1", st, 0)
ln = heat_line(r)
ok("calm: report has the CPU heat line", "package throttled 5.0 min" in ln and "8.0 h logged" in ln and
   "p95 70°C" in ln and "WARN above 30 min or p95 95°C" in ln, ln)
ok("calm: metrics in --json", (r.get("metrics", {}).get("cpu_heat") or {}).get("thr_min") == 5.0, r.get("metrics", {}).get("cpu_heat"))
ok("calm: no CPU-throttle finding", not active(sd))
r = run(sd, "calm2", st, 1)
ok("calm again: nothing new, no note", r["_rc"] == 0 and "CPU throttles" not in r["_notes"], (r["_rc"], r["_notes"][:300]))

# 2. throttled 35 min: WARN, alerted once (inbox note)
clear_inbox(sd)
csvs(st, hours=8, thr_min=35)
r = run(sd, "hot1", st, 2)
ok("35 min: WARN alerted in the note", r["_rc"] == 1 and "CPU throttles a lot: 35 min package-throttled in 24 h" in r["_notes"],
   (r["_rc"], r["_notes"][:400]))
ok("35 min: report lists it and the line", "CPU throttles a lot" in r["_report"] and "package throttled 35.0 min" in heat_line(r),
   heat_line(r))
ok("35 min: finding is active", active(sd))
clear_inbox(sd)
r = run(sd, "hot2", st, 3)
ok("35 min again: ongoing, no new note", r["_rc"] == 0 and "CPU throttles" not in r["_notes"], (r["_rc"], r["_notes"][:300]))

# 3. hysteresis: 25 min (between 20 and 30) keeps it; 15 min clears it
csvs(st, hours=8, thr_min=25)
r = run(sd, "hyst", st, 4)
ok("25 min while active: still active (clears below 20)", active(sd) and r["_rc"] == 0, r["_rc"])
csvs(st, hours=8, thr_min=15)
r = run(sd, "clear", st, 5)
ok("15 min: cleared", not active(sd) and "package throttled 15.0 min" in heat_line(r), heat_line(r))
csvs(st, hours=8, thr_min=25)
r = run(sd, "fresh25", st, 6)
ok("25 min when not active: no WARN (limit 30)", not active(sd) and r["_rc"] == 0, r["_rc"])

# 4. p95: 10 % of the rows at 99 °C over 8 h -> WARN; the same over 2 h (< 6 h logged) -> no WARN
sd2 = T / "s2"
csvs(st, hours=8, thr_min=1)
run(sd2, "p95base", st, 7)
csvs(st, hours=2, thr_min=1, hot_share=0.1)
r = run(sd2, "p95short", st, 8)
ok("p95 99 over 2 h only: no WARN (needs 6 h logged)", not active(sd2) and "p95 99°C" in heat_line(r), heat_line(r))
csvs(st, hours=8, thr_min=1, hot_share=0.1)
r = run(sd2, "p95hot", st, 9)
ok("p95 99 over 8 h: WARN names the p95", active(sd2) and "CPU throttles a lot: p95 99°C in 24 h" in r["_notes"],
   r["_notes"][:400])

# 5. only rows older than 24 h -> data gap, no line
sd3 = T / "s3"
csvs(st, hours=4, thr_min=50, offset_h=26)
r = run(sd3, "old", st, 10)
ok("rows older than 24 h only: gap, no line, no WARN",
   not heat_line(r) and "cputemp-log logged nothing in the last 24 h" in r["_report"] and not active(sd3),
   [g for g in r.get("gaps", [])])
# 6. a span across midnight reads both daily CSVs
csvs(st, hours=23, thr_min=10)
r = run(sd3, "twodays", st, 11)
ok("23 h across two CSV files: all rows counted", "23.0 h logged" in heat_line(r) and "package throttled 10.0 min" in heat_line(r),
   heat_line(r))

# 7. no cputemp-log installed: no line, no gap; a broken one: a gap
sd4 = T / "s4"
r = run(sd4, "none", st, 12, bin_=T / "no-such-cputemp-log")
ok("no cputemp-log: no line and no gap", not heat_line(r) and "CPU heat" not in r["_report"], heat_line(r))
bad = T / "bad-cputemp-log"
bad.write_text("#!/bin/sh\necho not json\n")
bad.chmod(0o755)
r = run(sd4, "broken", st, 13, bin_=bad)
ok("broken cputemp-log: data gap", "CPU heat: cputemp-log gave no usable summary" in r["_report"], r.get("gaps"))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
