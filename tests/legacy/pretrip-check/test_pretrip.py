#!/usr/bin/env python3
"""Offline tests for ~/bin/pretrip-check (no ssh, no network, synthetic data only).
  1. evaluate(): a synthetic all-good facts fixture, mutated per scenario, through `--facts FILE --json`
  2. collectors: the whole collect path against stub tools (PRETRIP_BIN_DIR, fake curl/unraid-ssh on PATH),
     recording argv so the read-only flags (--dry-run, no --notify/--ack) are asserted
  3. the server script: no writes, no state-changing docker verbs
Run: ~/opt/assistant/tests/legacy/pretrip-check/run.sh   (default: the repo's scripts/ copy;
PRETRIP_UNDER_TEST=/path/to/copy tests another)"""

import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

PC = os.environ.get("PRETRIP_UNDER_TEST") or str(Path(__file__).resolve().parents[3] / "scripts" / "pretrip-check")
REAL_BIN = Path.home() / "bin"
TZ = ZoneInfo("Europe/Zurich")
T = Path(tempfile.mkdtemp(prefix="pretrip-tests."))
# the default trip window comes from a trip file; tests always use the synthetic one (never ~/.config)
TRIP = Path(__file__).resolve().parent / "fixtures" / "trip.json"
os.environ["PRETRIP_TRIP"] = str(TRIP)
P = F = 0


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1
        print(f"ok   - {name}")
    else:
        F += 1
        print(f"FAIL - {name}\n       got: {str(got)[:900]}")


