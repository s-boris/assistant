#!/usr/bin/env bash
# Offline dry run of ~/boot.sh (container-start setup). Exit 0 = all pass.
# Runs a copy of boot.sh whose /home/node, /etc/claude-code/, /etc/cron.deny, /etc/at.deny and /usr/local/bin/ paths
# point into a temp dir, with a PATH of logging stand-ins only (shim.sh: sudo, apt-get, dpkg, curl, pip, npm, python3,
# locale, id, crontab, tmux, claude, restart and the ~/bin helpers it starts) plus the few real tools it needs
# (guard.sh: install, chmod, mkdir, mktemp, rm, ln, timeout), which refuse any absolute path outside the temp dir.
# The real boot.sh only ever gets read.
# Scenarios: fresh (nothing installed), warm (everything present, after the fresh run), apt failing, no network.
# Covers: exit 0 everywhere; a warm run makes no install calls; one apt-get update per boot (plus one after adding the
# gh repo) and none after a failed one; bounded curl calls; background steps really backgrounded (stubs sleep, boot
# returns before they end, its stdout closes); no writes outside the temp dir; never restart/claude/crontab/tmux.
# The image's start-claude is kept (~/.local/state/image-orig) before ours replaces it, and only then.
# Also a static tool-dependency lint (lint_deps.py): every command ~/bin looks up or runs must be a builtin, in ~/bin,
# in the image baseline (image-baseline.txt) or installed by boot.sh, or be allowlisted there with a reason.
# The bot-token chmod line is left to the path guard (no fake token file is made).
# BOOT_SCRIPT=<path> tests another copy.
set -uo pipefail
B=${BOOT_SCRIPT:-/home/node/boot.sh}
D=$(cd "$(dirname "$0")" && pwd)
TO=$(command -v timeout)
T=$(mktemp -d "${TMPDIR:-/tmp}/boot-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "FAIL: $*"; }
check() { if eval "$2"; then ok; else bad "$1"; fi; }

export BT_ROOT=$T BT_SHIMBIN=$T/shim BT_REALBIN=$T/realbin BT_VIOL=$T/violations.log
mkdir -p "$T/shim" "$T/realbin" "$T/guarded" "$T/present"
for c in sudo apt-get apt dpkg curl wget pip pip3 npm npx python python3 locale locale-gen id crontab tmux claude \
         restart tg systemctl service docker ssh-keygen; do ln -s "$D/shim.sh" "$T/shim/$c"; done
for c in install chmod mkdir mktemp rm ln timeout; do ln -s "$(command -v "$c")" "$T/realbin/$c"; ln -s "$D/guard.sh" "$T/guarded/$c"; done
for c in cmp grep cat sleep dirname readlink; do ln -s "$(command -v "$c")" "$T/guarded/$c"; done   # read-only tools
for c in ssh img2pdf age faketime nmap smbclient gcc pdftotext tesseract gh; do ln -s "$D/shim.sh" "$T/present/$c"; done
: > "$BT_VIOL"

# Real files boot.sh would touch if it escaped: their ctime must not change.
REAL="/etc/claude-code/managed-settings.json /usr/local/bin/start-claude /home/node/.local/bin/heartbeat /etc/locale.gen
      /home/node/.local/state/image-orig/start-claude /etc/cron.deny /etc/at.deny"
ctimes() { for f in $REAL; do printf '%s %s\n' "$f" "$(stat -c %Z "$f" 2>/dev/null)"; done; }
before=$(ctimes)

check "boot.sh parses (sh -n)" 'sh -n "$B"'
check "boot.sh ends with exit 0" '[ "$(grep -v "^[[:space:]]*\(#.*\)\?$" "$B" | tail -n 1)" = "exit 0" ]'
check "every curl has a time limit" '[ -z "$(grep -E "(^|[^-])curl " "$B" | grep -v "^[[:space:]]*#" | grep -v -- "--max-time")" ]'
check "apt-get update only in the helper" '[ "$(grep -v "^[[:space:]]*#" "$B" | grep -c "apt-get.* update")" -le 1 ]'

mkscen() {  # mkscen NAME: its own home, fake root and boot.sh copy
  S=$T/$1; H=$S/home; R=$S/root
  mkdir -p "$S/tmp" "$H/bin" "$H/plugins/telegram-plus/ops" "$H/plugins/heartbeat/ops" \
           "$H/opt" "$R/etc/claude-code" "$R/usr/local/bin"
  echo '{"x":1}' > "$H/plugins/telegram-plus/ops/managed-settings.json"
  printf '#!/bin/sh\n' > "$H/plugins/telegram-plus/ops/start-claude"; chmod 755 "$H/plugins/telegram-plus/ops/start-claude"
  printf '#!/bin/sh\n# image\n' > "$R/usr/local/bin/start-claude"   # the image's own launcher (a rebuilt container)
  printf '#!/bin/sh\n# hb\n' > "$H/plugins/heartbeat/ops/heartbeat-cron"
  for c in restart-cause whisper-server-ctl browser flowd; do ln -s "$D/shim.sh" "$H/bin/$c"; done
  sed -e "s#/home/node#$H#g" -e "s#/etc/claude-code/#$R/etc/claude-code/#g" -e "s#/usr/local/bin/#$R/usr/local/bin/#g" \
      -e "s#/etc/\(cron\|at\)\.deny#$R/etc/\1.deny#g" -e "s#/etc/\(timezone\|localtime\)#$R/etc/\1#g" "$B" > "$S/boot.copy"
}
warmup() {  # make everything "present" in scenario $S
  for v in letter-venv/bin/python readlater-venv/bin/python3 research-venv/bin/python3 bills-venv/bin/python3 tgread-venv/bin/python3 vnc-venv/bin/vncdo trackid-venv/bin/python danceloop-venv/bin/python3 dive-venv/bin/python drive-venv/bin/python trackinfo-venv/bin/python3; do
    mkdir -p "$(dirname "$H/opt/$v")"; ln -sf "$D/shim.sh" "$H/opt/$v"; done
  mkdir -p "$H/opt/danceloop-models"; echo model > "$H/opt/danceloop-models/pose_landmarker_lite.task"; echo model > "$H/opt/danceloop-models/pose_landmarker_full.task"
  cp "$H/plugins/telegram-plus/ops/managed-settings.json" "$R/etc/claude-code/"
  cp "$H/plugins/telegram-plus/ops/start-claude" "$R/usr/local/bin/"
  mkdir -p "$H/.local/bin"; cp "$H/plugins/heartbeat/ops/heartbeat-cron" "$H/.local/bin/heartbeat"
  echo evalbot > "$R/etc/cron.deny"; echo evalbot > "$R/etc/at.deny"   # the sudo stub never writes them
  ln -sfn /usr/share/zoneinfo/Etc/UTC "$R/etc/localtime"; echo Etc/UTC > "$R/etc/timezone"   # zone files agree
}
runboot() {  # runboot MODE [VAR=VAL...]: run the copy, set RC, SECS, LOG; stdout captured (proves it closes)
  local mode=$1; shift
  LOG=$S/calls.$mode.log; BG=$S/bg.$mode.log; : > "$LOG"; : > "$BG"
  local path="$T/shim:$T/guarded"; [ "$mode" = warm ] && path="$T/shim:$T/present:$T/guarded"
  local t0=$EPOCHREALTIME
  OUT=$(env -i HOME="$H" PATH="$path" TMPDIR="$S/tmp" BT_ROOT="$BT_ROOT" BT_SHIMBIN="$BT_SHIMBIN" BT_REALBIN="$BT_REALBIN" \
        BT_VIOL="$BT_VIOL" BT_LOG="$LOG" BT_BG="$BG" BT_MODE="$mode" BT_BG_SLEEP="${BG_SLEEP:-0}" "$@" \
        "$TO" 60 /bin/sh "$S/boot.copy" 2>&1); RC=$?
  SECS=$(awk -v a="$t0" -v b="$EPOCHREALTIME" 'BEGIN { printf "%.2f", b - a }')
}
waitbg() {  # waitbg N: wait (max 15 s) until N background stubs ended
  local i; for i in $(seq 1 150); do [ "$(grep -c '^end ' "$BG")" -ge "$1" ] && return 0; sleep 0.1; done; return 1
}
lt() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a < b) }'; }
INST='^(sudo|apt-get|curl|pip|npm|python3? .*-m venv|real install)( |$)'
installs() { grep -cE "$INST" "$LOG"; }
updates() { grep -c '^sudo .*apt-get .*update' "$LOG"; }

