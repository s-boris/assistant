#!/usr/bin/env bash
# Offline tests for ~/bin/unraid-update-check (daily: tell the owner ONCE per new stable Unraid release). Exit 0 = all pass.
# HOME is a temp dir (state: $HOME/.local/state/unraid-health/update-notified). Nothing leaves the box:
#   ssh  -> PATH shim that runs the remote sed locally against a fixture /etc/unraid-version (refuses anything else),
#   unraid-ssh (UNRAID_SSH_BIN) -> fake with mode root|verbs|emulate and `run version`,
#   curl -> PATH shim that records the URL and serves a fixture answer, tg -> PATH shim that records the message.
# Covers up to date, new release (exact message), once per version, the next release, unreachable server, curl
# failure, junk answers (empty, HTML, error JSON) never touching the state, failed tg retried, missing release
# notes, verbs/emulate mode, default unraid-ssh path, the release-site failure streak (ONE ~/inbox note on the 3rd failed
# run in a row, Recovered on the next good answer; state $HOME/.local/state/unraid-update-check/).
# UNRAID_UPDATE_CHECK_BIN=<path> tests another copy.
# Synthetic profile (tests never read the real one): tests/legacy/profile once that suite moves here, else ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$HOME/opt/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
set -uo pipefail
U=${UNRAID_UPDATE_CHECK_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/unraid-update-check}
REAL_STATE=/home/node/.local/state/unraid-health/update-notified
T=$(mktemp -d "${TMPDIR:-/tmp}/unraid-update-check-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
_pg=$HOME/bin/profile-get   # names the server from the synthetic profile; a copy under test may have none beside it
export FAKE=$T/fake HOME=$T/home UNRAID_SSH_BIN=$T/shims/fake-unraid-ssh
mkdir -p "$T/shims" "$FAKE" "$HOME"
[ -x "$_pg" ] && ln -s "$_pg" "$T/shims/profile-get"
real_sum() { sha256sum "$REAL_STATE" 2>/dev/null | cut -d' ' -f1; }
real_before=$(real_sum)
real_notes() { find /home/node/inbox -maxdepth 1 -name 'unraid-update-check-failing-*' 2>/dev/null | wc -l; }
real_notes_before=$(real_notes)

# ---- shims
mk() { printf '#!/bin/bash\n%s\n' "$2" > "$T/shims/$1"; chmod +x "$T/shims/$1"; }
mk ssh 'echo "ssh $*" >> "$FAKE/calls.log"
[ -e "$FAKE/ssh-broken" ] && { echo "ssh: connect to host unraid: No route to host" >&2; exit 255; }
args=("$@"); host=${args[-2]}; cmd=${args[-1]}
[ "$host" = unraid ] || { echo "ssh shim: unexpected host $host" >&2; exit 99; }
cmd=${cmd//\/etc\/unraid-version/$FAKE/unraid-version}
case $cmd in *" /etc/"*|*"/boot"*|*"/mnt"*) echo "ssh shim: refusing $cmd" >&2; exit 98 ;; esac
exec sh -c "$cmd"'
mk fake-unraid-ssh 'echo "us $*" >> "$FAKE/calls.log"
[ -e "$FAKE/us-mode-broken" ] && exit 255
case $1 in
  mode) cat "$FAKE/mode" 2>/dev/null || echo root ;;
  run) [ "$2" = version ] || { echo "claude-bot-cmd: rejected" >&2; exit 1; }; cat "$FAKE/unraid-version" ;;
  *) exit 2 ;;
