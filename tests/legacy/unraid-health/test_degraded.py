#!/usr/bin/env python3
"""Offline tests for ~/bin/unraid-health (2026-10-30): degraded array and docker.img growth.
  - array:degraded: the 26-30.10 disk12 story (healthy -> DISK_DSBL with its device -> DISK_NP_DSBL emulated,
    device gone after a reboot -> rebuild -> OK), the 7-day and away-period escalation (trips.json and calendar
    trips via core.away), the start date seeded from earlier daily reports, and the exemption kept only for a slot
    that never held a disk (an unassigned parity2).
  - docker.img: fs:docker.img:growth against unraid-capacity's daily sample from 20-48 h ago (warn once, quiet on
    flat days, too new / too old references ignored) and the 75% band.
Mutates fixtures/collect-2026-10-12.txt per scenario, runs with --input/--state-dir/--reports-dir/--no-send in a temp
dir. Never touches SSH, the real state or reports, or the owner's Telegram (the sender is a stub that must stay
unused). UH_BIN overrides the script path (to test a copy before installing it)."""
import atexit, datetime as dt, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

UH = os.environ.get("UH_BIN", "/home/node/bin/unraid-health")
if "array:degraded" not in Path(UH).read_text(errors="replace"):
    print(f"skip - {UH} has no array:degraded rule yet (older copy)")
    sys.exit(0)
HERE = Path(__file__).resolve().parent
BASE = (HERE / "fixtures/collect-2026-10-12.txt").read_text()
T = Path(tempfile.mkdtemp(prefix="uh-degraded."))
atexit.register(shutil.rmtree, T, True)
TZ = ZoneInfo("Europe/Zurich")
TODAY = dt.datetime.now(TZ).date()
NOW = dt.datetime.now(TZ).timestamp()
TG_MARK = T / "tg-called"
TG = T / "tg-stub"
TG.write_text(f"#!/bin/sh\necho \"$@\" >> {TG_MARK}\n")
TG.chmod(0o755)
NONE = str(T / "nonexistent")
P = F = 0


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1; print(f"ok   - {name}")
    else:
        F += 1; print(f"FAIL - {name}\n       got: {str(got)[:900]}")


def set_disk(raw, name, **kv):
    a = raw.index(f'["{name}"]\n')
    b = min(x for x in (raw.find("\n[", a + 1), raw.find("\n=====UH", a)) if x != -1)
    blk = raw[a:b]
    for k, v in kv.items():
        blk, n = re.subn(rf'^{k}="[^"]*"$', f'{k}="{v}"', blk, flags=re.M)
        assert n == 1, (name, k)
    return raw[:a] + blk + raw[b:]


def set_var(raw, **kv):
    for k, v in kv.items():
        raw, n = re.subn(rf'^{k}="[^"]*"$', f'{k}="{v}"', raw, count=1, flags=re.M)
        assert n == 1, k
    return raw


def add_disk12(raw):
    """A disk12 block (cloned from disk11) in front of parity2."""
    a = raw.index('["disk11"]\n')
    b = raw.index('["parity2"]\n')
    blk = raw[a:b].replace('["disk11"]', '["disk12"]').replace('name="disk11"', 'name="disk12"')
    blk = blk.replace('idx="11"', 'idx="12"').replace("/mnt/disk11", "/mnt/disk12").replace("md11p1", "md12p1")
    return raw[:b] + blk + raw[b:]


def set_docker(raw, pct):
    """The df line of /var/lib/docker at pct% used (same total)."""
    m = re.search(r"^(\S+\s+btrfs\s+)(\d+) (\d+) (\d+)(\s+)\d+%( /var/lib/docker)$", raw, re.M)
    total = int(m.group(3)) + int(m.group(4))
    used = int(total * pct / 100)
    line = f"{m.group(1)}{m.group(2)} {used} {total - used}{m.group(5)}{pct:.0f}%{m.group(6)}"
    return raw[:m.start()] + line + raw[m.end():]


DOCKER_USED = int(re.search(r"^\S+\s+btrfs\s+\d+ (\d+) \d+\s+\d+% /var/lib/docker$", BASE, re.M).group(1))
DOCKER_FREE = int(re.search(r"^\S+\s+btrfs\s+\d+ \d+ (\d+)\s+\d+% /var/lib/docker$", BASE, re.M).group(1))

