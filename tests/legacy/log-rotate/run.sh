#!/usr/bin/env bash
# Tests for log-rotate (scripts/log-rotate, deployed to ~/bin), all on temp copies: HOME, the state dir and the crontab live in a temp dir.
# Covered: targets (crontab >> and 2>> incl. $HOME, *.log/*.err under the state dir; comments, json state files,
# symlinks and small logs untouched), cut at a line start, newest lines kept, same inode and mode, an open O_APPEND
# writer continues at the new end, --dry-run changes nothing, a second run is a no-op, --list, usage errors.
# Env: LOG_ROTATE_BIN = the copy under test (default: the repo's scripts/log-rotate; CI stages it). Exit 0 = all pass.
set -uo pipefail
LR=${LOG_ROTATE_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/log-rotate}
T=$(mktemp -d "${TMPDIR:-/tmp}/log-rotate-tests.XXXXXX")
trap 'exec 3>&- 2>/dev/null; rm -rf "$T"' EXIT
export HOME=$T LC_ALL=C.UTF-8
unset LOG_ROTATE_STATE LOG_ROTATE_CRONTAB
S=$T/.local/state
mkdir -p "$S/job/nested" "$T/logs" "$T/outside"
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
# has tests the substring in the shell: `printf | grep -q` under pipefail reads as no match when grep exits
# on the match before printf (one write per line) is done, SIGPIPE (a false FAIL)
has() { if [[ $2 == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
big() { seq -f 'line %08g padding padding padding padding' 1 50000 > "$1"; }   # ~2.4 MB, numbered lines
sum() { md5sum < "$1" | cut -c1-32; }
kb() { echo $(( $(stat -c %s "$1") / 1024 )); }

big "$S/job/cron.log"; chmod 640 "$S/job/cron.log"
big "$S/job/nested/run.err"
big "$S/job/state.json"
big "$T/logs/cron-a.log"
big "$T/logs/err-b.txt"
big "$T/logs/commented.log"
big "$T/outside/target.log"; ln -s "$T/outside/target.log" "$S/job/link.log"
seq 1 1000 > "$S/job/small.log"
head -c 2500000 /dev/zero | tr '\0' 'x' > "$S/job/oneline.log"
cat > "$T/crontab" <<EOF
PATH=/usr/bin:/bin
5 4 * * * /bin/true >> $T/logs/cron-a.log 2>&1
6 4 * * * /bin/true 2>> \$HOME/logs/err-b.txt >/dev/null
# 7 4 * * * /bin/true >> $T/logs/commented.log 2>&1
EOF
untouched="$S/job/state.json $T/logs/commented.log $T/outside/target.log $S/job/small.log"
declare -A before; for f in $untouched "$S/job/cron.log"; do before[$f]=$(sum "$f"); done
last_line=$(tail -n 1 "$S/job/cron.log")
inode=$(stat -c %i "$S/job/cron.log")
BIG=$(kb "$S/job/cron.log")

echo "# usage"
timeout 20 "$LR" --keep 300 --limit 200 </dev/null >/dev/null 2>&1; eq "--keep above --limit = usage error" "$?" "2"

echo "# --list and --dry-run"
out=$(timeout 20 "$LR" --list </dev/null)
has "--list marks a big state log" "$out" "$(printf '* %8d KB  %s' "$BIG" "$S/job/cron.log")"
has "--list shows a crontab >> target" "$out" "$T/logs/cron-a.log"
has "--list shows a crontab 2>> \$HOME target" "$out" "$T/logs/err-b.txt"
has "--list shows small logs unmarked" "$out" "        3 KB  $S/job/small.log"
eq "--list skips json, symlinks and commented lines" "$(printf '%s\n' "$out" | grep -cE 'state.json|link.log|target.log|commented.log')" "0"
out=$(timeout 20 "$LR" --dry-run </dev/null)
eq "--dry-run names 5 files" "$(printf '%s\n' "$out" | grep -c '^would trim ')" "5"
eq "--dry-run changes nothing" "$(sum "$S/job/cron.log")" "${before[$S/job/cron.log]}"

echo "# trim"
exec 3>>"$S/job/cron.log"          # a writer that keeps the log open, like cron >> or a nohup daemon
out=$(timeout 20 "$LR" </dev/null); rc=$?
eq "exit 0" "$rc" "0"
eq "one line per trimmed file" "$(printf '%s\n' "$out" | grep -c '^trimmed ')" "5"
has "reports old and new size" "$out" "trimmed $S/job/cron.log: $BIG KB -> 255 KB"
for f in "$S/job/cron.log" "$S/job/nested/run.err" "$T/logs/cron-a.log" "$T/logs/err-b.txt" "$S/job/oneline.log"; do
    n=$(kb "$f"); if [ "$n" -le 256 ] && [ "$n" -ge 250 ]; then ok "kept ~256 KB: ${f#$T/}"; else no "kept ~256 KB: ${f#$T/}" "$n KB"; fi
done
eq "starts at a line start" "$(head -n 1 "$S/job/cron.log" | grep -cE '^line [0-9]{8} padding padding padding padding$')" "1"
eq "newest line kept" "$(tail -n 1 "$S/job/cron.log")" "$last_line"
eq "every kept line is whole" "$(grep -cvE '^line [0-9]{8} padding padding padding padding$' "$S/job/cron.log")" "0"
eq "same inode" "$(stat -c %i "$S/job/cron.log")" "$inode"
eq "same mode" "$(stat -c %a "$S/job/cron.log")" "640"
size=$(stat -c %s "$S/job/cron.log")
echo "appended after the trim" >&3; exec 3>&-
eq "an open O_APPEND writer continues at the new end" "$(stat -c %s "$S/job/cron.log")" "$((size + 24))"
eq "its line is the last one" "$(tail -n 1 "$S/job/cron.log")" "appended after the trim"
eq "a line-less log keeps its raw tail" "$(tr -d 'x' < "$S/job/oneline.log" | wc -c | tr -d ' ')" "0"
for f in $untouched; do eq "untouched: ${f#$T/}" "$(sum "$f")" "${before[$f]}"; done
eq "symlink still a symlink" "$(test -L "$S/job/link.log" && echo yes)" "yes"

echo "# idempotent"
snap=$(cat "$S/job/cron.log" "$T/logs/cron-a.log" | md5sum)
out=$(timeout 20 "$LR" </dev/null)
eq "second run prints nothing" "$out" ""
eq "second run changes nothing" "$(cat "$S/job/cron.log" "$T/logs/cron-a.log" | md5sum)" "$snap"

echo "# env overrides"
mkdir -p "$T/alt"; big "$T/alt/x.log"
out=$(LOG_ROTATE_STATE=$T/alt LOG_ROTATE_CRONTAB=$T/none timeout 20 "$LR" --dry-run </dev/null)
eq "LOG_ROTATE_STATE / LOG_ROTATE_CRONTAB pick the targets" "$out" "would trim $T/alt/x.log: $BIG KB -> ~256 KB"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
