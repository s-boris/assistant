#!/usr/bin/env python3
"""Offline tests for ~/bin/unraid-health (2026-10-15): sync awareness (disk being rebuilt is INFO, others
CRIT, CRIT again after a failed sync), temperature floors / hddheat hand-off / NVMe WCTEMP counter, and the
ack list (CLI + matching + expiry + CRIT safety). Mutates fixtures/collect-2026-10-12.txt per scenario and
runs with --input/--state-dir/--no-send in a temp dir. Never touches SSH, the real state, the real ack file
or the owner's Telegram. UH_BIN overrides the script path (to test a copy before installing it)."""
import atexit, datetime as dt, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

UH = os.environ.get("UH_BIN", "/home/node/bin/unraid-health")
HERE = Path(__file__).resolve().parent
BASE = (HERE / "fixtures/collect-2026-10-12.txt").read_text()
T = Path(tempfile.mkdtemp(prefix="uh-sync-ack."))
atexit.register(shutil.rmtree, T, True)
ACK = T / "ack.json"
NO_HEAT = T / "no-hddheat-runs.log"
HEAT = T / "hddheat-runs.log"
TODAY = dt.datetime.now(ZoneInfo("Europe/Zurich")).date().isoformat()
P = F = 0


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1; print(f"ok   - {name}")
    else:
        F += 1; print(f"FAIL - {name}\n       got: {str(got)[:700]}")


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


def notes(raw, items):
    a = raw.index("=====UH notify_unread\n") + len("=====UH notify_unread\n")
    b = raw.index("=====UH ", a)
    body = ""
    for ts, event, subj, imp in items:
        name = re.sub(r"\W", "_", event) + f"_{ts}.notify"
        body += (f"@file={name}\ntimestamp={ts}\nevent=\"{event}\"\nsubject=\"{subj}\"\ndescription=\"x\"\n"
                 f"importance=\"{imp}\"\nlink=\"/Main\"\n\n")
    return raw[:a] + body + raw[b:]


def run(sd, raw, tag, heat=False, send=False):
    f = T / f"{tag}.txt"; f.write_text(raw)
    env = dict(os.environ, UNRAID_HEALTH_TG="/bin/true", UNRAID_HEALTH_ACK_FILE=str(ACK),
               UNRAID_HEALTH_HDDHEAT_LOG=str(HEAT if heat else NO_HEAT), UNRAID_HEALTH_TZ="Europe/Zurich")
    env.pop("UNRAID_HEALTH_NO_SEND", None)
    if heat:
        HEAT.write_text("x\n")       # hddheat logged a run just now
    r = subprocess.run([UH, "--json", *([] if send else ["--no-send"]), "--input", str(f), "--state-dir", str(sd),
                        "--reports-dir", str(sd / "reports"), "--inbox-dir", str(sd / "inbox")],
                       capture_output=True, text=True, env=env, timeout=60)
    try:
        return json.loads(r.stdout[r.stdout.find("{"):])
    except Exception:
        print(r.stdout[-1500:], r.stderr[-1500:]); raise


def ack_cli(*args):
    env = dict(os.environ, UNRAID_HEALTH_ACK_FILE=str(ACK))
    return subprocess.run([UH, "ack", *args], capture_output=True, text=True, env=env, timeout=30)


def titles(res, level=None):
    return [a["msg"] for a in res["alerts"] if level is None or a["level"] == level]


def has(lst, s):
    return any(s in x for x in lst)


def report(sd):
    return sorted((sd / "reports").glob("*.md"))[-1].read_text()


real = Path("/home/node/.local/state/unraid-health/state.json")
real_before = real.read_bytes() if real.exists() else None
real_ack = Path("/home/node/.config/unraid-health/ack.json")
real_ack_before = real_ack.read_bytes() if real_ack.exists() else None

# ---------------------------------------------------------------- sync awareness
P2 = dict(device="sdk", id="WDC_WD160EDFZ-TEST", status="DISK_INVALID", rotational="1", transport="ata",
          type="Parity", size="15625879500")
SYNC = dict(mdNumDisabled="0", mdResync="15625879500", mdResyncPos="9167719428", mdResyncDb="1543796",
            mdResyncDt="30", mdResyncAction="recon Q")
running = notes(set_var(set_disk(BASE, "parity2", **P2), **SYNC),
                [(1791965701, "Unraid Data-Rebuild", "Notice [HOMESRV] - Data-Rebuild started", "warning")])