P2OK = dict(device="sdk", id="TEST_PARITY2", idSb="TEST_PARITY2", status="DISK_OK", color="green-on",
            size="15625879500", sizeSb="15625879500", rotational="1", transport="ata")
D12 = dict(device="sdz", id="TEST_DISK12", idSb="TEST_DISK12", size="9766436812", sizeSb="9766436812",
           status="DISK_OK", color="green-on", fsSize="9764349900", fsFree="5126228124", fsUsed="4638121776")
WITH12 = set_disk(add_disk12(BASE), "parity2", **P2OK)
HEALTHY = set_var(set_disk(WITH12, "disk12", **D12), mdNumDisabled="0", mdNumInvalid="0")
DSBL = set_var(set_disk(WITH12, "disk12", **dict(D12, status="DISK_DSBL", color="red-off", numErrors="256")),
               mdNumDisabled="1", mdNumInvalid="1")
# after the reboot: no device, superblock and the emulated filesystem still there (live shape 28-30.10)
GONE = set_var(set_disk(WITH12, "disk12", **dict(D12, device="", id="", idSb="", size="0", status="DISK_NP_DSBL",
                                                 color="red-off")), mdNumDisabled="1", mdNumInvalid="1")
REBUILD = set_var(set_disk(WITH12, "disk12", **dict(D12, id="TEST_DISK12B", idSb="TEST_DISK12B", status="DISK_DSBL",
                                                    color="red-on")),
                  mdNumDisabled="1", mdNumInvalid="1", mdResync="9766436812", mdResyncPos="1000000000",
                  mdResyncDb="1543796", mdResyncDt="30", mdResyncAction="recon D12")


def run(sd, raw, tag, samples=NONE, trips=NONE, calcache=NONE):
    f = T / f"{tag}.txt"; f.write_text(raw)
    env = dict(os.environ, UNRAID_HEALTH_TG=str(TG), UNRAID_HEALTH_TZ="Europe/Zurich",
               UNRAID_HEALTH_ACK_FILE=str(T / "ack.json"), UNRAID_HEALTH_HDDHEAT_LOG=NONE,
               UNRAID_HEALTH_CPUTEMP=NONE, UNRAID_HEALTH_CAPACITY_SAMPLES=str(samples),
               UNRAID_HEALTH_TRIPS=str(trips), UNRAID_HEALTH_CALCACHE=str(calcache))
    r = subprocess.run([UH, "--json", "--no-send", "--input", str(f), "--state-dir", str(sd),
                        "--reports-dir", str(sd / "reports"), "--inbox-dir", str(sd / "inbox")],
                       capture_output=True, text=True, env=env, timeout=60)
    try:
        res = json.loads(r.stdout[r.stdout.find("{"):])
    except Exception:
        print(r.stdout[-1500:], r.stderr[-1500:]); raise
    res["rc"] = r.returncode
    return res


def find(res, key, where=("alerts", "ongoing")):
    return next((a for w in where for a in res[w] if a["key"] == key), None)


def keys(res, where="alerts"):
    return [a["key"] for a in res[where]]


def report(sd):
    return sorted((sd / "reports").glob("*.md"))[-1].read_text()


def state(sd):
    return json.loads((sd / "state.json").read_text())


def trips_file(name, frm, to):
    p = T / name
    p.write_text(json.dumps({"trips": {"x": {"name": "trip", "from": frm.isoformat(), "to": to.isoformat()}}}))
    return p


def samples_file(name, *pts):
    """unraid-capacity samples.jsonl lines: (hours ago, docker used bytes)."""
    p = T / name
    p.write_text("".join(json.dumps({"ts": NOW - h * 3600, "date": "x", "docker_img": {
        "size": 53687091200, "used": used, "free": DOCKER_USED + DOCKER_FREE - used}}) + "\n" for h, used in pts))
    return p


