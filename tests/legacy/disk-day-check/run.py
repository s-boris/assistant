#!/usr/bin/env python3
"""Tests for ~/bin/disk-day-check. Exit 0 = all pass.

Server-independent: synthetic `health` / `capacity` verb outputs (placeholder serials and host),
served by a fake unraid-ssh through the DISK_DAY_CHECK_SSH env seam; HOME is a fresh temp dir per
case (state, config). DDC_SCRIPT=/path tests another copy (default ~/bin/disk-day-check).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

SRC = os.environ.get("DDC_SCRIPT", "/home/node/bin/disk-day-check")
SUF = "TEST1234"
NOW = 1800000000
TB10_KB = 9766436812  # 10 TB disk, 1K blocks
FS10_KB = 9765891496  # its xfs size
fails = passes = 0


def disk(
    name,
    idx,
    dev,
    serial,
    size_kb,
    fs_kb,
    free_kb,
    status="DISK_OK",
    temp="*",
    fs="xfs",
    fsstatus="Mounted",
    errors=0,
    rot="1",
    typ="Data",
):
    return "\n".join(
        [
            f'["{name}"]',
            f'idx="{idx}"',
            f'name="{name}"',
            f'device="{dev}"',
            f'id="MODEL-X_{serial}"',
            f'size="{size_kb}"',
            f'rotational="{rot}"',
            f'status="{status}"',
            f'temp="{temp}"',
            f'numErrors="{errors}"',
            f'type="{typ}"',
            f'idSb="MODEL-X_{serial}"',
            f'fsType="{fs}"',
            f'fsStatus="{fsstatus}"',
            f'fsMountpoint="/mnt/{name}"' if fsstatus == "Mounted" else 'fsMountpoint=""',
            f'fsSize="{fs_kb}"',
            f'fsFree="{free_kb}"',
            "",
        ]
    )


def smart(realloc=0, pending=0, unc=0, crc=5, health="PASSED"):
    return "\n".join(
        [
            "SMART overall-health self-assessment test result: " + health,
            f"  5 Reallocated_Sector_Ct   0x0033   100   100   005    Pre-fail  Always       -       {realloc}",
            "194 Temperature_Celsius     0x0002   132   132   000    Old_age   Always       -       40 (Min/Max 19/60)",
            f"197 Current_Pending_Sector  0x0022   100   100   000    Old_age   Always       -       {pending}",
            f"198 Offline_Uncorrectable   0x0008   100   100   000    Old_age   Offline      -       {unc}",
            f"199 UDMA_CRC_Error_Count    0x000a   200   200   000    Old_age   Always       -       {crc}",
            "",
        ]
    )


def health(
    where="array",
    slot="disk3",
    md="STARTED",
    resync=0,
    pos=0,
    action="check P Q",
    new_status="DISK_OK",
    new_fsstatus="Mounted",
    new_fs="xfs",
    new_temp="40",
    new_size=TB10_KB,
    new_fs_kb=FS10_KB,
    other_status="DISK_OK",
    other_fsstatus="Mounted",
    other_errors=0,
    sb_errs=0,
    unm=0,
    smart_kw=None,
    notices=(),
    preclear=True,
    nosmart=False,
):
    v = [
        f'mdState="{md}"',
        'fsState="Started"' if md == "STARTED" else 'fsState="Stopped"',
        f'mdNumDisabled="{1 if other_status == "DISK_DSBL" else 0}"',
        'mdNumInvalid="0"',
        'mdNumMissing="0"',
        f'mdResync="{resync}"',
        f'mdResyncPos="{pos}"',
        'mdResyncSize="15625879500"',
        f'mdResyncAction="{action}"',
        f'sbSyncErrs="{sb_errs}"',
        'sbSyncExit="0"',
        f'fsNumUnmountable="{unm}"',
        'fsUnmountableMask=""',
    ]
    d = [
        disk("parity", 0, "sda", "PAR1", 15625879500, 0, 0, fsstatus=""),
        disk(
            "disk1",
            1,
            "sdb",
            "DATA1",
            13672382412,
            13670295500,
            1000000000,
            status=other_status,
            fsstatus=other_fsstatus,
            errors=other_errors,
            temp="39",
        ),
        disk("disk2", 2, "sdc", "DATA2", 3907018532, 3905945600, 500000000),
        disk("parity2", 29, "sdd", "PAR2", 15625879500, 0, 0, fsstatus=""),
        disk("cache", 30, "nvme0n1", "NVME1", 976762552, 976562500, 900000000, rot="0", temp="58", typ="Cache"),
    ]
    devs = ""
    serial_dev = "sdx"
    if where == "array":
        d.insert(
            3,
            disk(
                slot,
                slot[4:] if slot.startswith("disk") else 31,
                serial_dev,
                "AAAA" + SUF,
                new_size,
                new_fs_kb if new_fsstatus == "Mounted" else 0,
                new_fs_kb - 100000 if new_fsstatus == "Mounted" else 0,
                status=new_status,
                fsstatus=new_fsstatus,
                fs=new_fs,
                temp=new_temp,
            ),
        )
        sslot = slot
    elif where == "devs":
        devs = "\n".join(
            [
                '["dev1"]',
                'name="dev1"',
                f'id="MODEL-X_AAAA{SUF}"',
                f'device="{serial_dev}"',
                'sectors="19532873728"',
                'sector_size="512"',
                'rotational="1"',
                f'temp="{new_temp}"',
                "",
            ]
        )
        sslot = "dev1"
    else:
        sslot = None
    out = [
        "",
        "=====UH meta",
        f"now={NOW}",
        "host=TESTHOST",
        "preclear=" + (f"/dev/{serial_dev} " if preclear and where == "devs" else ""),
        "",
        "=====UH var",
        *v,
        "",
        "=====UH disks",
        *d,
        "=====UH devs",
        devs,
        "=====UH display",
        'hot="50"',
        'max="55"',
        'hotssd="60"',
        'maxssd="70"',
        "",
        "=====UH parity_log",
        "2026 Jan  1 00:00:00|100|100|0|0|check P|15625879500",
        "",
    ]
    if sslot and not nosmart:
        out += [f"=====UH smartctl {sslot} {serial_dev}", smart(**(smart_kw or {})), "=====UH_RC 0", ""]
    out += ["=====UH notify_unread"]
    for i, (t, imp, subj) in enumerate(notices):
        out += [
            f"@file=n{i}.notify",
            f"timestamp={t}",
            'event="x"',
            f'subject="Warning [TESTHOST] - {subj}"',
            'description="x"',
            f'importance="{imp}"',
            "",
        ]
    out += ["=====UH syslog_errors", "", "=====UH end", ""]
    return "\n".join(out)


def capacity(slot="disk3", added=True, exclude="", media_free=None):
    mounts = ["/dev/md1p1 /mnt/disk1 xfs", "/dev/md2p1 /mnt/disk2 xfs"]
    if added:
        mounts.append(f"/dev/md{slot[4:]}p1 /mnt/{slot} xfs")
    if media_free is None:
        media_free = 1500000000 + (FS10_KB if added else 0)
    return "\n".join(
        [
            "",
            "=====UC meta",
            f"now={NOW}",
            "",
            "=====UC shares",
            '["media"]',
            'name="media"',
            'include=""',
            f'exclude="{exclude}"',
            'useCache="yes"',
            f'free="{media_free}"',
            "",
            '["appdata"]',
            'name="appdata"',
            'include=""',
            'exclude=""',
            'useCache="only"',
            "",
            "=====UC mounts",
            *mounts,
            "",
            "=====UC end",
            "",
        ]
    )


FAKE = """#!/bin/sh
[ "$1" = run ] || exit 9
f="$DDC_FIX/$2.out"
[ -f "$f" ] || { echo "fake: no $2" >&2; exit 1; }
cat "$f"
"""


class Case:
    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="ddc-test.")
        self.fix = os.path.join(self.home, "fix")
        os.makedirs(self.fix)
        self.fake = os.path.join(self.home, "fake-ssh")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        os.chmod(self.fake, 0o755)
        os.makedirs(os.path.join(self.home, ".config/preclear-watch"))
        with open(os.path.join(self.home, ".config/preclear-watch/serial"), "w") as f:
            f.write(SUF + "\n")

    def set(self, h=None, c=None):
        for name, txt in (("health", h), ("capacity", c)):
            p = os.path.join(self.fix, name + ".out")
            if txt is None:
                if os.path.exists(p):
                    os.unlink(p)
            else:
                with open(p, "w") as f:
                    f.write(txt)

    def run(self, *args):
        env = dict(os.environ, HOME=self.home, DISK_DAY_CHECK_SSH=self.fake, DDC_FIX=self.fix)
        r = subprocess.run([SRC, *args], env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr

    def state(self):
        p = os.path.join(self.home, ".local/state/disk-day-check", SUF + ".json")
        return json.load(open(p)) if os.path.exists(p) else None

    def close(self):
        shutil.rmtree(self.home, ignore_errors=True)


def ok(name, cond, detail=""):
    global fails, passes
    if cond:
        passes += 1
        print("ok   - " + name)
    else:
        fails += 1
        print("FAIL - " + name + ("\n       " + detail.replace("\n", "\n       ") if detail else ""))


def case(name, h, c, *args, rc=None, has=(), lacks=(), state=None):
    k = Case()
    try:
        if state is not None:
            os.makedirs(os.path.join(k.home, ".local/state/disk-day-check"))
            with open(os.path.join(k.home, ".local/state/disk-day-check", SUF + ".json"), "w") as f:
                json.dump(state, f)
        k.set(h, c)
        r, out, err = k.run(*args)
        good = (rc is None or r == rc) and all(s in out for s in has) and not any(s in out for s in lacks)
        ok(name, good, f"rc={r}\n{out}{err}")
        return k, r, out
    finally:
        k.close()


BEFORE = {
    "last_unassigned_t": NOW - 3600,
    "crc_baseline": 5,
    "before": {"array_size_kb": 13670295500 + 3905945600, "dev_size_bytes": 19532873728 * 512},
}
FAR_PAST = ["--since", str(NOW - 3600)]

# 1. before the add: clean "not added yet", baseline saved
k = Case()
k.set(health(where="devs", new_temp="49"), capacity(added=False))
r, out, err = k.run()
ok("not added: rc 0", r == 0, out + err)
ok("not added: headline", out.startswith("⏸ Disk …" + SUF + " not added yet"), out)
ok("not added: preclear running", "preclear running" in out, out)
st = k.state() or {}
ok(
    "not added: baseline saved",
    st.get("before", {}).get("dev_size_bytes") == 19532873728 * 512
    and st.get("crc_baseline") == 5
    and st.get("last_unassigned_t"),
    json.dumps(st),
)
# 2. same HOME, now added: uses the saved baseline, all fine
k.set(health(), capacity())
r, out, err = k.run("--since", str(NOW - 60))
ok("added fine: rc 0 + verdict", r == 0 and out.startswith("✅ Disk 3 in, all fine"), out + err)
ok("added fine: summary line", "xfs, 10.0 TB" in out and "SMART clean, CRC 5 (baseline 5)" in out, out)
r, out, err = k.run("--json", "--since", str(NOW - 60))
try:
    j = json.loads(out)
except ValueError:
    j = {}
ok(
    "json: state ok, slot, growth",
    j.get("state") == "ok" and j.get("slot") == "disk3" and j.get("capacity", {}).get("array_growth_kb") == FS10_KB,
    out[:400],
)
ok("state: added_seen_t recorded", "added_seen_t" in (k.state() or {}))
k.close()

case(
    "preclear done (not running)",
    health(where="devs", preclear=False),
    capacity(added=False),
    rc=0,
    has=("not added yet", "done / stopped"),
)
case(
    "not added + array problem still listed",
    health(where="devs", other_status="DISK_DSBL"),
    capacity(added=False),
    rc=0,
    has=("not added yet", "Disk 1: disabled (emulated)"),
)
case("disk missing", health(where="none"), capacity(added=False), rc=1, has=("❌ Disk …" + SUF + " not found",))
case("health verb fails", None, None, rc=3)
case(
    "capacity verb fails: still checks", health(), None, *FAR_PAST, rc=0, has=("✅ Disk 3 in, all fine",), state=BEFORE
)
case(
    "clearing in progress",
    health(resync=1, pos=6250351800, action="clear", new_fsstatus="Unmountable", unm=1),
    capacity(added=False),
    *FAR_PAST,
    rc=0,
    has=("⏳ Disk 3: clearing Disk 3: 40 %",),
    lacks=("Format",),
)
case(
    "format pending",
    health(new_fsstatus="Unmountable: Unsupported or no file system", unm=1),
    capacity(added=False),
    *FAR_PAST,
    rc=0,
    has=("⏳ Disk 3:", "waits for Format"),
)
case(
    "another disk unmountable: do not format",
    health(new_fsstatus="Unmountable", unm=2, other_fsstatus="Unmountable"),
    capacity(added=False),
    *FAR_PAST,
    rc=1,
    has=("DON'T format", "Disk 1"),
)
case(
    "other disk disabled/emulated",
    health(other_status="DISK_DSBL"),
    capacity(),
    *FAR_PAST,
    rc=1,
    has=("⚠️ Disk 3:", "Disk 1: disabled (emulated)", "1 disk(s) disabled"),
)
case(
    "read errors on a disk", health(other_errors=12), capacity(), *FAR_PAST, rc=1, has=("Disk 1: 12 read/write errors",)
)
case("array stopped", health(md="STOPPED"), capacity(), *FAR_PAST, rc=1, has=("Array is stopped",))
case(
    "CRC grew vs baseline",
    health(smart_kw={"crc": 7}),
    capacity(),
    *FAR_PAST,
    rc=1,
    has=("CRC errors 5 → 7 (cable)",),
    state=BEFORE,
)
case(
    "CRC baseline from preclear-watch state absent → flag",
    health(smart_kw={"crc": 7}),
    capacity(),
    "--crc-baseline",
    "7",
    *FAR_PAST,
    rc=0,
    has=("✅",),
)
case("pending sectors", health(smart_kw={"pending": 8}), capacity(), *FAR_PAST, rc=1, has=("8 pending sectors",))
case(
    "SMART failed", health(smart_kw={"health": "FAILED!"}), capacity(), *FAR_PAST, rc=1, has=("SMART health: FAILED!",)
)
case(
    "no SMART section: watch item",
    health(nosmart=True),
    capacity(),
    *FAR_PAST,
    rc=0,
    has=("✅", "SMART of the new disk not in this read"),
)
case(
    "new disk hot: watch only",
    health(new_temp="52"),
    capacity(),
    *FAR_PAST,
    rc=0,
    has=("✅", "Watch: Disk 3 52 °C (hot 50)"),
)
case("new disk over max", health(new_temp="56"), capacity(), *FAR_PAST, rc=1, has=("Disk 3 56 °C (max 55)",))
case("SSD uses SSD limits (58 °C fine)", health(), capacity(), *FAR_PAST, rc=0, lacks=("cache", "Cache"))
case(
    "parity sync running after the add",
    health(resync=1, pos=1562587950, action="recon P Q"),
    capacity(),
    *FAR_PAST,
    rc=0,
    has=("Parity sync / rebuild running (recon P Q): 10 %",),
)
case("last parity check had errors", health(sb_errs=3), capacity(), *FAR_PAST, has=("3 errors",))
case("wrong file system", health(new_fs="btrfs"), capacity(), *FAR_PAST, rc=1, has=("file system btrfs, expected xfs",))
case("--fs btrfs accepts btrfs", health(new_fs="btrfs"), capacity(), "--fs", "btrfs", *FAR_PAST, rc=0, has=("✅",))
case(
    "wrong slot vs --slot",
    health(),
    capacity(),
    "--slot",
    "disk12",
    *FAR_PAST,
    rc=1,
    has=("New disk is Disk 3, expected Disk 12",),
)
case(
    "size ≠ precleared disk",
    health(new_size=TB10_KB // 2, new_fs_kb=FS10_KB // 2),
    capacity(),
    *FAR_PAST,
    rc=1,
    has=("≠ the precleared disk",),
    state=BEFORE,
)
case("file system too small", health(new_fs_kb=FS10_KB // 2), capacity(), *FAR_PAST, rc=1, has=("too small",))
case(
    "array total did not grow",
    health(),
    capacity(),
    *FAR_PAST,
    rc=0,
    has=("Array total grew only",),
    state={"before": {"array_size_kb": 13670295500 + 3905945600 + FS10_KB}},
)
case(
    "media share free misses the new disk",
    health(),
    capacity(media_free=1000),
    *FAR_PAST,
    rc=0,
    has=("media share free", "doesn't include Disk 3"),
)
case(
    "share excludes the new disk",
    health(),
    capacity(exclude="disk3"),
    *FAR_PAST,
    rc=0,
    has=("Share media excludes Disk 3",),
)
case("not in mount table", health(), capacity(added=False), *FAR_PAST, rc=0, has=("/mnt/disk3 not in the mount table",))
notes = [
    (NOW - 7200, "alert", "Disk 9 overheated"),  # before --since: ignored
    (NOW - 600, "warning", "Disk 3 returned to normal temperature"),  # normal-again: ignored
    (NOW - 300, "alert", "Disk 1 in error state (disk dsbl)"),
    (NOW - 200, "warning", "Parity check finished (2 errors)"),
    (NOW - 100, "normal", "Array health report [PASS]"),
]
k, r, out = case(
    "notifications since the add",
    health(notices=notes),
    capacity(),
    "--since",
    str(NOW - 3600),
    rc=1,
    has=("Notice: Disk 1 in error state", "Watch: Notice: Parity check finished"),
    lacks=("Disk 9", "returned to normal", "health report", "TESTHOST"),
)
case("notifications: --since 1h form", health(notices=notes), capacity(), "--since", "1h", rc=None)
case(
    "notification before the add: ignored via saved state",
    health(notices=notes[:1]),
    capacity(),
    rc=0,
    has=("✅",),
    state=dict(BEFORE, last_unassigned_t=NOW - 3600),
)
case("usage: bad serial", health(), capacity(), "--serial", "a-b", rc=2)
case("usage: bad slot", health(), capacity(), "--slot", "cache", rc=2)
case("usage: bad since", health(), capacity(), "--since", "yesterday", rc=2)
case(
    "--serial overrides config (other disk → missing)",
    health(),
    capacity(),
    "--serial",
    "ZZZZ",
    rc=1,
    has=("not found",),
)
case("--no-state writes nothing", health(where="devs"), capacity(added=False), "--no-state", rc=0)

# --json for not added carries the unassigned block
k = Case()
k.set(health(where="devs"), capacity(added=False))
r, out, err = k.run("--json", "--no-state")
try:
    j = json.loads(out)
except ValueError:
    j = {}
ok(
    "json not added",
    j.get("state") == "not_added" and j.get("unassigned", {}).get("dev") == "dev1" and k.state() is None,
    out[:300],
)
k.close()

print(f"\n{passes} passed, {fails} failed")
sys.exit(1 if fails else 0)
