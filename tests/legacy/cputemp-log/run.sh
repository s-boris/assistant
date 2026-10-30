#!/usr/bin/env bash
# Offline tests for ~/bin/cputemp-log (read-only CPU heat logger for the Unraid server). Exit 0 = all pass.
# Never touches the server or the real state: CPUTEMP_STATE is a temp dir and CPUTEMP_SSH is a fake ssh that runs
# the remote loop locally against a fixture /sys tree (CPUTEMP_SYSROOT), which is made read-only and checksummed
# before/after to prove the loop writes nothing. UNRAID_SSH_BIN is a fake unraid-ssh: `mode`, and `run cputemp`
# (the lockdown dispatcher's verb = the same REMOTE loop, run locally on the fixture; FAKE_VERB=refuse = an older
# dispatcher without the verb). Process checks only count processes whose CPUTEMP_STATE is this test's, so a
# live logger on the same machine does not count.
# CPUTEMP_LOG_BIN=<path> tests another copy.
set -uo pipefail
B=${CPUTEMP_LOG_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/cputemp-log}
T=$(mktemp -d "${TMPDIR:-/tmp}/cputemp-log-tests.XXXXXX")
pass=0 fail=0
ok() { pass=$((pass+1)); }
bad() { fail=$((fail+1)); echo "FAIL: $*"; }
check() { local name=$1; shift; if "$@"; then ok; else bad "$name"; fi; }
pids_f() {  # pgrep -f without procps (the CI runner's image has none): pids whose command line matches ERE $1
  local d a c
  for d in /proc/[0-9]*; do
    [ "${d#/proc/}" = "$BASHPID" ] && continue
    mapfile -d '' -t a < "$d/cmdline" 2>/dev/null || continue
    c="${a[*]}"
    [ -n "$c" ] && [[ $c =~ $1 ]] && echo "${d#/proc/}"
  done
  return 0
}
cleanup() { kill $(pids_f "$T/") 2>/dev/null; chmod -R u+w "$T" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
export CPUTEMP_STATE=$T/state CPUTEMP_SSH=$T/fake-ssh UNRAID_SSH_BIN=$T/fake-unraid-ssh CPUTEMP_HOST=fakehost
export TZ=Europe/Zurich
mkdir -p "$CPUTEMP_STATE"

# ---- fixture /sys: coretemp (package + 2 cores), an nvme hwmon, RAPL, 4 cpus = 2 HT P-cores, 2 E-cores
S=$T/sys
w() { mkdir -p "$(dirname "$S/$1")"; printf '%s\n' "$2" > "$S/$1"; }
w class/hwmon/hwmon0/name nvme;      w class/hwmon/hwmon0/temp1_label Composite; w class/hwmon/hwmon0/temp1_input 99000
w class/hwmon/hwmon1/name coretemp
w class/hwmon/hwmon1/temp1_label "Package id 0"; w class/hwmon/hwmon1/temp1_input 55000
w class/hwmon/hwmon1/temp2_label "Core 0";       w class/hwmon/hwmon1/temp2_input 61000
w class/hwmon/hwmon1/temp3_label "Core 4";       w class/hwmon/hwmon1/temp3_input 72000
w class/powercap/intel-rapl:0/energy_uj 1000000
w class/powercap/intel-rapl:0/max_energy_range_uj 262143328850
w devices/cpu_core/cpus 0-1
for n in 0 1 2 3; do
  w devices/system/cpu/cpu$n/topology/physical_package_id 0
  w devices/system/cpu/cpu$n/cpufreq/scaling_cur_freq $(( n < 2 ? 5500000 : 800000 ))
  w devices/system/cpu/cpu$n/thermal_throttle/package_throttle_count 100
  w devices/system/cpu/cpu$n/thermal_throttle/package_throttle_total_time_ms 500
  w devices/system/cpu/cpu$n/thermal_throttle/core_throttle_count 10
  w devices/system/cpu/cpu$n/thermal_throttle/core_throttle_total_time_ms 40
done
w devices/system/cpu/cpu0/topology/core_id 0; w devices/system/cpu/cpu1/topology/core_id 4
w devices/system/cpu/cpu2/topology/core_id 8; w devices/system/cpu/cpu3/topology/core_id 9
w devices/system/cpu/cpu1/cpufreq/scaling_cur_freq 4500000
chmod -R a-w "$S"
sum_before=$(cd "$S" && find . -type f -exec md5sum {} + | sort | md5sum)

cat > "$T/fake-ssh" <<EOF
#!/usr/bin/env bash
# fake ssh: record args, run the remote command (last arg) locally against the fixture /sys
printf '%s\n' "\$@" > "$T/ssh.args"
export CPUTEMP_SYSROOT=$S
eval "\${@: -1}"
EOF
python3 -I -c 'import importlib.machinery as m,sys; sys.dont_write_bytecode=True; print(m.SourceFileLoader("c", sys.argv[1]).load_module().REMOTE, end="")' "$B" > "$T/remote.sh"
cat > "$T/fake-unraid-ssh" <<EOF
#!/usr/bin/env bash
[ "\$1" = mode ] && { echo "\${FAKE_MODE:-root}"; exit 0; }
printf '%s\n' "\$@" > "$T/us.args"
[ "\${FAKE_VERB:-}" = refuse ] && { echo "claude-bot-cmd: rejected: unknown verb 'cputemp'" >&2; exit 1; }
CPUTEMP_SYSROOT=$S exec bash -c "\$(cat "$T/remote.sh")"
EOF
chmod +x "$T/fake-ssh" "$T/fake-unraid-ssh"
test_procs() {  # pids matching $1 whose environment has this test's CPUTEMP_STATE
  local p
  for p in $(pids_f "$1"); do
    grep -qx "CPUTEMP_STATE=$CPUTEMP_STATE" <<<"$(tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null)" && echo "$p"
  done
}

# ---- 1. run: 3 samples through the fake session
CPUTEMP_MAX_SAMPLES=3 CPUTEMP_INTERVALS=0.2,0.2 timeout 30 "$B" run; rc=$?
day=$(date +%F); csvf=$CPUTEMP_STATE/$day.csv
check "run exits 0" [ "$rc" = 0 ]
check "csv has header + 3 rows" [ "$(wc -l < "$csvf" 2>/dev/null)" = 4 ]
check "ssh got BatchMode and host" grep -qx fakehost "$T/ssh.args"
row2=$(sed -n 3p "$csvf")
IFS=, read -r ts dts pkg core pw mhz pn pms cn cms win <<<"$row2"
check "pkg temp parsed (55.0)" [ "$pkg" = 55.0 ]
check "hottest core = 72.0 (nvme ignored)" [ "$core" = 72.0 ]
check "P-core MHz = mean of cpu0-1 only (5000)" [ "$mhz" = 5000 ]
check "power 0.0 W with an unchanged counter" [ "$pw" = 0.0 ]
check "throttle deltas 0" [ "$pn,$pms,$cn,$cms" = "0,0,0,0" ]
first=$(sed -n 2p "$csvf" | cut -d, -f2,5,7)
check "first row after connect has blank deltas" [ "$first" = ",," ]
sum_after=$(cd "$S" && find . -type f -exec md5sum {} + | sort | md5sum)
check "fixture /sys unchanged (read-only loop)" [ "$sum_before" = "$sum_after" ]
check "remote script has no write redirections" bash -c "! python3 -c 'import importlib.machinery as m;print(m.SourceFileLoader(\"c\",\"$B\").load_module().REMOTE)' | grep -Eq '>[^&]|tee |dd |mkdir|rm |touch|echo [0-9]+ *>'"

# ---- 2. pure functions: deltas, wrap, reset, HT dedupe, windows, p95, rotation
cat > "$T/unit.py" <<'EOF'
import datetime as dt, importlib.machinery, os, sys
c = importlib.machinery.SourceFileLoader("c", sys.argv[1]).load_module()
fails = []
def eq(name, a, b):
    if a != b: fails.append("%s: %r != %r" % (name, a, b))
topo = c.parse_topo("T emax=1000000 pcpus=0,1 topo=0:0.0,1:0.0,2:0.4,3:0.4")
eq("topo emax", topo["emax"], 1000000)
L = lambda ts, e, pt, pms, ct: "S ts=%s pkg=60000 core=70000 e=%d fs=9000000 fn=2 pthr=%d pthrms=%d ct=%s" % (ts, e, pt, pms, ct)
a = c.parse_sample(L("100.0", 900000, 10, 50, "0:5:20,1:5:20,2:7:30,3:7:30"), topo)
eq("HT siblings deduped", len(a["cores"]), 2)
b = c.parse_sample(L("110.0", 200000, 13, 80, "0:9:60,1:9:60,2:8:35,3:8:35"), topo)
t = dt.datetime(2026, 10, 16, 1, 0, 0)
r = c.make_row(a, b, topo, t)
eq("wrap: (1000000+1-900000+200000)/10s = 0.03 W", r["pkg_w"], "0.0")
topo2 = dict(topo, emax=100_000_000)
a2 = c.parse_sample(L("100.0", 99_000_000, 10, 50, ""), topo2)
b2 = c.parse_sample(L("110.0", 49_000_001, 10, 50, ""), topo2)
eq("wrap power 5.0 W", c.make_row(a2, b2, topo2, t)["pkg_w"], "5.0")
a3 = c.parse_sample(L("100.0", 0, 10, 50, ""), topo); b3 = c.parse_sample(L("110.0", 1_250_000_000, 10, 50, ""), topo)
eq("power 125 W", c.make_row(a3, b3, topo, t)["pkg_w"], "125.0")
eq("pkg thr n", r["pkg_thr_n"], "3"); eq("pkg thr ms", r["pkg_thr_ms"], "30")
eq("core thr n summed per core (4+1)", r["core_thr_n"], "5"); eq("worst core ms", r["core_thr_ms_max"], "40")
eq("dt", r["dt_s"], "10.0"); eq("mhz", r["pcore_mhz"], "4500"); eq("win", r["win"], "night")
reset = c.parse_sample(L("120.0", 300000, 2, 5, "0:1:1,2:1:1"), topo)
r2 = c.make_row(b, reset, topo, t)
eq("counter reset -> blank", (r2["pkg_thr_n"], r2["pkg_thr_ms"], r2["core_thr_n"]), ("", "", ""))
eq("first row blank dt", c.make_row(None, b, topo, t)["dt_s"], "")
for hm, w in (("00:54", "other"), ("00:55", "night"), ("06:34", "night"), ("06:35", "other"), ("11:55", "day"),
              ("20:04", "day"), ("20:05", "other"), ("23:00", "other")):
    h, m = map(int, hm.split(":"))
    eq("window " + hm, c.window_of(dt.datetime(2026, 10, 16, h, m)), w)
eq("interval in window", c.interval_at(dt.datetime(2026, 10, 16, 2, 0)), 10.0)
eq("interval outside", c.interval_at(dt.datetime(2026, 10, 16, 9, 0)), 60.0)
eq("p95 of 1..100", c.p95(list(range(1, 101))), 95); eq("p95 of 1..10", c.p95(list(range(1, 11))), 10)
eq("p95 single", c.p95([7]), 7)
st = os.environ["CPUTEMP_STATE"]
os.environ["CPUTEMP_NOW"] = "2026-10-16"
for d in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-16"):
    open(os.path.join(st, d + ".csv"), "w").close()
open(os.path.join(st, "notes.txt"), "w").close()
c.rotate()
left = sorted(f for f in os.listdir(st) if f.startswith("2026-10-0") or f.startswith("2026-10-16") or f == "notes.txt")
eq("rotation keeps 14 days (03..16) and other files", left, ["2026-10-03.csv", "2026-10-16.csv", "notes.txt"])
for f in ("2026-10-03.csv", "2026-10-16.csv", "notes.txt"):
    os.remove(os.path.join(st, f))
print("\n".join(fails)); sys.exit(1 if fails else 0)
EOF
out=$(python3 -I "$T/unit.py" "$B" 2>&1); check "unit: $out" [ $? = 0 ]

# ---- 3. summary on a crafted day
mkdir -p "$T/s2"
cat > "$T/s2/2026-10-15.csv" <<'EOF'
ts,dt_s,pkg_c,core_max_c,pkg_w,pcore_mhz,pkg_thr_n,pkg_thr_ms,core_thr_n,core_thr_ms_max,win
2026-10-15 01:00:00,,50.0,52.0,,800,,,,,night
2026-10-15 01:00:10,10.0,90.0,100.0,180.0,5500,40,200,60,300,night
2026-10-15 01:00:20,10.0,70.0,71.0,60.0,4000,10,1300,5,900,night
2026-10-15 12:00:00,30.0,45.0,47.0,40.0,900,0,0,0,0,day
2026-10-15 22:00:00,60.0,44.0,46.0,30.0,800,0,0,0,0,other
EOF
out=$(CPUTEMP_STATE=$T/s2 "$B" summary 2026-10-15)
nl=$(grep '^night' <<<"$out")
check "summary night: 3 rows max 100 p95 100 thr 1.5 s core 1.2 s mean 120.0 W" \
  bash -c "[[ '$nl' =~ ^night\ 00:55-06:35\ +3\ +100\ +100\ +1\.5\ +1\.2\ +120\.0\ +3433$ ]]"
rows_ok() { grep -q '^day 11:55-20:05 *1 ' <<<"$out" && grep -q '^other *1 ' <<<"$out" && grep -q '^all *5 ' <<<"$out"; }
check "summary day + other + all rows" rows_ok
sum=$(CPUTEMP_STATE=$T/s2 CPUTEMP_NOW=2026-10-16 "$B" summary yesterday) && grep -q '^all *5 ' <<<"$sum"; check "summary yesterday" [ $? = 0 ]
CPUTEMP_STATE=$T/s2 "$B" summary 2026-01-01 >/dev/null; check "summary empty day -> 1" [ $? = 1 ]
CPUTEMP_STATE=$T/s2 "$B" summary nonsense >/dev/null 2>&1; check "summary bad day -> 2" [ $? = 2 ]

# ---- 3b. spans (START..END across daily CSVs), --json, compare
mkdir -p "$T/s3"
cat > "$T/s3/2026-10-15.csv" <<'EOF'
ts,dt_s,pkg_c,core_max_c,pkg_w,pcore_mhz,pkg_thr_n,pkg_thr_ms,core_thr_n,core_thr_ms_max,win
2026-10-15 22:00:00,60.0,50.0,52.0,40.0,3000,0,0,0,0,other
2026-10-15 23:59:00,60.0,60.0,62.0,60.0,3200,1,600,1,500,other
EOF
cat > "$T/s3/2026-10-16.csv" <<'EOF'
ts,dt_s,pkg_c,core_max_c,pkg_w,pcore_mhz,pkg_thr_n,pkg_thr_ms,core_thr_n,core_thr_ms_max,win
2026-10-16 01:00:00,,50.0,50.0,,3000,,,,,night
2026-10-16 01:00:10,10.0,90.0,100.0,150.0,5500,10,2000,5,1500,night
2026-10-16 01:00:20,10.0,80.0,85.0,90.0,5000,5,1000,2,800,night
2026-10-16 09:00:00,60.0,60.0,61.0,70.0,3400,1,300,1,300,other
2026-10-16 09:30:00,60.0,70.0,72.0,80.0,3500,2,1200,1,1000,other
2026-10-16 10:00:00,60.0,55.0,56.0,50.0,3300,0,0,0,0,other
EOF
jq_py() { python3 -I -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if ($1) else 1)"; }
c3() { CPUTEMP_STATE=$T/s3 "$B" "$@"; }
c3 summary "2026-10-15 23:00..2026-10-16 02:00" --json > "$T/span.json"; rc=$?
check "span summary across midnight -> 0" [ "$rc" = 0 ]
check "span json: 4 rows from both CSVs, thr 3.6 s, 3 night rows, label" jq_py \
  'd["windows"]["all"]["rows"]==4 and abs(d["windows"]["all"]["thr_s"]-3.6)<1e-9 and d["windows"]["night"]["rows"]==3 and d["label"]=="2026-10-15 23:00..2026-10-16 02:00"' < "$T/span.json"
check "span json: 80 s logged = 0.022 h, 3.6 s throttled = 162 s/h" jq_py \
  'abs(d["windows"]["all"]["hours"]-0.022)<1e-9 and abs(d["windows"]["all"]["thr_s_per_h"]-162.0)<1e-6' < "$T/span.json"
sum=$(c3 summary "2026-10-16 09:00..09:30") && grep -q '^all *1 ' <<<"$sum"; check "END as HH:MM = START's day, end exclusive" [ $? = 0 ]
c3 summary "2026-10-16 10:00..09:00" >/dev/null 2>&1; check "span ending before it starts -> 2" [ $? = 2 ]
c3 summary "2026-10-16 05:00..06:00" --json > "$T/empty.json"; rc=$?
check "empty span --json -> 1 with first null" bash -c "[ $rc = 1 ] && grep -q '\"first\": null' '$T/empty.json'"
CPUTEMP_NOW="2026-10-16 09:30:00" c3 summary "2026-10-16 09:00..now" --json | jq_py 'd["windows"]["all"]["rows"]==1'
check "now = CPUTEMP_NOW with a time" [ $? = 0 ]
out=$(c3 compare 2026-10-15 2026-10-16); rc=$?
check "compare two days -> 0" [ "$rc" = 0 ]
check "compare header names both spans" bash -c "grep -q '^before 2026-10-15 ' <<<\"\$1\" && grep -q '^after  2026-10-16 ' <<<\"\$1\"" _ "$out"
check "compare other/before: 2 rows max 62 p95 62 thr 0.6 s 18.0 s/h 50.0 W 3100 MHz" \
  grep -Eq '^other +before +2 +0\.0 +62 +62 +0\.6 +18\.0 +50\.0 +3100$' <<<"$out"
check "compare other/after: 3 rows max 72 p95 72 thr 1.5 s 30.0 s/h 66.7 W 3400 MHz" \
  grep -Eq '^ +after +3 +0\.1 +72 +72 +1\.5 +30\.0 +66\.7 +3400$' <<<"$out"
check "compare change row: +10 +10 +12.0 s/h +16.7 W +300 MHz" grep -Eq '^ +change +\+10 +\+10 +\+12\.0 +\+16\.7 +\+300$' <<<"$out"
check "compare night: before 0 rows shown as -, after 3 rows, no change row" bash -c \
  "grep -Eq '^night 00:55-06:35 +before +0 +- ' <<<\"\$1\" && grep -Eq '^ +after +3 +0\.0 +100 +100 +3\.0 +540\.0 +120\.0 +4500$' <<<\"\$1\" && [ \"\$(grep -c change <<<\"\$1\")\" = 2 ]" _ "$out"
check "compare --before/--after = positional" [ "$(c3 compare --before 2026-10-15 --after 2026-10-16)" = "$out" ]
c3 compare --at "2026-10-16 01:00:10" --hours 3 --json > "$T/at.json"; rc=$?
check "compare --at -> 0" [ "$rc" = 0 ]
check "compare --at: before/after = the 3 h on either side" jq_py \
  'd["before"]["start"]=="2026-10-15 22:00:10" and d["after"]["end"]=="2026-10-16 04:00:10" and d["before"]["windows"]["all"]["rows"]==2 and d["after"]["windows"]["all"]["rows"]==2' < "$T/at.json"
c3 compare 2026-01-01 2026-01-02 >/dev/null; check "compare with no samples on either side -> 1" [ $? = 1 ]
for bad in "2026-10-15" "--hours 3 2026-10-15 2026-10-16" "--at 2026-10-16 2026-10-15" \
           "2026-10-15 2026-10-16 2026-10-17" "--at 2026-10-16 --hours 0" "--at" "nonsense 2026-10-16"; do
  # shellcheck disable=SC2086
  c3 compare $bad >/dev/null 2>&1; check "compare $bad -> 2" [ $? = 2 ]
done

# ---- 4. ensure / stop / start supervision (fast intervals)
export CPUTEMP_INTERVALS=0.3,0.3
"$B" ensure; sleep 2
p1=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["pid"])' "$CPUTEMP_STATE/status.json" 2>/dev/null)
check "ensure started the loop" kill -0 "${p1:-0}"
"$B" status >/dev/null; check "status 0 while running" [ $? = 0 ]
"$B" ensure; sleep 1
p2=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["pid"])' "$CPUTEMP_STATE/status.json")
check "second ensure keeps the same loop" [ "$p1" = "$p2" ]
n1=$(wc -l < "$CPUTEMP_STATE/$day.csv"); sleep 1; n2=$(wc -l < "$CPUTEMP_STATE/$day.csv")
check "loop keeps writing rows" [ "$n2" -gt "$n1" ]
check "one remote loop while running" [ "$(test_procs "^bash -c set -u" | wc -l)" = 1 ]
"$B" stop >/dev/null; sleep 0.5
check "stop ends the loop" bash -c "! kill -0 $p1 2>/dev/null"
check "stop sets the disabled flag" [ -f "$CPUTEMP_STATE/disabled" ]
"$B" status >/dev/null; check "status 1 when stopped" [ $? = 1 ]
"$B" ensure; sleep 1
no_proc() { [ -z "$(test_procs "$1")" ]; }
check "ensure respects stop" no_proc "$B run"
"$B" start; sleep 1.5
p3=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["pid"])' "$CPUTEMP_STATE/status.json")
check "start clears flag and runs" bash -c "[ ! -f '$CPUTEMP_STATE/disabled' ] && kill -0 $p3"
"$B" stop >/dev/null; sleep 0.5
check "no loop or fake remote loop left behind" no_proc "$B run"
check "no fake remote loop left behind" no_proc "^bash -c set -u"

