#!/usr/bin/env python3
"""Offline tests for ~/bin/hwtrend: baseline, stable non-zero counters stay silent, increases alert once,
resettable counters, disk swap, stale SMART cache, temperature streaks, parity-check errors.
Uses a recorded collector output (fixtures/collect-2026-10-12.txt) mutated per scenario; runs with
--input/--state-dir/--inbox-dir in a temp dir."""

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HW = os.environ.get("HWTREND_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "hwtrend"
)  # HWTREND_BIN=<copy> tests a copy
HERE = Path(__file__).resolve().parent
BASE = (HERE / "fixtures/collect-2026-10-12.txt").read_text()
P = F = 0


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1
        print(f"ok   - {name}")
    else:
        F += 1
        print(f"FAIL - {name}\n       got: {str(got)[:700]}")


def sections_map(raw, pred, fn):
    """Apply fn(text) to the body of every '=====UH name args' section where pred(header) is true."""
    parts = re.split(r"(^=====UH [^\n]*\n)", raw, flags=re.M)
    for i in range(1, len(parts), 2):
        if pred(parts[i][8:].strip()):
            parts[i + 1] = fn(parts[i + 1])
    return "".join(parts)


def smart_attr(raw, slot, aid, value, which=("smartctl", "smart_cache")):
    return sections_map(
        raw,
        lambda h: h.split()[0] in which and h.split()[1] == slot,
        lambda t: re.sub(rf"^(\s*{aid} \S+\s.*?-\s+)\d+(.*)$", rf"\g<1>{value}\g<2>", t, flags=re.M),
    )


def btrfs(raw, dev, counter, value):
    return sections_map(
        raw,
        lambda h: h.startswith("btrfs_stats"),
        lambda t: re.sub(rf"^(\[{re.escape(dev)}\]\.{counter}\s+)\d+", rf"\g<1>{value}", t, flags=re.M),
    )


def disk_field(raw, slot, field, value):
    return sections_map(
        raw,
        lambda h: h in ("disks", "devs"),
        lambda t: re.sub(
            rf'(name="{slot}"\n(?:[^\[\n][^\n]*\n)*?){field}="[^"]*"',
            lambda m: m.group(1) + f'{field}="{value}"',
            t,
            count=1,
        ),
    )


def cpu(raw, temp, cores=None):
    """Set the package sensor; cores=N sets every core to N (default: leave the cores as recorded)."""

    def f(t):
        j = json.loads(t)
        c = j["coretemp-isa-0000"]
        c["CPU Temp"]["temp1_input"] = temp
        if cores is not None:
            for label, vals in c.items():
                if label.startswith("Core"):
                    for k in vals:
                        if k.endswith("_input"):
                            vals[k] = cores
        return json.dumps(j, indent=1) + "\n"

    return sections_map(raw, lambda h: h == "sensors", f)


def var(raw, key, value):
    return sections_map(raw, lambda h: h == "var", lambda t: re.sub(rf"^{key}=.*$", f'{key}="{value}"', t, flags=re.M))


class Run:
    def __init__(self):
        self.d = Path(tempfile.mkdtemp(prefix="hwtrend-tests."))
        atexit.register(shutil.rmtree, self.d, True)  # no leftovers in /tmp (self-cleanup 2026-10-13)
        self.n = 0

    def __call__(self, raw, date, extra=()):
        self.n += 1
        f = self.d / f"in-{self.n}.txt"
        f.write_text(raw)
        inbox = self.d / "inbox"
        before = set(inbox.glob("*.md")) if inbox.exists() else set()
        p = subprocess.run(
            [
                HW,
                "--input",
                str(f),
                "--state-dir",
                str(self.d / "state"),
                "--inbox-dir",
                str(inbox),
                "--date",
                date,
                *extra,
            ],
            capture_output=True,
            text=True,
        )
        notes = [x.read_text() for x in (set(inbox.glob("*.md")) if inbox.exists() else set()) - before]
        return p.returncode, p.stdout + p.stderr, notes


# CPU is 86 °C in the fixture (above 80): keep it cool unless a test is about it
COOL = cpu(BASE, 55.0, cores=55.0)