# 1. fresh container: nothing installed, network up; background stubs sleep 2 s
mkscen fresh
BG_SLEEP=2 runboot fresh
echo "fresh: rc=$RC ${SECS}s, $(installs) install calls, $(updates) apt-get update"
check "fresh: exit 0" '[ "$RC" = 0 ]'
check "fresh: no output" '[ -z "$OUT" ]'
check "fresh: returns before the 2 s background stubs end (${SECS}s)" 'lt "$SECS" 1.8'
check "fresh: no background stub ended before boot returned" '! grep -q "^end " "$BG"'
check "fresh: 12 background starts (4 helpers, 8 venv rebuilds)" 'waitbg 12 && [ "$(grep -c "^start " "$BG")" = 12 ]'
check "fresh: installs packages" '[ "$(installs)" -ge 5 ]'
check "fresh: apt-get update once + once after the gh repo ($(updates))" '[ "$(updates)" = 2 ]'
check "fresh: chrome downloaded into TMPDIR" 'grep -q "^curl .*-o $S/tmp/" "$LOG"'
check "fresh: every curl call bounded" '[ -z "$(grep "^curl " "$LOG" | grep -v -- "--max-time")" ]'
check "fresh: heartbeat wrapper installed in the temp home" 'cmp -s "$H/.local/bin/heartbeat" "$H/plugins/heartbeat/ops/heartbeat-cron"'
IMGSC=$S/image-start-claude; cp "$R/usr/local/bin/start-claude" "$IMGSC"
check "fresh: the image's start-claude kept before ours is installed" 'cmp -s "$H/.local/state/image-orig/start-claude" "$IMGSC"'