# ---- 5. ensure replaces a wedged loop (holds the lock, stale status)
rm -f "$CPUTEMP_STATE/disabled"
python3 -c 'import fcntl,sys,time;f=open(sys.argv[1],"a");fcntl.flock(f,fcntl.LOCK_EX);time.sleep(60)' "$CPUTEMP_STATE/run.lock" &
wp=$!; sleep 0.5
printf '{"pid": %d, "started": "2026-01-01T00:00:00", "last_row": "2026-01-01T00:00:00", "last_connect": "2026-01-01T00:00:00"}' "$wp" > "$CPUTEMP_STATE/status.json"
"$B" ensure; sleep 1.5
check "wedged loop killed" bash -c "! kill -0 $wp 2>/dev/null"
p4=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["pid"])' "$CPUTEMP_STATE/status.json")
check "fresh loop started" bash -c "[ '$p4' != '$wp' ] && kill -0 $p4"
"$B" stop >/dev/null; sleep 0.5

# ---- 6. SSH lockdown (verbs / emulate): the dispatcher's cputemp verb through unraid-ssh, never raw ssh
rm -f "$CPUTEMP_STATE/disabled" "$T/ssh.args" "$T/us.args"
n0=$(wc -l < "$csvf")
FAKE_MODE=verbs CPUTEMP_MAX_SAMPLES=2 CPUTEMP_INTERVALS=0.2,0.2 timeout 30 "$B" run; rc=$?
check "verbs mode: run exits 0 after 2 rows" [ "$rc" = 0 ]
check "verbs mode: rows written through the verb" [ "$(wc -l < "$csvf")" = $((n0 + 2)) ]
check "verbs mode: no raw ssh session" [ ! -f "$T/ssh.args" ]
check "verbs mode: unraid-ssh --host fakehost run cputemp (no other arguments)" \
  [ "$(tr '\n' ' ' < "$T/us.args")" = "--host fakehost run cputemp " ]
