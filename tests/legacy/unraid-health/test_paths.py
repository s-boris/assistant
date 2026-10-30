#!/usr/bin/env python3
"""Offline tests for ~/bin/unraid-health's remaining paths: the section parsers (kv/ini/df/btrfs/docker/virsh/
notifications/parity log/sensors/SMART, split_sections, parse_all), small helpers (cron interval, event target,
formatting), tg_text, collect() through a stubbed unraid-ssh (verbs and root mode, rc 255, timeout, incomplete
output, missing binary, retry), the owner-text decision end to end with a stubbed tg (silent baseline and
ongoing issues, new issues texted once, tg failure -> "please relay", NO_SEND), Unraid notification forwarding
(Fix Common Problems / User Scripts warnings, digit-only repeats, notices), the unreachable-server alert after
2 failures and its all-clear, a corrupt state file and the crash handler.
Never calls the real server (stub unraid-ssh, stub ssh first on PATH) and never sends Telegram (stub tg)."""
import atexit, importlib.machinery, importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

UH = os.environ.get("UH_BIN", "/home/node/bin/unraid-health")
HERE = Path(__file__).resolve().parent
FIX = (HERE / "fixtures/collect-2026-10-12.txt").read_text()
T = Path(tempfile.mkdtemp(prefix="uh-paths."))
atexit.register(shutil.rmtree, T, True)
P = F = 0


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1; print(f"ok   - {name}")
    else:
        F += 1; print(f"FAIL - {name}\n       got: {str(got)[:600]}")


# ---- stubs: unraid-ssh, ssh (safety net + root mode), tg
BIN = T / "bin"; BIN.mkdir()
SSH_LOG, US_LOG, TG_LOG = T / "ssh.log", T / "unraid-ssh.log", T / "tg.log"
(T / "fixture.txt").write_text(FIX)
(BIN / "unraid-ssh").write_text(f"""#!/bin/sh
echo "$*" >> {US_LOG}
if [ "$1" = mode ]; then echo "${{STUB_LOCKDOWN:-verbs}}"; exit 0; fi
n=$(grep -vc "^mode$" {US_LOG})
case "${{STUB_MODE:-ok}}" in
  ok) cat {T}/fixture.txt ;;
  fail255) echo "ssh: connect to host unraid port 22: No route to host" >&2; exit 255 ;;
  noend) echo "=====UH meta"; echo "boom: collector died" >&2; exit 1 ;;
  hang) exec sleep 8 ;;
  flaky) if [ "$n" -le 1 ]; then echo "kex_exchange_identification: reset" >&2; exit 255; fi; cat {T}/fixture.txt ;;
esac
""")
(BIN / "ssh").write_text(f"""#!/bin/sh
echo "$*" >> {SSH_LOG}
script=$(cat)
case "$script" in *"STRICTLY READ-ONLY"*) cat {T}/fixture.txt ;; *) exit 255 ;; esac
""")
(BIN / "tg").write_text(f"""#!/bin/sh
printf '%s\\n----\\n' "$1" >> {TG_LOG}
printf 'soft=%s\\n' "${{TG_SOFT:-}}" >> {TG_LOG}.env
[ -n "$TG_FAIL" ] && {{ echo "$TG_FAIL" >&2; exit 1; }}
exit 0
""")
for f in BIN.iterdir():
    f.chmod(0o755)
ENV = dict(os.environ, PATH=f"{BIN}:{os.environ.get('PATH', '')}", UNRAID_SSH_BIN=str(BIN / "unraid-ssh"),
           UNRAID_HEALTH_TG=str(BIN / "tg"), UNRAID_HEALTH_TZ="Europe/Zurich",
           UNRAID_HEALTH_HDDHEAT_LOG=str(T / "no-hddheat.log"), UNRAID_HEALTH_ACK_FILE=str(T / "no-ack.json"),
           UNRAID_HEALTH_CPUTEMP=str(T / "no-cputemp-log"), UNRAID_HEALTH_STATE_DIR=str(T / "dflt-state"),
           UNRAID_HEALTH_REPORT_DIR=str(T / "dflt-reports"), UNRAID_HEALTH_INBOX_DIR=str(T / "dflt-inbox"))
