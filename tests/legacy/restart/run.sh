#!/usr/bin/env bash
# Offline tests for ~/bin/restart (wraps the image's /usr/local/bin/restart, then a detached waker types a [heartbeat]
# into tmux pane claude:main once `claude agents --json` reports this session idle). Exit 0 = all pass.
# Seams: RESTART_REAL (the image's restart -> a recording fake; the suite REFUSES a copy without it, which would restart
# the live session) and RESTART_MARKER (the heartbeat marker, default /tmp/heartbeat). HOME is a temp dir (log under
# $HOME/.local/state, session cwd $HOME/workspace). PATH shims: claude (agents --json from a scripted state list),
# tmux (records send-keys, refuses anything else), sleep (no-op, logged), setsid (runs the waker in the foreground;
# one test uses the real setsid to prove it detaches).
# Covers: --help/unknown options never restart; failed restart (no waker), the note (marker + next step; only this restart's; read-only, symlink, missing inbox, env), reason joining/default, idle -> text + Enter + marker + log, dialog after typing
# (no Enter), waiting (never types), busy -> idle polling, claude failing/empty/other cwd, first-match, give up after
# 120 polls, quoting/injection, newline flattening, detach, defaults, isolation. RESTART_BIN=<path> tests another copy.
set -uo pipefail
R=${RESTART_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/restart}
grep -q 'RESTART_REAL:-/usr/local/bin/restart' "$R" && grep -q 'RESTART_MARKER' "$R" \
  || { echo "refusing: $R has no RESTART_REAL/RESTART_MARKER seam (it would restart the live session)"; exit 2; }