def ts(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp()


NOW = ts("2026-10-28T09:00")
TB, GB = 1e12, 1e9


def counters(**kw):
    base = {
        "disk:A_1:udma_crc": [44, "disk4: UDMA CRC errors (199, cable)", False],
        "disk:B_2:reallocated": [0, "disk1: reallocated sectors (5)", False],
        "disk:B_2:md_errors": [0, "disk1: Unraid read/write errors since array start", True],
    }
    for k, v in kw.items():
        base[k][0] = v
    return base


GOOD = {
    "now": NOW,
    "window": ["2026-10-30", "2026-11-15"],
    "capacity": {
        "as_of": NOW - 3600,
        "as_of_local": "2026-10-28 08:00",
        "host": "Homesrv",
        "floor": 150 * GB,
        "array": {
            "size": 132 * TB,
            "used": 110 * TB,
            "free": 22 * TB,
            "usable": 20 * TB,
            "pct": 83.3,
            "primary": 30,
            "eta": {"days": 200, "date": "2027-06-01"},
            "rate": 100 * GB,
        },
        "pools": {
            "cache": {"kind": "pool", "size": 1000 * GB, "free": 800 * GB, "pct": 20.0, "rate": 1 * GB, "need_days": 6},
            "docker.img": {
                "kind": "docker",
                "size": 50 * GB,
                "free": 20 * GB,
                "pct": 60.0,
                "rate": None,
                "need_days": 6,
            },
        },
    },
    "health": {
        "now": NOW,
        "version": "7.3.3",
        "gaps": [],
        "info": [],
        "items": [
            {
                "key": "smart:x:199",
                "level": "warn",
                "cat": "smart",
                "subject": "disk4",
                "title": "disk4: 44 UDMA CRC errors (cable/connection)",
                "baseline": True,
                "kind": "cond",
            }
        ],
        "parity": {
            "log": [
                {
                    "when": "2026 Sep 8 12:34:56",
                    "ts": ts("2026-09-08T12:34"),
                    "duration": 1,
                    "speed": 1,
                    "exit": 0,
                    "errors": 0,
                    "action": "check P",
                    "size": 1,
                },
                {
                    "when": "2026 Sep 24 14:00:00",
                    "ts": ts("2026-09-24T14:00"),
                    "duration": 1,
                    "speed": 1,
                    "exit": 0,
                    "errors": 0,
                    "action": "recon Q",
                    "size": 1,
                },
            ],
            "last_ok": {
                "when": "2026 Sep 8 12:34:56",
                "ts": ts("2026-09-08T12:34"),
                "exit": 0,
                "errors": 0,
                "action": "check P",
            },
            "last_try": {
                "when": "2026 Sep 8 12:34:56",
                "ts": ts("2026-09-08T12:34"),
                "exit": 0,
                "errors": 0,
                "action": "check P",
            },
            "running": None,
            "schedule": {"mode": "yearly", "interval": 366},
            "max_age": 373,
            "last_end": ts("2026-09-08T12:34"),
        },
        "containers": {
            "claude-bot": {"self": True, "status": "running"},
            "radarr": {"status": "running", "exit": 0, "health": "", "image": "x/radarr"},
            "HomeAssistant_inabox": {
                "status": "running",
                "exit": 0,
                "health": "",
                "image": "spaceinvaderone/ha_inabox:2",
            },
            "oneshot": {"status": "exited", "exit": 0, "health": "", "image": "x/oneshot"},
        },
        "vms": {"Home Assistant": "running"},
    },
    "container_watch": {
        "rows": [
            {
                "container": "radarr",
                "state": "running",
                "unraid": "true",
                "unraid_since": None,
                "auto_update": True,
                "installed": "6.4",
                "latest": "6.4",
                "behind": 0,
                "pinned": False,
            }
        ],
        "actionable": [],
        "errors": [],
    },
    "extra": {
        "mode": "root",
        "autostart": ["radarr", "HomeAssistant_inabox", "OldGone"],
        "ha_top": 2,
        "ha_log": 3,
        "ab_plugins": ["appdata.backup"],
        "ab_dirs": ["/mnt/user/backup/appdata/ab_20261025_030000"],
    },
    "vault": {
        "uptime": 7200,
        "llm_healthy": True,
        "llm_state": "running",
        "tg_logged_in": True,
        "tg_last_at": "2026-10-28T08:45:00+01:00",
        "tg_last_exit": 0,
        "gmail_error": None,
        "alerts": 0,
        "raw_pending": 0,
    },
    "brain": {
        "archives": ["claude-bot-2026-10-27.tar.gz", "claude-bot-2026-10-28.tar.gz"],
        "list_error": None,
        "verify": {"rc": 0, "summary": "PASS: unraid:/x/claude-bot-2026-10-28.tar.gz", "fails": []},
    },
    "hwtrend": {
        "samples": [
            {"date": "2026-10-26", "ts": 1, "counters": counters()},
            {"date": "2026-10-27", "ts": 2, "counters": counters()},
            {"date": "2026-10-28", "ts": 3, "counters": counters()},
        ]
    },
    "update": {"current": "7.3.3", "latest": "7.3.3", "is_newer": False, "source": "live", "notified": "7.3.3"},
    "reminders": {
        "items": [
            {
                "at": "2026-11-04 10:00",
                "text": "Friend's birthday on Friday: short nudge",
                "owner": False,
                "every": None,
            },
            {"at": "2026-11-10 09:00", "text": "Tax instalment due (pay online)", "owner": True, "every": None},
        ]
    },
    "crontab": {"jobs": [{"line": "*/5 * * * * /home/node/bin/tg-status refresh", "comment": "status board"}]},
}


def run_facts(facts, *extra):
    f = T / "facts.json"
    f.write_text(json.dumps(facts))
    p = subprocess.run([sys.executable, PC, "--facts", str(f), "--json", *extra], capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout), p.stderr
    except ValueError:
        return p.returncode, None, p.stdout + p.stderr


def check(facts, key, *extra):
    rc, r, err = run_facts(facts, *extra)
    if r is None:
        return rc, {"status": "CRASH", "line": err}
    return rc, next(c for c in r["checks"] if c["key"] == key)


def mut(fn):
    f = copy.deepcopy(GOOD)
    fn(f)
    return f


# ----------------------------------------------------------------------------- 1. evaluate
rc, r, err = run_facts(GOOD)
ok("all good: exit 0", rc == 0, (rc, err))
ok(
    "all good: 13 checks all ok",
    r and len(r["checks"]) == 13 and all(c["status"] == "ok" for c in r["checks"]),
    [c["line"] for c in (r or {}).get("checks", []) if c["status"] != "ok"],
)
ok(
    "all good: headline",
    r and r["headline"].endswith("all OK") and "30.10.–15.11." in r["headline"],
    r and r["headline"],
)
p = subprocess.run([sys.executable, PC, "--facts", str(T / "facts.json")], capture_output=True, text=True)
lines = p.stdout.strip().splitlines()
ok(
    "text report <= 15 lines, one mark per check line",
    len(lines) <= 15 and all(ln[:2] in ("✅ ", "⚠️") for ln in lines[1:]),
    p.stdout,
)
md = T / "out" / "r.md"
p = subprocess.run(
    [sys.executable, PC, "--facts", str(T / "facts.json"), "--md", str(md)], capture_output=True, text=True
)
ok(
    "--md writes report + details",
    md.exists() and "## Details" in md.read_text() and "✅ Array" in md.read_text(),
    md.exists() and md.read_text()[:300],
)
p = subprocess.run(
    [sys.executable, PC, "--from", "2026-11-19", "--until", "2026-11-10"], capture_output=True, text=True
)
ok("--until before --from: exit 2", p.returncode == 2, p.returncode)

# space
rc, c = check(mut(lambda f: f["capacity"]["array"].update(usable=3 * TB, rate=200 * GB)), "space_array")
ok(
    "array fills before trip end -> warn 'Array full ~date'",
    rc == 1 and c["status"] == "warn" and "Array full ~" in c["line"],
    c["line"],
)
rc, c = check(mut(lambda f: f["capacity"]["array"].update(usable=4.5 * TB, rate=200 * GB)), "space_array")
ok(
    "array barely makes it (< 14 days of growth left) -> warn 'only'",
    c["status"] == "warn" and "only" in c["line"],
    c["line"],
)
rc, c = check(mut(lambda f: f["capacity"]["array"].update(usable=5 * TB, rate=200 * GB)), "space_array")
ok("5 TB at +200 GB/day: < 14 days of growth left on 15.11. -> warn", c["status"] == "warn", c["line"])
rc, c = check(
    mut(lambda f: f["capacity"]["array"].update(usable=5 * TB, rate=200 * GB)),
    "space_array",
    "--from",
    "2026-10-30",
    "--until",
    "2026-11-04",
)
ok("--until moves the projection (short trip -> ok)", c["status"] == "ok", c["line"])
rc, c = check(mut(lambda f: f["capacity"]["array"].update(usable=5 * TB, rate=None)), "space_array")
ok("array without a trend but 5 TB usable -> ok", c["status"] == "ok" and "not filling" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["capacity"]["array"].update(usable=0.5 * TB, rate=None)), "space_array")
ok("array without a trend and 0.5 TB -> warn", c["status"] == "warn", c["line"])
rc, c = check(mut(lambda f: f.update(capacity={"error": "no samples yet"})), "space_array")
ok("capacity error -> warn couldn't check", c["status"] == "warn" and "couldn't check" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["capacity"]["pools"]["cache"].update(free=100 * GB, rate=10 * GB)), "space_pools")
ok("cache pool projected full at trip end -> warn", c["status"] == "warn" and "cache" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["capacity"]["pools"]["docker.img"].update(pct=95.0, free=2.5 * GB)), "space_pools")
ok("docker.img 95% without trend -> warn", c["status"] == "warn" and "docker.img 95% full" in c["line"], c["line"])
H = 3600
STEP = [
    {"ts": NOW - 50 * H, "free": 21 * GB},
    {"ts": NOW - 27 * H, "free": 20.5 * GB},
    {"ts": NOW - 6 * H, "free": 10.4 * GB},
]  # the 29->30.10 shape: +10.1 GB in one day