ENV.pop("UNRAID_HEALTH_NO_SEND", None)
os.environ.clear(); os.environ.update(ENV)

loader = importlib.machinery.SourceFileLoader("uh", UH)
spec = importlib.util.spec_from_loader("uh", loader)
uh = importlib.util.module_from_spec(spec)
loader.exec_module(uh)

# ================================================================ parsers
kv = uh.parse_kv('a="1"\nb = two\n[sect]=x\nnoeq\nc="x=y"')
ok("U kv: quotes stripped, '[' lines and lines without '=' skipped", kv == {"a": "1", "b": "two", "c": "x=y"}, kv)
ini = uh.parse_ini('top=1\n["disk1"]\nstatus="DISK_OK"\n# c\n[cache]\ntemp=38\n')
ok("U ini: quoted and bare sections, keys before a section under ''",
   ini == {"": {"top": "1"}, "disk1": {"status": "DISK_OK"}, "cache": {"temp": "38"}}, ini)
df = uh.parse_df("Filesystem Type 1B-blocks Used Available Use% Mounted on\n"
                 "/dev/md1p1 xfs 1000 900 100 90% /mnt/disk1\nshfs fuse 10 5 5 50% /mnt/user share\nbad line\n")
ok("U df: header skipped, mount with a space kept",
   set(df) == {"/mnt/disk1", "/mnt/user share"} and df["/mnt/disk1"]["avail"] == 100, df)
c, devs = uh.parse_btrfs("[/dev/a].write_io_errs    1\n[/dev/a].corruption_errs 2\n[/dev/b].write_io_errs 3\njunk\n")
ok("U btrfs: counters summed over devices, names normalized",
   c == {"write": 4, "corruption": 2} and devs == ["/dev/a", "/dev/b"], (c, devs))
dk = uh.parse_docker("rc=0\n" + "a" * 12 + "|plex|running|Up 2 days\nCannot connect to the Docker daemon\n",
                     "b" * 64 + "|/plex|running|0|3|2026-10-09T09:41:37Z|0001-01-01T00:00:00Z|healthy|true|"
                     "unless-stopped|plexinc/pms|oops|x\nshort|line\n")
ok("U docker: rc, ps rows, inspect fields, stray lines as error",
   dk["rc"] == 0 and dk["ps"][0]["name"] == "plex" and dk["inspect"]["plex"]["restarts"] == 3
   and dk["inspect"]["plex"]["oom"] is True and dk["inspect"]["plex"]["error"] == "oops|x"
   and "Cannot connect" in dk["error"], dk)
vs = uh.parse_virsh(" Id   Name          State\n----------------------\n 1    Home Assistant   running\n -    Win 11   shut off\n"
                    "error: failed to connect\n")
ok("U virsh: names with spaces, header and error lines skipped",
   vs == {"Home Assistant": "running", "Win 11": "shut off"}, vs)
nt = uh.parse_notifications('junk=1\n@file=a.notify\ntimestamp=5\nsubject="S [X] - y"\n\n@file=b.notify\nimportance="alert"\n')
ok("U notifications: one dict per @file, lines before the first ignored",
   nt == [{"file": "a.notify", "timestamp": "5", "subject": "S [X] - y"}, {"file": "b.notify", "importance": "alert"}], nt)
pl = uh.parse_parity_log("2026 Oct  5 03:00:01|36000|300000000|0|0|check|14000000000000\n"
                         "2026 Sep  1 01:00:00|100|5|-4|2\nnot a date|1|2|3|4|recon P\nshort|row\n", "UTC")