sd = T / "sync"
run(sd, BASE, "s0")
res = run(sd, running, "s1")
ok("S1 parity2 rebuilt by its own sync: no CRIT", titles(res, "crit") == [], titles(res))
ok("S1 'Data-Rebuild started' not forwarded", not has(titles(res), "Data-Rebuild"), titles(res))
ok("S1 parity2 status is INFO with progress", has(res["info"], "parity2: status DISK_INVALID")
   and has(res["info"], "being rebuilt by the running parity sync 58.7%"), res["info"])
ok("S1 array invalid count is INFO", has(res["info"], "Array reports 1 invalid disk(s): parity2, being rebuilt"),
   res["info"])
ok("S1 sync remembered in state", json.loads((sd / "state.json").read_text()).get("sync", {}).get("targets")
   == ["parity2"], (sd / "state.json").read_text()[:300])
ok("S1 report lists the rebuild", "being rebuilt by the running sync: parity2" in report(sd), report(sd)[:600])

two = set_var(set_disk(running, "disk3", status="DISK_INVALID"), mdNumInvalid="2")
res = run(sd, two, "s2")
ok("S2 a different invalid disk during the sync stays CRIT", has(titles(res, "crit"), "disk3: status DISK_INVALID"),
   titles(res))
ok("S2 array count CRIT for the other disk only",
   has(titles(res, "crit"), "Array reports 1 invalid disk(s) besides parity2 (being rebuilt)"), titles(res))
ok("S2 parity2 still INFO", not has(titles(res), "parity2: status"), titles(res))

failed = set_var(set_disk(BASE, "parity2", **P2), mdNumDisabled="0", sbSyncExit="-5", sbSyncErrs="3")
res = run(sd, failed, "s3")
ok("S3 sync stopped with errors -> CRIT event", has(titles(res, "crit"), "Parity sync of parity2 stopped with "
                                                    "errors: exit code -5, 3 sync error(s)"), titles(res))
ok("S3 target still invalid after the sync -> CRIT again", has(titles(res, "crit"), "parity2: status DISK_INVALID")
   and has([a["msg"] for a in res["alerts"] + res["ongoing"] if a["level"] == "crit"],
           "Array reports 1 invalid disk(s)"), titles(res))

sd = T / "sync-ok"
run(sd, BASE, "o0")
run(sd, running, "o1")
done = set_var(set_disk(BASE, "parity2", **dict(P2, status="DISK_OK")), mdNumDisabled="0", mdNumInvalid="0")
res = run(sd, done, "o2")
ok("O2 sync finished cleanly -> no alerts", titles(res) == [], titles(res))
ok("O2 noted as finished", has(res["info"], "Parity sync of parity2 finished without errors"), res["info"])

sd = T / "rebuild-d3"
run(sd, BASE, "d0")
d3 = set_var(set_disk(BASE, "disk3", status="DISK_INVALID"), mdNumInvalid="2", mdResync="15625879500",
             mdResyncPos="100", mdResyncDb="1000", mdResyncDt="30", mdResyncAction="recon D3")
res = run(sd, d3, "d1")
ok("D1 data rebuild of disk3 (recon D3): no CRIT", titles(res, "crit") == [], titles(res))
ok("D1 disk3 INFO as data rebuild", has(res["info"], "disk3: status DISK_INVALID")
   and has(res["info"], "running data rebuild"), res["info"])

# ---------------------------------------------------------------- temperatures
HOT6 = (1791900000, "Unraid Disk 6 temperature", "Warning [HOMESRV] - Disk 6 is hot (51  C)", "warning")
sd = T / "temps"
run(sd, BASE, "t0")
t1 = notes(set_disk(set_disk(BASE, "disk6", temp="52"), "cache", temp="62"), [HOT6])
res = run(sd, t1, "t1")
ok("T1 HDD 52 C and NVMe 62 C under load -> silent", titles(res) == [], titles(res))
ok("T1 Unraid 'Disk 6 is hot' not forwarded (below warn 58)", has(res["info"], "below warn 58"), res["info"])
t2 = notes(set_disk(set_disk(BASE, "disk6", temp="59"), "cache", temp="66"), [])
res = run(sd, t2, "t2")
ok("T2 NVMe 66 C -> WARN at the 65 floor", has(titles(res, "warn"), "cache: 66°C (warn 65, critical 75)"), titles(res))
ok("T2 HDD 59 C without hddheat -> WARN at 58", has(titles(res, "warn"), "disk6: 59°C (warn 58, critical 60)"),
   titles(res))
