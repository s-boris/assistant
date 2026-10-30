#!/usr/bin/env bash
# Offline tests for ~/bin/hddheat (permanent HDD heat alert). Exit 0 = all pass.
# Never touches the server or Telegram: `ssh` and ~/bin/unraid-ssh are PATH/env shims that replay a fixture written
# by fx(), `tg` is a logging stub, state + inbox are temp dirs, and the clock is HDDHEAT_NOW (Europe/Zurich times).
# The real ~/.local/state/hddheat is checked untouched without racing the live cron: every hddheat run goes through
# writeaudit.py (Python audit hooks, PEP 578), which logs each write-side file op and each program it starts; the
# suite fails on any write outside its temp dir (live state, ~/inbox, anything) or any program not one of its shims.
# A before/after listing of the live dir backs that up: names the cron never writes must stay identical; the cron's
# own files (state.json, runs.log, cron.log, .lock, crash.stamp, jsonstate temp files) may change only when the live
# runs.log shows a cron run inside the test window. HDDHEAT_BIN=<path> tests another copy (it needs ~/bin/lib
# next to it, e.g. a symlink).
set -uo pipefail
HH=${HDDHEAT_BIN:-/home/node/bin/hddheat}
REAL=/home/node/.local/state/hddheat
T=$(mktemp -d "${TMPDIR:-/tmp}/hddheat-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/shims" "$T/state" "$T/inbox"
WA=$(dirname "$(realpath "$0")")/writeaudit.py AUD=$T/audit.log
# CRONF = what the live cron itself writes in $REAL (state.json and runs.log via jsonstate temp files, .lock, crash.stamp;
# cron.log from the cron line's redirect)
CRONF='^(state\.json|runs\.log|cron\.log|\.lock|crash\.stamp|\.(state\.json|runs\.log|crash\.stamp)\..*\.tmp)$'
real_other() { (cd "$REAL" 2>/dev/null && find . -mindepth 1 -maxdepth 1 -printf '%P\n' | grep -Ev "$CRONF" | sort |
                while IFS= read -r f; do stat -c '%n %s %Y %a' -- "$f"; [ -f "$f" ] && md5sum -- "$f"; done); }
other_before=$(real_other)
# hh ARGS: run hddheat under the write audit (same env; exit code and output as without it)
hh() { echo x >> "$T/hh.count"; python3 "$WA" "$AUD" "$HH" "$@"; }
export FIX=$T/fix.txt LOGD=$T
cat > "$T/shims/ssh" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$LOGD/ssh.log"
cat > "$LOGD/ssh.stdin"
if [ -e "$LOGD/ssh-fail" ]; then echo "ssh: connect to host fakehost port 22: Connection refused" >&2; exit 255; fi
if [ -e "$LOGD/ssh-cut" ]; then grep -v '^=====HH end' "$FIX"; exit 0; fi
cat "$FIX"
EOF
cat > "$T/shims/unraid-ssh" <<'EOF'
#!/bin/sh
if [ "$1" = mode ]; then cat "$LOGD/ssh-mode" 2>/dev/null || echo root; exit 0; fi
printf '%s\n' "$*" >> "$LOGD/unraid-ssh.log"
case " $* " in *" run heat "*) cat "$FIX" ;; *) sed 's/^=====HH /=====UC /' "$FIX" ;; esac   # heat = this collector
EOF
cat > "$T/shims/tg" <<'EOF'
#!/bin/sh
[ -e "$LOGD/tg-fail" ] && { echo "tg: send failed (stub)" >&2; exit 1; }
echo x >> "$LOGD/tg.count"
printf '%s\n' "$*" > "$LOGD/tg.last"
printf '%s\n' "${TG_SOFT:-}" > "$LOGD/tg.soft"
printf '%s\n---\n' "$*" >> "$LOGD/tg.log"
EOF
chmod +x "$T/shims"/*
export PATH=$T/shims:$PATH HDDHEAT_STATE_DIR=$T/state HDDHEAT_INBOX_DIR=$T/inbox HDDHEAT_TG=$T/shims/tg \
       HDDHEAT_HOST=fakehost UNRAID_SSH_BIN=$T/shims/unraid-ssh
unset HDDHEAT_WARN HDDHEAT_NORMAL HDDHEAT_URGENT HDDHEAT_STEP HDDHEAT_RENOTIFY_H HDDHEAT_MAX_GAP_MIN HDDHEAT_NOW

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
sends() { [ -f "$T/tg.count" ] && wc -l < "$T/tg.count" | tr -d ' ' || echo 0; }
last() { cat "$T/tg.last" 2>/dev/null; }
notes() { ls "$T/inbox" 2>/dev/null | grep -c "^${1:-}" ; }
reset() { rm -rf "$T/state" "$T/inbox" "$T/tg.count" "$T/tg.last" "$T/tg.log" "$T"/ssh-* "$T/ssh.log" "$T/unraid-ssh.log"
          mkdir -p "$T/state" "$T/inbox"; }

# fx [name=temp ...] [pos=N size=N act=..] [id_disk1=X] -> collector output (HH format). Unset disks are spun down.
# Array: parity, parity2, disk1-disk4 (disk4 is an SSD in the array), pools cache (NVMe) + flash.
fx() {
  local -A V=()
  local a
  for a in "$@"; do V[${a%%=*}]=${a#*=}; done
  printf '\n=====HH meta\nnow=1\nhost=Testhost\n'
  printf '\n=====HH var\nmdState="STARTED"\nmdResyncPos="%s"\nmdResyncSize="%s"\nmdResyncAction="%s"\n' \
    "${V[pos]:-0}" "${V[size]:-15625879500}" "${V[act]:-check P}"
  printf '\n=====HH disks\n'
  d() { [[ " ${V[gone]:-} " == *" $1 "* ]] && return
        printf '["%s"]\nname="%s"\ndevice="sdx"\nid="%s"\ntransport="%s"\nrotational="%s"\nspundown="0"\nstatus="DISK_OK"\ntemp="%s"\ntype="%s"\n' \
        "$1" "$1" "${V[id_$1]:-FAKE_${1^^}}" "$3" "$4" "${V[$1]:-*}" "$2"; }
  d parity Parity ata 1; d parity2 Parity scsi 1; d disk1 Data ata 1; d disk2 Data ata 1; d disk3 Data scsi 1
  d disk4 Data ata 0; d cache Cache nvme 0; d flash Flash usb 1
  printf '\n=====HH end\n'
}
# at "YYYY-MM-DD HH:MM" [fx args] -> runs hddheat at that Zurich time; sets $out $err $rc
at() {
  local when=$1; shift
  fx "$@" > "$FIX"
  out=$(HDDHEAT_NOW=$(TZ=Europe/Zurich date -d "$when" +%s) hh ${XARGS:-} 2>"$T/err"); rc=$?
  err=$(cat "$T/err")
}
D=2026-10-15

echo "# help and the read-only collector"
o=$(hh --help); rc=$?
eq "--help rc 0" "$rc" "0"
has "--help names the commands" "$o" "{run,show,status}"
hh frobnicate >/dev/null 2>&1; eq "unknown command: rc 2" "$?" "2"
col=$(hh --show-collector)
has "collector says it is read-only" "$col" "STRICTLY READ-ONLY"
hasnt "collector runs no smartctl (code lines)" "$(printf '%s\n' "$col" | grep -v '^#')" "smartctl"
eq "collector has no write commands or output redirection" "$(printf '%s\n' "$col" | python3 -I -c '
import re, sys
bad = []
for ln in sys.stdin.read().splitlines():
    code = ln.split("#", 1)[0]
    if re.search(r"(?<![0-9&])>(?!&)", code) or re.search(r"\b(rm|mv|cp|touch|tee|dd|mkdir|chmod|chown|truncate|mdcmd|hdparm|sed -i)\b", code):
        bad.append(ln.strip())
print(bad)')" "[]"

echo "# all cool: silent"
reset
at "$D 14:00" parity=40 disk1=41 disk2=39 cache=70
eq "cool: rc 0" "$rc" "0"
eq "cool: no text" "$(sends)" "0"
eq "cool: no inbox note" "$(notes)" "0"
eq "cool: prints nothing" "$out$err" ""
has "ssh went to the host with bash -s" "$(cat "$T/ssh.log")" "fakehost bash -s"
has "the collector was piped in" "$(cat "$T/ssh.stdin")" "=====HH disks"
has "runs.log has the run" "$(cat "$T/state/runs.log")" "max=disk1:41"
eq "state saved" "$(python3 -I -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s["disks"]["disk1"]["temp"])' "$T/state/state.json")" "41"

echo "# NVMe pool, flash and an SSD in the array never alert"
reset
at "$D 14:00" cache=75 disk4=70; at "$D 14:10" cache=75 disk4=70; at "$D 14:20" cache=75 disk4=70
eq "pool/SSD: no text" "$(sends)" "0"
hasnt "pool/SSD: not even tracked" "$(cat "$T/state/state.json")" '"cache"'

echo "# >= 58 on two consecutive checks -> one text, then silence"
reset
at "$D 14:00" disk1=58
eq "1st hot reading: rc 0" "$rc" "0"
eq "1st hot reading: no text" "$(sends)" "0"
at "$D 14:10" disk1=59 parity=45
eq "2nd hot reading: rc 1" "$rc" "1"
eq "2nd hot reading: one text" "$(sends)" "1"
m=$(last)
has "text: header with host" "$m" "🔥 HDD heat on Testhost"
eq "heat text: urgent (plain tg, no TG_SOFT)" "$(cat "$T/tg.soft")" ""
has "text: disk name and temp" "$m" "Disk 1: 59°C"
has "text: hint" "$m" "Check fans, airflow and dust."
has "text: what comes next" "$m" "Next text: 3°C hotter, in 6 h, or below 55°C."
hasnt "text: cool parity not listed" "$m" "Parity"
hasnt "text: fresh alert has no 'since'" "$m" "since"
eq "text: at most 10 lines" "$([ "$(printf '%s\n' "$m" | wc -l)" -le 10 ] && echo y)" "y"
eq "heat text leaves one inbox note" "$(notes hddheat-)" "1"
has "inbox note says it was already texted" "$(cat "$T"/inbox/hddheat-*)" "Already texted"
at "$D 14:20" disk1=59; at "$D 14:30" disk1=60; at "$D 15:00" disk1=61
eq "same alert, +2°C within 6 h: silent" "$(sends)" "1"
eq "silent run: rc 0" "$rc" "0"
at "$D 15:10" disk1=62
eq "+3°C over the last text: text again" "$(sends)" "2"
has "hotter text: delta" "$(last)" "Disk 1: 62°C (+3°C since my last text, hot since 14:10)"
at "$D 15:20" disk1=62; at "$D 16:00" disk1=63
eq "after the hotter text: silent again" "$(sends)" "2"
at "$D 21:00" disk1=60
eq "5 h 50 min after the last text: silent" "$(sends)" "2"
at "$D 21:10" disk1=60
eq "still hot after 6 h: one more text" "$(sends)" "3"
has "still-hot text" "$(last)" "Disk 1: 60°C (hot since 14:10, peak 63°C)"
at "$D 21:20" disk1=60
eq "right after: silent" "$(sends)" "3"
at "$D 21:30" disk1=56; at "$D 21:40" disk1=55
eq "55-57: hysteresis, no text" "$(sends)" "3"
at "$D 21:50" disk1=54
eq "below 55: back-to-normal text" "$(sends)" "4"
eq "back-to-normal text is one short line" "$(last)" "✅ Disk 1 back to normal: 54°C"
eq "back-to-normal only: non-urgent (TG_SOFT=1)" "$(cat "$T/tg.soft")" "1"
eq "back-to-normal leaves no new inbox note" "$(notes hddheat-)" "3"
at "$D 22:00" disk1=50
eq "after normal: silent" "$(sends)" "4"
at "$D 22:10" disk1=58; at "$D 22:20" disk1=58
eq "new episode later: new text" "$(sends)" "5"

echo "# what counts as consecutive"
reset
at "$D 14:00" disk1=59; at "$D 14:40" disk1=59
eq "40 min apart: not consecutive" "$(sends)" "0"
at "$D 14:50" disk1=59
eq "then the next one: text" "$(sends)" "1"
reset
at "$D 14:00" disk1=59; at "$D 14:10"; at "$D 14:20" disk1=59
eq "spun down in between: streak broken" "$(sends)" "0"
reset
at "$D 14:00" disk1=59; at "$D 14:10" disk1=57; at "$D 14:20" disk1=59
eq "57 in between: streak broken" "$(sends)" "0"

echo "# parity names, several disks in one message, parity-check hint"
reset
at "$D 14:00" parity2=58 disk3=60 pos=7812939750; at "$D 14:10" parity2=58 disk3=61 pos=7812939750
eq "two disks: one text" "$(sends)" "1"
m=$(last)
has "parity 2 named" "$m" "Parity 2: 58°C"
has "disk 3 in the same text" "$m" "Disk 3: 61°C"
has "parity-check hint" "$m" "Parity check running (50%). Check fans"
at "$D 14:20" parity2=58 disk3=61 pos=1 act="recon Q"
at "$D 14:30" parity2=50 disk3=61 act="recon Q" pos=1
eq "one disk back to normal: its own short text" "$(sends)" "2"
eq "only the disk that changed" "$(last)" "✅ Parity 2 back to normal: 50°C"

echo "# quiet hours (00-08): < 60 waits for 08:00, >= 60 texts at once"
reset
at "$D 02:00" disk2=58; at "$D 02:10" disk2=59
eq "night 59: rc 1 (held)" "$rc" "1"
eq "night 59: no text" "$(sends)" "0"
has "runs.log: held" "$(tail -1 "$T/state/runs.log")" "held for 08:00"
has "status shows the held alert" "$(hh status)" "held"
at "$D 02:20" disk2=59; at "$D 05:00" disk2=59
eq "night: still held" "$(sends)" "0"
at "$D 08:00" disk2=58
eq "08:00: held text goes out" "$(sends)" "1"
has "08:00 text: hot since and peak" "$(last)" "Disk 2: 58°C (hot since 02:10, peak 59°C)"
at "$D 08:10" disk2=58; at "$D 13:00" disk2=58
eq "after the 08:00 text: silence restarts (no instant 6 h repeat)" "$(sends)" "1"

reset
at "$D 02:00" disk2=58; at "$D 02:10" disk2=58; at "$D 02:20" disk2=60
eq "held alert reaching 60 at night: texted at once" "$(sends)" "1"
has "night text" "$(last)" "Disk 2: 60°C (hot since 02:10)"
at "$D 02:30" disk2=61; at "$D 02:40" disk2=62
eq "night, +2 after the text: silent" "$(sends)" "1"
at "$D 02:50" disk2=63
eq "night, +3 and >= 60: texted at once" "$(sends)" "2"
at "$D 03:00" disk2=50
eq "night back to normal: held" "$(sends)" "2"
at "$D 07:50" disk2=45
eq "07:50: still held" "$(sends)" "2"
at "$D 08:00" disk2=44
eq "08:00: back-to-normal goes out" "$(sends)" "3"
eq "08:00 normal text" "$(last)" "✅ Disk 2 back to normal: 50°C since 03:00"

reset
at "$D 01:00" disk1=61; at "$D 01:10" disk1=61
eq "night >= 60 from the start: texted at once" "$(sends)" "1"

reset
at "$D 02:00" disk1=58; at "$D 02:10" disk1=59; at "$D 03:00" disk1=52; at "$D 06:00" disk1=40
eq "whole episode inside quiet hours: nothing at night" "$(sends)" "0"
at "$D 08:00" disk1=40
eq "08:00: one summary text" "$(sends)" "1"
m=$(last)
has "summary header" "$m" "🌙 HDD heat on Testhost overnight, over now:"
has "summary line" "$m" "✅ Disk 1 back to normal: 52°C since 03:00 (peak 59°C, hot from 02:10)"
at "$D 08:10" disk1=40
eq "after the summary: silent" "$(sends)" "1"

reset
at "$D 23:40" disk3=59; at "$D 23:50" disk3=59
eq "evening alert texted" "$(sends)" "1"
at "2026-10-16 05:40" disk3=59; at "2026-10-16 05:50" disk3=59
eq "6 h later in quiet hours, < 60: held" "$(sends)" "1"
at "2026-10-16 08:00" disk3=59
eq "held repeat goes out at 08:00" "$(sends)" "2"
has "repeat text carries the date of the start" "$(last)" "Disk 3: 59°C (hot since 15.10 23:50)"

echo "# failures"
reset
touch "$T/ssh-fail"
at "$D 14:00" disk1=59
eq "ssh down: rc 2" "$rc" "2"
eq "ssh down: no text" "$(sends)" "0"
eq "ssh down: no inbox note" "$(notes)" "0"
has "ssh down: reason on stderr" "$err" "Connection refused"
has "ssh down: runs.log" "$(cat "$T/state/runs.log")" "collect-fail #1"
rm "$T/ssh-fail"; touch "$T/ssh-cut"
at "$D 14:10" disk1=59
eq "collector cut off: rc 2" "$rc" "2"
has "collector cut off: says so" "$err" "collector incomplete"
rm "$T/ssh-cut"
at "$D 14:20" disk1=59; at "$D 14:30" disk1=59
eq "after recovery: normal alerting" "$(sends)" "1"
eq "failure counter reset" "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["ssh_fail"])' "$T/state/state.json")" "0"

reset
touch "$T/tg-fail"
at "$D 14:00" disk1=59; at "$D 14:10" disk1=59
eq "tg down: no text" "$(sends)" "0"
eq "tg down: one inbox note" "$(notes hddheat-tg-failed)" "1"
has "tg down: note carries the text" "$(cat "$T"/inbox/hddheat-tg-failed-*)" "Disk 1: 59°C"
at "$D 14:20" disk1=59
eq "tg still down: no second note" "$(notes hddheat-tg-failed)" "1"
rm "$T/tg-fail"
at "$D 14:30" disk1=59
eq "tg back: the held text goes out once" "$(sends)" "1"
at "$D 14:40" disk1=59
eq "then silence" "$(sends)" "1"

echo "# SSH lockdown: the read-only heat verb (this collector, run by the dispatcher)"
reset
echo verbs > "$T/ssh-mode"
at "$D 14:00" disk1=59 pos=5000000000 act=recon; at "$D 14:10" disk1=59 pos=5000000000 act=recon
eq "verbs mode: text" "$(sends)" "1"
eq "verbs mode: heat verb used" "$(head -1 "$T/unraid-ssh.log")" "--host fakehost run heat"
eq "verbs mode: parity-sync progress kept in the text" "$(grep -c 'Parity sync running (31%)' "$T/tg.last")" "1"
eq "verbs mode: plain ssh not used" "$([ -e "$T/ssh.log" ] && echo used || echo unused)" "unused"
rm "$T/ssh-mode"

echo "# swapped disk, removed slot"
reset
at "$D 14:00" disk1=59; at "$D 14:10" disk1=59
at "$D 14:20" disk1=40 id_disk1=NEW_DISK
eq "swapped disk: no back-to-normal for the old one" "$(sends)" "1"
at "$D 14:30" disk1=59 id_disk1=NEW_DISK
eq "swapped disk: starts a fresh streak" "$(sends)" "1"

reset
at "$D 02:00" disk1=59; at "$D 02:10" disk1=59
at "$D 03:00" gone=disk1
hasnt "slot removed: state forgotten" "$(cat "$T/state/state.json")" '"disk1"'
at "$D 08:00"
eq "slot removed from the array: its held text is dropped" "$(sends)" "0"
reset
at "$D 14:00" disk1=59; at "$D 14:10" disk1=59
at "$D 14:20" gone="parity parity2 disk1 disk2 disk3 disk4 cache flash"
eq "empty disks.ini: rc 0, nothing judged" "$rc" "0"
at "$D 14:30" disk1=40
eq "empty disks.ini forgot nothing: back-to-normal still comes" "$(sends)" "2"

echo "# --dry-run, show, status"
reset
at "$D 14:00" disk1=59
XARGS=--dry-run at "$D 14:10" disk1=59
eq "dry-run: no text" "$(sends)" "0"
has "dry-run: prints the text" "$out" "Disk 1: 59°C"
eq "dry-run: state untouched (streak still 1)" "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["disks"]["disk1"]["streak"])' "$T/state/state.json")" "1"
fx disk1=47 parity=44 pos=3906469875 > "$FIX"
o=$(hh show); rc=$?
eq "show: rc 0" "$rc" "0"
has "show: temps" "$o" "Disk 1"
has "show: spun-down disk" "$o" "spun down"
has "show: parity progress" "$o" "Parity check running (25%)."
hasnt "show: pool not listed" "$o" "cache"
eq "show: no text" "$(sends)" "0"
o=$(HDDHEAT_STATE_DIR=$T/empty hh status)
has "status without state" "$o" "no state yet"

echo "# crash: one inbox note, exit 3"
reset
mkdir -p "$T/state/state.json"
at "$D 14:00" disk1=40
eq "crash: rc 3" "$rc" "3"
eq "crash: one note" "$(notes hddheat-crash)" "1"
at "$D 14:10" disk1=40
eq "crash again: still one note a day" "$(notes hddheat-crash)" "1"

echo "# the real state stayed untouched"
TR=$(realpath "$T")
eq "every hddheat run was audited" "$(grep -c '^run' "$AUD")" "$(wc -l < "$T/hh.count" | tr -d ' ')"
has "the audit sees the runs' own writes (positive control)" "$(grep -P '^w\t' "$AUD")" "$TR/state/state.json"
eq "no write outside the test dir (live state, ~/inbox, ...)" \
   "$(awk -F'\t' -v t="$TR/" '$1 == "w" && index($4, t) != 1 && $4 != "/dev/null" { print $3, $4 }' "$AUD" | sort -u)" ""
eq "no write into the live state dir" "$(awk -F'\t' -v r="$(realpath -m "$REAL")" \
   '$1 == "w" && ($4 == r || index($4, r "/") == 1) { print $3, $4 }' "$AUD" | sort -u)" ""
eq "every program hddheat started is a test shim" \
   "$(awk -F'\t' -v s="$TR/shims/" '$1 == "x" && index($4, s) != 1 { print $3, $4 }' "$AUD" | sort -u)" ""
eq "live dir: files the cron never writes unchanged" "$(real_other)" "$other_before"
# The live job's own files (CRONF: state.json, runs.log, .lock, ...) may change during the run: the live tick (cron or
# the hub's) is not ours to time, and waiting for its runs.log line flaked under load (30.10). The audit above is the
# proof that no test run wrote there; the files the job never writes are compared exactly (real_other).

echo
echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