# 1. baseline is silent, stable non-zero counters (disk4 CRC 44, docker.img 2829) never alert
r = Run()
rc, out, notes = r(COOL, "2026-10-12")
ok("baseline: rc 0, no note", rc == 0 and not notes, out)
ok(
    "baseline lists known non-zero counters",
    "disk4" in out and "UDMA CRC errors (199, cable) 44" in out and "btrfs write 2829, read 1, corruption 2" in out,
    out,
)
rc, out, notes = r(COOL, "2026-10-13")
ok("day 2 unchanged: silent", rc == 0 and not notes, out)
ok(
    "sample file per day",
    sorted(p.name for p in (r.d / "state").glob("*.json") if p.name != "state.json")
    == ["2026-10-12.json", "2026-10-13.json"],
)

# 2. a SMART counter increase alerts once; a rerun / next day with the same value is silent
raw = smart_attr(COOL, "disk4", 199, 46)
rc, out, notes = r(raw, "2026-10-14")
ok(
    "CRC 44→46 alerts",
    rc == 1 and len(notes) == 1 and "disk4: UDMA CRC errors (199, cable): 44 → 46 (+2)" in notes[0],
    out,
)
rc, out, notes = r(raw, "2026-10-14")
ok("same-day rerun: no re-alert", rc == 0 and not notes, out)
rc, out, notes = r(raw, "2026-10-15")
ok("stable at 46: silent", rc == 0 and not notes, out)

# 3. stale Unraid SMART cache with a lower value, then back: silent (high-water mark)
rc, out, notes = r(smart_attr(raw, "disk4", 199, 44), "2026-10-16")
ok("lower (stale cache) value: silent", rc == 0 and not notes, out)
rc, out, notes = r(raw, "2026-10-17")
ok("back to 46: silent", rc == 0 and not notes, out)

# 4. new pending sector on a spun-down disk (from Unraid's cache)
rc, out, notes = r(smart_attr(raw, "disk6", 197, 1), "2026-10-18")
ok("pending 0→1 on disk6 alerts", rc == 1 and notes and "disk6: pending sectors (197): 0 → 1" in notes[0], out)

# 5. docker.img btrfs write errors go up
raw2 = smart_attr(smart_attr(raw, "disk6", 197, 1), "disk4", 199, 46)
rc, out, notes = r(btrfs(raw2, "/dev/loop2", "write_io_errs", 2900), "2026-10-19")
ok(
    "docker.img write 2829→2900 alerts once (twin mount deduped)",
    rc == 1 and len(notes) == 1 and notes[0].count("btrfs write errors: 2829 → 2900") == 1,
    notes or out,
)
raw2 = btrfs(raw2, "/dev/loop2", "write_io_errs", 2900)
# btrfs counter reset (docker.img recreated): drop to 0 silent, then 3 alerts
rc, out, notes = r(btrfs(raw2, "/dev/loop2", "write_io_errs", 0), "2026-10-20")
ok("btrfs reset to 0: silent", rc == 0 and not notes, out)
rc, out, notes = r(btrfs(raw2, "/dev/loop2", "write_io_errs", 3), "2026-10-21")
ok(
    "btrfs after reset 0→3 alerts",
    rc == 1 and notes and "2829" not in notes[0].split("Known")[0] and "0 → 3" in notes[0],
    notes or out,
)
raw2 = btrfs(raw2, "/dev/loop2", "write_io_errs", 3)

# 6. Unraid md read/write errors (reset at array start)
rc, out, notes = r(disk_field(raw2, "disk3", "numErrors", 5), "2026-10-22")
ok(
    "md errors 0→5 alerts",
    rc == 1 and notes and "disk3: Unraid read/write errors since array start: 0 → 5" in notes[0],
    out,
)
rc, out, notes = r(raw2, "2026-10-23")
ok("md errors back to 0 (array restart): silent", rc == 0 and not notes, out)

# 7. disk swap: a different id in the disk4 slot with its own non-zero CRC is new known state, not an increase
rc, out, notes = r(disk_field(raw2, "disk4", "id", "NEW_DISK_SERIAL"), "2026-10-24")
ok("swapped disk: silent", rc == 0 and not notes, out)