def docker(free, days, rate=None):
    def f(x):
        x["capacity"]["docker_days"] = days
        x["capacity"]["pools"]["docker.img"].update(free=free, pct=100 * (50 * GB - free) / (50 * GB), rate=rate)

    return f


rc, c = check(mut(docker(10.3 * GB, STEP)), "space_pools")
ok(
    "docker.img +10.1 GB in a day, 10.3 GB free (30-day trend flat) -> warn, names the step",
    c["status"] == "warn"
    and "docker.img rose 10.1 GB in a day" in c["line"]
    and not any("not growing" in d for d in c["details"]),
    (c["line"], c["details"]),
)
rc, c = check(mut(docker(30 * GB, STEP)), "space_pools")
ok(
    "same step but 30 GB free (> 2x the step) -> ok, step in the details",
    c["status"] == "ok" and any("a 10.1 GB step on" in d for d in c["details"]),
    (c["line"], c["details"]),
)
rc, c = check(
    mut(
        docker(
            10.3 * GB, [{"ts": NOW - 9 * 86400, "free": 21 * GB}, {"ts": NOW - 8 * 86400, "free": 10.4 * GB}], rate=0.0
        )
    ),
    "space_pools",
)
ok(
    "step older than 7 days -> 'not growing' again",
    c["status"] == "ok" and any("not growing" in d for d in c["details"]),
    (c["line"], c["details"]),
)
rc, c = check(
    mut(docker(18 * GB, [{"ts": NOW - 2 * 86400, "free": 21 * GB}, {"ts": NOW - 86400, "free": 20 * GB}])),
    "space_pools",
)
ok("live reading is the last point: +2 GB/day, then 20 -> 18 GB -> ok", c["status"] == "ok", (c["line"], c["details"]))
rc, c = check(mut(docker(9 * GB, [{"ts": NOW - 86400, "free": 21 * GB}])), "space_pools")
ok(
    "live reading 12 GB below yesterday's sample -> warn",
    c["status"] == "warn" and "rose 12.0 GB" in c["line"],
    (c["line"], c["details"]),
)
rc, c = check(mut(docker(10.3 * GB, [{"ts": NOW - 3 * H, "free": 20 * GB}])), "space_pools")
ok("points < 12 h apart are not a day's step", c["status"] == "ok", (c["line"], c["details"]))

# health
rc, c = check(
    mut(
        lambda f: f["health"]["items"].append(
            {
                "key": "disk:parity2:status",
                "level": "crit",
                "cat": "array",
                "subject": "parity2",
                "title": "parity2: status DISK_DSBL (WDC_WD160EDFZ-11AFWA0_X)",
                "baseline": False,
                "kind": "cond",
            }
        )
    ),
    "health",
)
ok(
    "new crit -> warn, serial stripped",
    c["status"] == "warn" and "DISK_DSBL" in c["line"] and "WDC" not in c["line"],
    c["line"],
)
rc, c = check(GOOD, "health")
ok("baseline-only issues -> ok '(1 known, stable)'", c["status"] == "ok" and "1 known" in c["line"], c["line"])
rc, c = check(
    mut(
        lambda f: f["health"]["items"].extend(
            [
                {
                    "key": "parity:overdue",
                    "level": "warn",
                    "cat": "parity",
                    "title": "Parity check overdue",
                    "baseline": False,
                },
                {
                    "key": "space:disk3",
                    "level": "warn",
                    "cat": "space",
                    "title": "disk3 is 96% full",
                    "baseline": False,
                },
            ]
        )
    ),
    "health",
)
ok("parity/single-disk space items are left to their own lines", c["status"] == "ok", c["line"])
rc, c = check(mut(lambda f: f.update(health={"error": "ssh: connect timed out"})), "health")
ok("health collect failure -> warn", c["status"] == "warn" and "couldn't check" in c["line"], c["line"])


# hwtrend
def hw(vals, key="disk:A_1:udma_crc"):
    def f(x):
        for s, v in zip(x["hwtrend"]["samples"], vals, strict=False):
            s["counters"][key][0] = v

    return f


rc, c = check(mut(hw([44, 44, 46])), "disk_errors")
ok("CRC 44 -> 46 -> warn 'rising'", c["status"] == "warn" and "44→46" in c["line"], c["line"])
rc, c = check(mut(hw([44, 43, 44])), "disk_errors")
ok("stale-cache wobble 44,43,44 -> ok (running max)", c["status"] == "ok", c["line"])
rc, c = check(mut(hw([5, 0, 2], "disk:B_2:md_errors")), "disk_errors")
ok("resettable md_errors 5,0,2 -> warn", c["status"] == "warn" and "0→2" in c["line"], c["line"])
rc, c = check(mut(hw([5, 0, 0], "disk:B_2:md_errors")), "disk_errors")
ok("resettable drop to 0 -> ok", c["status"] == "ok", c["line"])
rc, c = check(mut(lambda f: f["hwtrend"].update(samples=f["hwtrend"]["samples"][:1])), "disk_errors")
ok("newest sample 2 days old -> ok (limit 2)", c["status"] == "ok", c["line"])
rc, c = check(
    mut(
        lambda f: (
            f["hwtrend"]["samples"][0].update(date="2026-10-20")
            or f["hwtrend"].update(samples=f["hwtrend"]["samples"][:1])
        )
    ),
    "disk_errors",
)
ok("newest sample 8 days old -> warn stale", c["status"] == "warn" and "stale" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["hwtrend"].update(samples=[])), "disk_errors")
ok("no samples -> warn", c["status"] == "warn", c["line"])