# ------------------------------------------------------------- the 26-30.10 story
sd = T / "story"
res = run(sd, HEALTHY, "a0")
ok("A0 healthy baseline: no array:degraded", find(res, "array:degraded") is None, res["ongoing"])
res = run(sd, DSBL, "a1")
d = find(res, "array:degraded")
ok("A1 disabled with its device: array:degraded new, warn (day 0, no trip)",
   d and d["status"] == "new" and d["level"] == "warn", d)
ok("A1 title: disabled, contents emulated, 0 days, parity still covers one",
   d and "disk12 disabled, contents emulated since" in d["title"] and "(0 days)" in d["title"]
   and "parity still covers 1 more disk failure" in d["title"], d and d["title"])
ok("A1 the fresh failure stays CRIT (disk status + array counts)",
   {"disk:disk12:status", "array:disabled", "array:invalid"} <= {a["key"] for a in res["alerts"]
                                                                  if a["level"] == "crit"}, keys(res))
ok("A1 since kept in state", "disk12" in state(sd).get("degraded", {}), state(sd).get("degraded"))
since1 = state(sd)["degraded"]["disk12"]["since"]

res = run(sd, GONE, "a2")
d = find(res, "array:degraded")
ok("A2 device gone after the reboot: still degraded, ongoing, nothing new", d and d["status"] == "ongoing"
   and res["alerts"] == [] and res["rc"] == 0, (d, keys(res)))
ok("A2 not an expected empty slot", res["metrics"]["array"]["empty_slots"] == []
   and res["metrics"]["array"]["emulated"] == ["disk12"], res["metrics"]["array"])
ok("A2 title says emulated (no device)", d and "disk12 emulated (no device) since" in d["title"], d and d["title"])
ok("A2 generic disk12 CRITs resolve into array:degraded",
   {"disk:disk12:status", "array:disabled", "array:invalid"} <= {p["key"] for p in res["resolved"]},
   [p["key"] for p in res["resolved"]])
ok("A2 same start date as A1", state(sd)["degraded"]["disk12"]["since"] == since1, state(sd)["degraded"])
ok("A2 lost id remembered", state(sd).get("slot_hist") == {"disk12": "TEST_DISK12"}, state(sd).get("slot_hist"))
rep = report(sd)
ok("A2 report: DEGRADED, not 'expected'", "DEGRADED: disk12 emulated (no device)" in rep and "expected" not in rep,
   [ln for ln in rep.splitlines() if "Disabled" in ln])
ok("A2 report lists 12 data disks (the emulated one counts)", "2 parity + 12 data disks" in rep,
   [ln for ln in rep.splitlines() if "State:" in ln])

st = state(sd)
st["degraded"]["disk12"]["since"] = NOW - 8 * 86400
(sd / "state.json").write_text(json.dumps(st))
res = run(sd, GONE, "a3")
d = find(res, "array:degraded")
ok("A3 8 days degraded: escalated to critical", d and d["status"] == "escalated" and d["level"] == "crit"
   and "(8 days)" in d["title"], d)

res = run(sd, REBUILD, "a4")
ok("A4 rebuild running onto disk12: array:degraded is info", find(res, "array:degraded") is None
   and any(x.startswith("Array degraded: disk12 being rebuilt") for x in res["info"]), (keys(res), res["info"]))
ok("A4 disk12 status info, no CRIT for it", not [a for a in res["alerts"] if a["level"] == "crit"
                                                  and "disk12" in a["title"]], keys(res))
ok("A4 start date kept during the rebuild", state(sd)["degraded"]["disk12"]["since"] == NOW - 8 * 86400,
   state(sd).get("degraded"))
done = set_var(set_disk(WITH12, "disk12", **dict(D12, id="TEST_DISK12B", idSb="TEST_DISK12B")),
               mdNumDisabled="0", mdNumInvalid="0")
res = run(sd, done, "a5")
ok("A5 rebuilt: no array:degraded, nothing new", find(res, "array:degraded") is None and res["alerts"] == [],
   keys(res))
ok("A5 state forgets the degraded slot and the lost id", "degraded" not in state(sd) and "slot_hist" not in state(sd),
   {k: state(sd).get(k) for k in ("degraded", "slot_hist")})

