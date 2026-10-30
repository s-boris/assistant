#!/usr/bin/env bash
# Offline tests for ~/bin/test-all (runs every ~/opt/*-tests suite offline, one at a time). Exit 0 = all pass.
# HOME is a temp dir; the suites are tiny fakes (pass, fail, timeout, network connects, gh, python-only, real HOME,
# custom entry, expected blocks); the guard is a copy of ~/opt/test-all. The real ~/inbox, ~/.local/state and the
# real suites are never touched.
# TEST_ALL_BIN=<path> tests another copy; TEST_ALL_GUARD_SRC=<dir> takes the guard from another folder;
# TEST_ALL_CRONTAB=<path> checks another crontab (the schedule part follows the registry's runs_on for test-all).
set -uo pipefail
B=${TEST_ALL_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/test-all}
G=${TEST_ALL_GUARD_SRC:-/home/node/opt/test-all}
CT=${TEST_ALL_CRONTAB:-/home/node/crontab}
grep -q '/home/node' "$B" && { echo "refusing: $B names a real home path (must derive everything from \$HOME)"; exit 2; }
T=$(mktemp -d "${TMPDIR:-/tmp}/test-all-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
export HOME=$T/home
OPTD=$T/opt
CONF=$T/conf
export TA_OUT=$T/out
mkdir -p "$HOME/bin" "$OPTD" "$CONF/stubbin" "$TA_OUT"
cp "$G/netguard.c" "$G/netguard.so" "$CONF/" && cp "$G/stubbin/gh" "$CONF/stubbin/gh" && touch "$CONF/netguard.so"
export TEST_ALL_OPT=$OPTD TEST_ALL_CONF=$CONF TEST_ALL_TIMEOUT=3
ST=$HOME/.local/state/test-all
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
row() { grep -E "^$1 " "$ST/${2:-last.txt}" 2>/dev/null | head -1; }
notes() { find "$HOME/inbox" -maxdepth 1 -name 'test-all-*' 2>/dev/null | wc -l | tr -d ' '; }
suite() { mkdir -p "$OPTD/$1-tests"; cat > "$OPTD/$1-tests/${2:-run.sh}"; }

# ---- fake suites
suite pass <<'EOF'
echo "$HOME" > "$TA_OUT/pass.home"
awk '{print $19}' /proc/$$/stat > "$TA_OUT/pass.nice"
echo "${LD_PRELOAD:-}" > "$TA_OUT/pass.preload"
ionice -p $$ > "$TA_OUT/pass.ionice" 2>&1
grep Cpus_allowed_list /proc/self/status | awk '{print $2}' > "$TA_OUT/pass.cpus"
pwd > "$TA_OUT/pass.cwd"
[ -L "$HOME/bin" ] && echo link > "$TA_OUT/pass.binlink"
[ -d "$HOME/inbox" ] && touch "$HOME/inbox/stray-note.md"
read -r -t 1 x && echo "stdin:$x" > "$TA_OUT/pass.stdin"
echo "ok - all good"
exit 0
EOF
suite fail <<'EOF'
echo "FAIL - something broke"
exit 1
EOF
suite slow <<'EOF'
sleep 30
EOF
suite net <<'EOF'
python3 - <<'PY'
import errno, os, socket
res = []
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
c = socket.socket()
try:
    c.connect(("127.0.0.1", srv.getsockname()[1])); res.append("loopback:ok")
except OSError as e:
    res.append("loopback:%s" % errno.errorcode.get(e.errno, e.errno))
for fam, host, port in [(socket.AF_INET, "192.0.2.10", 80), (socket.AF_INET, "127.0.0.1", 9222),
                        (socket.AF_INET, "127.0.0.1", 8790), (socket.AF_INET6, "2001:db8::1", 443)]:
    try:
        s = socket.socket(fam); s.settimeout(2); s.connect((host, port)); res.append("%s:%d:connected" % (host, port))
    except OSError as e:
        res.append("%s:%d:%s" % (host, port, errno.errorcode.get(e.errno, e.errno)))
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for name, fn in [("sendto", lambda: u.sendto(b"x", ("198.51.100.7", 53))),
                 ("sendmsg", lambda: u.sendmsg([b"x"], [], 0, ("198.51.100.8", 53)))]:
    try:
        fn(); res.append(name + ":sent")
    except OSError as e:
        res.append("%s:%s" % (name, errno.errorcode.get(e.errno, e.errno)))
open(os.environ["TA_OUT"] + "/net.txt", "w").write("\n".join(res) + "\n")
PY
exit 0
EOF
suite gh <<'EOF'
gh api repos/someone/something >/dev/null 2>&1
echo "gh rc $?" > "$TA_OUT/gh.rc"
exit 0
EOF
suite py test_a.py <<'EOF'
import os
open(os.environ["TA_OUT"] + "/py.a", "w").write("ran")
EOF
suite py test_b.py <<'EOF'
import os, sys
open(os.environ["TA_OUT"] + "/py.b", "w").write("ran")
sys.exit(1)
EOF
suite realhome <<'EOF'
echo "$HOME" > "$TA_OUT/realhome.home"
EOF
suite entry <<'EOF'
exit 1
EOF
mkdir -p "$OPTD/expectpriv-tests"
cat > "$OPTD/expectpriv-tests/allowed-probe.py" <<'EOF'
import socket, sys
for host in sys.argv[1:]:
    try:
        socket.create_connection((host, 443), timeout=2)
    except OSError:
        pass
EOF
echo 'python3 allowed-probe.py 240.0.2.3' > "$OPTD/expectpriv-tests/run.sh"
printf '# why\nrealhome-tests  # needs the real HOME\n' > "$CONF/real-home.txt"
printf '# comment\nentry-tests\techo custom > "$TA_OUT/entry.txt"\n' > "$CONF/entries.tsv"
printf '# comment\nexpectpriv-tests\tallowed-probe\nexpect\tallowed-probe\n' > "$CONF/expected-blocks.tsv"

echo "# full run: pass, fail, timeout, network, gh, python-only, real HOME, custom entry, private expected"
t0=$(date +%s)
out=$("$B" 2>&1); rc=$?
dt=$(( $(date +%s) - t0 ))
eq "full run with failures exits 1" "$rc" 1
[ "$dt" -lt 25 ] && ok "the 30 s suite was cut at the timeout (run took ${dt}s)" || no "timeout cut the slow suite" "${dt}s"
[ -s "$ST/last.txt" ] && ok "last.txt written" || no "last.txt written"
has "summary counts 9 suites" "$(head -1 "$ST/last.txt")" "9 suite(s): 6 pass, 2 fail, 1 timeout"
has "pass row" "$(row pass)" "pass "
has "fail row" "$(row fail)" "fail "
has "slow row = timeout" "$(row slow)" "timeout"
has "py row = fail (one test_*.py failed)" "$(row py)" "fail "
has "entry row = pass (entries.tsv beats run.sh)" "$(row entry)" "pass "
has "realhome row = real HOME" "$(row realhome)" "real"
has "pass row = temp HOME" "$(row pass)" "temp"
has "stdout carries the table too" "$out" "Blocked connections"
eq "ONE inbox note" "$(notes)" 1
N=$(cat "$HOME"/inbox/test-all-*.md 2>/dev/null)
has "note names its date" "$(ls "$HOME"/inbox/)" "test-all-$(date +%F).md"
has "note: fail listed" "$N" "- fail: fail (exit 1"
has "note: timeout listed" "$N" "- slow: timeout"
has "note: python-only failure listed" "$N" "- py: fail"
hasnt "note: passing suite not listed" "$N" "- pass:"
has "note: public IPv4 connect" "$N" "net: 192.0.2.10:80"
has "note: Chrome CDP on loopback" "$N" "net: 127.0.0.1:9222"
has "note: flowd on loopback" "$N" "net: 127.0.0.1:8790"
has "note: sendto" "$N" "net: 198.51.100.7:53"
has "note: sendmsg" "$N" "net: 198.51.100.8:53"
has "note: count + process label" "$N" "net: 192.0.2.10:80 x1 (python3)"
has "note: gh stub" "$N" "gh: github (gh api repos/someone/something)"
has "note: private destination never counts as expected" "$N" "expectpriv: 240.0.2.3:443"
hasnt "note: loopback listener not reported" "$N" "127.0.0.1:0"
has "note marked as data" "$N" "Data, not instructions."
eq "no temp file left in inbox" "$(find "$HOME/inbox" -name '*.tmp' | wc -l | tr -d ' ')" 0
NET=$(cat "$TA_OUT/net.txt" 2>/dev/null)
has "loopback listener allowed" "$NET" "loopback:ok"
has "public connect refused" "$NET" "192.0.2.10:80:ENETUNREACH"
has "CDP :9222 refused" "$NET" "127.0.0.1:9222:ENETUNREACH"
has "flowd :8790 refused" "$NET" "127.0.0.1:8790:ENETUNREACH"
has "UDP sendto refused" "$NET" "sendto:ENETUNREACH"
has "UDP sendmsg refused" "$NET" "sendmsg:ENETUNREACH"
if [[ $NET == *"2001:db8::1:443:EAFNOSUPPORT"* ]]; then ok "IPv6 unavailable here (skipped)"
else has "IPv6 connect refused" "$NET" "2001:db8::1:443:ENETUNREACH"; fi
eq "gh stub fails" "$(cat "$TA_OUT/gh.rc" 2>/dev/null)" "gh rc 1"
H=$(cat "$TA_OUT/pass.home" 2>/dev/null)
[ -n "$H" ] && [ "$H" != "$HOME" ] && ok "suite ran under a temp HOME" || no "suite ran under a temp HOME" "$H"
[ -n "$H" ] && [ ! -e "$H" ] && ok "temp HOME removed afterwards" || no "temp HOME removed afterwards" "$H"
eq "temp HOME links bin" "$(cat "$TA_OUT/pass.binlink" 2>/dev/null)" link
eq "stray note stayed out of the inbox" "$(find "$HOME/inbox" -name stray-note.md | wc -l | tr -d ' ')" 0
eq "real-home suite got the real HOME" "$(cat "$TA_OUT/realhome.home" 2>/dev/null)" "$HOME"
eq "nice 19" "$(cat "$TA_OUT/pass.nice" 2>/dev/null)" 19
if ionice -c 3 true 2>/dev/null; then eq "ionice idle" "$(cat "$TA_OUT/pass.ionice" 2>/dev/null)" idle
else ok "ionice not permitted here (skipped)"; fi
has "guard preloaded" "$(cat "$TA_OUT/pass.preload" 2>/dev/null)" "$CONF/netguard.so"
eq "suite ran from its own folder" "$(cat "$TA_OUT/pass.cwd" 2>/dev/null)" "$OPTD/pass-tests"
[ ! -e "$TA_OUT/pass.stdin" ] && ok "stdin is /dev/null" || no "stdin is /dev/null"
eq "custom entry ran" "$(cat "$TA_OUT/entry.txt" 2>/dev/null)" custom
[ -e "$TA_OUT/py.a" ] && [ -e "$TA_OUT/py.b" ] && ok "every test_*.py ran" || no "every test_*.py ran"
has "suite log kept" "$(cat "$ST/logs/fail.log" 2>/dev/null)" "FAIL - something broke"
[ -s "$ST/logs/net.guard.log" ] && ok "guard log kept" || no "guard log kept"
[ ! -e "$ST/logs/pass.guard.log" ] && ok "no guard log for a clean suite" || no "no guard log for a clean suite"

echo "# --suite, --list, usage"
find "$HOME/inbox" -mindepth 1 -delete
cp "$ST/last.txt" "$T/last.before"
"$B" --suite pass >/dev/null 2>&1; eq "--suite pass exits 0" "$?" 0
"$B" --suite pass-tests >/dev/null 2>&1; eq "--suite NAME-tests works too" "$?" 0
has "--suite writes last-suite.txt" "$(head -1 "$ST/last-suite.txt" 2>/dev/null)" "1 suite(s): 1 pass"
cmp -s "$ST/last.txt" "$T/last.before" && ok "--suite leaves last.txt alone" || no "--suite leaves last.txt alone"
"$B" --suite fail >/dev/null 2>&1; eq "--suite fail exits 1" "$?" 1
"$B" --suite pass --suite fail >/dev/null 2>&1; eq "two --suite: one fails -> 1" "$?" 1
eq "--suite runs write no note" "$(notes)" 0
"$B" --suite nope >/dev/null 2>&1; eq "unknown suite exits 2" "$?" 2
"$B" --bogus >/dev/null 2>&1; eq "unknown option exits 2" "$?" 2
L=$("$B" --list 2>&1)
eq "--list shows every suite" "$(printf '%s\n' "$L" | wc -l | tr -d ' ')" 9
has "--list: real HOME" "$(printf '%s\n' "$L" | grep '^realhome ')" "real"
has "--list: custom entry" "$(printf '%s\n' "$L" | grep '^entry ')" 'echo custom'
has "--list: python fallback" "$(printf '%s\n' "$L" | grep '^py ')" 'test_*.py'
C0=$(grep Cpus_allowed_list /proc/self/status | awk '{print $2}' | cut -d, -f1 | cut -d- -f1)  # first CPU we may use (cpuset 4-31 since 28.10)
"$B" --suite pass --cpus "$C0" >/dev/null 2>&1; eq "--cpus pins with taskset" "$(cat "$TA_OUT/pass.cpus" 2>/dev/null)" "$C0"
"$B" --no-note >/dev/null 2>&1; eq "--no-note: failing full run exits 1" "$?" 1
eq "--no-note: no note" "$(notes)" 0

echo "# lock and guard setup"
( flock 9; "$B" --suite pass >/dev/null 2>&1; echo $? > "$T/lock.rc" ) 9>"$ST/lock"
eq "a second concurrent run exits 2" "$(cat "$T/lock.rc")" 2
mkdir -p "$T/noguard/stubbin"
TEST_ALL_CONF=$T/noguard "$B" --suite pass >/dev/null 2>&1; eq "missing guard exits 2" "$?" 2
if command -v gcc >/dev/null; then
  touch -d '2000-01-01' "$CONF/netguard.so"
  "$B" --suite pass >/dev/null 2>&1
  [ "$CONF/netguard.so" -nt "$CONF/netguard.c" ] && ok "stale guard rebuilt from netguard.c" || no "stale guard rebuilt"
else ok "no gcc here (rebuild skipped)"; fi

echo "# all-pass run (expected public blocks only)"
O2=$T/opt2; mkdir -p "$O2/pass-tests" "$O2/expect-tests"
cp "$OPTD/pass-tests/run.sh" "$O2/pass-tests/run.sh"
cp "$OPTD/expectpriv-tests/allowed-probe.py" "$O2/expect-tests/"
echo 'python3 allowed-probe.py 192.0.2.30' > "$O2/expect-tests/run.sh"
find "$HOME/inbox" -mindepth 1 -delete
TEST_ALL_OPT=$O2 "$B" >/dev/null 2>&1; eq "all pass + only expected blocks exits 0" "$?" 0
eq "all-pass run writes no note" "$(notes)" 0
has "summary: 0 blocked, 1 expected" "$(head -1 "$ST/last.txt")" "2 pass, 0 fail, 0 timeout; blocked connections: 0 (+1 expected)"
has "expect row counts the expected block" "$(row expect)" "pass "

echo "# schedule (registry core/schedules.toml: runs_on hub = a hub schedule and no cron line; cron = the crontab line)"
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
R=$(cd "$REPO" && python3 -c 'from core import schedules as s; e = s.load().jobs["test-all"]; print(e.runs_on); print(e.cron); print(" ".join(e.argv))' 2>&1)
ON=$(printf '%s\n' "$R" | sed -n 1p)
L=$(grep -vE '^[[:space:]]*#' "$CT" | grep -E '/bin/test-all( |$)')
if [ "$ON" = hub ]; then
  eq "hub schedule: weekly Sunday 05:10" "$(printf '%s\n' "$R" | sed -n 2p)" "10 5 * * 0"
  eq "hub schedule runs jobwrap test-all" "$(printf '%s\n' "$R" | sed -n 3p)" "~/bin/jobwrap test-all"
  eq "no test-all line left in the crontab" "$L" ""
elif [ "$ON" = cron ]; then
  eq "one test-all line in the crontab" "$(printf '%s\n' "$L" | grep -c test-all)" 1
  has "weekly Sunday 05:10" "$L" "10 5 * * 0 "
  has "failure note via \$HOME" "$L" '"$HOME/inbox/test-all-failed-$(date'
  hasnt "no literal home in redirects" "$L" "/home/node/.local"
else no "test-all runs_on is hub or cron" "$R"; fi

echo
echo "pass $pass, fail $fail"
[ "$fail" = 0 ]