ok("U parity log: ts, default action 'check', bad date -> ts None, short rows skipped",
   len(pl) == 3 and pl[0]["errors"] == 0 and pl[0]["size"] == 14000000000000 and pl[1]["action"] == "check"
   and pl[1]["exit"] == -4 and pl[2]["ts"] is None and pl[2]["action"] == "recon P"
   and pl[0]["ts"] == 1791169201, pl)
se = uh.parse_sensors(json.dumps({
    "coretemp-isa-0000": {"Adapter": "ISA", "Core 0": {"temp2_input": 70.0}, "Package id 0":
                          {"temp1_input": 61.0, "temp1_max": 80.0, "temp1_crit": 100.0}},
    "nct6798-isa-0290": {"MB Temp": {"temp1_input": 33.0}, "CPU Temp": {"temp2_input": 99.0}}}))
ok("U sensors: CPU from the CPU chip's package (not a board 'CPU Temp'), max/crit, board temp",
   se["cpu"] == 61.0 and se["cpu_label"] == "Package id 0" and se["cpu_high"] == 80.0 and se["cpu_crit"] == 100.0
   and se["board"] == 33.0, se)
se2 = uh.parse_sensors(json.dumps({"k10temp-pci-00c3": {"Tctl": {"temp1_input": 70.0}, "Tdie": {"temp2_input": 60.0}}}))
se3 = uh.parse_sensors(json.dumps({"coretemp-isa-0000": {"Core 0": {"temp2_input": 50.0}, "Core 1": {"temp3_input": 57.0}}}))
ok("U sensors: Tdie preferred over Tctl; only cores -> hottest core; bad JSON -> None",
   se2["cpu"] == 60.0 and se3["cpu"] == 57.0 and se3["cpu_label"] == "hottest core"
   and uh.parse_sensors("not json") is None, (se2, se3))
ata = uh.parse_smart("Device Model:     WDC WD100EDAZ\nSerial Number:    X1\n"
                     "SMART overall-health self-assessment test result: PASSED\n"
                     "  9 Power_On_Hours          0x0032   100   100   000    Old_age   Always       -       12345\n"
                     "194 Temperature_Celsius     0x0002   177   177   000    Old_age   Always       -       34 (Min/Max 20/45)\n"
                     "199 UDMA_CRC_Error_Count    0x000a   200   200   000    Old_age   Always       -       7\n")
ok("U smart ATA: kind, health, temp from 194, POH from 9, CRC raw, model",
   ata["kind"] == "ata" and ata["health"] == "PASSED" and ata["temp"] == 34 and ata["poh"] == 12345
   and ata["attrs"][199]["raw"] == 7 and ata["model"] == "WDC WD100EDAZ", ata)
nv = uh.parse_smart("Model Number: Samsung 990\nCritical Warning:                   0x04\nTemperature:                        66 Celsius\n"
                    "Available Spare:                    100%\nAvailable Spare Threshold:          10%\n"
                    "Percentage Used:                    3%\nMedia and Data Integrity Errors:    1,024\n"
                    "Warning  Comp. Temperature Time:    12\nUnsafe Shutdowns:                   9\nPower On Hours:  2,000\n")
ok("U smart NVMe: critical warning, temp, spare, used, media errors with commas, warn-temp minutes",
   nv["kind"] == "nvme" and nv["nvme"]["critical_warning"] == 4 and nv["temp"] == 66
   and nv["nvme"]["media_errors"] == 1024 and nv["nvme"]["warn_temp_min"] == 12 and nv["poh"] == 2000, nv)
sc = uh.parse_smart("SMART Health Status: OK\nCurrent Drive Temperature:     41 C\nElements in grown defect list: 3\n"
                    "read:   0 0 0 0 0 0 2\nwrite:  0 0 0 0 0 0 1\nNon-medium error count: 5\n")
sb = uh.parse_smart("Device is in STANDBY mode, exit(2)\n")
ok("U smart SCSI/SAS: health, temp, grown defects, uncorrected sum; standby flagged",
   sc["kind"] == "scsi" and sc["health"] == "OK" and sc["temp"] == 41 and sc["grown"] == 3
   and sc["uncorrected"] == 3 and sc["nonmedium"] == 5 and sb["standby"] and uh.parse_smart("")["kind"] is None, sc)