check "verbs mode: same parsed values as the raw loop (pkg 55.0, hottest core 72.0)" \
  bash -c 'tail -n 1 "$1" | cut -d, -f3,4 | grep -qx "55.0,72.0"' _ "$csvf"
rm -f "$T/us.args"
FAKE_MODE=emulate CPUTEMP_MAX_SAMPLES=1 CPUTEMP_INTERVALS=0.2,0.2 timeout 30 "$B" run
check "emulate mode: also through the verb" [ -f "$T/us.args" ] && [ ! -f "$T/ssh.args" ]
FAKE_MODE=verbs FAKE_VERB=refuse "$B" run & vp=$!; sleep 1.5
check "older dispatcher (verb refused): status says reinstall pending" grep -q "refused the cputemp verb (owner's reinstall pending)" "$CPUTEMP_STATE/status.json"
check "older dispatcher: status keeps the 'verbs mode' marker (ensure leaves the waiting loop alone)" grep -q 'verbs mode' "$CPUTEMP_STATE/status.json"
check "older dispatcher: still no raw ssh" [ ! -f "$T/ssh.args" ]
check "older dispatcher: loop waits (still running, one attempt)" bash -c "kill -0 $vp && [ \"\$(grep -c 'refused the cputemp verb' '$CPUTEMP_STATE/run.log')\" = 1 ]"
kill "$vp" 2>/dev/null; wait "$vp" 2>/dev/null
sum_after=$(cd "$S" && find . -type f -exec md5sum {} + | sort | md5sum)
check "fixture /sys unchanged after the verb runs" [ "$sum_before" = "$sum_after" ]