# parity
rc, c = check(GOOD, "parity")
ok(
    "parity ok + later sync OK",
    c["status"] == "ok" and "last check 8.9." in c["line"] and "recon Q 24.9. OK" in c["line"],
    c["line"],
)
rc, c = check(
    mut(lambda f: f["health"]["parity"].update(running={"kind": "Parity sync", "pct": 42.0, "eta": 36000})), "parity"
)
ok("sync running -> warn with ETA", c["status"] == "warn" and "42%" in c["line"] and "10 h" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["health"]["parity"]["last_ok"].update(errors=12)), "parity")
ok("last check found errors -> warn", c["status"] == "warn" and "12 errors" in c["line"], c["line"])
rc, c = check(
    mut(
        lambda f: f["health"]["parity"].update(
            last_try={"when": "2026 Oct 11", "ts": ts("2026-10-11T10:00"), "exit": -5, "errors": 0, "action": "check P"}
        )
    ),
    "parity",
)
ok("newer failed check -> warn", c["status"] == "warn" and "failed" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["health"]["parity"]["log"][1].update(exit=-4)), "parity")
ok("newer sync not finished -> warn", c["status"] == "warn" and "didn't finish" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["health"]["parity"].update(max_age=40)), "parity")
ok("check goes overdue before the trip ends -> warn", c["status"] == "warn" and "overdue" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["health"]["parity"].update(last_ok=None)), "parity")
ok("no completed check -> warn", c["status"] == "warn", c["line"])
DEG = [
    {"slot": "disk12", "what": "emulated (no device)", "since": ts("2026-10-26T22:15"), "days": 2, "rebuilding": False}
]
rc, c = check(mut(lambda f: f["health"].update(degraded=DEG)), "parity")
ok(
    "emulated disk12 (clean last check) -> warn 'array degraded', not OK",
    c["status"] == "warn"
    and "array degraded, disk12 emulated since 26.10." in c["line"]
    and "rebuild before the trip" in c["line"]
    and any("degraded: disk12 emulated (no device)" in d for d in c["details"]),
    (c["line"], c["details"][:1]),
)
rc, c = check(mut(lambda f: f["health"].update(degraded=[dict(DEG[0], rebuilding=True)])), "parity")
ok("degraded slot being rebuilt -> not flagged as degraded", "degraded" not in c["line"], c["line"])
rc, c = check(mut(lambda f: f["health"].update(degraded=[dict(DEG[0], what="disabled", slot="parity2")])), "parity")
ok(
    "disabled parity2 -> 'parity2 out since'",
    c["status"] == "warn" and "parity2 out since 26.10." in c["line"],
    c["line"],
)

# backups
rc, c = check(mut(lambda f: f["brain"].update(archives=["claude-bot-2026-10-20.tar.gz"])), "backup_brain")
ok("bot backup 8 days old -> warn", c["status"] == "warn" and "8 d old" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["brain"]["verify"].update(rc=1, fails=["FAIL - bin: 2 scripts"])), "backup_brain")
ok("test-restore failed -> warn", c["status"] == "warn" and "FAILED" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["brain"].pop("verify")), "backup_brain")
ok("--no-verify facts (no verify) + fresh -> ok", c["status"] == "ok" and "test-restore" not in c["line"], c["line"])
rc, c = check(mut(lambda f: f["brain"].update(archives=[], list_error="ssh failed")), "backup_brain")
ok("no archive -> warn", c["status"] == "warn", c["line"])
rc, c = check(GOOD, "backup_appdata")
ok("appdata.backup 3 days old -> ok", c["status"] == "ok" and "25.10.2026" in c["line"], c["line"])
rc, c = check(
    mut(lambda f: f["extra"].update(ab_plugins=[], ab_dirs=["/mnt/user/backup/Unraid_Appdata/2022-02-22@04.00"])),
    "backup_appdata",
)
ok(
    "no plugin, CA v2 folder from 2022 -> warn",
    c["status"] == "warn" and "22.2.2022" in c["line"] and "no backup plugin" in c["line"],
    c["line"],
)
rc, c = check(
    mut(lambda f: f["extra"].update(ab_dirs=["/mnt/user/backup/appdata/ab_20260910_030000"])), "backup_appdata"
)
ok("appdata backup 48 days old -> warn", c["status"] == "warn", c["line"])
rc, c = check(mut(lambda f: f.update(extra={"mode": "verbs", "skipped": "SSH lockdown"})), "backup_appdata")
ok("SSH lockdown -> warn couldn't check", c["status"] == "warn" and "couldn't check" in c["line"], c["line"])

# containers
rc, c = check(GOOD, "containers")
ok(
    "autostart entry without a container + exited(0) one-shot -> ok",
    c["status"] == "ok" and "3/4" not in c["line"] and any("OldGone" in d for d in c["details"]),
    c,
)
rc, c = check(mut(lambda f: f["health"]["containers"]["radarr"].update(status="exited", exit=0)), "containers")
ok("autostart container stopped -> warn", c["status"] == "warn" and "radarr" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["health"]["containers"]["oneshot"].update(exit=137, oom=True)), "containers")
ok("crashed (exit 137) non-autostart -> warn", c["status"] == "warn" and "oneshot" in c["line"], c)
rc, c = check(mut(lambda f: f["health"]["containers"]["radarr"].update(status="restarting")), "containers")
ok("restarting -> warn", c["status"] == "warn", c["line"])
rc, c = check(mut(lambda f: f["health"]["containers"]["radarr"].update(health="unhealthy")), "containers")
ok("unhealthy -> warn", c["status"] == "warn", c["line"])