sd = T / "temps-heat"
run(sd, BASE, "h0", heat=True)
h1 = notes(set_disk(BASE, "disk6", temp="59"), [(1791900000 + 7200, *HOT6[1:])])
res = run(sd, h1, "h1", heat=True)
ok("H1 hddheat active: no HDD temp alert from here", titles(res) == [], titles(res))
ok("H1 Unraid HDD heat warning left to hddheat", has(res["info"], "hddheat's job"), res["info"])
ok("H1 report says so", "judged by hddheat" in report(sd), report(sd)[:900])
sd = T / "wctemp"
run(sd, BASE, "w0")
wc = lambda n: BASE.replace("Warning  Comp. Temperature Time:    0", f"Warning  Comp. Temperature Time:    {n}")
res = run(sd, wc(12), "w1")
ok("W1 NVMe minutes above its own warning temp -> WARN", has(titles(res, "warn"), "12 min above the drive's own "
                                                              "warning temperature"), titles(res))
res = run(sd, wc(12), "w2")
ok("W2 unchanged counter -> silent", titles(res) == [], titles(res))
res = run(sd, wc(20), "w3")
ok("W3 counter rose -> alert again", has(titles(res), "20 min above") and has(titles(res), "previously 12"),
   titles(res))

# ---------------------------------------------------------------- ack list
r = ack_cli("add", "container:unifi-controller-reborn", "--until", "2099-01-01", "--reason", "rollback test")
ok("K CLI add", r.returncode == 0 and "Added a1" in r.stdout, r.stdout + r.stderr)
ok("K CLI rejects a bad match", ack_cli("add", "bogus:x", "--until", "2099-01-01", "--reason", "x").returncode == 2)
ok("K CLI rejects a past date", ack_cli("add", "disk:disk1", "--until", "2020-01-01", "--reason", "x").returncode == 2)
r = ack_cli("list")
ok("K CLI list", "a1" in r.stdout and "active" in r.stdout and "rollback test" in r.stdout, r.stdout)

unifi = re.sub(r"^([0-9a-f]{64}\|/unifi-controller-reborn\|)running\|0\|(\d+)\|([^|]*)\|[^|]*\|healthy\|",
               r"\1exited|143|\2|\3|2026-10-14T19:02:00Z|unhealthy|", BASE, count=1, flags=re.M)
unifi = re.sub(r"^([0-9a-f]{64}\|/Firefly-III\|)running\|0\|", r"\1exited|137|", unifi, count=1, flags=re.M)
assert unifi.count("|/unifi-controller-reborn|exited|143|") == 1 and "|/Firefly-III|exited|137|" in unifi
sd = T / "ack"
run(sd, BASE, "k0")
res = run(sd, unifi, "k1", send=True)
ok("K1 acked container not new", not has(titles(res), "unifi-controller-reborn"), titles(res))
ok("K1 un-acked issue still new", has(titles(res), "Firefly-III exited with code 137"), titles(res))
ok("K1 acked findings listed (stale health of a stopped container not judged)",
   len(res["acked"]) == 1 and all(a["ack"]["id"] == "a1" for a in res["acked"]),
   res["acked"])
ok("K1 report section", "## Acknowledged" in report(sd) and "rollback test" in report(sd), report(sd)[:900])
note = "".join(p.read_text() for p in (sd / "inbox").glob("*"))
ok("K1 inbox note without the acked issue", "Firefly-III" in note and "unifi" not in note, note[:600])
res = run(sd, unifi, "k2")
ok("K2 still acked, not resolved", titles(res) == [] and not has([p["key"] for p in res["resolved"]], "unifi"),
   (titles(res), res["resolved"]))
ACK.write_text(json.dumps({"version": 1, "acks": [{"id": "a1", "match": "container:unifi-controller-reborn",
                                                   "expires": TODAY, "reason": "rollback test"}]}))
res = run(sd, unifi, "k3")
ok("K3 ack expired -> alerts as new", has(titles(res), "unifi-controller-reborn exited with code 143")
   and not has(titles(res), "unifi-controller-reborn is unhealthy"), titles(res))
ok("K3 expiry noted", has(res["info"], "expired " + TODAY), res["info"])
ACK.write_text(json.dumps({"version": 1, "acks": [{"id": "a2", "match": "disk:disk3", "expires": "2099-01-01",
                                                   "reason": "x"}]}))