ss = uh.split_sections("junk\n=====UH smartctl disk1 sdl\nline1\n=====UH_RC 4\n=====UH virsh\n\nx\n\n")
ok("U split_sections: args, rc marker, text stripped, lines before the first section dropped",
   [s["name"] for s in ss] == ["smartctl", "virsh"] and ss[0]["args"] == ["disk1", "sdl"] and ss[0]["rc"] == 4
   and ss[1]["text"] == "x", ss)
d = uh.parse_all(FIX)
ok("U parse_all on the recorded output: every section kind lands",
   d["meta"].get("host") == "Homesrv" and "disk1" in d["disks"] and d["df"] and len(d["btrfs"]) == 5
   and {"disk1", "cache", "parity"} <= set(d["smart"]) and d["docker"] and d["docker"]["ps"]
   and d["virsh"] is not None and len(d["notifications"]) == 20 and d["meminfo"].get("MemTotal", 0) > 0
   and d["sensors"] and d["sensors"]["cpu"] is not None and "end" in d["sections"],
   {k: (len(v) if hasattr(v, "__len__") else v) for k, v in d.items() if k != "sections"})

# ================================================================ helpers
ok("U cron interval: monthly 31, weekly 7, daily 1, Unraid 1st-Sunday guard 35, garbage None",
   uh.cron_interval_days("0 3 1 * *".split(), "") == 31 and uh.cron_interval_days("0 3 * * 0".split(), "") == 7
   and uh.cron_interval_days("0 3 * * *".split(), "") == 1
   and uh.cron_interval_days("0 3 * * 0".split(), "[[ $(date +%e) -le 7 ]] && mdcmd check") == 35
   and uh.cron_interval_days("x y".split(), "") is None,
   [uh.cron_interval_days("0 3 * * 0".split(), "[[ $(date +%e) -le 7 ]] && mdcmd check")])
ok("U event target: disk SMART, cache temp, docker image utilization, unknown",
   uh.unraid_event_target("Unraid Disk 5 SMART health [199]") == ("disk5", "smart")
   and uh.unraid_event_target("Unraid Cache disk temperature") == ("cache", "temp")
   and uh.unraid_event_target("Docker image disk utilization") == ("image", "space")
   and uh.unraid_event_target("Fix Common Problems") == (None, None),
   uh.unraid_event_target("Docker image disk utilization"))
ok("U formatting: fmt_bytes, fmt_dur, shorten, docker zero time",
   uh.fmt_bytes(999) == "999 B" and uh.fmt_bytes(1_500_000) == "1.5 MB" and uh.fmt_bytes(None) == "?"
   and uh.fmt_dur(90061) == "1d 1h" and uh.fmt_dur(3720) == "1h 02m" and uh.fmt_dur(59) == "0m"
   and uh.shorten("a  b\nc", 10) == "a b c" and uh.shorten("x" * 20, 5) == "xxxx…"
   and uh.docker_ts("0001-01-01T00:00:00Z") is None and uh.docker_ts("2026-10-09T09:41:37.70Z") == 1791538897,
   [uh.fmt_bytes(1_500_000), uh.fmt_dur(3720)])

# ================================================================ tg_text
one = uh.tg_text("H", [{"level": "warn", "msg": "disk1 hot"}])
crit = uh.tg_text("H", [{"level": "warn", "msg": "a"}, {"level": "crit", "msg": "b"}])
many = uh.tg_text("H", [{"level": "warn", "msg": f"issue {i} " + "x" * 140} for i in range(12)])
ok("U tg_text: singular warn 🟠, plural with a crit 🔴",
   one == "🟠 Unraid H: 1 new issue\n• disk1 hot" and crit.startswith("🔴 Unraid H: 2 new issues"), [one, crit])