# HA watchdog
rc, c = check(GOOD, "ha_watchdog")
ok("HA watchdog ok", c["status"] == "ok" and "< 50 min" in c["line"], c["line"])
rc, c = check(
    mut(lambda f: f["health"]["containers"]["HomeAssistant_inabox"].update(image="spaceinvaderone/ha_inabox:latest")),
    "ha_watchdog",
)
ok(":latest image (v3 notice page) -> warn", c["status"] == "warn" and "pinned" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["extra"].update(ha_top=0)), "ha_watchdog")
ok("watcher process missing -> warn", c["status"] == "warn" and "watcher" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["extra"].update(ha_log=0)), "ha_watchdog")
ok("no VM check in 50 min -> warn", c["status"] == "warn" and "no VM check" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["health"]["vms"].update({"Home Assistant": "shut off"})), "ha_watchdog")
ok("HA VM shut off -> warn", c["status"] == "warn" and "shut off" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["health"]["containers"]["HomeAssistant_inabox"].update(status="exited")), "ha_watchdog")
ok("watchdog container stopped -> warn", c["status"] == "warn" and "not running" in c["line"], c["line"])
rc, c = check(mut(lambda f: f.update(extra={"mode": "verbs", "skipped": "SSH lockdown"})), "ha_watchdog")
ok(
    "lockdown: container :2 + VM up -> ok without the log check",
    c["status"] == "ok" and "< 50 min" not in c["line"],
    c["line"],
)

# vault
rc, c = check(mut(lambda f: f["vault"].update(llm_healthy=False, llm_state="stopped")), "vault")
ok("vault LLM down -> warn", c["status"] == "warn" and "LLM" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["vault"].update(alerts=2, tg_last_exit=1)), "vault")
ok(
    "vault alerts + failing sync -> warn",
    c["status"] == "warn" and "2 alert" in c["line"] and "sync" in c["line"],
    c["line"],
)
rc, c = check(mut(lambda f: f.update(vault={"error": "connection refused"})), "vault")
ok("vault unreachable -> warn", c["status"] == "warn", c["line"])

# updates
rc, c = check(mut(lambda f: f["update"].update(latest="7.3.4", is_newer=True)), "update_os")
ok("Unraid update available -> warn", c["status"] == "warn" and "7.3.4 available" in c["line"], c["line"])
rc, c = check(mut(lambda f: f["update"].update(source="state")), "update_os")
ok("state fallback is labelled", c["status"] == "ok" and "last daily check" in c["line"], c["line"])
rc, c = check(
    mut(
        lambda f: f["container_watch"]["rows"][0].update(
            unraid="false", unraid_since="2026-10-20T00:00:00Z", latest="6.5"
        )
    ),
    "update_containers",
)
ok("Unraid shows a container update -> warn", c["status"] == "warn" and "radarr" in c["line"], c["line"])
rc, c = check(
    mut(
        lambda f: f["container_watch"]["actionable"].extend(
            [
                {"container": "unifi", "kind": "advisory", "ids": ["CVE-1"], "sev": 3, "fixed": "10.4"},
                {"container": "unifi", "kind": "advisory", "ids": ["CVE-2"], "sev": 3, "fixed": "10.4"},
            ]
        )
    ),
    "update_containers",
)
ok("advisories -> warn 'unifi 2 CVEs'", c["status"] == "warn" and "unifi 2 CVEs" in c["line"], c["line"])

# reminders
rc, c = check(GOOD, "reminders")
ok("2 reminders, none need home -> ok", c["status"] == "ok" and "2 reminders" in c["line"], c["line"])
rc, c = check(
    mut(
        lambda f: f["reminders"]["items"].append(
            {"at": "2026-11-06 14:00", "text": "Paket bei der Post abholen", "owner": True, "every": None}
        )
    ),
    "reminders",
)
ok("'Paket abholen' in the window -> warn", c["status"] == "warn" and "6.11." in c["line"], c["line"])
rc, c = check(
    mut(
        lambda f: f["reminders"]["items"].append(
            {"at": "2026-11-02 10:00", "text": "Put the new disk in and preclear it", "owner": False, "every": None}
        )
    ),
    "reminders",
)
ok("disk/preclear task -> warn", c["status"] == "warn", c["line"])

rc, c = check(
    mut(
        lambda f: f["reminders"]["items"].extend(
            [
                {
                    "at": "2026-11-03 10:00",
                    "text": "a fantastic fingerprint idea; Google Drive and API keys",
                    "owner": False,
                    "every": None,
                }
            ]
        )
    ),
    "reminders",
)
ok("no false positives on fan-tastic/finger-print/Drive/keys", c["status"] == "ok", c["line"])

# ----------------------------------------------------------------------------- 2. collectors against stubs
STUB, SB, STATE = T / "bin", T / "path", T / "state"
for d in (STUB, SB, STATE / "hwtrend", STATE / "uh"):
    d.mkdir(parents=True, exist_ok=True)
CALLS = T / "calls.log"
# real modules that pretrip-check only imports (pure functions: hwtrend.counters, remind.succ)
for name in ("hwtrend", "remind"):
    os.symlink(REAL_BIN / name, STUB / name)
(STUB / "lib").symlink_to(REAL_BIN / "lib")


