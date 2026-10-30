#!/usr/bin/env bash
# Offline tests for ~/bin/unraid-capacity (capacity history + fill-up forecast). Exit 0 = all pass.
# Never touches the server: `ssh` and ~/bin/unraid-ssh are PATH/env shims that replay collector-ok.txt, `tg` is a
# logging stub, and state + inbox are temp dirs (UNRAID_CAPACITY_STATE_DIR / _INBOX_DIR, also used by the crash
# handler). gen.py writes synthetic sample histories. The real ~/.local/state/unraid-capacity is checked untouched.
# UNRAID_CAPACITY_BIN=<path> tests another copy (it needs ~/bin/lib next to it, e.g. a symlink).
# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
set -uo pipefail
UC=${UNRAID_CAPACITY_BIN:-/home/node/bin/unraid-capacity}
HERE=$(cd "$(dirname "$0")" && pwd)
REAL=/home/node/.local/state/unraid-capacity
T=$(mktemp -d "${TMPDIR:-/tmp}/unraid-capacity-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/shims" "$T/state" "$T/inbox"
real_before=$(stat -c '%n %Y %s' "$REAL"/samples.jsonl "$REAL"/alerts.json "$REAL"/backfill.jsonl 2>/dev/null)
NOW=$(date +%s)
sed "s/@NOW@/$NOW/" "$HERE/collector-ok.txt" > "$T/ok.txt"
grep -v '^=====UC end' "$T/ok.txt" > "$T/incomplete.txt"
sed 's/^mdState="STARTED"/mdState="STOPPED"/' "$T/ok.txt" > "$T/stopped.txt"
sed 's/^type="Data"/type="Unassigned"/' "$T/ok.txt" > "$T/nodata.txt"
export FIX=$T/ok.txt LOGD=$T
cat > "$T/shims/ssh" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$LOGD/ssh.log"
cat > "$LOGD/ssh.stdin"
if [ -e "$LOGD/ssh-fail" ]; then echo "ssh: connect to host fakehost port 22: Connection refused" >&2; exit 255; fi
cat "$FIX"
EOF
cat > "$T/shims/unraid-ssh" <<'EOF'
#!/bin/sh
if [ "$1" = mode ]; then cat "$LOGD/ssh-mode" 2>/dev/null || echo root; exit 0; fi
printf '%s\n' "$*" >> "$LOGD/unraid-ssh.log"
cat "$FIX"
EOF
cat > "$T/shims/tg" <<'EOF'
#!/bin/sh
[ -e "$LOGD/tg-fail" ] && { echo "tg: send failed (stub)" >&2; exit 1; }
printf '%s\n' "$*" >> "$LOGD/tg.log"
printf '%s\n' "${TG_SOFT:-}" > "$LOGD/tg.soft"
EOF
chmod +x "$T/shims"/*
export PATH=$T/shims:$PATH UNRAID_CAPACITY_STATE_DIR=$T/state UNRAID_CAPACITY_INBOX_DIR=$T/inbox \
       UNRAID_CAPACITY_TG=$T/shims/tg UNRAID_CAPACITY_HOST=fakehost UNRAID_SSH_BIN=$T/shims/unraid-ssh \
       UNRAID_CAPACITY_SSH_RETRY_WAIT=0
unset UNRAID_CAPACITY_NO_SEND UNRAID_CAPACITY_SHARE

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
lines() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }
notes() { ls "$T/inbox" 2>/dev/null | wc -l | tr -d ' '; }
js() { python3 -I -c 'import json,sys; r = json.loads(sys.stdin.read()); print('"$1"')'; }
gen() { python3 -I "$HERE/gen.py" "$T/state/samples.jsonl" "$@"; }
alerts() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])))' "$T/state/alerts.json" 2>/dev/null || echo none; }

echo "# help and argument parsing"
out=$("$UC" --help); rc=$?
eq "--help rc 0" "$rc" "0"
has "--help lists the commands" "$out" "{record,backfill,forecast,show-collector}"
has "--help carries the forecast method" "$out" "Forecast method"
err=$("$UC" 2>&1); rc=$?
eq "no command: rc 2" "$rc" "2"
has "no command: argparse says so" "$err" "the following arguments are required"
"$UC" forecast --bogus >/dev/null 2>&1; eq "unknown option: rc 2" "$?" "2"
"$UC" frobnicate >/dev/null 2>&1; eq "unknown command: rc 2" "$?" "2"

echo "# the collector stays read-only"
col=$("$UC" show-collector); rc=$?
eq "show-collector rc 0" "$rc" "0"
has "collector says it is read-only" "$col" "STRICTLY READ-ONLY"
has "collector ends with the end marker" "$col" "S end"
eq "collector has no write commands or output redirection" "$(printf '%s\n' "$col" | python3 -I -c '
import re, sys
bad = []
for ln in sys.stdin.read().splitlines():
    code = ln.split("#", 1)[0]
    if re.search(r"(?<![0-9&])>(?!&)", code) or re.search(r"\b(rm|mv|cp|touch|tee|dd|mkdir|chmod|chown|truncate|mkfs|sed -i)\b", code):
        bad.append(ln.strip())
print(bad)')" "[]"

echo "# record from saved collector output"
out=$("$UC" record --input "$T/ok.txt" --dry-run 2>"$T/err"); rc=$?
eq "record --dry-run rc 0" "$rc" "0"
has "dry run says not saved" "$(cat "$T/err")" "[dry-run] not saved"
eq "dry run saves nothing" "$(lines "$T/state/samples.jsonl")" "0"
eq "data disks only (parity/flash skipped), sorted" "$(printf '%s' "$out" | js 'sorted(r["disks"])')" "['disk1', 'disk2', 'disk3']"
eq "cache counted as a pool" "$(printf '%s' "$out" | js 'sorted(r["pools"])')" "['cache']"
eq "df numbers in bytes" "$(printf '%s' "$out" | js 'r["disks"]["disk2"]')" "{'size': 18000000000000, 'used': 17000000000000, 'free': 1000000000000, 'status': 'DISK_OK'}"
eq "unmounted disk falls back to emhttp KiB numbers" "$(printf '%s' "$out" | js '(r["disks"]["disk3"]["size"], r["disks"]["disk3"]["used"], r["disks"]["disk3"]["src"])')" "(1024000000, 614400000, 'emhttp')"
eq "array totals" "$(printf '%s' "$out" | js '(r["array"]["used"], r["array"]["n"])')" "(29000614400000, 3)"
eq "docker.img from the loop mount" "$(printf '%s' "$out" | js 'r["docker_img"]["used"]')" "30000000000"
eq "media share floor KiB -> bytes" "$(printf '%s' "$out" | js '(r["alloc"]["allocator"], r["alloc"]["floor"])')" "('highwater', 102400000000)"
eq "timestamp from the server clock" "$(printf '%s' "$out" | js 'r["ts"]')" "$NOW"
out=$("$UC" record --input "$T/ok.txt"); rc=$?
eq "record rc 0" "$rc" "0"
has "record prints the summary" "$out" "recorded "
has "record summary: pools and docker.img" "$out" "cache 40%, docker.img 30%"
eq "record appends one sample" "$(lines "$T/state/samples.jsonl")" "1"
"$UC" --state-dir "$T/other" record --input "$T/ok.txt" >/dev/null
eq "--state-dir overrides the state dir" "$(lines "$T/other/samples.jsonl")" "1"
err=$("$UC" record --input "$T/incomplete.txt" 2>&1); rc=$?
eq "incomplete output: rc 2" "$rc" "2"
has "incomplete output: says why" "$err" "incomplete collector output"
err=$("$UC" record --input "$T/stopped.txt" 2>&1); rc=$?
eq "array stopped: rc 2" "$rc" "2"
has "array stopped: not saved, says why" "$err" "no sample saved: array not started (mdState=STOPPED)"
err=$("$UC" record --input "$T/nodata.txt" 2>&1); rc=$?
has "no data disks: refused" "$err" "no mounted array data disks found"
eq "bad inputs appended nothing" "$(lines "$T/state/samples.jsonl")" "1"

echo "# record over ssh (shims)"
out=$("$UC" record); rc=$?
eq "root mode: rc 0" "$rc" "0"
has "root mode: one ssh session to the host alias, bash -s" "$(cat "$T/ssh.log")" "BatchMode=yes"
has "root mode: host alias + bash -s" "$(cat "$T/ssh.log")" "fakehost bash -s"
has "root mode: the collector goes in on stdin" "$(cat "$T/ssh.stdin")" "STRICTLY READ-ONLY"
eq "root mode: sample saved" "$(lines "$T/state/samples.jsonl")" "2"
echo verbs > "$T/ssh-mode"; rm -f "$T/ssh.log"
"$UC" record >/dev/null; rc=$?
eq "lockdown (verbs) mode: rc 0" "$rc" "0"
eq "lockdown mode: allowlisted verb via unraid-ssh" "$(cat "$T/unraid-ssh.log")" "--host fakehost run capacity"
eq "lockdown mode: no raw ssh" "$(lines "$T/ssh.log")" "0"
echo root > "$T/ssh-mode"; touch "$T/ssh-fail"
err=$("$UC" record 2>&1); rc=$?
eq "ssh failing: rc 2" "$rc" "2"
has "ssh failing: says why" "$err" "could not collect: collector incomplete (exit 255)"
eq "ssh failing: two attempts" "$(lines "$T/ssh.log")" "2"
eq "ssh failing: nothing appended" "$(lines "$T/state/samples.jsonl")" "3"
rm -f "$T/ssh-fail" "$T/ssh.log"

echo "# forecast"
out=$("$UC" --state-dir "$T/empty" forecast); rc=$?
eq "no samples: rc 2" "$rc" "2"
eq "no samples: JSON error" "$(printf '%s' "$out" | js '(r["ok"], r["error"][:15])')" "(False, 'no samples yet:')"
err=$("$UC" --state-dir "$T/empty" forecast --alert 2>&1); rc=$?
eq "no samples --alert: rc 2" "$rc" "2"
has "no samples --alert: stderr" "$err" "forecast: no samples yet"
gen --rate 100
out=$("$UC" forecast); rc=$?
eq "forecast rc 0" "$rc" "0"
eq "30-day rate is primary, ~100 GB/day" "$(printf '%s' "$out" | js '(r["array"]["primary"], round(r["array"]["trend30"]["rate"] / 1e9))')" "(30, 100)"
eq "usable = free above the share floor" "$(printf '%s' "$out" | js 'r["array"]["usable"]')" "$(( 4295200 * 1000000 ))"
eq "array full in ~43 days" "$(printf '%s' "$out" | js 'r["array"]["eta"]["days"]')" "43.0"
eq "alert: array only" "$(printf '%s' "$out" | js '[a["key"] for a in r["alerts"]]')" "['array']"
eq "pools + docker.img forecast, flat = not filling" "$(printf '%s' "$out" | js '(sorted(r["pools"]), r["pools"]["cache"]["eta"])')" "(['cache', 'docker.img'], None)"
eq "high-water model: each data disk gets a projected full date" "$(printf '%s' "$out" | js 'all(r["disks"][d]["model_eta"] for d in ("disk1", "disk2"))')" "True"
out=$("$UC" forecast --alert); rc=$?
eq "--alert within 60 days: rc 1" "$rc" "1"
has "--alert message" "$out" "Unraid Homesrv: array projected full in ~43 days"
out=$("$UC" forecast --human); rc=$?
eq "--human rc 0" "$rc" "0"
has "--human: array line" "$out" "Array: 31.5 TB of 36.0 TB used (87.5%)"
has "--human: growth line" "$out" "Growth: 30 d +100 GB/day"
has "--human: alert line" "$out" "ALERT: array projected full in ~43 days"
gen --rate 10
out=$("$UC" forecast --alert); rc=$?
eq "slow growth: --alert rc 0" "$rc" "0"
eq "slow growth: --alert prints nothing" "$out" ""
gen --rate 100 --drop-day 20 --drop-gb 3000
out=$("$UC" forecast)
eq "one-off 3 TB delete excluded from the trend" "$(printf '%s' "$out" | js '(len(r["array"]["trend30"]["excluded"]), round(r["array"]["trend30"]["rate"] / 1e9))')" "(1, 100)"
gen --rate 10 --cache-rate 50
out=$("$UC" forecast --alert); rc=$?
eq "cache pool filling in 10 days: rc 1" "$rc" "1"
has "cache pool alert text" "$out" "cache pool projected full in ~10 days"
# pools + docker.img: steps both ways (pulls/builds and prunes), nothing excluded, net growth gate
dkr() { printf '%s' "$1" | js '(lambda t, p: ('"$2"'))(r["pools"]["docker.img"]["trend30"], r["pools"]["docker.img"])'; }
gen --rate 10
out=$("$UC" forecast)
eq "docker.img flat: not filling, rate 0, no ETA" "$(dkr "$out" 't["filling"], t["rate"], p["eta"]')" "(False, 0.0, None)"
gen --rate 10 --docker-rate 6
out=$("$UC" forecast)
eq "docker.img steady growth: filling at ~6 GB/day" "$(dkr "$out" 't["filling"], round(t["rate"] / 1e9, 2), t["method"]')" "(True, 6.0, 'steps')"
out=$("$UC" forecast --alert); rc=$?
eq "docker.img steady growth: --alert rc 1" "$rc" "1"
has "docker.img alert text" "$out" "docker.img projected full in ~12 days"
F=23,23,23,23,23,23,23,23,23,23,23,23,23,23,23,23,23,23
gen --rate 10 --docker-gb "$F,50,51,52,53,54,55,30,30,30,30,30,30"
out=$("$UC" forecast)
eq "pull-then-prune: not filling, no ETA" "$(dkr "$out" 't["filling"], t["rate"], p["eta"]')" "(False, 0.0, None)"
eq "pull-then-prune: the prune is not excluded" "$(dkr "$out" 't["excluded"], round(t["net_recent"]["change"] / 1e9)')" "([], -24)"
out=$("$UC" forecast --alert); rc=$?
eq "pull-then-prune: no alert (rc 0)" "$rc" "0"
gen --rate 10 --days 10 --docker-gb 23.25,23.39,30.27,30.72,30.78,31.14,33.47,28.81,29.44,28.43
out=$("$UC" forecast)
eq "pull/prune history: not filling (old method said +1.2 GB/day)" "$(dkr "$out" 't["filling"], round(t["raw_rate"] / 1e9, 2), p["eta"]')" "(False, 0.55, None)"
out=$("$UC" forecast --human)
has "--human: docker.img not filling with its net change" "$out" "net -1.8 GB in 7 d"
gen --rate 10 --docker-gb "$F,$F,30,30"
out=$("$UC" forecast)
eq "one recent pull: filling, but not extrapolated (rate < 0.5 GB/day)" "$(dkr "$out" 't["filling"], t["rate"] < 0.5e9, p["eta"]["days"] > 100')" "(True, True, True)"
gen --rate 10 --days 7 --docker-rate 6
out=$("$UC" forecast)
eq "docker.img: 6 days of samples = no trend yet" "$(dkr "$out" 't["ok"], t["need_days"], p["eta"]')" "(False, 7, None)"
gen --rate 10
sed "s/@NOW@/$(( NOW + 60 ))/" "$HERE/collector-ok.txt" > "$T/live.txt"
n=$(lines "$T/state/samples.jsonl")
out=$(FIX=$T/live.txt "$UC" forecast --live); rc=$?
eq "--live rc 0" "$rc" "0"
eq "--live: the fresh reading is 'now'" "$(printf '%s' "$out" | js 'r["as_of"]')" "$(( NOW + 60 ))"
eq "--live: not saved" "$(lines "$T/state/samples.jsonl")" "$n"
touch "$T/ssh-fail"
out=$("$UC" forecast --live 2>"$T/err"); rc=$?
eq "--live with ssh down: falls back, rc 0" "$rc" "0"
has "--live with ssh down: says so" "$(cat "$T/err")" "live reading failed"
rm -f "$T/ssh-fail"

echo "# --notify: new or worse only"
gen --rate 100
out=$("$UC" forecast --alert --notify); rc=$?
eq "first alert: rc 1" "$rc" "1"
eq "first alert: texted once" "$(lines "$T/tg.log")" "1"
eq "forecast alert is non-urgent (TG_SOFT=1)" "$(cat "$T/tg.soft" 2>/dev/null)" "1"
has "texted: the alert" "$(cat "$T/tg.log")" "📦 Unraid Homesrv: array projected full in ~43 days"
eq "first alert: one inbox note" "$(notes)" "1"
has "inbox note: owner texted yes" "$(cat "$T"/inbox/*)" "Owner texted: yes"
eq "alert state: array at the 60-day band" "$(alerts | python3 -I -c 'import ast,sys; print(ast.literal_eval(sys.stdin.read())["array"]["band"])')" "60"
out=$("$UC" forecast --alert --notify)
has "same band again: not re-sent" "$out" "[not re-sent"
eq "same band again: no new text" "$(lines "$T/tg.log")" "1"
gen --rate 200
"$UC" forecast --alert --notify >/dev/null
eq "worse (30-day band): texted again" "$(lines "$T/tg.log")" "2"
eq "alert state: 30-day band" "$(alerts | python3 -I -c 'import ast,sys; print(ast.literal_eval(sys.stdin.read())["array"]["band"])')" "30"
gen --rate 10
"$UC" forecast --alert --notify >/dev/null
eq "back out of range: band cleared" "$(alerts)" "{}"
gen --rate 100; rm -f "$T/state/alerts.json"; n=$(notes)
err=$("$UC" forecast --alert --notify --no-send 2>&1 >/dev/null)
has "--no-send: prints instead" "$err" "[no-send] would text the owner"
eq "--no-send: no text, no note" "$(lines "$T/tg.log")/$(notes)" "2/$n"
rm -f "$T/state/alerts.json"
UNRAID_CAPACITY_NO_SEND=1 "$UC" forecast --alert --notify >/dev/null 2>&1
eq "UNRAID_CAPACITY_NO_SEND=1: no text" "$(lines "$T/tg.log")" "2"
rm -f "$T/state/alerts.json"
"$UC" forecast --alert --notify --no-send --dry-run >/dev/null 2>&1
eq "--dry-run: alert state not saved" "$(alerts)" "none"
touch "$T/tg-fail"; rm -f "$T/inbox"/*
out=$("$UC" forecast --alert --notify)
has "tg failing: reported" "$out" "[notified: tg FAILED"
has "tg failing: the note asks me to relay" "$(cat "$T"/inbox/*)" "Owner texted: FAILED"
rm -f "$T/tg-fail" "$T/inbox"/*
gen --rate 10 --age-days 5
"$UC" forecast --alert --notify >/dev/null
has "stale samples: inbox note" "$(cat "$T"/inbox/* 2>/dev/null)" "unraid-capacity: samples are stale"
eq "stale samples: owner not texted" "$(lines "$T/tg.log")" "2"
"$UC" forecast --alert --notify >/dev/null
eq "stale samples: noted once" "$(notes)" "1"

echo "# crash handler"
rm -f "$T/inbox"/*
printf '{"ts": %s, "disks": {}}\n' "$NOW" > "$T/state/samples.jsonl"
err=$("$UC" forecast 2>&1); rc=$?
eq "crash: rc 3" "$rc" "3"
has "crash: traceback on stderr" "$err" "Traceback"
has "crash: inbox note in the (test) inbox" "$(cat "$T"/inbox/* 2>/dev/null)" "unraid-capacity crashed"
rm -f "$T/inbox"/*
UNRAID_CAPACITY_NO_SEND=1 "$UC" forecast >/dev/null 2>&1
eq "crash with NO_SEND: no note" "$(notes)" "0"

echo "# real state untouched"
eq "~/.local/state/unraid-capacity unchanged" "$(stat -c '%n %Y %s' "$REAL"/samples.jsonl "$REAL"/alerts.json "$REAL"/backfill.jsonl 2>/dev/null)" "$real_before"

echo "# URL source: profile first, media.json fallback, own config, missing live file (test_url_source.py)"
if pu=$(UNRAID_CAPACITY_BIN=$UC python3 -I "$HERE/test_url_source.py" 2>&1); then ok "url source suite: $(printf '%s\n' "$pu" | tail -1)"
else no "url source suite" "$(printf '%s\n' "$pu" | grep '^FAIL' | head -8)"; fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