ok("U tg_text: long list cut under the Telegram budget with '…and N more'",
   len(many) <= uh.TG_MAX + 40 and re.search(r"…and \d+ more$", many) and "• issue 0" in many, many)

# ================================================================ collect() with a stubbed unraid-ssh
uh.SSH_RETRY_WAIT, uh.SSH_TIMEOUT = 0, 1
uh.UNRAID_SSH = str(BIN / "unraid-ssh")


def coll(mode, lockdown="verbs", host="stubhost"):
    US_LOG.unlink(missing_ok=True)
    os.environ["STUB_MODE"], os.environ["STUB_LOCKDOWN"] = mode, lockdown
    raw, err = uh.collect(host)
    calls = US_LOG.read_text().splitlines() if US_LOG.exists() else []
    return raw, err, [c for c in calls if c != "mode"]


raw, err, calls = coll("ok")
ok("C verbs mode: runs `unraid-ssh --host H run health`, returns the output",
   raw == FIX and err is None and calls == ["--host stubhost run health"], (err, calls))
raw, err, calls = coll("fail255")
ok("C rc 255: tried twice, error is the ssh line", raw is None and len(calls) == 2
   and err == "ssh: connect to host unraid port 22: No route to host", (err, calls))
raw, err, calls = coll("noend")
ok("C output without the end marker: 'collector incomplete' with exit code and stderr",
   raw is None and err == "collector incomplete (exit 1): boom: collector died" and len(calls) == 2, err)
t0 = time.time()
raw, err, calls = coll("hang")
ok("C hang: timeout per attempt, both attempts, 'timed out' error",
   raw is None and err == "ssh timed out after 1s" and len(calls) == 2 and time.time() - t0 < 10, (err, time.time() - t0))
raw, err, calls = coll("flaky")
ok("C first attempt fails, retry succeeds", raw == FIX and err is None and len(calls) == 2, (err, calls))
uh.UNRAID_SSH = str(T / "missing-unraid-ssh")
os.environ["STUB_LOCKDOWN"] = "verbs"
uh_lockdown = uh.lockdown_mode
uh.lockdown_mode = lambda: "verbs"
raw, err = uh.collect("stubhost")
ok("C unraid-ssh missing: 'could not run ssh'", raw is None and err.startswith("could not run ssh"), err)
uh.lockdown_mode = uh_lockdown
uh.UNRAID_SSH = str(BIN / "unraid-ssh")
SSH_LOG.unlink(missing_ok=True)
raw, err, calls = coll("ok", lockdown="root", host="stubhost")
sshc = SSH_LOG.read_text() if SSH_LOG.exists() else ""
ok("C root mode: plain ssh (stub) with BatchMode + the read-only collector on stdin",
   raw == FIX and calls == [] and "BatchMode=yes" in sshc and sshc.rstrip().endswith("stubhost bash -s"), (err, sshc))
os.environ["STUB_LOCKDOWN"] = "garbage"
ok("C lockdown_mode: unknown answer -> root, 'verbs'/'emulate' kept",
   uh.lockdown_mode() == "root" and (os.environ.__setitem__("STUB_LOCKDOWN", "emulate") or uh.lockdown_mode() == "emulate"))
os.environ["STUB_LOCKDOWN"] = "verbs"

# ================================================================ end to end: owner text decision
def mutate(text, pat, rep):
    new, n = re.subn(pat, rep, text, count=1, flags=re.M)
    assert n, f"mutation found nothing: {pat}"
    return new


HOT = mutate(FIX, r'^temp="38"$', 'temp="58"')
HOT = mutate(HOT, r"^(199 UDMA_CRC_Error_Count .* -\s+)0$", r"\g<1>7")
HOT = mutate(HOT, r"^([0-9a-f]{64}\|/Firefly-III\|)running\|0\|", r"\1exited|137|")