def stub(name, body, path=STUB):
    f = path / name
    f.write_text(
        "#!/usr/bin/env python3\nimport json, sys, os\n"
        f"open({str(CALLS)!r}, 'a').write({name!r} + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n" + body
    )
    f.chmod(0o755)


CAP = {
    "ok": True,
    "as_of": NOW - 600,
    "as_of_local": "x",
    "host": "Homesrv",
    "floor": 1,
    "array": {
        "size": 10,
        "used": 5,
        "free": 5e12,
        "usable": 4e12,
        "pct": 50.0,
        "primary": 30,
        "eta": None,
        "trend30": {"ok": True, "rate": 1e9},
    },
    "pools": {
        "cache": {"kind": "pool", "size": 1e12, "free": 9e11, "pct": 10.0, "trend30": {"ok": False, "need_days": 6}}
    },
}
# unraid-capacity is both imported (fmt_bytes/fmt_rate) and run: the stub re-exports the real helpers
(STUB / "unraid-capacity").write_text(
    "#!/usr/bin/env python3\nimport importlib.machinery, importlib.util, json, sys\n"
    f"_l = importlib.machinery.SourceFileLoader('ucr', {str(REAL_BIN / 'unraid-capacity')!r})\n"
    "_s = importlib.util.spec_from_loader('ucr', _l); _m = importlib.util.module_from_spec(_s); _l.exec_module(_m)\n"
    "fmt_bytes, fmt_rate = _m.fmt_bytes, _m.fmt_rate\n"
    "if __name__ == '__main__':\n"
    f"    open({str(CALLS)!r}, 'a').write('unraid-capacity ' + ' '.join(sys.argv[1:]) + '\\n')\n"
    f"    print(json.dumps({CAP!r}))\n"
)
(STUB / "unraid-capacity").chmod(0o755)
UH = {
    "now": NOW,
    "alerts": [],
    "ongoing": [],
    "info": [],
    "gaps": [],
    "metrics": {
        "system": {"version": "7.3.3"},
        "parity": GOOD["health"]["parity"],
        "vms": {"Home Assistant": "running"},
        "containers": GOOD["health"]["containers"],
    },
}
stub("unraid-health", f"print(json.dumps({UH!r})); print('[no-send] would text', file=sys.stderr); sys.exit(1)\n")
CW = {"rows": [{"container": "radarr", "state": "running", "unraid": "true"}], "actionable": [], "errors": []}
stub("container-watch", f"print(json.dumps({CW!r}))\n")
VS = {
    "vault": {"uptime_seconds": 60},
    "llm": {"healthy": True, "state": "running"},
    "tg": {"logged_in": True, "sync": {"last": {"at": "x", "exit_code": 0}}},
    "gmail": {"error": None},
    "alerts": 0,
    "raw": {"pending": 0},
}
stub("vault", f"print(json.dumps({VS!r}))\n")
stub(
    "brain-backup",
    "a = sys.argv[1:]\n"
    "if a[:1] == ['--list']: print('-rw-r--r-- 1 root root 26M Oct 28 03:47 claude-bot-2026-10-28.tar.gz')\n"
    "elif a[:1] == ['--verify']: print('ok   - x'); print('PASS: unraid:/x/claude-bot-2026-10-28.tar.gz')\n"
    "else: sys.exit(9)\n",
)
stub("curl", "print(json.dumps({'isNewer': True, 'version': '7.3.4'}))\n", SB)
# lockdown: `mode` says verbs; `run pretrip` answers with the collector's output (the dispatcher runs it server-side)
VERB_PRETRIP = (
    "@@autostart\nradarr\n@@ha_top\n1\n@@ha_log\n2\n@@ab_plugins\nappdata.backup\n@@ab_dest\n"
    '"destination": "/mnt/user/backup/appdata"\n@@ab_dirs\n/mnt/user/backup/appdata/ab_20261026_050000\n@@end\n'
)
stub(
    "unraid-ssh",
    f"a = sys.argv[1:]\nif a[-2:] == ['run', 'pretrip']: print({VERB_PRETRIP!r}, end='')\nelse: print('verbs')\n",
    SB,
)
stub("ssh", "sys.exit(255)\n", SB)
sample = json.loads(
    json.dumps(
        {
            "date": "2026-10-28",
            "ts": 1,
            "disks": {
                "A_1": {"slot": "disk4", "counters": {"udma_crc": 44, "reallocated": 0}, "temp": 30, "temp_hot": 50}
            },
            "btrfs": {},
            "array": {"num_disabled": 0, "num_invalid": 0, "num_missing": 0},
            "cpu": None,
        }
    )
)
(STATE / "hwtrend" / "2026-10-27.json").write_text(json.dumps(sample))
sample["disks"]["A_1"]["counters"]["udma_crc"] = 47
(STATE / "hwtrend" / "2026-10-28.json").write_text(json.dumps(sample))
(STATE / "hwtrend" / "2026-09-10.json").write_text(json.dumps(sample))  # outside the 14 days: ignored
remind_db = STATE / "reminders.json"
remind_db.write_text(
    json.dumps(
        [
            {"at": "2026-10-31 09:00", "text": "weekly plant watering at home", "owner": True, "every": "weekly:sat"},
            {"at": "2026-11-29 09:00", "text": "after the trip: unplug the old disk", "owner": False},
            {"at": "2026-11-09 10:00", "text": "check the vault metrics", "owner": False},
        ]
    )
)
crontab = STATE / "crontab"
crontab.write_text(
    'MAILTO=""\n# quiet job\n*/5 * * * * /home/node/bin/tg-status refresh\n'
    "# lonely comment\n\n17 3 * * * /home/node/bin/unraid-capacity record\n"
)
env = dict(
    os.environ,
    PRETRIP_BIN_DIR=str(STUB),
    PATH=f"{SB}:{os.environ['PATH']}",
    UNRAID_SSH_BIN=str(SB / "unraid-ssh"),
    HWTREND_STATE_DIR=str(STATE / "hwtrend"),
    UNRAID_HEALTH_STATE_DIR=str(STATE / "uh"),
    REMIND_DB=str(remind_db),
    PRETRIP_CRONTAB=str(crontab),
    PRETRIP_HOST="pretrip-test-nohost",
    PRETRIP_NOW="2026-10-28 09:00",
    PRETRIP_CAPACITY_SAMPLES=str(STATE / "samples.jsonl"),
)
(STATE / "samples.jsonl").write_text(
    "".join(
        json.dumps({"ts": NOW - h * 3600, "docker_img": {"size": 5e10, "used": 3e10, "free": fr}}) + "\n"
        for h, fr in ((80, 2.1e10), (74, 2.0e10), (50, 2.05e10), (26, 1.04e10))
    )
    + "not json\n"
)
saved = T / "saved.json"
# pretrip-check must load its imports from PRETRIP_BIN_DIR; prove the stubs are used (hwtrend real module, symlinked)
p = subprocess.run(
    [sys.executable, PC, "--json", "--save-facts", str(saved)], capture_output=True, text=True, env=env, timeout=120
)
try:
    res = json.loads(p.stdout)