# ------------------------------------------------------------- first sight: dated from the daily reports
sd = T / "seed"
res = run(sd, HEALTHY, "b0")
d1, d2, d3 = (TODAY - dt.timedelta(days=n) for n in (1, 2, 3))
(sd / "reports" / f"{d2.isoformat()}.md").write_text(   # the morning report after an evening failure
    "# Unraid health report\n- **CRIT** disk12: status DISK_DSBL (X)\n"
    f"  - `{d3:%b} {d3.day:2d} 22:15:14 Homesrv kernel: md: disk12 write error, sector=1`\n")
(sd / "reports" / f"{d1.isoformat()}.md").write_text("## Array\n- State: STARTED; empty slot(s): disk12\n")
(sd / "reports" / f"{d3.isoformat()}.md").write_text("## Array\n- all fine\n")
res = run(sd, GONE, "b1")
d = find(res, "array:degraded")
want = f"since {d3.day:02d}.{d3.month:02d}. (3 days)"
ok("B1 no record yet: since = the first report listing it, refined by its md error line", d and want in d["title"]
   and d["status"] == "new", d and d["title"])
seed = dt.datetime.fromtimestamp(state(sd)["degraded"]["disk12"]["since"], TZ)
ok("B1 seeded at the md error time the evening before", (seed.date(), seed.hour, seed.minute) == (d3, 22, 15), seed)
sd = T / "seed2"
run(sd, HEALTHY, "b3")
(sd / "reports" / f"{d1.isoformat()}.md").write_text("- **CRIT** disk12: status DISK_DSBL (X)\n")
res = run(sd, GONE, "b4")
s0 = dt.datetime.fromtimestamp(state(sd)["degraded"]["disk12"]["since"], TZ)
ok("B4 no md error line: since = the first report's day", (s0.date(), s0.hour) == (d1, 0), s0)
ok("B1 never texted (--no-send)", not TG_MARK.exists(), TG_MARK.exists() and TG_MARK.read_text())

sd = T / "fresh"
res = run(sd, GONE, "b2")
d = find(res, "array:degraded")
ok("B2 first run on the emulated shape: listed (baseline), not exempt", d and d["baseline"]
   and res["metrics"]["array"]["empty_slots"] == [], (d, res["metrics"]["array"]["empty_slots"]))

# ------------------------------------------------------------- away periods
for tag, frm, to, lvl, txt in (
        ("c1", TODAY + dt.timedelta(days=10), TODAY + dt.timedelta(days=26), "crit", "; away from "),
        ("c2", TODAY + dt.timedelta(days=20), TODAY + dt.timedelta(days=36), "warn", None),
        ("c3", TODAY - dt.timedelta(days=2), TODAY + dt.timedelta(days=5), "crit", "; away now, until "),
        ("c4", TODAY - dt.timedelta(days=20), TODAY - dt.timedelta(days=5), "warn", None)):
    sd = T / tag
    tf = trips_file(f"{tag}.json", frm, to)
    run(sd, HEALTHY, f"{tag}a", trips=tf)
    res = run(sd, GONE, f"{tag}b", trips=tf)
    d = find(res, "array:degraded")
    ok(f"{tag.upper()} trip {frm}..{to}: {lvl}" + (f" with '{txt.strip('; ')}'" if txt else ""),
       d and d["level"] == lvl and (txt is None or txt in d["title"]) and (txt or "away" not in d["title"]),
       d and (d["level"], d["title"]))
sd = T / "cal"
cal = T / "events.json"
cal.write_text(json.dumps({"events": [{"start": (TODAY + dt.timedelta(days=5)).isoformat(),
                                       "end": (TODAY + dt.timedelta(days=8)).isoformat(), "all_day": True,
                                       "flags": {"away_overnight": True}}]}))
run(sd, HEALTHY, "d0", calcache=cal)
res = run(sd, GONE, "d1", calcache=cal)
d = find(res, "array:degraded")
ok("D1 calendar trip of 3 away nights in 5 days (core.away): crit", d and d["level"] == "crit"
   and "(in 5 days)" in d["title"], d and (d["level"], d["title"]))