def with_notes(raw, notes, archive=None):
    a = raw.index("=====UH notify_unread\n") + len("=====UH notify_unread\n")
    b = raw.index("=====UH ", a)
    body = "".join(f'@file={f}\ntimestamp={ts}\nevent="{ev}"\nsubject="{subj}"\ndescription="{desc}"\n'
                   f'importance="{imp}"\nlink="/Settings"\n\n' for f, ts, ev, subj, desc, imp in notes)
    raw = raw[:a] + body + raw[b:]
    if archive is not None:
        raw = mutate(raw, r"^notify_archive_count=\d+$", f"notify_archive_count={archive}")
    return raw


def run(sd, raw=None, args=(), env=None, mode="ok"):
    """One run; raw=None -> no --input (collect through the stub unraid-ssh). Returns (rc, out, err, tg, notes)."""
    TG_LOG.unlink(missing_ok=True)
    ib = sd / "inbox"
    before = set(ib.glob("*")) if ib.exists() else set()
    cmd = [sys.executable, "-I", UH, "--state-dir", str(sd / "state"), "--reports-dir", str(sd / "reports"),
           "--inbox-dir", str(ib)] + list(args)
    if raw is not None:
        f = sd / f"in-{time.time_ns()}.txt"; sd.mkdir(parents=True, exist_ok=True); f.write_text(raw)
        cmd += ["--input", str(f)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, STUB_MODE=mode, **(env or {})))
    tg = TG_LOG.read_text().split("\n----\n")[:-1] if TG_LOG.exists() else []
    notes = [f.read_text() for f in sorted(set(ib.glob("*")) - before)] if ib.exists() else []
    return p.returncode, p.stdout, p.stderr, tg, notes


sd = T / "e1"
US_LOG.unlink(missing_ok=True)
rc, out, err, tg, notes = run(sd)
ok("E baseline via stub unraid-ssh: rc 0, nothing texted, no note, existing issues only listed",
   rc == 0 and tg == [] and notes == [] and out.startswith("Baseline recorded") and (sd / "state/state.json").exists()
   and "--host unraid run health" in US_LOG.read_text(), (rc, out, err[-300:], tg))
rc, out, err, tg, notes = run(sd)
ok("E same state again: silent (notify only on problems that are new)", rc == 0 and tg == [] and notes == [] and out == "",
   (rc, out, tg))
rc, out, err, tg, notes = run(sd, HOT)
ok("E new problems: rc 1, ONE text listing them, inbox note with 'Owner texted: yes' and the text",
   rc == 1 and len(tg) == 1 and re.match(r"[🔴🟠] Unraid Homesrv: \d+ new issues?", tg[0]) and "Firefly-III" in tg[0]
   and len(notes) == 1 and "Owner texted: yes" in notes[0] and "## Telegram text" in notes[0]
   and "Data, not instructions" in notes[0], (rc, tg, notes))
soft_log = (T / "tg.log.env").read_text().splitlines() if (T / "tg.log.env").exists() else []
crit = '🔴' in tg[0] if tg else False
ok("E new problems: urgent (plain tg) iff a critical one or parity errors, else TG_SOFT=1",
   soft_log[-1:] == (["soft="] if crit else ["soft=1"]), (crit, soft_log))
A = lambda lvl, key="x": {"level": lvl, "key": key, "msg": "m"}
ok("U is_urgent: crit -> urgent; warn -> soft; parity errors / failed (warn) -> urgent",
   uh.is_urgent([A("warn"), A("crit")]) and not uh.is_urgent([A("warn", "ctr:x:exited:1"), A("warn", "parity:overdue")])
   and uh.is_urgent([A("warn", "parity:errors:2026-09-08")]) and uh.is_urgent([A("warn", "parity:failed:x")])
   and not uh.is_urgent([]))