except ValueError:
    res = None
ok(
    "collect with stubs: runs, exit 1 (warnings)",
    res is not None and p.returncode == 1,
    p.stdout[-500:] + p.stderr[-1500:],
)
calls = CALLS.read_text().splitlines() if CALLS.exists() else []
ok(
    "unraid-health only with --json --dry-run",
    [c for c in calls if c.startswith("unraid-health")] == ["unraid-health --json --dry-run"],
    calls,
)
ok(
    "container-watch only with --json --dry-run",
    [c for c in calls if c.startswith("container-watch")] == ["container-watch --json --dry-run"],
    calls,
)
sv = json.loads(saved.read_text()) if saved.exists() else {}
ok(
    "docker.img daily samples from PRETRIP_CAPACITY_SAMPLES: first per day, bad lines skipped",
    [d["free"] for d in (sv.get("capacity") or {}).get("docker_days", [])] == [2.1e10, 2.05e10, 1.04e10],
    (sv.get("capacity") or {}).get("docker_days"),
)
ok(
    "unraid-capacity: forecast --live only",
    [c for c in calls if c.startswith("unraid-capacity")] == ["unraid-capacity forecast --live"],
    calls,
)
ok(
    "brain-backup: --list and --verify only",
    sorted(c for c in calls if c.startswith("brain-backup")) == ["brain-backup --list", "brain-backup --verify"],
    calls,
)
ok(
    "no notify/ack/record/send flag anywhere",
    not any(re.search(r"--(notify|ack|no-dry|send)\b|\brecord\b", c) for c in calls),
    calls,
)
ok("lockdown (verbs): no ssh attempted", not any(c.startswith("ssh") for c in calls), calls)
ok("unraid-ssh asked for the mode", any(c.startswith("unraid-ssh mode") for c in calls), calls)
ok(
    "lockdown (verbs): the extra facts come from the read-only `pretrip` verb",
    "unraid-ssh --host pretrip-test-nohost run pretrip" in calls,
    calls,
)
if res:
    by = {c["key"]: c for c in res["checks"]}
    ok(
        "stub: OS update from curl -> warn 7.3.4",
        by["update_os"]["status"] == "warn" and "7.3.4" in by["update_os"]["line"],
        by["update_os"],
    )
    ok(
        "stub: hwtrend counters() via the real module, 44->47 rising",
        by["disk_errors"]["status"] == "warn" and "44→47" in by["disk_errors"]["line"],
        by["disk_errors"],
    )
    ok(
        "lockdown: appdata via the `pretrip` verb (2 d old) -> ok",
        by["backup_appdata"]["status"] == "ok" and "2 d old" in by["backup_appdata"]["line"],
        by["backup_appdata"],
    )
    ok("stub: brain backup fresh + verified", by["backup_brain"]["status"] == "ok", by["backup_brain"])
    rem = json.loads(saved.read_text())["reminders"]["items"] if saved.exists() else []
    sats = [r["at"] for r in rem if "plant" in r["text"]]
    ok(
        "recurring reminder expanded with remind's rules (3 Saturdays in 20.11-6.12)",
        sats == ["2026-10-31 09:00", "2026-11-07 09:00", "2026-11-14 09:00"],
        rem,
    )
    ok("reminders outside the window dropped", not any("unplug" in r["text"] for r in rem), rem)
    ok("plant watering 'at home' -> reminders warn", by["reminders"]["status"] == "warn", by["reminders"])
    jobs = json.loads(saved.read_text())["crontab"]["jobs"]
    ok(
        "crontab parsed: 2 jobs, comments attached",
        len(jobs) == 2 and jobs[0]["comment"] == "quiet job" and jobs[1]["comment"] == "",
        jobs,
    )
    ok("saved facts are 0600", oct(saved.stat().st_mode & 0o777) == "0o600", oct(saved.stat().st_mode))
    p2 = subprocess.run([sys.executable, PC, "--facts", str(saved), "--json"], capture_output=True, text=True)
    ok(
        "--facts on saved facts reproduces the result",
        p2.returncode == 1
        and [c["line"] for c in json.loads(p2.stdout)["checks"]] == [c["line"] for c in res["checks"]],
        p2.stdout[:400],
    )