# 2. warm: same home, every tool present now (second boot)
warmup
runboot warm
echo "warm: rc=$RC ${SECS}s, $(installs) install calls"
check "warm: exit 0" '[ "$RC" = 0 ]'
check "warm: no install calls" '[ "$(installs)" = 0 ] || { grep -E "$INST" "$LOG" | head -5; false; }'
check "warm: under 2 s (${SECS}s)" 'lt "$SECS" 2'
check "warm: 4 background helpers started" 'waitbg 4 && [ "$(grep -c "^start " "$BG")" = 4 ]'
runboot warm; check "warm: third run makes no install calls either" '[ "$(installs)" = 0 ] && waitbg 4'
check "warm: ours installed, so the kept image copy stays the image's" 'cmp -s "$H/.local/state/image-orig/start-claude" "$IMGSC"'
echo Etc/OLD > "$R/etc/timezone"; runboot warm   # /etc/timezone disagrees with /etc/localtime: one sudo tee, no output
check "warm: /etc/timezone set from /etc/localtime when they differ" '[ "$RC" = 0 ] && [ -z "$OUT" ] && [ "$(grep -c "^sudo tee $R/etc/timezone$" "$LOG")" = 1 ] && waitbg 4'
echo Etc/UTC > "$R/etc/timezone"

# 3. apt failing
mkscen aptfail
runboot fresh BT_APT_FAIL=1
echo "apt failing: rc=$RC ${SECS}s, $(updates) apt-get update"
check "apt failing: exit 0" '[ "$RC" = 0 ]'
check "apt failing: under 3 s (${SECS}s)" 'lt "$SECS" 3'
check "apt failing: one apt-get update, no install after it" '[ "$(updates)" = 1 ] && ! grep -q "^sudo .*apt-get .*install" "$LOG"'
check "apt failing: helpers + letter venv still start, apt-dependent venvs skipped" 'waitbg 5 && sleep 0.2 && [ "$(grep -c "^start " "$BG")" = 5 ]'