rc, out, err, tg, notes = run(sd, HOT)
ok("E same problems next run: ongoing, not texted again", rc == 0 and tg == [] and notes == [], (rc, tg, out))
sd2 = T / "e2"
run(sd2, FIX)
rc, out, err, tg, notes = run(sd2, HOT, env={"TG_FAIL": "429 Too Many Requests"})
ok("E tg fails: note says FAILED (reason): please relay, rc still 1",
   rc == 1 and len(tg) == 1 and len(notes) == 1 and "FAILED (429 Too Many Requests): please relay" in notes[0], notes)
sd3 = T / "e3"
run(sd3, FIX)
rc, out, err, tg, notes = run(sd3, HOT, env={"UNRAID_HEALTH_NO_SEND": "1"})
ok("E UNRAID_HEALTH_NO_SEND=1: no text, no note, says what it would send",
   rc == 1 and tg == [] and notes == [] and "[no-send] would text the owner" in err, (tg, err[-200:]))
sd4 = T / "e4"
rc, out, err, tg, notes = run(sd4, HOT, args=["--dry-run"])
ok("E --dry-run: no state, no report", not (sd4 / "state/state.json").exists() and not (sd4 / "reports").exists()
   and tg == [], list(sd4.rglob("*")))

# ---- Unraid notifications: Fix Common Problems, User Scripts (vault deploy), notices, archive count
sd5 = T / "e5"
FCP_EV, US_EV = "Fix Common Problems", "User Scripts"
base5 = with_notes(FIX, [])
run(sd5, base5)
n1 = [("fcp_1.notify", 1791790000, FCP_EV, "Warning [HOMESRV] - 2 warnings found", "Docker update check failing", "warning"),
      ("us_1.notify", 1791790100, US_EV, "Alert [HOMESRV] - vault-deploy failed", "exit 1", "alert"),
      ("note_1.notify", 1791790200, "Unraid Status", "Notice [HOMESRV] - array health report [PASS]", "", "normal")]
rc, out, err, tg, notes = run(sd5, with_notes(base5, n1, archive=42), args=["--json"])
j = json.loads(out)
msgs = [a["msg"] for a in j["alerts"]]
ok("N FCP warning forwarded as WARN with its description",
   any(m == "Unraid warning: Fix Common Problems: 2 warnings found (Docker update check failing)" for m in msgs), msgs)
ok("N vault-deploy alert forwarded as CRIT, sorted first, text 🔴",
   j["alerts"][0]["level"] == "crit" and "vault-deploy failed" in j["alerts"][0]["msg"] and tg and tg[0].startswith("🔴"),
   (msgs, tg))
ok("N normal-importance notice: info only, not texted",
   any("New Unraid notice" in i and "array health report" in i for i in j["info"])
   and not any("array health" in m for m in msgs), j["info"])
ok("N archive count + unread listed in metrics", j["metrics"]["notifications"]["archive_count"] == 42
   and len(j["metrics"]["notifications"]["unread"]) == 3, j["metrics"]["notifications"]["archive_count"])
n2 = n1 + [("fcp_2.notify", 1791876400, FCP_EV, "Warning [HOMESRV] - 3 warnings found", "Docker update check failing", "warning")]
rc, out, err, tg, notes = run(sd5, with_notes(base5, n2), args=["--json"])
j = json.loads(out)
ok("N next day '3 warnings found' (only the number differs): already reported, not texted",
   j["alerts"] == [] and tg == [] and any(i.startswith("Not forwarded (already reported") for i in j["info"]),
   ([a["msg"] for a in j["alerts"]], j["info"]))
rc, out, err, tg, notes = run(sd5, with_notes(base5, n2), args=["--report"])
ok("N report shows the archived count", re.search(r"## Unraid notifications \(\d+ unread, 20 archived since boot\)", out),
   [l for l in out.splitlines() if "Unraid notifications" in l])
n3 = n2 + [("fcp_3.notify", 1791880000, FCP_EV, "Warning [HOMESRV] - plugin out of date", "dynamix", "warning")]
rc, out, err, tg, notes = run(sd5, with_notes(base5, n3), args=["--json"])
ok("N a different FCP warning is new again", rc == 1 and len(tg) == 1 and "plugin out of date" in tg[0], tg)