# ---- 6. watchdog ages across both Zurich DST switches (real seconds, not wall-clock differences)
dst=$(python3 - "$B" <<'PY' 2>&1
import datetime as dt, importlib.machinery as M, importlib.util as U, sys
from zoneinfo import ZoneInfo
l = M.SourceFileLoader('cputemp', sys.argv[1]); s = U.spec_from_loader('cputemp', l); m = U.module_from_spec(s); l.exec_module(m)
Z = ZoneInfo('Europe/Zurich')
at = lambda *t, fold=0: dt.datetime(*t, fold=fold, tzinfo=Z).timestamp()
out = [
    round(m.age_s(m.stamp(dt.datetime(2027, 3, 28, 1, 59, 50)), at(2027, 3, 28, 3, 0, 10))),          # spring: 20 s, not 1 h 20 s
    round(m.age_s(m.stamp(dt.datetime(2026, 10, 25, 2, 59, 50)), at(2026, 10, 25, 2, 0, 10, fold=1))),  # fall: 20 s, not -59 min
    round(m.age_s(m.stamp(dt.datetime(2026, 10, 25, 2, 50)), at(2026, 10, 25, 2, 30, fold=1))),          # stalled in the repeated hour
    m.stamp(dt.datetime(2026, 10, 25, 2, 30, fold=1))[-6:], m.stamp(dt.datetime(2026, 10, 25, 2, 30))[-6:],
    round(m.age_s('2027-03-28T01:59:50', at(2027, 3, 28, 3, 0, 10))),                                  # an old offset-less status
]
print(' '.join(map(str, out)))
PY
)
check "DST ages: spring 20 s, fall 20 s, a stall in the repeated hour 2400 s, stamps +01:00/+02:00, old status 20 s ($dst)" \
  [ "$dst" = "20 20 2400 +01:00 +02:00 20" ]

echo "cputemp-log tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