# 4. no network
mkscen nonet
runboot fresh BT_NET=0
echo "no network: rc=$RC ${SECS}s, $(updates) apt-get update"
check "no network: exit 0" '[ "$RC" = 0 ]'
check "no network: under 3 s (${SECS}s)" 'lt "$SECS" 3'
check "no network: at most one apt-get update" '[ "$(updates)" -le 1 ]'
check "no network: chrome temp dir cleaned up" '[ -z "$(ls -A "$S/tmp")" ]'
check "no network: helpers + letter venv still start" 'waitbg 5 && sleep 0.2 && [ "$(grep -c "^start " "$BG")" = 5 ]'

# Everywhere
check "never wrote or reached outside the temp dir, never called restart/claude/crontab/tmux" \
  '[ ! -s "$BT_VIOL" ] || { head -5 "$BT_VIOL"; false; }'
check "no real home path left in the copies" '! grep -q /home/node <<< "$(sed "s#$T##g" "$T"/*/boot.copy)"'   # $T may sit under the home (CI)
check "real files untouched" '[ "$(ctimes)" = "$before" ]'

# 5. Static tool-dependency lint (lint_deps.py): every command ~/bin looks up or runs comes from the image baseline,
#    boot.sh, ~/bin or the lint's allowlist. Each of its checks counts here; a fixture calling nonexistent tools fails it.
# Budget = CPU seconds (user + sys), not wall time: test-all runs suites at nice 19 / idle IO, often on the E-cores,
# so wall time measured the host's load (29.10: 6.0 s wall at 4.9 s CPU, red). ~5-7 s CPU today (200 Python files
# parsed); 15 s still catches a runaway lint.
TIMEFORMAT='%3U %3S'
{ time { LINT=$("$TO" 30 python3 -I "$D/lint_deps.py" --boot "$B" </dev/null 2>&1); LRC=$?; }; } 2>"$T/lint.cpu"
unset TIMEFORMAT
SECS=$(awk '{ printf "%.2f", $1 + $2 }' "$T/lint.cpu")
echo "dep lint: ${SECS}s CPU"
printf '%s\n' "$LINT" | grep -v '^ok: '     # summary, skipped counts, failures with their details
n=0
while IFS= read -r l; do
  case $l in "ok: "*) ok; n=$((n + 1));; "FAIL: "*) fail=$((fail + 1)); n=$((n + 1));; esac
done <<EOF
$LINT
EOF
check "dep lint: exit 0 with all 7 checks reported ($n)" '[ "$LRC" = 0 ] && [ "$n" = 7 ]'
check "dep lint: under 15 s CPU (${SECS}s)" '[ -n "$SECS" ] && lt "$SECS" 15'
NEG=$("$TO" 30 python3 -I "$D/lint_deps.py" --bin "$D/fixtures/missing-tool" --boot "$B" --only provided </dev/null 2>&1)
NRC=$?
check "dep lint: fixture calling nonexistent tools fails the lint (rc=$NRC)" '[ "$NRC" = 1 ]'
check "dep lint: all 4 nonexistent fixture tools reported (command -v, which(), argv, helper argv)" \
  '[ "$(printf "%s\n" "$NEG" | grep -cE "^  no-such-tool-(sh|py|which|helper): ")" = 4 ]'
check "dep lint: fixture decoys in comments, strings and heredocs not reported" '! grep -q decoy <<<"$NEG"'
sed -E 's/(^|[^[:alnum:]_.-])ffmpeg([^[:alnum:]_.-]|$)/\1\2/g' "$B" > "$T/boot.noffmpeg"   # drift: ffmpeg dropped
DRIFT=$("$TO" 30 python3 -I "$D/lint_deps.py" --boot "$T/boot.noffmpeg" </dev/null 2>&1); DRC=$?
check "dep lint: a boot.sh without ffmpeg fails it (ffprobe unprovided, map entry stale)" \
  '[ "$DRC" = 1 ] && grep -q "^  ffprobe: " <<<"$DRIFT" && grep -q "^  ffmpeg: not in" <<<"$DRIFT"'

echo "boot-tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