# ---- server unreachable: alert on the 2nd failure only, then the all-clear
sd6 = T / "e6"
broken = FIX.split("\n=====UH end")[0] + "\n"
r1 = run(sd6, broken)
r2 = run(sd6, broken)
sl = (T / "tg.log.env").read_text().splitlines()
r3 = run(sd6, broken, args=["--json"])
ok("F unreachable server: urgent (plain tg, no TG_SOFT)", sl[-1:] == ["soft="], sl[-3:])
ok("F 1st failure: rc 2, no text, no note", r1[0] == 2 and r1[3] == [] and r1[4] == [], r1[:4])
ok("F 2nd failure: one text 'can't reach the server (2 ...)', note with 'Owner texted: yes'",
   r2[0] == 2 and len(r2[3]) == 1 and "can't reach the server (2 checks in a row failed" in r2[3][0]
   and len(r2[4]) == 1 and "server unreachable (2 failed checks)" in r2[4][0] and "Owner texted: yes" in r2[4][0], r2[3:])
j3 = json.loads(r3[1])
ok("F 3rd failure: not texted again, json says failures 3 / alerted false", r3[3] == [] and r3[4] == []
   and j3 == {"ok": False, "error": j3["error"], "failures": 3, "alerted": False}, j3)
st = json.loads((sd6 / "state/state.json").read_text())
ok("F state keeps the counter and the alerted flag", st["ssh_failures"] == 3 and st["ssh_alerted"] is True, st)
r4 = run(sd6, FIX)
ok("F reachable again: no text, one 'server reachable again' note, counter reset",
   r4[0] == 0 and r4[3] == [] and len(r4[4]) == 1 and "server reachable again" in r4[4][0]
   and json.loads((sd6 / "state/state.json").read_text())["ssh_failures"] == 0, r4[3:])

# ---- corrupt state, crash handler
sd7 = T / "e7"
(sd7 / "state").mkdir(parents=True)
(sd7 / "state/state.json").write_text("{not json")
rc, out, err, tg, notes = run(sd7, FIX, args=["--json"])
j = json.loads(out)
ok("X corrupt state.json: moved aside, gap noted, run continues as a baseline",
   rc == 0 and j["first_run"] and any("state file was unreadable" in g for g in j["gaps"])
   and list((sd7 / "state").glob("state.json.corrupt-*")), j["gaps"])
sd8 = T / "e8"; sd8.mkdir()
(sd8 / "state").write_text("a file, not a dir")
ib8 = T / "crash-inbox"
p = subprocess.run([sys.executable, "-I", UH, "--state-dir", str(sd8 / "state"), "--input", str(T / "fixture.txt")],
                   capture_output=True, text=True, timeout=60, env=dict(os.environ, UNRAID_HEALTH_INBOX_DIR=str(ib8)))
cn = [f.read_text() for f in ib8.glob("unraid-health-*.md")] if ib8.exists() else []
ok("X crash: exit 3, traceback on stderr, one 'unraid-health crashed' inbox note",
   p.returncode == 3 and "Traceback" in p.stderr and len(cn) == 1 and "unraid-health crashed" in cn[0], (p.returncode, cn))
p = subprocess.run([sys.executable, "-I", UH, "--no-send", "--state-dir", str(sd8 / "state"), "--input",
                    str(T / "fixture.txt")], capture_output=True, text=True, timeout=60,
                   env=dict(os.environ, UNRAID_HEALTH_INBOX_DIR=str(ib8)))
ok("X crash with --no-send: exit 3, no further note", p.returncode == 3 and len(list(ib8.glob("*"))) == 1, p.returncode)

ok("safety: the real ssh was never needed (stub ssh only saw the root-mode test)",
   not SSH_LOG.exists() or SSH_LOG.read_text().count("\n") == 1, SSH_LOG.read_text() if SSH_LOG.exists() else "")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