esac'
mk curl 'echo "curl $*" >> "$FAKE/calls.log"
[ -e "$FAKE/curl-broken" ] && { echo "curl: (22) The requested URL returned error: 503" >&2; exit 22; }
cat "$FAKE/answer"'
mk tg 'echo "tg $*" >> "$FAKE/calls.log"
[ -e "$FAKE/tg-broken" ] && { echo "curl: (6) Could not resolve host" >&2; exit 6; }
printf "%s\n" "$*" >> "$FAKE/tg.log"
printf "%s\n" "${TG_SOFT:-}" > "$FAKE/tg.soft"'
export PATH=$T/shims:$PATH

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
ST=$HOME/.local/state/unraid-health/update-notified
state() { cat "$ST" 2>/dev/null || echo "<none>"; }
sent() { [ -f "$FAKE/tg.log" ] && wc -l < "$FAKE/tg.log" || echo 0; }
calls() { cat "$FAKE/calls.log" 2>/dev/null; }
clear() { rm -f "$FAKE"/calls.log "$FAKE"/tg.log "$FAKE"/*-broken "$FAKE"/mode; }
reset() { clear; rm -rf "$HOME/.local"; }
server() { printf 'version="%s"\nbuild="1"\n' "$1" > "$FAKE/unraid-version"; }
answer() { printf '%s\n' "$1" > "$FAKE/answer"; }
newer() { answer "{\"basefile\":\"unRAIDServer-$1-x86_64.zip\",\"version\":\"$1\",\"isNewer\":true,\"isEligible\":true,\"changelogPretty\":\"https://docs.unraid.net/unraid-os/release-notes/$1/\"}"; }
current() { answer "{\"version\":\"$1\",\"isNewer\":false,\"isEligible\":true}"; }
run() { "$U" > "$T/out" 2>&1; }

echo "# up to date"
reset; server 7.3.3; current 7.3.3
run; rc=$?
eq "up to date: rc 0, silent" "$rc:$(cat "$T/out")" "0:"
eq "up to date: no message" "$(sent)" "0"
eq "up to date: state = running version (state dir created)" "$(state)" "7.3.3"
eq "root mode: asks the lockdown mode, then one ssh" "$(calls | cut -d' ' -f1 | tr '\n' ' ')" "us ssh curl "
has "root mode: ssh is batch with a connect timeout" "$(calls | grep '^ssh')" "ssh -o BatchMode=yes -o ConnectTimeout=10 unraid sed -n"
eq "curl: Unraid's stable endpoint with the running version, fails on HTTP errors, 20 s cap" "$(calls | grep '^curl')" \
   "curl -fsS --max-time 20 https://releases.unraid.net/os?branch=stable&current_version=7.3.3"

echo "# a new release"
clear; newer 7.3.4
run; rc=$?
eq "new release: rc 0" "$rc" "0"
eq "new release: one message" "$(sent)" "1"
eq "new release: exact message" "$(cat "$FAKE/tg.log")" \
   "🖥️ Unraid 7.3.4 is out (Testsrv runs 7.3.3). Release notes: https://docs.unraid.net/unraid-os/release-notes/7.3.4/ — update via Tools › Update OS when convenient (reboots the server, I'm offline for a few minutes)."
eq "new release: remembered" "$(state)" "7.3.4"
eq "update notice is non-urgent (TG_SOFT=1)" "$(cat "$FAKE/tg.soft" 2>/dev/null)" "1"
run; run
eq "same release again: no second message" "$(sent)" "1"
newer 7.4.0; run
eq "the next release: told again" "$(sent)" "2"
has "the next release: about 7.4.0" "$(tail -1 "$FAKE/tg.log")" "Unraid 7.4.0 is out (Testsrv runs 7.3.3)"
eq "the next release: remembered" "$(state)" "7.4.0"
clear; server 7.4.0; current 7.4.0; run
eq "after he updated: up to date, state = running version" "$(sent):$(state)" "0:7.4.0"

echo "# server unreachable or odd"
reset; mkdir -p "$(dirname "$ST")"; echo 7.3.4 > "$ST"; touch "$FAKE/ssh-broken"; newer 7.3.5
run; rc=$?
eq "ssh down: rc 0, silent (unraid-health reports that)" "$rc:$(cat "$T/out")" "0:"
eq "ssh down: no curl, no message" "$(calls | grep -c '^curl\|^tg')" "0"
eq "ssh down: state untouched" "$(state)" "7.3.4"
clear; printf 'build="1"\n' > "$FAKE/unraid-version"
run; eq "no version line: no curl, state untouched" "$(calls | grep -c '^curl'):$(state)" "0:7.3.4"
clear; server 7.3.3; touch "$FAKE/curl-broken"
run; rc=$?
eq "curl fails: rc 0, no message, state untouched" "$rc:$(sent):$(state)" "0:0:7.3.4"

echo "# junk answers never touch the state (else a release he knows about is announced again)"
for junk in '' '<html><body>Bad gateway</body></html>' '{"error":"rate limited"}' '[1,2]'; do
  clear; server 7.3.3; answer "$junk"
  run; rc=$?
  eq "junk '${junk:0:20}': rc 0, no message, state kept" "$rc:$(sent):$(state)" "0:0:7.3.4"
done
clear; newer 7.3.4; run
eq "junk then the known release again: still no repeat" "$(sent)" "0"

echo "# a failed message is retried"
reset; server 7.3.3; newer 7.3.4; touch "$FAKE/tg-broken"
run; rc=$?
eq "tg fails: not remembered" "$(state)" "<none>"
eq "tg fails: it did try" "$(calls | grep -c '^tg')" "1"
rm -f "$FAKE/tg-broken"; run
eq "next run: told, then remembered" "$(sent):$(state)" "1:7.3.4"

echo "# release notes missing"
reset; server 7.3.3; answer '{"version":"7.3.4","isNewer":true}'
run
eq "no changelogPretty: still one message" "$(sent)" "1"
hasnt "no changelogPretty: never 'null' in the message" "$(cat "$FAKE/tg.log")" "null"
eq "no changelogPretty: clean wording" "$(cat "$FAKE/tg.log")" \
   "🖥️ Unraid 7.3.4 is out (Testsrv runs 7.3.3). Update via Tools › Update OS when convenient (reboots the server, I'm offline for a few minutes)."

echo "# verbs and emulate mode (owner's SSH lockdown)"
for m in verbs emulate; do
  reset; echo $m > "$FAKE/mode"; server 7.3.3; newer 7.3.4
  run; rc=$?
  eq "$m: rc 0, one message" "$rc:$(sent)" "0:1"
  eq "$m: the version verb, no ssh" "$(calls | grep -c '^us run version$'):$(calls | grep -c '^ssh')" "1:0"
  has "$m: version parsed from the verb's output" "$(calls | grep '^curl')" "current_version=7.3.3"
done
reset; echo verbs > "$FAKE/mode"; printf 'nonsense\n' > "$FAKE/unraid-version"; newer 7.3.4
run; eq "verbs, unparsable answer: no curl" "$(calls | grep -c '^curl')" "0"
reset; touch "$FAKE/us-mode-broken"; server 7.3.3; current 7.3.3
run; eq "unraid-ssh broken: falls back to root ssh" "$(calls | grep -c '^ssh'):$(state)" "1:7.3.3"
reset; server 7.3.3; current 7.3.3
UNRAID_SSH_BIN=$T/nope "$U" > /dev/null 2>&1; eq "unraid-ssh missing: root ssh" "$(calls | grep -c '^ssh'):$(state)" "1:7.3.3"
reset; mkdir -p "$HOME/bin"; cp "$T/shims/fake-unraid-ssh" "$HOME/bin/unraid-ssh"; echo verbs > "$FAKE/mode"
server 7.3.3; current 7.3.3
env -u UNRAID_SSH_BIN "$U" > /dev/null 2>&1
eq "default unraid-ssh is \$HOME/bin/unraid-ssh" "$(calls | grep -c '^us run version$')" "1"

echo "# release site failing: ONE note on the 3rd failed run in a row, Recovered on the next good answer"
FD=$HOME/.local/state/unraid-update-check
own_out() { grep -v '^curl: (22)' "$T/out"; }   # curl's own error line was there before
failing() { find "$HOME/inbox" -maxdepth 1 -name 'unraid-update-check-failing-*.md' 2>/dev/null | wc -l; }
fnote() { find "$HOME/inbox" -maxdepth 1 -name 'unraid-update-check-failing-*.md' 2>/dev/null | sort | head -1; }
count() { head -1 "$FD/fails" 2>/dev/null || echo none; }
reset; find "$HOME" -maxdepth 1 -name inbox -exec rm -rf {} +; server 7.3.3; current 7.3.3; run
eq "good answer, no streak: no counter, no note" "$(count):$(failing)" "none:0"
touch "$FAKE/curl-broken"; run; rc=$?
eq "fail 1 (curl): rc 0, silent, no note" "$rc:$(own_out):$(failing)" "0::0"
eq "fail 1: counted" "$(count)" "1"
rm -f "$FAKE/curl-broken"; answer '<html>Bad gateway</html>'; run; rc=$?
eq "fail 2 (junk): rc 0, no note" "$rc:$(failing):$(count)" "0:0:2"
answer ''; run; rc=$?
eq "fail 3 (empty): rc 0, silent, ONE note" "$rc:$(cat "$T/out"):$(failing)" "0::1"
n1=$(fnote)
has "note: endpoint and count" "$(cat "$n1")" "no usable answer from releases.unraid.net 3 runs in a row (since $(date +%F); last: empty answer)"
has "note: the manual check uses the running version" "$(cat "$n1")" "current_version=7.3.3"
has "note: data, not instructions" "$(cat "$n1")" "Data, not instructions."
eq "note path remembered" "$(cat "$FD/note")" "$n1"
touch "$FAKE/curl-broken"; run; run; answer '{"error":"rate limited"}'; rm -f "$FAKE/curl-broken"; run; rc=$?
eq "fails 4-6: rc 0, still one note, no message, state kept" "$rc:$(failing):$(sent):$(state)" "0:1:0:7.3.3"
eq "fails 4-6: counted" "$(count)" "6"
has "curl failure: exit code in the streak" "$(grep -c . "$FD/fails")" "2"
current 7.3.3; run; rc=$?
eq "good answer: rc 0, silent" "$rc:$(cat "$T/out")" "0:"
eq "good answer: streak reset" "$(count):$(ls -A "$FD")" "none:"
if grep -Eq '^Recovered [0-9-]{10} [0-9:]{8} after 6 failed run\(s\)\.$' <<<"$(tail -1 "$n1")"; then ok "good answer: Recovered appended"
else no "good answer: Recovered appended" "$(tail -2 "$n1")"; fi
run; eq "recovered once only" "$(grep -c '^Recovered' "$n1")" "1"
touch "$FAKE/curl-broken"; run; run; run
eq "a new streak: a second note" "$(failing)" "2"
find "$HOME/inbox" -maxdepth 1 -name 'unraid-update-check-failing-*' -delete; run
eq "note deleted: the streak stays silent" "$(failing):$(count)" "0:4"
rm -f "$FAKE/curl-broken"; run; rc=$?
eq "note deleted, then good: rc 0, reset, no new note" "$rc:$(count):$(failing)" "0:none:0"
echo "# server unreachable neither counts nor resets; tg failing is not a fetch failure"
clear; touch "$FAKE/curl-broken"; run; touch "$FAKE/ssh-broken"; run; run; run
eq "ssh down: not counted, not reset" "$(count):$(failing)" "1:0"
rm -f "$FAKE/ssh-broken"; run; run
eq "then 2 more failures: the note" "$(count):$(failing)" "3:1"
clear; newer 7.3.9; touch "$FAKE/tg-broken"; run; rc=$?
eq "good answer, tg fails: streak reset, exit as before (non-zero)" "$(count):$([ "$rc" -ne 0 ] && echo nonzero)" "none:nonzero"
echo "# the counter never changes the exit code"
reset; find "$HOME" -maxdepth 1 -name inbox -exec rm -rf {} +; mkdir -p "$HOME/.local/state"; : > "$FD"
server 7.3.3; touch "$FAKE/curl-broken"; run; run; run; rc=$?
eq "state dir not creatable: rc 0, silent, no note" "$rc:$(own_out):$(failing)" "0::0"
rm -f "$FD"; mkdir -p "$FD"; printf 'garbage\n\n' > "$FD/fails"; run
eq "corrupt counter: starts over at 1" "$(count)" "1"
: > "$HOME/inbox"; run; run; rc=$?
eq "inbox not writable: rc 0, silent" "$rc:$(own_out)" "0:"
rm -f "$HOME/inbox"

echo "# isolation"
eq "no note in the real inbox" "$(real_notes)" "$real_notes_before"
eq "real state untouched" "$(real_sum)" "$real_before"
eq "every message went to the tg shim" "$(command -v tg)" "$T/shims/tg"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