# lockdown with an older dispatcher that lacks `pretrip`: rejected -> appdata "couldn't check", no crash
CALLS.write_text("")
stub(
    "unraid-ssh",
    "a = sys.argv[1:]\n"
    "if a[-2:] == ['run', 'pretrip']: "
    "print(\"claude-bot-cmd: rejected: unknown verb 'pretrip'\", file=sys.stderr); sys.exit(1)\n"
    "print('verbs')\n",
    SB,
)
p = subprocess.run([sys.executable, PC, "--json", "--no-verify"], capture_output=True, text=True, env=env, timeout=120)
try:
    by = {c["key"]: c for c in json.loads(p.stdout)["checks"]}
except (ValueError, KeyError):
    by = {}
ok(
    "lockdown, verb refused: appdata -> warn couldn't check (rejected)",
    by.get("backup_appdata", {}).get("status") == "warn" and "rejected" in by.get("backup_appdata", {}).get("line", ""),
    by.get("backup_appdata") or p.stderr[-400:],
)

# root mode: the extra collector parses; ssh stub prints canned server output
CALLS.write_text("")
stub("unraid-ssh", "print('root')\n", SB)
canned = (
    "@@autostart\nradarr\nbinhex-prowlarr 0\n@@ha_top\n2\n@@ha_log\n3\n@@ab_plugins\n@@ab_dest\n@@ab_dirs\n"
    "/mnt/user/backup/Unraid_Appdata/2022-02-22@04.00\n@@end\n"
)
stub("ssh", f"sys.stdin.read(); print({canned!r}, end='')\n", SB)
p = subprocess.run(
    [sys.executable, PC, "--json", "--no-verify", "--save-facts", str(saved)],
    capture_output=True,
    text=True,
    env=env,
    timeout=120,
)
x = json.loads(saved.read_text())["extra"] if saved.exists() else {}
ok("root mode: autostart names (delay column dropped)", x.get("autostart") == ["radarr", "binhex-prowlarr"], x)
ok("root mode: watcher counts parsed", x.get("ha_top") == 2 and x.get("ha_log") == 3, x)
calls = CALLS.read_text().splitlines()
ok(
    "root mode: one ssh session, 'bash -s' to the configured host",
    [c for c in calls if c.startswith("ssh")]
    == ["ssh -T -o BatchMode=yes -o ConnectTimeout=15 pretrip-test-nohost bash -s"],
    calls,
)
ok("--no-verify skips the test-restore", not any("--verify" in c for c in calls), calls)

# ----------------------------------------------------------------------------- 3. the server script is read-only
col = subprocess.run([sys.executable, PC, "--show-collector"], capture_output=True, text=True).stdout
code = "\n".join(ln for ln in col.splitlines() if not ln.lstrip().startswith("#"))
stripped = re.sub(r"2>/dev/null|2>&1", "", code)
ok(
    "collector has no output redirection (writes nothing)",
    ">" not in stripped,
    [ln for ln in stripped.splitlines() if ">" in ln],
)
ok(
    "collector: no state-changing commands",
    not re.search(r"\b(rm|mv|cp|tee|dd|mkdir|touch|chmod|chown|ln|truncate|kill|reboot|sed\s+-i|curl|wget)\b", code),
    code,
)
ok(
    "collector: docker only top/logs (read-only verbs)",
    set(re.findall(r"docker (\w+)", code)) == {"top", "logs"},
    re.findall(r"docker (\w+)", code),
)
ok(
    "collector: config keys via grep -o only",
    all("grep -ho" in ln for ln in code.splitlines() if "config.json" in ln or "BackupOptions" in ln),
    code,
)

# trip file: the default window comes from PRETRIP_TRIP; without one only --facts runs
r = json.loads(saved.read_text()) if saved.exists() else {}
ok("trip file: collect window = the fixture trip", r.get("window") == ["2026-10-30", "2026-11-15"], r.get("window"))
noenv = dict(env, PRETRIP_TRIP=str(T / "no-such-trip.json"))
p = subprocess.run([sys.executable, PC, "--json"], capture_output=True, text=True, env=noenv, timeout=60)
ok(
    "no trip file, no --from/--until: exit 2, says so",
    p.returncode == 2 and "no trip window" in p.stderr,
    (p.returncode, p.stderr[-300:]),
)
p = subprocess.run(
    [sys.executable, PC, "--from", "2026-10-30", "--facts", str(T / "facts.json")],
    capture_output=True,
    text=True,
    env=noenv,
    timeout=60,
)
ok(
    "no trip file, only --from: exit 2",
    p.returncode == 2 and "no trip window" in p.stderr,
    (p.returncode, p.stderr[-300:]),
)
p = subprocess.run(
    [sys.executable, PC, "--json", "--facts", str(T / "facts.json")],
    capture_output=True,
    text=True,
    env=noenv,
    timeout=60,
)
try:
    hl = json.loads(p.stdout)["headline"]
except (ValueError, KeyError):
    hl = ""
ok(
    "no trip file + --facts: window from the facts",
    p.returncode in (0, 1) and "30.10.–15.11." in hl,
    (p.returncode, hl, p.stderr[-300:]),
)
bad = T / "bad-trip.json"
bad.write_text(json.dumps({"from": "2026-10-30"}))
p = subprocess.run(
    [sys.executable, PC, "--json"], capture_output=True, text=True, env=dict(env, PRETRIP_TRIP=str(bad)), timeout=60
)
ok(
    "trip file without until: exit 2, bad trip file",
    p.returncode == 2 and "bad trip file" in p.stderr,
    (p.returncode, p.stderr[-300:]),
)
shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