inv3 = set_var(set_disk(BASE, "disk3", status="DISK_INVALID"), mdNumInvalid="2")
res = run(sd, inv3, "k4")
ok("K4 a warn-level ack never hides a CRIT", has(titles(res, "crit"), "disk3: status DISK_INVALID"), titles(res))
ACK.write_text(json.dumps({"version": 1, "acks": [{"id": "a2", "match": "disk:disk3", "expires": "2099-01-01",
                                                   "reason": "x", "max_level": "crit"}]}))
res = run(sd, inv3, "k5")
ok("K5 --max-level crit ack hides it, not 'resolved'", not has(titles(res), "disk3")
   and has([a["msg"] for a in res["acked"]], "disk3: status")
   and not has([p["key"] for p in res["resolved"]], "disk:disk3"), (titles(res), res["resolved"]))
ACK.write_text("{broken")
res = run(sd, inv3, "k6")
ok("K6 broken ack file: no acks, noted as a gap, run still works", has(res["gaps"], "unreadable")
   and has(titles(res) + [a["msg"] for a in res["ongoing"]], "disk3: status"), res["gaps"])
ACK.write_text(json.dumps({"version": 1, "acks": [{"id": "a1", "match": "key:x*", "expires": "2099-01-01"}]}))
ok("K CLI rm", ack_cli("rm", "a1").returncode == 0 and json.loads(ACK.read_text())["acks"] == [])
ok("K CLI rm unknown -> 1", ack_cli("rm", "a9").returncode == 1)

# ---------------------------------------------------------------- stopped:NAME acks (stopped on purpose)
r = ack_cli("add", "stopped:unifi-controller-reborn", "--until", "never", "--reason", "replaced, stopped on purpose")
ok("S CLI add stopped: --until never", r.returncode == 0 and json.loads(ACK.read_text())["acks"][-1]["expires"]
   == "never", r.stdout + r.stderr)
ok("S CLI rejects a bad --until", ack_cli("add", "stopped:x", "--until", "someday", "--reason", "x").returncode == 2)
r = ack_cli("list")
ok("S CLI list: never is active", "until never (active" in r.stdout, r.stdout)
sd = T / "stopped"
run(sd, BASE, "s0")
res = run(sd, unifi, "s1")
ok("S1 stopped container acked (no expiry)", not has(titles(res), "unifi-controller-reborn")
   and has([a["msg"] for a in res["acked"]], "unifi-controller-reborn exited with code 143")
   and has(titles(res), "Firefly-III exited"), (titles(res), res["acked"]))
ok("S1 report shows the reason", "replaced, stopped on purpose" in report(sd), report(sd)[:600])
loop = re.sub(r"^([0-9a-f]{64}\|/unifi-controller-reborn\|)running\|", r"\1restarting|", BASE, count=1, flags=re.M)
res = run(sd, loop, "s2")
ok("S2 a crash loop of that container still alerts", has(titles(res), "unifi-controller-reborn is restarting"),
   titles(res))
gone = re.sub(r"^[0-9a-f]{64}\|/?unifi-controller-reborn\|.*\n", "", unifi, flags=re.M)
assert "unifi-controller-reborn" not in gone.split("=====UH docker", 1)[1].split("=====UH virsh")[0]
res = run(sd, unifi, "s3")
res = run(sd, gone, "s4")
ok("S4 removing the stopped container is acked too", not has(titles(res), "unifi-controller-reborn")
   and has([a["msg"] for a in res["acked"]], "unifi-controller-reborn has disappeared"), (titles(res), res["acked"]))
ACK.write_text(json.dumps({"version": 1, "acks": [{"id": "a1", "match": "stopped:unifi-controller-reborn",
                                                   "expires": "never"}]}))
sd = T / "stopped2"
run(sd, BASE, "s5")
res = run(sd, set_disk(unifi, "disk3", status="DISK_INVALID"), "s6")
ok("S6 stopped: never hides other findings", has(titles(res, "crit"), "disk3: status DISK_INVALID")
   and not has(titles(res), "unifi-controller-reborn"), titles(res))

ok("real state.json untouched", (real.read_bytes() if real.exists() else None) == real_before)
ok("real ack.json untouched", (real_ack.read_bytes() if real_ack.exists() else None) == real_ack_before)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