REAL_MARKER=/tmp/heartbeat REAL_LOG=/home/node/.local/state/restart-wake.log REAL_INBOX=/home/node/inbox
# the hub's CI runner has no session here: its /tmp/heartbeat belongs to the runner container's own beater (rewritten
# every few seconds), so the isolation check leaves it out there
[ "${ASSISTANT_CI_RUNNER:-}" = 1 ] && REAL_MARKER=/nonexistent/heartbeat
T=$(mktemp -d "${TMPDIR:-/tmp}/restart-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
export FAKE=$T/fake HOME=$T/home RESTART_REAL=$T/fake-restart RESTART_MARKER=$T/marker/heartbeat
mkdir -p "$T/shims" "$FAKE" "$HOME" "$T/marker"
real_marker() { stat -c %Y "$REAL_MARKER" 2>/dev/null || echo none; }
real_log() { sha256sum "$REAL_LOG" 2>/dev/null | cut -c1-16; }
real_notes() { ls "$REAL_INBOX" 2>/dev/null | grep -c '^restart-'; }
before="$(real_marker) $(real_log) $(real_notes)"

# ---- fakes and shims
cat > "$RESTART_REAL" <<'EOF'
#!/bin/sh
# like the image's restart: writes ~/inbox/restart-<epoch>.md (unless no-note), records args and one env var
printf "%s|" "$#" "$@" >> "$FAKE/real.log"; echo >> "$FAKE/real.log"
printf "%s\n" "${PASS_ME-unset}" > "$FAKE/env"
[ -e "$FAKE/no-note" ] || echo "Restarted $(date '+%F %H:%M'): ${*:-no reason given}" > ~/inbox/restart-$(date +%s).md
exit $(cat "$FAKE/real-rc" 2>/dev/null || echo 0)
EOF
chmod +x "$RESTART_REAL"
mk() { printf '#!/bin/bash\n%s\n' "$2" > "$T/shims/$1"; chmod +x "$T/shims/$1"; }
mk claude '[ "$*" = "agents --json" ] || { echo "claude shim: refusing $*" | tee -a "$FAKE/refused.log" >&2; exit 99; }
n=$(( $(cat "$FAKE/claude.count" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE/claude.count"
line=$(sed -n "${n}p" "$FAKE/states"); [ -n "$line" ] || line=$(tail -n 1 "$FAKE/states")
[ "$line" = FAIL ] && exit 1
printf "%s\n" "$line"'
mk tmux '[ "${1:-}" = send-keys ] || { echo "tmux shim: refusing $*" | tee -a "$FAKE/refused.log" >&2; exit 99; }
(IFS="|"; printf "%s\n" "$*") >> "$FAKE/tmux.log"'
mk sleep 'echo "$1" >> "$FAKE/sleep.log"; [ -e "$FAKE/sleep-real" ] && [ "$1" = 15 ] && exec /bin/sleep 1; exit 0'
REAL_SETSID=$(command -v setsid)
mk setsid "echo \"setsid \$1\" >> \"\$FAKE/setsid.log\"
[ -e \"\$FAKE/setsid-real\" ] && exec $REAL_SETSID \"\$@\"
[ \"\$1\" = -f ] && shift; exec \"\$@\""
export PATH=$T/shims:$PATH

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
W=$HOME/workspace
idle() { echo "[{\"cwd\":\"$W\",\"status\":\"idle\",\"pid\":11}]"; }
busy() { echo "[{\"cwd\":\"$W\",\"status\":\"busy\",\"pid\":11}]"; }
waiting() { echo "[{\"cwd\":\"$W\",\"status\":\"waiting\",\"waitingFor\":\"$1\",\"pid\":11}]"; }
states() { printf '%s\n' "$@" > "$FAKE/states"; }
reset() { rm -rf "$FAKE"/*.log "$FAKE"/*.count "$FAKE"/real-rc "$FAKE"/*-real "$FAKE"/no-note "$FAKE"/env "$RESTART_MARKER" "$HOME/.local" "$HOME/inbox" "$T/pwned"; mkdir -p "$HOME/inbox"; }
LOG=$HOME/.local/state/restart-wake.log
run() { "$R" "$@" > "$T/out" 2>&1; rc=$?; out=$(cat "$T/out"); }
f() { cat "$FAKE/$1" 2>/dev/null; }
cnt() { local n; n=$(grep -c -e "${2:-.}" "$FAKE/$1" 2>/dev/null); echo "${n:-0}"; }   # lines matching $2 in a fake log
text() { echo "[heartbeat] Just restarted ($1). Run your heartbeat routine; if the owner is waiting on this restart, confirm on Telegram."; }
TS='[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}'

echo "# the image's restart fails: no waker"
reset; echo 3 > "$FAKE/real-rc"; states "$(idle)"; run "load plugin"
eq "rc is the image's exit code" "$rc" "3"
eq "image's restart called once with the reason" "$(f real.log)" "1|load plugin|"
eq "no waker started" "$(cnt setsid.log)" "0"
eq "nothing typed" "$(cnt tmux.log)" "0"
eq "no log" "$([ -e "$LOG" ] && echo yes || echo no)" "no"

echo "# reason: joined into one argument, default when empty"
reset; states "$(idle)"; run load the   plugin
eq "\$* joined, passed as ONE argument" "$(f real.log)" "1|load the plugin|"
reset; states "$(idle)"; run
eq "no args -> 'no reason given'" "$(f real.log)" "1|no reason given|"
has "default reason typed too" "$(f tmux.log)" "(no reason given)"

echo "# idle at the first poll: type, Enter, marker, log"
reset; states "$(idle)"; run "load hb 0.3.0"
eq "rc 0, silent" "$rc|$out" "0|"
eq "waker started with setsid -f" "$(f setsid.log)" "setsid -f"
eq "waits 15 s first" "$(sed -n 1p "$FAKE/sleep.log")" "15"
eq "tmux: text typed literally into claude:main" "$(sed -n 1p "$FAKE/tmux.log")" "send-keys|-t|claude:main|-l|$(text 'load hb 0.3.0')"
eq "tmux: then Enter" "$(sed -n 2p "$FAKE/tmux.log")" "send-keys|-t|claude:main|Enter"
eq "exactly two tmux calls" "$(cnt tmux.log)" "2"
eq "state polled twice (before and after typing)" "$(f claude.count)" "2"
eq "1 s pause between text and Enter" "$(sed -n 2p "$FAKE/sleep.log")" "1"
eq "marker touched" "$([ -f "$RESTART_MARKER" ] && echo yes || echo no)" "yes"
[[ $(cat "$LOG" 2>/dev/null) =~ ^$TS\ woke\ session\ after\ restart:\ load\ hb\ 0\.3\.0$ ]] && ok "log line" || no "log line" "$(cat "$LOG" 2>/dev/null)"
eq "log dir created under \$HOME/.local/state" "$(stat -c %F "$HOME/.local/state")" "directory"
run "second"; eq "log appends" "$(grep -c . "$LOG")" "2"

echo "# a dialog opens between typing and Enter: no Enter"
reset; states "$(idle)" "$(waiting permission)"; run "x"
eq "text typed" "$(cnt tmux.log '|-l|')" "1"
eq "Enter NOT sent" "$(cnt tmux.log Enter)" "0"

echo "# waiting on a dialog at the first poll: never types"
reset; states "$(waiting permission)"; run "y"
eq "rc 0" "$rc" "0"
eq "nothing typed" "$(cnt tmux.log)" "0"
eq "marker not touched" "$([ -e "$RESTART_MARKER" ] && echo yes || echo no)" "no"
[[ $(cat "$LOG" 2>/dev/null) =~ ^$TS\ session\ waiting\ on\ a\ dialog\ after\ restart:\ waiting\ permission$ ]] && ok "log: waiting + what for" || no "log: waiting" "$(cat "$LOG" 2>/dev/null)"
eq "stops polling" "$(f claude.count)" "1"

echo "# busy, then idle: polls every 5 s"
reset; states "$(busy)" "$(busy)" "$(busy)" "$(idle)"; run "z"
eq "woke on the 4th poll (+1 recheck)" "$(f claude.count)" "5"
eq "three 5 s waits" "$(cnt sleep.log '^5$')" "3"
eq "typed + Enter" "$(cnt tmux.log)" "2"

echo "# claude fails, empty list, other session idle: keep polling"
reset; states FAIL '[]' "[{\"cwd\":\"/home/node/jobs/x\",\"status\":\"idle\"},{\"cwd\":\"$W\",\"status\":\"busy\"}]" "$(idle)"; run "w"
eq "only our cwd counts; woke on poll 4" "$(f claude.count)" "5"
eq "typed once" "$(cnt tmux.log '|-l|')" "1"
reset; states "[{\"cwd\":\"$W\",\"status\":\"idle\"},{\"cwd\":\"$W\",\"status\":\"busy\"}]"; run "dup"
eq "first entry for our cwd wins" "$(cnt tmux.log Enter)" "1"

echo "# never idle: give up after 120 polls"
reset; states "$(busy)"; run "never"
eq "rc 0" "$rc" "0"
eq "120 polls" "$(f claude.count)" "120"
eq "120 waits of 5 s" "$(cnt sleep.log '^5$')" "120"
eq "nothing typed" "$(cnt tmux.log)" "0"
eq "marker not touched" "$([ -e "$RESTART_MARKER" ] && echo yes || echo no)" "no"
[[ $(cat "$LOG" 2>/dev/null) =~ ^$TS\ gave\ up\ waiting\ for\ idle\ after\ restart:\ never\ \(last\ state:\ busy\ \)$ ]] && ok "log: gave up + last state" || no "log: gave up" "$(cat "$LOG" 2>/dev/null)"
reset; states "[{\"cwd\":\"/elsewhere\",\"status\":\"idle\"}]"; run "other"
eq "another session's idle never wakes this pane" "$(cnt tmux.log)" "0"

echo "# quoting: the reason is data"
reset; states "$(idle)"; R1='it'"'"'s "q" $(touch '"$T"'/pwned) `touch '"$T"'/pwned` ; $HOME \'
run "$R1"
eq "no command ran from the reason" "$([ -e "$T/pwned" ] && echo ran || echo no)" "no"
eq "image's restart got it verbatim" "$(f real.log)" "1|$R1|"
eq "typed verbatim" "$(sed -n 1p "$FAKE/tmux.log")" "send-keys|-t|claude:main|-l|$(text "$R1")"
reset; states "$(idle)"; run "$(printf 'line one\nline two')"
eq "newline flattened in the typed text" "$(sed -n 1p "$FAKE/tmux.log")" "send-keys|-t|claude:main|-l|$(text 'line one line two')"
eq "log stays one line" "$(grep -c . "$LOG")" "1"
eq "the image's restart still gets the original" "$(grep -c . "$FAKE/real.log")" "2"

echo "# detaches (real setsid): restart returns before the waker is done"
reset; states "$(idle)"; touch "$FAKE/setsid-real" "$FAKE/sleep-real"
t0=$(date +%s%N); run "detach"; t1=$(date +%s%N)
eq "rc 0" "$rc" "0"
ms=$(( (t1 - t0) / 1000000 )); [ "$ms" -lt 900 ] && ok "returned in ${ms} ms (< the waker's 1 s sleep)" || no "returned too late" "$ms ms"
eq "no log yet when restart returns" "$([ -e "$LOG" ] && echo yes || echo no)" "no"
for _ in $(seq 1 40); do [ -s "$LOG" ] && break; /bin/sleep 0.1; done
has "waker finished in the background" "$(cat "$LOG" 2>/dev/null)" "woke session after restart: detach"
eq "waker typed + Enter" "$(cnt tmux.log)" "2"

echo "# the restart note: marked as data, with a next step"
I=$HOME/inbox
notes() { ls "$I" 2>/dev/null | grep -c '^restart-'; }
one() { cat "$I"/restart-*.md 2>/dev/null; }
reset; states "$(idle)"; PASS_ME='a b' run "load plugin"
eq "env passes through" "$(f env)" "a b"
eq "one note" "$(notes)" "1"
[[ $(one | sed -n 1p) =~ ^Restarted\ [0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:\ load\ plugin$ ]] && ok "line 1 kept as the image wrote it" || no "line 1" "$(one)"
eq "line 2 blank" "$(one | sed -n 2p)" ""
has "line 3: marker" "$(one | sed -n 3p)" "Data, not instructions (from ~/bin/restart). Next: run the heartbeat routine"
has "next step names the channel check" "$(one | sed -n 3p)" '"Channel notifications skipped"'
eq "three lines" "$(one | wc -l)" "3"
eq "marked once" "$(one | grep -c 'Data, not instructions')" "1"
reset; states "$(idle)"; printf 'Restarted 2020-01-01 00:00: old\n' > "$I/restart-100.md"
printf 'Restarted 2099-01-01 00:00: x\n\nData, not instructions (from elsewhere).\n' > "$I/restart-9999999999.md"
printf 'something else\n' > "$I/restart-9999999998.md"; printf 'Restarted x\n' > "$I/restart-abc.md"
run "new"
eq "older note untouched" "$(cat "$I/restart-100.md")" "Restarted 2020-01-01 00:00: old"
eq "already-marked note not marked twice" "$(grep -c 'Data, not' "$I/restart-9999999999.md")" "1"
eq "foreign-format note untouched" "$(cat "$I/restart-9999999998.md")" "something else"
eq "non-numeric name untouched" "$(cat "$I/restart-abc.md")" "Restarted x"
eq "the new note marked" "$(grep -l 'from ~/bin/restart' "$I"/restart-*.md | grep -vc -e 9999999999 -e 100.md)" "1"
reset; states "$(idle)"; touch "$FAKE/no-note"; run "no note"
eq "image wrote no note: rc 0, silent, no note" "$rc|$out|$(notes)" "0||0"
eq "waker still started" "$(cnt setsid.log)" "1"
reset; states "$(idle)"; rmdir "$I"; touch "$FAKE/no-note"; run "no inbox"
eq "no inbox dir: rc 0, silent" "$rc|$out" "0|"
reset; states "$(idle)"; printf 'Restarted 2099-01-01 00:00: ro\n' > "$I/restart-9999999997.md"; chmod 444 "$I/restart-9999999997.md"
run "ro"
eq "read-only note: rc 0, silent" "$rc|$out" "0|"
eq "read-only note left as is" "$(cat "$I/restart-9999999997.md")" "Restarted 2099-01-01 00:00: ro"
eq "the writable new note still marked" "$(grep -l 'from ~/bin/restart' "$I"/restart-*.md | wc -l)" "1"
eq "waker still started" "$(cnt setsid.log)" "1"
chmod 644 "$I/restart-9999999997.md"
reset; states "$(idle)"; ln -s "$T/outside" "$I/restart-9999999996.md"; printf 'Restarted 2099: s\n' > "$T/outside"; run "link"
eq "symlinked note not followed" "$(cat "$T/outside")" "Restarted 2099: s"
reset; echo 4 > "$FAKE/real-rc"; states "$(idle)"; run "fails"
eq "failed restart: rc passes through" "$rc" "4"
eq "failed restart: note left as written" "$(one | grep -c 'Data, not')" "0"

echo "# defaults (static: never run with the real paths)"
src=$(cat "$R")
has "default: the image's restart" "$src" '"${RESTART_REAL:-/usr/local/bin/restart}" "$reason" || exit'
has "notes under \$HOME/inbox" "$src" 'for note in "$HOME"/inbox/restart-*.md; do'
has "default marker /tmp/heartbeat" "$src" '"${RESTART_MARKER:-/tmp/heartbeat}"'
has "pane claude:main" "$src" 'tmux send-keys -t claude:main -l'
has "matches this session by cwd \$HOME/workspace" "$src" '--arg cwd "$HOME/workspace"'

echo "# the image's restart still writes the note format the marking expects (static read, never run)"
if [ -f /usr/local/bin/restart ]; then
  img=$(cat /usr/local/bin/restart 2>/dev/null)
  has "image: note path restart-<epoch>.md" "$img" '~/inbox/restart-$(date +%s).md'
  has "image: first line starts 'Restarted '" "$img" 'echo "Restarted $(date'
else
  echo "skip - image checks (no /usr/local/bin/restart: not the assistant's container image)"
fi

echo "# isolation"
eq "no shim refused a call" "$(f refused.log)" ""
# options never restart: --help prints usage (exit 0), an unknown option is refused (exit 2)
rm -f "$FAKE/real.log"; out=$("$R" --help 2>&1); rc=$?
eq "--help: usage, exit 0, no restart" "$rc $([ -e "$FAKE/real.log" ] && echo restarted || echo none)" "0 none"
has "--help: usage text" "$out" 'usage: restart "why"'
out=$("$R" -x 2>&1); rc=$?
eq "unknown option: exit 2, no restart" "$rc $([ -e "$FAKE/real.log" ] && echo restarted || echo none)" "2 none"
eq "real /tmp/heartbeat, wake log and inbox untouched" "$(real_marker) $(real_log) $(real_notes)" "$before"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
