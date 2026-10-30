#!/usr/bin/env python3
"""Offline tests for ~/bin/unraid-health's Unraid-notification handling (dedupe, "returned to normal",
current temp below threshold, no re-alert of an already reported warning).
Uses a recorded collector output (fixtures/collect-2026-10-12.txt, 2026-10-12) with its notify_unread
section and disk6 temperature swapped per scenario; runs with --input/--state-dir/--no-send in a temp dir.
Never touches SSH, the real state or the owner's Telegram."""
import atexit, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

UH = os.environ.get("UH_BIN", "/home/node/bin/unraid-health")
HERE = Path(__file__).resolve().parent
BASE = (HERE / "fixtures/collect-2026-10-12.txt").read_text()
T = Path(tempfile.mkdtemp(prefix="uh-tests."))
atexit.register(shutil.rmtree, T, True)  # no leftovers in /tmp (self-cleanup 2026-10-13)
P = F = 0

def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1; print(f"ok   - {name}")
    else:
        F += 1; print(f"FAIL - {name}\n       got: {str(got)[:600]}")

HOT = ("Unraid Disk 6 temperature", "Warning [HOMESRV] - Disk 6 is hot (51  C)", "warning")
NORMAL = ("Unraid Disk 6 message", "Notice [HOMESRV] - Disk 6 returned to normal temperature", "normal")

def note(ts, ev):
    event, subj, imp = ev
    name = re.sub(r"\W", "_", event) + f"_{ts}.notify"
    return (f"@file={name}\ntimestamp={ts}\nevent=\"{event}\"\nsubject=\"{subj}\"\n"
            f"description=\"WDC_WD100EDAZ-11F3RA0_8HJZ0KAH (sda)\"\nimportance=\"{imp}\"\nlink=\"/Main\"\n\n")

def collect(notes, disk6_temp="*"):
    raw = BASE
    a = raw.index("=====UH notify_unread\n") + len("=====UH notify_unread\n")
    b = raw.index("=====UH ", a)
    if notes is not None:
        body = "".join(note(ts, ev) for ts, ev in sorted(notes, key=lambda x: -x[0]))
        raw = raw[:a] + body + raw[b:]
    raw = re.sub(r'(name="disk6"\n(?:[^\[\n][^\n]*\n)*?)temp="[^"]*"', lambda m: m.group(1) + f'temp="{disk6_temp}"', raw, count=1)
    return raw

def run(sd, raw, tag):
    f = T / f"{tag}.txt"; f.write_text(raw)
    env = dict(os.environ, UNRAID_HEALTH_NO_SEND="1", UNRAID_HEALTH_TG="/bin/false",
               UNRAID_HEALTH_HDDHEAT_LOG=str(T / "no-hddheat.log"), UNRAID_HEALTH_ACK_FILE=str(T / "no-ack.json"))
    r = subprocess.run([UH, "--json", "--no-send", "--input", str(f), "--state-dir", str(sd),
                        "--reports-dir", str(T / "reports"), "--inbox-dir", str(T / "inbox")],
                       capture_output=True, text=True, env=env, timeout=60)
    i = r.stdout.find("{")
    try:
        return json.loads(r.stdout[i:]), r
    except Exception:
        print(r.stdout[-1500:], r.stderr[-1500:]); raise

def disk6(res):
    return [a for a in res["alerts"] if "Disk 6" in a.get("title", "")]

def infos(res, s):
    return [x for x in res["info"] if s in x]

D0 = 1791600000  # 2026-10-10 ~04:40 Zurich
H = 3600
real = Path("/home/node/.local/state/unraid-health/state.json")
real_before = real.read_bytes() if real.exists() else None

# --- scenario A: dedupe, no re-alert, resolve by "returned to normal", re-occurrence
sd = T / "stA"
run(sd, collect([]), "a0")                                       # baseline
res, _ = run(sd, collect([(D0, HOT), (D0 + 12 * H, HOT)]), "a1")  # two identical warnings, no normal
ok("A1 two identical new warnings -> one alert", len(disk6(res)) == 1, disk6(res))
ok("A1 second one noted as already reported", len(infos(res, "already reported")) == 1, res["info"])
res, _ = run(sd, collect([(D0, HOT), (D0 + 12 * H, HOT), (D0 + 24 * H, HOT)]), "a2")
ok("A2 same warning again next day, no normal in between -> not new", disk6(res) == [], disk6(res))
res, _ = run(sd, collect([(D0 + 24 * H, HOT), (D0 + 25 * H, NORMAL), (D0 + 30 * H, HOT), (D0 + 31 * H, NORMAL)]), "a3")
ok("A3 warning followed by 'returned to normal' -> not alerted", disk6(res) == [], disk6(res))
ok("A3 noted as resolved by Unraid", len(infos(res, "Resolved by Unraid")) == 1, res["info"])
res, _ = run(sd, collect([(D0 + 31 * H, NORMAL), (D0 + 40 * H, HOT)]), "a4")
ok("A4 re-occurs after a normal message, still hot -> alerted again", len(disk6(res)) == 1, res["info"])
res, _ = run(sd, collect([(D0 + 50 * H, HOT)], disk6_temp="37"), "a5")
ok("A5 new warning but disk now 37C (< warn 58) -> not alerted", disk6(res) == [], disk6(res))
ok("A5 noted as below warn", len(infos(res, "below warn 58")) == 1, res["info"])
res, _ = run(sd, collect([(D0 + 60 * H, HOT)], disk6_temp="59"), "a6")  # HDD warn floor 58 (hddheat rule)
ok("A6 new warning, disk now 59C -> our own temp alert, no duplicate Unraid line",
   len([a for a in res["alerts"] if a.get("cat") == "temp" and a.get("subject") == "disk6"]) == 1
   and disk6(res) == [], res["alerts"])
ok("A6 no alert listed twice", len({(a["level"], a["msg"]) for a in res["alerts"]}) == len(res["alerts"]),
   [a["msg"] for a in res["alerts"]])

# --- scenario B: replay of 2026-10-12 (real notify_unread) after a 2026-10-11 run
sd = T / "stB"
fx_unread = re.findall(r"^@file=(.+)$", BASE, re.M)
run(sd, collect([]), "b0")
st = json.loads((sd / "state.json").read_text())
st["notifications_seen"] = [f for f in fx_unread if f not in ("Unraid_Disk_6_temperature_1791752461.notify",
                                                               "Unraid_Disk_6_message_1791754321.notify")]
(sd / "state.json").write_text(json.dumps(st))
res, _ = run(sd, collect(None), "b1")
ok("B 2026-10-12 replay: 'Disk 6 is hot' with a later normal -> not texted", disk6(res) == [], disk6(res))
rep = sorted((T / "reports").glob("*.md"))[-1].read_text()
ok("B report marks it new, resolved", "Disk 6 is hot (51  C) (new, resolved)" in rep, rep[:300])

ok("real state.json untouched", (real.read_bytes() if real.exists() else None) == real_before)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