# 8. temperatures: one hot day silent, 2 in a row alert once, re-armed after cooling
r2 = Run()
hot = disk_field(COOL, "cache", "temp", 63)  # SSD hot threshold from Unraid = 60
r2(COOL, "2026-10-12")
rc, out, notes = r2(hot, "2026-10-13")
ok("1 hot sample: silent", rc == 0 and not notes, out)
rc, out, notes = r2(hot, "2026-10-13")
ok("same-day rerun doesn't count as a 2nd sample", rc == 0 and not notes, out)
rc, out, notes = r2(hot, "2026-10-14")
ok("2 hot days: alert", rc == 1 and notes and "cache: 63°C, above 60°C for 2 daily samples" in notes[0], out)
rc, out, notes = r2(hot, "2026-10-15")
ok("3rd hot day: no repeat", rc == 0 and not notes, out)
r2(COOL, "2026-10-16")
r2(hot, "2026-10-17")
rc, out, notes = r2(hot, "2026-10-18")
ok("new hot streak after cooling alerts again", rc == 1 and notes and "cache: 63°C" in notes[0], out)
r3 = Run()
r3(BASE, "2026-10-12")  # fixture: hottest core (Core 28) 86 °C, normal for an i9-13900K
rc, out, notes = r3(BASE, "2026-10-13")
ok("hottest core 86 °C 2 days: silent (threshold 95)", rc == 0 and not notes, out)
r4 = Run()
r4(cpu(BASE, 97.0, cores=96.0), "2026-10-12")
rc, out, notes = r4(cpu(BASE, 97.0, cores=96.0), "2026-10-13")
ok("hottest core 96 °C 2 days: alert", rc == 1 and notes and "CPU hottest core: 96°C, above 95°C" in notes[0], out)
rc, out, notes = r3(disk_field(BASE, "cache", "spundown", 1), "2026-10-14")
ok("spun-down reading never counts", rc == 0 and not notes, out)
r5 = Run()
pk = cpu(BASE, 86.0, cores=55.0)  # package 86 °C, all cores 55 °C
r5(pk, "2026-10-12")
rc, out, notes = r5(pk, "2026-10-13")
ok("package 86 / cores 55 for 2 days: silent", rc == 0 and not notes, out)
rc, out, notes = r5(cpu(BASE, 91.0, cores=55.0), "2026-10-14")
ok("package +5 (86→91): silent", rc == 0 and not notes, out)
rc, out, notes = r5(cpu(BASE, 97.0, cores=55.0), "2026-10-15")
ok(
    "package > +5 over its high-water (91→97): alert",
    rc == 1
    and notes
    and "CPU package sensor: 97°C, more than 5°C above its highest earlier reading (91°C)" in notes[0],
    out,
)
rc, out, notes = r5(cpu(BASE, 97.0, cores=55.0), "2026-10-16")
ok("package stable at 97: silent", rc == 0 and not notes, out)

# 9. parity check finished with errors
r4 = Run()
r4(COOL, "2026-10-12")
rc, out, notes = r4(var(var(COOL, "sbSyncErrs", 7), "sbSynced2", 1790000000), "2026-10-13")
ok(
    "parity check with errors alerts",
    rc == 1 and notes and "parity check ended" in notes[0] and "7 errors" in notes[0],
    out,
)
rc, out, notes = r4(var(var(COOL, "sbSyncErrs", 7), "sbSynced2", 1790000000), "2026-10-14")
ok("same check: no repeat", rc == 0 and not notes, out)

# 10. show / history / dry-run
p = subprocess.run([HW, "history", "disk4.199", "--state-dir", str(r.d / "state")], capture_output=True, text=True)
ok("history disk4.199", "2026-10-12  44" in p.stdout and "2026-10-14  46" in p.stdout, p.stdout + p.stderr)
p = subprocess.run([HW, "show", "--state-dir", str(r.d / "state")], capture_output=True, text=True)
ok(
    "show prints latest sample",
    p.returncode == 0 and "Hardware trend sample 2026-10-24" in p.stdout,
    p.stdout + p.stderr,
)
n_before = len(list((r.d / "state").glob("*.json")))
rc, out, notes = r(smart_attr(raw2, "disk1", 5, 8), "2026-10-25", ["--dry-run"])
ok(
    "dry-run: prints alert, writes nothing",
    rc == 1
    and "reallocated sectors (5): 0 → 8" in out
    and not notes
    and len(list((r.d / "state").glob("*.json"))) == n_before,
    out,
)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