# ------------------------------------------------------------- the exemption: only a slot that never held a disk
sd = T / "empty"
run(sd, BASE, "e0")
res = run(sd, BASE, "e1")
ok("E1 unassigned parity2 (grey, no superblock): no array:degraded, no CRIT",
   find(res, "array:degraded") is None and not [a for a in res["ongoing"] if a["level"] == "crit"],
   [(a["key"], a["level"]) for a in res["ongoing"]])
ok("E1 report: expected empty slot", "(= the empty parity2 slot; expected)" in report(sd),
   [ln for ln in report(sd).splitlines() if "Disabled" in ln])
p2gone = set_disk(BASE, "parity2", color="red-off", sizeSb="15625879500")
res = run(sd, p2gone, "e2")
d = find(res, "array:degraded")
ok("E2 parity2 dropped out (red, superblock size): degraded, single parity left",
   d and "parity2 disabled (no device) since" in d["title"] and "parity still covers 1 more disk failure" in d["title"],
   d and d["title"])

# ------------------------------------------------------------- docker.img growth
sd = T / "docker"
run(sd, BASE, "g0")
grow = samples_file("grow.jsonl", (53, DOCKER_USED - 20e9), (29, DOCKER_USED - 10e9), (5, DOCKER_USED - 1e9))
res = run(sd, BASE, "g1", samples=grow)
g = find(res, "fs:docker.img:growth")
ok("G1 +10 GB against the 29-h-old sample: warn, new", g and g["level"] == "warn" and g["status"] == "new"
   and "docker.img grew 10.0 GB in 29 h" in g["title"], g)
ok("G1 metrics: per 24 h scaled down from 29 h", abs(res["metrics"]["docker_growth"]["per24"] - 10e9 * 24 / 29) < 1e8,
   res["metrics"].get("docker_growth"))
res = run(sd, BASE, "g2", samples=grow)
ok("G2 same growth next run: ongoing, not alerted again", find(res, "fs:docker.img:growth", ("ongoing",))
   and "fs:docker.img:growth" not in keys(res), keys(res))
flat = samples_file("flat.jsonl", (29, DOCKER_USED - 1e9))
res = run(sd, BASE, "g3", samples=flat)
ok("G3 flat day (+1 GB): cleared", find(res, "fs:docker.img:growth") is None
   and "fs:docker.img:growth" in [p["key"] for p in res["resolved"]], [p["key"] for p in res["resolved"]])
res = run(sd, BASE, "g4", samples=samples_file("new.jsonl", (5, DOCKER_USED - 10e9)))
ok("G4 reference only 5 h old: no growth finding", find(res, "fs:docker.img:growth") is None
   and "docker_growth" not in res["metrics"], res["metrics"].get("docker_growth"))
res = run(sd, BASE, "g5", samples=samples_file("old.jsonl", (60, DOCKER_USED - 10e9)))
ok("G5 reference 60 h old: ignored", find(res, "fs:docker.img:growth") is None, keys(res))
res = run(sd, BASE, "g6", samples=samples_file("shrink.jsonl", (24, DOCKER_USED + 8e9)))
ok("G6 shrank (cleanup): nothing", find(res, "fs:docker.img:growth") is None, keys(res))

sd = T / "band"
run(sd, BASE, "h0")
res = run(sd, set_docker(BASE, 76), "h1")
s = find(res, "space:docker.img")
ok("H1 docker.img crosses 75%: warn once", s and s["status"] == "new" and s["level"] == "warn", s)
res = run(sd, set_docker(BASE, 80), "h2")
ok("H2 80%: same band, no new alert", "space:docker.img" not in keys(res), keys(res))
res = run(sd, set_docker(BASE, 74.5), "h3")
ok("H3 74.5%: still held by the 1% hysteresis below 75", find(res, "space:docker.img") is not None,
   keys(res, "ongoing"))
res = run(sd, set_docker(BASE, 91), "h4")
s = find(res, "space:docker.img")
ok("H4 91%: the next band alerts again", s and s["status"] == "worse" and s["level"] == "warn", s)
res = run(sd, set_docker(BASE, 70), "h5")
ok("H5 70%: cleared", find(res, "space:docker.img") is None, keys(res, "ongoing"))

ok("the owner was never texted", not TG_MARK.exists(), TG_MARK.exists() and TG_MARK.read_text())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
