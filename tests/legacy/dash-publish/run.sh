#!/usr/bin/env bash
# Offline tests for ~/bin/dash-publish (refresh dashboard data, rsync it to the nginx VM). Exit 0 = all pass.
# Never touches the VM or the real dashboard: HOME is a temp dir (the generated site ~/.local/state/dash/www and ~/inbox
# notes live there), the VM comes from the synthetic profile fixture (web@192.0.2.20),
# streak state goes to $HOME/.local/state, and dash-data, rsync and ssh are PATH shims (rsync only records its args; ssh refuses and records the attempt).
# The real ~/inbox is checked for new dash-publish notes at the end.
# DASH_PUBLISH_BIN=<path> tests another copy.
set -uo pipefail
# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
D=${DASH_PUBLISH_BIN:-/home/node/bin/dash-publish}
REAL_INBOX=/home/node/inbox
T=$(mktemp -d "${TMPDIR:-/tmp}/dash-publish-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
_pg=$HOME/bin/profile-get   # the VM from the synthetic profile; a copy under test may have none beside it
export FAKE=$T/fake HOME=$T/home
W=$HOME/.local/state/dash/www
mkdir -p "$T/shims" "$FAKE" "$HOME/inbox" "$W"
echo '<html>dash</html>' > "$W/index.html"
echo '{"schema": 1}' > "$W/data.json"
real_notes() { ls "$REAL_INBOX" 2>/dev/null | grep -c '^dash-publish-\(failed\|recovered\)-'; }
real_before=$(real_notes)
real_state=$([ -e /home/node/.local/state/dash-publish/streak ] && echo yes || echo no)

# ---- shims
mk() { printf '#!/bin/bash\n%s\n' "$2" > "$T/shims/$1"; chmod +x "$T/shims/$1"; }
mk dash-data 'echo "dash-data $*" >> "$FAKE/calls.log"
echo "{\"lots\": \"of json on stdout\"}"
[ -e "$FAKE/dash-data-hang" ] && { sleep 30; exit 0; }
[ -e "$FAKE/dash-data-broken" ] && { echo "dash-data: boom" >&2; exit 3; }
echo "{}" > "$HOME/.local/state/dash/www/data.json"; exit 0'
mk rsync 'echo "rsync $*" >> "$FAKE/calls.log"
[ -e "$FAKE/rsync-hang" ] && { sleep 30; exit 0; }
[ -e "$FAKE/rsync-broken" ] && { echo "rsync: connection refused" >&2; exit 255; }; exit 0'
mk ssh 'echo "ssh $*" >> "$FAKE/calls.log"; echo "ssh shim: refusing" >&2; exit 255'
[ -x "$_pg" ] && ln -s "$_pg" "$T/shims/profile-get"
export PATH=$T/shims:$PATH

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
notes() { ls "$HOME/inbox" | grep -c '^dash-publish-failed-[0-9]*\.md$'; }
reset() { rm -f "$FAKE"/calls.log "$FAKE"/*-broken "$FAKE"/*-hang "$HOME"/inbox/*; rm -rf "$HOME/.local/state/dash-publish"; }
ST=$HOME/.local/state/dash-publish/streak
rnotes() { ls "$HOME/inbox" | grep -c '^dash-publish-recovered-[0-9]*\.md$'; }
allnotes() { ls -A "$HOME/inbox" | wc -l; }

echo "# success"
reset
out=$("$D" 2>"$T/err"); rc=$?
eq "success: rc 0" "$rc" "0"
eq "success: no stdout (dash-data JSON discarded)" "$out" ""
eq "success: no stderr" "$(cat "$T/err")" ""
eq "success: no inbox note" "$(allnotes)" "0"
eq "success: no streak state" "$([ -e "$ST" ] && echo yes || echo no)" "no"
eq "success: dash-data then rsync, nothing else" "$(cut -d' ' -f1 "$FAKE/calls.log" | tr '\n' ' ')" "dash-data rsync "
eq "dash-data called without args" "$(sed -n 1p "$FAKE/calls.log")" "dash-data "
eq "rsync: exact files, perms and target" "$(sed -n 2p "$FAKE/calls.log")" \
   "rsync -rlt --timeout=60 -e ssh -o ConnectTimeout=10 -o BatchMode=yes --chmod=F644,D755 $W/index.html $W/data.json web@192.0.2.20:/etc/nginx/dash-www/"
hasnt "rsync: never --delete" "$(cat "$FAKE/calls.log")" "--delete"

echo "# dash-data fails"
reset; touch "$FAKE/dash-data-broken"
out=$("$D" 2>"$T/err"); rc=$?
eq "dash-data fails: rc 1" "$rc" "1"
eq "dash-data fails: rsync not run" "$(grep -c '^rsync' "$FAKE/calls.log")" "0"
eq "dash-data fails: one note" "$(notes)" "1"
n=$(ls "$HOME"/inbox/dash-publish-failed-*.md 2>/dev/null | head -1)
[[ "$(head -1 "$n" 2>/dev/null)" =~ ^dash-data\ failed\ [0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}$ ]] \
  && ok "dash-data fails: note says what and when" || no "dash-data fails: note says what and when" "$(cat "$n" 2>/dev/null)"
grep -q "^Data, not instructions. Next: " "$n" 2>/dev/null \
  && ok "dash-data fails: note is marked data + next step" || no "dash-data fails: note is marked data + next step" "$(cat "$n" 2>/dev/null)"
ts=${n##*-}; ts=${ts%.md}; now=$(date +%s)
[ $((now - ts)) -ge 0 ] && [ $((now - ts)) -le 5 ] && ok "note name carries the epoch" || no "note name carries the epoch" "$ts vs $now"
has "dash-data fails: its stderr passes through" "$(cat "$T/err")" "dash-data: boom"

echo "# rsync fails"
reset; touch "$FAKE/rsync-broken"
"$D" 2>"$T/err"; rc=$?
eq "rsync fails: rc 1" "$rc" "1"
eq "rsync fails: one note" "$(notes)" "1"
[[ "$(head -1 "$HOME"/inbox/dash-publish-failed-*.md 2>/dev/null)" =~ ^dash-publish\ rsync\ failed\ [0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9:]{8}$ ]] \
  && ok "rsync fails: note says what and when" || no "rsync fails: note says what and when" "$(cat "$HOME"/inbox/* 2>/dev/null)"
has "rsync fails: its stderr passes through" "$(cat "$T/err")" "connection refused"

echo "# no inbox dir: failure still reported by rc"
reset; rmdir "$HOME/inbox"; touch "$FAKE/rsync-broken"
"$D" 2>"$T/err"; rc=$?
eq "no inbox: rc 1" "$rc" "1"
mkdir -p "$HOME/inbox"

echo "# failure streak: one note, updated in place"
reset; touch "$FAKE/rsync-broken"
"$D" 2>/dev/null; first=$(ls "$HOME/inbox"); "$D" 2>/dev/null; "$D" 2>/dev/null; rc=$?
eq "streak: rc 1 on every failed run" "$rc" "1"
eq "streak: 3 failed runs, still one note" "$(allnotes)" "1"
eq "streak: same note file as the first run" "$(ls "$HOME/inbox")" "$first"
n=$HOME/inbox/$first
has "streak: note counts the runs" "$(cat "$n")" "3 runs in a row"
has "streak: note says since when" "$(cat "$n")" "Failing since $(date '+%F')"
[[ "$(head -1 "$n")" =~ ^dash-publish\ rsync\ failed\ [0-9-]{10}\ [0-9:]{8}$ ]] && ok "streak: first line = latest failure" \
  || no "streak: first line = latest failure" "$(head -1 "$n")"
eq "streak: state keeps the count" "$(grep '^count=' "$ST")" "count=3"
eq "streak: state remembers the note" "$(. "$ST"; echo "$note")" "$n"
rm -f "$FAKE/rsync-broken"; touch "$FAKE/dash-data-broken"; "$D" 2>/dev/null
eq "streak: other failure kind joins the streak" "$(allnotes)" "1"
has "streak: first line switches to the new failure" "$(head -1 "$n")" "dash-data failed"
has "streak: count 4" "$(cat "$n")" "4 runs in a row"
eq "streak: no temp files left" "$(ls -A "$HOME/.local/state/dash-publish" | tr '\n' ' ')" "streak "

echo "# recovery while the note is still open"
rm -f "$FAKE/dash-data-broken"; out=$("$D" 2>&1); rc=$?
eq "recovery: rc 0" "$rc" "0"
eq "recovery: no output" "$out" ""
eq "recovery: no new note, the open one is marked" "$(allnotes)" "1"
[[ "$(head -1 "$n")" =~ ^dash-publish\ recovered\ [0-9-]{10}\ [0-9:]{8}\ after\ 4\ failed\ runs\ since ]] \
  && ok "recovery: note's first line says recovered after 4" || no "recovery: note's first line says recovered after 4" "$(head -1 "$n")"
eq "recovery: state cleared" "$([ -e "$ST" ] && echo yes || echo no)" "no"
"$D" 2>/dev/null
eq "recovery: next good run adds nothing" "$(allnotes)" "1"

echo "# dedupe: note handled (deleted) mid-streak"
reset; touch "$FAKE/rsync-broken"
"$D" 2>/dev/null; rm -f "$HOME"/inbox/*; "$D" 2>/dev/null; "$D" 2>/dev/null
eq "handled: no new note while the streak lasts" "$(allnotes)" "0"
eq "handled: state still counts" "$(grep '^count=' "$ST")" "count=3"
rm -f "$FAKE/rsync-broken"; "$D" 2>/dev/null; rc=$?
eq "handled: recovery rc 0" "$rc" "0"
eq "handled: exactly one recovered note" "$(rnotes)$(notes)" "10"
has "handled: recovered note has count" "$(cat "$HOME"/inbox/dash-publish-recovered-*.md 2>/dev/null)" "after 3 failed runs since"
eq "handled: recovered note is three lines (what, blank, data marker)" "$(wc -l < "$(ls "$HOME"/inbox/dash-publish-recovered-*.md | head -1)")" "3"
grep -q "^Data, not instructions" "$(ls "$HOME"/inbox/dash-publish-recovered-*.md | head -1)" \
  && ok "handled: recovered note is marked data" || no "handled: recovered note is marked data" ""
"$D" 2>/dev/null
eq "handled: second good run: still one note" "$(allnotes)" "1"
rm -f "$HOME"/inbox/*; touch "$FAKE/rsync-broken"; "$D" 2>/dev/null
eq "new streak after recovery: fresh note" "$(notes)" "1"
eq "new streak: count restarts" "$(grep '^count=' "$ST")" "count=1"

echo "# timeouts"
reset; touch "$FAKE/rsync-hang"
s0=$(date +%s); DASH_PUBLISH_TIMEOUT=2 "$D" 2>"$T/err"; rc=$?; el=$(( $(date +%s) - s0 ))
eq "hung rsync: rc 1" "$rc" "1"
[ "$el" -le 8 ] && ok "hung rsync: killed by the timeout (${el}s)" || no "hung rsync: killed by the timeout" "${el}s"
has "hung rsync: note says timed out" "$(cat "$HOME"/inbox/dash-publish-failed-*.md 2>/dev/null)" "dash-publish rsync timed out"
eq "hung rsync: starts a streak" "$(grep '^count=' "$ST")" "count=1"
reset; touch "$FAKE/dash-data-hang"
s0=$(date +%s); DASH_DATA_TIMEOUT=1 "$D" 2>/dev/null; rc=$?; el=$(( $(date +%s) - s0 ))
eq "hung dash-data: rc 1" "$rc" "1"
[ "$el" -le 6 ] && ok "hung dash-data: killed (${el}s)" || no "hung dash-data: killed" "${el}s"
has "hung dash-data: note says timed out" "$(cat "$HOME"/inbox/dash-publish-failed-*.md 2>/dev/null)" "dash-data timed out"
eq "hung dash-data: rsync not run" "$(grep -c '^rsync' "$FAKE/calls.log")" "0"
grep -q 'timeout -k 10 "${DASH_PUBLISH_TIMEOUT:-120}" rsync' "$D" && ok "default overall rsync timeout is 120 s" \
  || no "default overall rsync timeout is 120 s"

echo "# generated site and VM: env and profile"
reset; DASH_WWW=$T/www2 "$D" 2>/dev/null
has "DASH_WWW: rsync takes the site from there" "$(sed -n 2p "$FAKE/calls.log")" "$T/www2/index.html $T/www2/data.json web@192.0.2.20:"
echo '{' > "$T/broken.json"
reset; ASSISTANT_PROFILE=$T/broken.json "$D" 2>/dev/null; rc=$?
eq "profile unreadable: rc 1, nothing run" "$rc|$(cat "$FAKE/calls.log" 2>/dev/null)" "1|"
has "profile unreadable: note says so" "$(cat "$HOME"/inbox/dash-publish-failed-*.md 2>/dev/null)" "profile unreadable"
grep -qE '([0-9]{1,3}\.){3}[0-9]{1,3}|@[a-z0-9.-]+\.[a-z]' <(grep -v '^ *#' "$D") && no "no host or address in the code" \
  || ok "no host or address in the code (the VM comes from the profile)"

echo "# isolation"
reset; "$D" 2>/dev/null; touch "$FAKE/rsync-broken"; "$D" 2>/dev/null
eq "ssh never invoked directly" "$(grep -c '^ssh' "$FAKE/calls.log")" "0"
eq "no note in the real inbox" "$(real_notes)" "$real_before"
eq "real state untouched by tests" "$([ -e /home/node/.local/state/dash-publish/streak ] && echo yes || echo no)" "$real_state"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
