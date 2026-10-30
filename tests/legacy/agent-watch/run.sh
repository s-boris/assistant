#!/usr/bin/env bash
# Tests for ~/bin/agent-watch. Exit 0 = all pass.
# Synthetic transcripts (fixture.py) in a temp project dir, temp state and inbox, a stub tg that logs instead of
# texting, and AGENT_WATCH_NOW as a fake clock. Every fixture message carries SECRET-CONTENT-XYZZY; the suite
# checks it never leaks into output, notes, texts or state. Real ~/.local/state/agent-watch and ~/inbox untouched
# (every file there listed with its mtime before and after; see real_snap).
set -uo pipefail
AW=${AGENT_WATCH_BIN:-${AW:-/home/node/bin/agent-watch}}   # AGENT_WATCH_BIN (or AW)=/path/to/copy tests a copy
HERE=$(cd "$(dirname "$0")" && pwd)
FX="python3 -I $HERE/fixture.py"
T=$(mktemp -d "${TMPDIR:-/tmp}/agent-watch-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
export AGENT_WATCH_PROJECT=$T/proj AGENT_WATCH_STATE_DIR=$T/state AGENT_WATCH_INBOX=$T/inbox AGENT_WATCH_TG=$T/tg
export AGENT_TITLES_FILE=$T/titles.json   # renamed agents (lib/agent_titles.py); never the real file
# the redactor: this tree's secretscan (its token patterns need no secrets sources); AGENT_WATCH_SECRETSCAN overrides
export AGENT_WATCH_SECRETSCAN=${AGENT_WATCH_SECRETSCAN:-$(cd "$HERE/../../../scripts" && pwd)/secretscan_py}
unset AGENT_WATCH_NOW AGENT_WATCH_WINDOW_MIN AGENT_WATCH_STALL_MIN AGENT_WATCH_PROGRESS_MIN
cat > "$T/tg" <<STUB
#!/bin/sh
[ -e "$T/tg-fail" ] && exit 1
printf '%s\n' "\$*" >> "$T/tg.log"
printf '%s\n' "\${TG_SOFT:-}" > "$T/tg.soft"
STUB
chmod +x "$T/tg"
# Where agent-watch and the titles lib write when an override above is not honoured: their defaults under the HOME
# this suite runs with and, when that is another dir, the host's (the live cron writes there every 5 min).
REAL_HOMES=("$HOME"); [ "$HOME" != /home/node ] && REAL_HOMES+=(/home/node)
real_snap() {  # path, mtime, size: every file in the state dir, every agent-watch note (and its temp file) in the inbox
  local h
  for h in "${REAL_HOMES[@]}"; do
    find "$h/.local/state/agent-watch" -maxdepth 1 -type f -printf '%p %T@ %s\n' 2>/dev/null
    find "$h/inbox" -maxdepth 1 -type f \( -name 'agent-watch-*' -o -name '.agent-watch-*' \) -printf '%p %T@ %s\n' 2>/dev/null
  done | sort
}
titles_snap() { local h; for h in "${REAL_HOMES[@]}"; do [ -f "$h/.local/state/agent-titles/titles.json" ] && echo "$h $(md5sum < "$h/.local/state/agent-titles/titles.json")"; done; }
real_before=$(real_snap)
titles_before=$(titles_snap)
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
# has/hasnt test the substring in the shell: `printf | grep -q` under pipefail reads as no match when grep exits
# on the match before printf (one write per line) is done, SIGPIPE (a false FAIL, or a false pass for hasnt)
has() { if [[ $2 == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ $2 == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
ep() { python3 -c 'import sys; from datetime import datetime; from zoneinfo import ZoneInfo; print(int(datetime.strptime(sys.argv[1], "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Europe/Zurich")).timestamp()))' "$1"; }
BASE=$(ep "2026-10-12 10:00")          # a Monday, 10:00 Zurich
m() { echo $(( BASE + $1 * 60 )); }     # epoch at BASE + N minutes
at() { NOW=$(m "$1"); }
aw() { AGENT_WATCH_NOW="$NOW" timeout 20 "$AW" "$@"; }
id() { printf 'a%s%0*d' "$1" $((16 - ${#1})) 1; }   # id b1 -> ab100000000000001
reset() { cat "$T"/inbox/* "$T"/tg.log "$T"/state/* >> "$T/all-output.txt" 2>/dev/null
          rm -rf "$T/proj" "$T/state" "$T/inbox" "$T/tg.log" "$T/tg-fail"; mkdir -p "$T/proj"; }
notes() { ls "$T/inbox" 2>/dev/null | grep -c "${1:-.}" ; }
tgn() { [ -f "$T/tg.log" ] && wc -l < "$T/tg.log" | tr -d ' ' || echo 0; }
state_of() { aw status | awk -v id="$1" '$1 == id {print $2}'; }
ALL_OUT=$T/all-output.txt
cap() { tee -a "$ALL_OUT"; }

echo "# A. missing dirs, help"
reset; rm -rf "$T/proj"; at 0
out=$(aw run 2>&1); eq "run with no project dir: rc 0, silent" "$?/$out" "0/"
out=$(aw status 2>&1 | cap); has "status with no project dir" "$out" "(none)"
[ -d "$T/inbox" ] && no "inbox not created when nothing happened" || ok "inbox not created when nothing happened"
aw help >/dev/null; eq "help rc 0" "$?" "0"
aw bogus >/dev/null 2>&1; eq "unknown command rc 2" "$?" "2"

echo "# B. classification + one note per stall"
reset; at 0
$FX agent "$T/proj" S1 "$(id 1)" tool_use "$(m -2)" --start "$(m -10)" --desc "Running one"
$FX agent "$T/proj" S1 "$(id 2)" final "$(m -30)" --desc "Finished one"
$FX agent "$T/proj" S1 "$(id 3)" interrupted "$(m -40)"
$FX agent "$T/proj" S1 "$(id 4)" tool_result "$(m -16)" --start "$(m -50)" --desc "Stuck one"
$FX agent "$T/proj" S1 "$(id 5)" thinking "$(m -200)" --desc "Too old"
$FX agent "$T/proj" S1 "$(id 6)" final "$(m -20)" --trailing-attachment
$FX agent "$T/proj" S1 "$(id 7)" tool_use "$(m -1)" --partial
$FX agent "$T/proj" S1 "$(id 8)" final "$(m -5)" --big 700000
$FX agent "$T/proj" S1 "$(id 9)" thinking "$(m -3)" --no-meta
st=$(aw status | cap)
eq "running (tool_use, 2 min silent)" "$(state_of "$(id 1)")" "running"
eq "done (end_turn text)" "$(state_of "$(id 2)")" "done"
eq "interrupted marker" "$(state_of "$(id 3)")" "interrupted"
eq "stalled (16 min silent)" "$(state_of "$(id 4)")" "stalled"
hasnt "agents outside the 2 h window are ignored" "$st" "$(id 5)"
eq "trailing attachment record skipped -> done" "$(state_of "$(id 6)")" "done"
eq "half-written last line ignored -> running" "$(state_of "$(id 7)")" "running"
eq "700 KB final record (tail grows) -> done" "$(state_of "$(id 8)")" "done"
eq "no meta.json -> still classified" "$(state_of "$(id 9)")" "running"
has "status header" "$st" "ID"
has "status shows description + run minutes" "$(printf '%s\n' "$st" | grep "$(id 4)")" "50m"
eq "stalled sorts first" "$(printf '%s\n' "$st" | sed -n 3p | awk '{print $1}')" "$(id 4)"
[ -e "$T/state/state.json" ] && no "status is read-only (no state written)" || ok "status is read-only (no state written)"
js=$(aw status --json | cap)
eq "status --json: rows with the same states" "$(printf '%s' "$js" | python3 -c 'import json,sys; d=json.load(sys.stdin); r={a["id"]: a for a in d["agents"]}; print(r[sys.argv[1]]["status"], r[sys.argv[2]]["status"], r[sys.argv[1]]["session"], sorted(r[sys.argv[1]]))' "$(id 4)" "$(id 2)")" "stalled done S1 ['depth', 'description', 'effort', 'id', 'kind', 'last', 'parent', 'session', 'start', 'status', 'title', 'type']"
[ -e "$T/state/state.json" ] && no "status --json is read-only" || ok "status --json is read-only"
aw run | cap >/dev/null
eq "one inbox note: the stalled agent" "$(notes)/$(notes stalled-"$(id 4)")" "1/1"
note=$(cat "$T"/inbox/agent-watch-stalled-* | cap)
has "note has the agent id" "$note" "$(id 4)"
has "note has the description" "$note" '"Stuck one"'
has "note has minutes silent" "$note" "no transcript growth for 16 min"
has "note says what the agent was doing" "$note" "got a tool result, model call pending"
has "untagged -> not owner-facing" "$note" "not owner-facing"
eq "untagged agents never text" "$(tgn)" "0"
aw run >/dev/null; eq "same run again: no duplicate note" "$(notes)" "1"
at 10; aw run >/dev/null; eq "10 min later: still one note" "$(notes)" "1"
at 20; aw run >/dev/null
eq "the running ones stall later -> one note each (ids 1, 7, 9)" "$(notes)/$(notes stalled-"$(id 1)")/$(notes stalled-"$(id 7)")/$(notes stalled-"$(id 9)")" "4/1/1/1"
eq "finished/interrupted agents are never noted" "$(notes "$(id 2)")$(notes "$(id 3)")$(notes "$(id 6)")" "000"

echo "# C. stall re-arms after the agent grows again"
at 21; $FX grow "$T/proj" S1 "$(id 4)" tool_use "$(m 21)"; aw run >/dev/null
eq "grew -> running again" "$(state_of "$(id 4)")" "running"
has "resume logged" "$(cat "$T/state/agent-watch.log")" "resumed $(id 4) (was stalled)"
at 30; aw run >/dev/null; eq "9 min silent: no new note" "$(notes stalled-"$(id 4)")" "1"
at 37; aw run >/dev/null; eq "16 min silent again -> second note for it" "$(notes stalled-"$(id 4)")" "2"
has "second note: waiting on a tool" "$(cat "$T"/inbox/agent-watch-stalled-"$(id 4)"-"$(m 37)".md)" "waiting on a tool call"
$FX grow "$T/proj" S1 "$(id 4)" final "$(m 38)"; at 50; aw run >/dev/null
eq "stalled agent then finishes -> done, no note" "$(state_of "$(id 4)")/$(notes stalled-"$(id 4)")" "done/2"

echo "# D. parent-session task notifications"
reset; at 0
$FX agent "$T/proj" S1 "$(id b1)" tool_use "$(m -20)"
$FX notif "$T/proj" S1 "$(id b1)" killed "$(m -20)"
$FX agent "$T/proj" S1 "$(id b2)" tool_result "$(m -20)" --start "$(m -70)"
$FX notif "$T/proj" S1 "$(id b2)" completed "$(m -60)"          # older than its last write: it was resumed
$FX agent "$T/proj" S1 "$(id b3)" tool_use "$(m -25)"
$FX notif "$T/proj" S1 "$(id b3)" failed "$(m -25)" --delivered  # only the delivered user record
$FX agent "$T/proj" S1 "$(id b4)" tool_use "$(m -3)"
$FX notif "$T/proj" S1 bshelltask completed "$(m -3)"            # a background shell task: ignored
aw run >/dev/null
eq "killed notification after the last write -> killed" "$(state_of "$(id b1)")" "killed"
eq "notification older than the last write (resumed) -> stalled" "$(state_of "$(id b2)")" "stalled"
eq "delivered notification (user record) -> failed" "$(state_of "$(id b3)")" "failed"
eq "only the resumed one gets a note" "$(notes)/$(notes "$(id b2)")" "1/1"
eq "running, no notification yet" "$(state_of "$(id b4)")" "running"
off1=$(python3 -c 'import json,sys; print(list(json.load(open(sys.argv[1]))["offsets"].values())[0]["off"])' "$T/state/state.json")
$FX notif "$T/proj" S1 "$(id b4)" stopped "$(m -3)"
at 1; aw run >/dev/null
eq "new notification picked up incrementally -> stopped" "$(state_of "$(id b4)")" "stopped"
off2=$(python3 -c 'import json,sys; print(list(json.load(open(sys.argv[1]))["offsets"].values())[0]["off"])' "$T/state/state.json")
[ "$off2" -gt "$off1" ] && ok "parent transcript offset advanced ($off1 -> $off2)" || no "offset advanced" "$off1 -> $off2"
$FX grow "$T/proj" S1 "$(id b4)" tool_use "$(m 2)"; at 2; aw run >/dev/null
eq "transcript grows after the notification (SendMessage resume) -> running" "$(state_of "$(id b4)")" "running"

echo "# E. nested agents whose parent ended"
reset; at 0
$FX agent "$T/proj" S1 "$(id p1)" final "$(m -29)"
$FX notif "$T/proj" S1 "$(id p1)" stopped "$(m -30)"
$FX agent "$T/proj" S1 "$(id c1)" tool_use "$(m -31)" --parent "$(id p1)"
$FX agent "$T/proj" S1 "$(id c2)" tool_use "$(m -31)" --parent "$(id p9)"
$FX agent "$T/proj" S1 "$(id c3)" tool_use "$(m -2)" --parent "$(id p1)"
aw run >/dev/null
eq "child silent since its parent stopped -> orphaned" "$(state_of "$(id c1)")" "orphaned"
eq "child whose parent is unknown -> stalled" "$(state_of "$(id c2)")" "stalled"
eq "child still writing -> running" "$(state_of "$(id c3)")" "running"
eq "orphaned children are not noted" "$(notes "$(id c1)")/$(notes "$(id c2)")" "0/1"

echo "# F. owner progress lines"
reset; at 0
D1=$(id d1); D2=$(id d2)
$FX agent "$T/proj" S1 "$D1" tool_use "$(m 0)" --start "$(m 0)" --desc "Build the thing"
$FX agent "$T/proj" S1 "$D2" tool_use "$(m 0)" --start "$(m -40)" --desc "Untagged long one"
out=$(aw tag ad1 owner "Fix the printer" | cap); eq "tag by prefix" "$out" "tagged $D1 owner: \"Fix the printer\""
step() { at "$1"; $FX grow "$T/proj" S1 "$D1" tool_use "$(m "$1")"; $FX grow "$T/proj" S1 "$D2" tool_use "$(m "$1")"; aw run >/dev/null; }
step 5; step 10; eq "no text before ~15 min" "$(tgn)" "0"
step 15; eq "first progress line at 15 min" "$(tail -1 "$T/tg.log" 2>/dev/null)" "⏳ Fix the printer: still working (15 min)"
eq "progress line is non-urgent (TG_SOFT=1)" "$(cat "$T/tg.soft" 2>/dev/null)" "1"
step 20; step 25; eq "nothing in between" "$(tgn)" "1"
step 30; eq "second line at 30 min" "$(tail -1 "$T/tg.log")" "⏳ Fix the printer: still working (30 min)"
NOW=$(( $(m 44) + 30 )); $FX grow "$T/proj" S1 "$D1" tool_use "$NOW"; aw run >/dev/null
eq "cron jitter: 14.5 min later still counts as ~15" "$(tgn)" "3"
hasnt "untagged agent never texts" "$(cat "$T/tg.log")" "Untagged"
has "status shows the tag and last progress time" "$(aw status | grep "$D1")" "Fix the printer (⏳10:44)"
at 62; aw run >/dev/null
eq "tagged agent stalls: no progress text while stalled" "$(tgn)" "3"
has "stall note names it owner-facing with title" "$(cat "$T"/inbox/agent-watch-stalled-"$D1"-*)" 'owner-facing, title "Fix the printer"'
has "stall note tells me to tell the owner" "$(cat "$T"/inbox/agent-watch-stalled-"$D1"-*)" "tell the owner"
$FX grow "$T/proj" S1 "$D1" final "$(m 63)"; at 80; aw run >/dev/null
eq "finished: no text, no note" "$(tgn)/$(notes stalled-"$D1")" "3/1"
out=$(aw tag "$D2" owner | cap); eq "tag without title uses the description" "$out" "tagged $D2 owner: \"Untagged long one\""
out=$(aw untag "$D2"); eq "untag" "$out" "untagged $D2"

echo "# G. quiet hours (texts only 08:00-24:00 Zurich)"
reset
E1=$(id e1); S=$(ep "2026-10-12 23:30")
$FX agent "$T/proj" S1 "$E1" tool_use "$S" --start "$S" --desc "Night job"
NOW=$S aw tag "$E1" owner "Night job" >/dev/null
qstep() { NOW=$(ep "$1"); $FX grow "$T/proj" S1 "$E1" tool_use "$NOW"; aw run >/dev/null; }
qstep "2026-10-12 23:50"; eq "23:50 is awake: text" "$(tgn)" "1"
qstep "2026-10-13 00:05"; qstep "2026-10-13 03:00"; qstep "2026-10-13 07:55"
eq "no texts 00:00-08:00" "$(tgn)" "1"
qstep "2026-10-13 08:00"; eq "08:00: text resumes" "$(tail -1 "$T/tg.log")" "⏳ Night job: still working (510 min)"

echo "# H. tg failure"
reset; at 0
F1=$(id f1)
$FX agent "$T/proj" S1 "$F1" tool_use "$(m 0)" --start "$(m -20)"
aw tag "$F1" owner "Flaky" >/dev/null
touch "$T/tg-fail"; aw run >/dev/null
eq "tg failed: one tg-failed note" "$(notes tg-failed)" "1"
at 5; $FX grow "$T/proj" S1 "$F1" tool_use "$(m 5)"; aw run >/dev/null
eq "still failing: retried, no second note" "$(notes tg-failed)/$(tgn)" "1/0"
rm "$T/tg-fail"; at 6; $FX grow "$T/proj" S1 "$F1" tool_use "$(m 6)"; aw run >/dev/null
eq "tg back: the line goes out" "$(tail -1 "$T/tg.log" 2>/dev/null)" "⏳ Flaky: still working (26 min)"

echo "# I. tag errors"
reset; at 0
$FX agent "$T/proj" S1 aaa1000000000001x tool_use "$(m 0)"
$FX agent "$T/proj" S1 aaa2000000000001x tool_use "$(m 0)"
aw tag aaa owner X >/dev/null 2>&1; eq "ambiguous prefix rc 1" "$?" "1"
aw tag zzzz owner X >/dev/null 2>&1; eq "unknown prefix rc 1" "$?" "1"
aw tag 'x;rm' owner X >/dev/null 2>&1; eq "junk id rc 1" "$?" "1"
aw tag aaa1 me X >/dev/null 2>&1; eq "unknown role rc 2" "$?" "2"
out=$(aw tag a0123456789abcdef owner "Early" 2>&1); has "full id not seen yet: tagged with a warning" "$out" "no transcript for it yet"
has "tags lists it" "$(aw tags)" 'a0123456789abcdef  owner  "Early"'
aw untag nope >/dev/null 2>&1; eq "untag unknown rc 1" "$?" "1"

echo "# J. same agent in two session dirs (fork/resume)"
reset; at 0
$FX agent "$T/proj" OLD "$(id j1)" final "$(m -50)"
$FX agent "$T/proj" NEW "$(id j1)" tool_use "$(m -1)"
st=$(aw status)
eq "listed once, newest copy wins" "$(printf '%s\n' "$st" | grep -c "$(id j1)")/$(state_of "$(id j1)")" "1/running"

echo "# K. corrupt state, crash note, lock"
reset; at 0
$FX agent "$T/proj" S1 "$(id k1)" tool_use "$(m -1)"
mkdir -p "$T/state"; echo '{garbage' > "$T/state/state.json"
out=$(aw status 2>&1); has "status survives a corrupt state.json" "$out" "$(id k1)"
aw run >/dev/null; eq "run survives it (rc 0)" "$?" "0"
ls "$T"/state/state.json.bad-* >/dev/null 2>&1 && ok "corrupt state moved aside" || no "corrupt state moved aside"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$T/state/state.json" && ok "fresh state.json is valid" || no "fresh state.json valid"
mkdir "$T/state/state.json.tmp"          # save_state can't write -> a crash
aw run >/dev/null 2>&1; eq "crash: rc 1" "$?" "1"
eq "crash: one inbox note" "$(notes crash)" "1"
aw run >/dev/null 2>&1; eq "same crash again: no second note" "$(notes crash)" "1"
has "crash logged with traceback" "$(cat "$T/state/agent-watch.log")" "Traceback"
rmdir "$T/state/state.json.tmp"
( flock "$T/state/lock" sleep 2 ) & sleep 0.3
t0=$(date +%s%N); aw run >/dev/null; rc=$?; t1=$(date +%s%N)
eq "locked: run exits 0 at once" "$rc/$(( (t1 - t0) / 1000000000 ))" "0/0"
wait

echo "# L. cost: 300 old agents + a 20 MB transcript + a 25 MB parent session"
reset; at 0
$FX filler "$T/proj" 300 "$(m -3000)"
$FX agent "$T/proj" BIG "$(id l1)" tool_use "$(m -1)" --big 300000
python3 - "$T/proj/BIG/subagents/agent-$(id l1).jsonl" "$T/proj/BIG.jsonl" <<'EOF'
import sys, json
p, parent = sys.argv[1], sys.argv[2]
body = open(p).read().splitlines()
pad = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'x' * 20000}})
with open(p, 'w') as f:
    f.write(body[0] + '\n' + (pad + '\n') * 1000 + '\n'.join(body[1:]) + '\n')
with open(parent, 'w') as f:
    f.write((pad + '\n') * 1250)
EOF
touch -d "@$(m -1)" "$T/proj/BIG/subagents/agent-$(id l1).jsonl"
t0=$(date +%s%N); aw run >/dev/null; t1=$(date +%s%N); ms1=$(( (t1 - t0) / 1000000 ))
t0=$(date +%s%N); aw run >/dev/null; t1=$(date +%s%N); ms2=$(( (t1 - t0) / 1000000 ))
[ "$ms1" -lt 1500 ] && ok "first run (full parent scan) ${ms1} ms < 1500" || no "first run fast" "${ms1} ms"
[ "$ms2" -lt 500 ] && ok "next run (cached, incremental) ${ms2} ms < 500" || no "next run fast" "${ms2} ms"
eq "big agent classified" "$(state_of "$(id l1)")" "running"
eq "old filler agents ignored" "$(aw status | grep -c ' f0000')" "0"

echo "# N. permission-stuck: a Bash call without a result (likely a hidden approval prompt)"
reset; at 0
fake=$(printf 'gh%s_%s%s' p abcdefghijklmnopqrstuvwxyz 0123456789AB)    # built at runtime: no token literal here
$FX agent "$T/proj" P "$(id p1)" tool_use "$(m -16)" --start "$(m -30)" --desc "Rm one" \
  --bash-cmd $'D=/x/scan\nrm -rf $D/*; ls $D'
$FX agent "$T/proj" P "$(id p2)" tool_use "$(m -16)" --start "$(m -30)" \
  --bash-cmd "curl -s -H token=$fake https://example.invalid/$(printf 'y%.0s' $(seq 120))"
$FX agent "$T/proj" P "$(id p3)" tool_use "$(m -16)" --start "$(m -30)"      # a pending non-Bash call
python3 - "$T/proj/P/subagents" "$(m -20)" "$(id p4)" "$(id p5)" <<'EOF'
import json, os, sys
from datetime import datetime, timezone
d, t, id4, id5 = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
iso = lambda x: datetime.fromtimestamp(x, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
def a(tid, cmd, x):
    return {'type': 'assistant', 'timestamp': iso(x), 'message': {'role': 'assistant', 'stop_reason': 'tool_use',
            'content': [{'type': 'tool_use', 'id': tid, 'name': 'Bash', 'input': {'command': cmd}}]}}
def u(tid, x):
    return {'type': 'user', 'timestamp': iso(x), 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': tid, 'content': 'ok'}]}}
go = {'type': 'user', 'timestamp': iso(t), 'message': {'role': 'user', 'content': 'go'}}
think = {'type': 'assistant', 'timestamp': iso(t + 3), 'message': {'role': 'assistant', 'stop_reason': None,
         'content': [{'type': 'thinking', 'thinking': 'hm'}]}}
files = {id4: [go, a('ta', 'sleep 100 # first-call', t + 1), a('tb', 'cd /x; rm -rf out/* # second-call', t + 1),
                u('ta', t + 101)],                                      # parallel calls: A answered, B pending
         id5: [go, a('tc', 'echo answered-call', t + 1), u('tc', t + 2), think]}       # answered, then thinking
for k, recs in files.items():
    p = os.path.join(d, f'agent-{k}.jsonl')
    with open(p, 'w') as f:
        f.write(''.join(json.dumps(r) + '\n' for r in recs))
    os.utime(p, (t + 101, t + 101))
EOF
aw run >/dev/null
eq "Bash call pending 16 min -> permission-stuck note, no plain one" "$(notes permission-stuck-"$(id p1)")/$(notes stalled-"$(id p1)")" "1/0"
n1=$(cat "$T"/inbox/agent-watch-permission-stuck-"$(id p1)"-*.md 2>/dev/null)
has "note carries the label" "$n1" "permission-stuck (likely approval prompt)"
has "note shows the command, newlines flattened" "$n1" '`D=/x/scan rm -rf $D/*; ls $D`'
has "note says TaskStop + rewrite" "$n1" "TaskStop $(id p1)"
has "note points to the report" "$n1" "subagent-bash-hangs-2026-10-16.md"
has "log names it" "$(cat "$T/state/agent-watch.log")" "stalled $(id p1) silent 16m permission-stuck"
n2=$(cat "$T"/inbox/agent-watch-permission-stuck-"$(id p2)"-*.md 2>/dev/null)
hasnt "token redacted in the note" "$n2" "$(printf '%s' "$fake" | cut -c1-6)"
has "redaction marker shown" "$n2" "[redacted]"
c2=$(printf '%s\n' "$n2" | grep '^- command' | sed 's/^[^`]*`//; s/`$//')
[ "${#c2}" -le 83 ] && [ "${#c2}" -ge 40 ] && ok "command cut to 80 chars (+...)" || no "command cut" "${#c2}: $c2"
eq "pending non-Bash call -> plain stalled note" "$(notes stalled-"$(id p3)")/$(notes permission-stuck-"$(id p3)")" "1/0"
n4=$(cat "$T"/inbox/agent-watch-permission-stuck-"$(id p4)"-*.md 2>/dev/null)
has "parallel calls: the unanswered one is shown" "$n4" "second-call"
hasnt "parallel calls: the answered one is not" "$n4" "first-call"
eq "answered Bash call, then thinking -> plain stalled" "$(notes stalled-"$(id p5)")/$(notes permission-stuck-"$(id p5)")" "1/0"
hasnt "no command text in state" "$(cat "$T"/state/state.json)" "rm -rf"
reset; at 0
$FX agent "$T/proj" Q "$(id q1)" tool_use "$(m -2)" --start "$(m -10)" --bash-cmd 'rm -f $T/*'
AGENT_WATCH_STALL_MIN=1 aw run >/dev/null
eq "Bash call pending 2 min (< 3) -> plain stalled note" "$(notes stalled-"$(id q1)")/$(notes permission-stuck-"$(id q1)")" "1/0"
$FX agent "$T/proj" Q "$(id q2)" tool_use "$(m -2)" --start "$(m -10)" --bash-cmd 'rm -f $T/*'
AGENT_WATCH_STALL_MIN=1 AGENT_WATCH_PERM_MIN=1 aw run >/dev/null
eq "AGENT_WATCH_PERM_MIN=1: 2 min -> permission-stuck" "$(notes permission-stuck-"$(id q2)")" "1"
reset; at 0
$FX agent "$T/proj" R "$(id r1)" tool_use "$(m -16)" --start "$(m -30)" --bash-cmd 'rm -rf $D/* # raw-r1'
AGENT_WATCH_SECRETSCAN=/bin/false aw run >/dev/null
$FX agent "$T/proj" R "$(id r2)" tool_use "$(m -16)" --start "$(m -30)" --bash-cmd 'rm -rf $D/* # raw-r2'
AGENT_WATCH_SECRETSCAN=/nonexistent/secretscan aw run >/dev/null
nr=$(cat "$T"/inbox/agent-watch-permission-stuck-*.md 2>/dev/null)
eq "redactor fails (rc 1, missing) -> both noted with a placeholder" "$(printf '%s\n' "$nr" | grep -c 'redaction failed')" "2"
hasnt "redactor fails -> raw command never shown" "$nr" "raw-r"

echo "# T. renamed agents (tg-status title -> lib/agent_titles.py's file)"
reset; at 0; rm -f "$T/titles.json"
E1=$(id e1); E2=$(id e2); E3=$(id e3)
$FX agent "$T/proj" S1 "$E1" tool_use "$(m 0)" --start "$(m 0)" --desc "Build the thing"
$FX agent "$T/proj" S1 "$E2" tool_use "$(m 0)" --start "$(m 0)" --desc "Second one"
$FX agent "$T/proj" S1 "$E3" tool_use "$(m 0)" --start "$(m 0)" --desc "Third one"
titles() { python3 -I -c 'import json, sys; out, now = sys.argv[1], float(sys.argv[2]); json.dump({a: {"title": t, "t": now} for a, t in zip(sys.argv[3::2], sys.argv[4::2])}, open(out, "w"))' "$T/titles.json" "$NOW" "$@"; }
eq "no titles file: spawn description" "$(aw status | grep "$E1" | sed 's/.*  //')" "Build the thing"
titles "$E1" "Printer fix" "$E2" "Renamed two"
has "renamed: the title, the spawn description beside it" "$(aw status | grep "$E1")" "  Printer fix (was: Build the thing)"
js=$(aw status --json | cap)
eq "--json: title = the override, description = the spawn description" "$(printf '%s' "$js" | python3 -I -c 'import json,sys; r = {a["id"]: a for a in json.load(sys.stdin)["agents"]}; print(r[sys.argv[1]]["title"], "|", r[sys.argv[1]]["description"], "|", r[sys.argv[2]]["title"])' "$E1" "$E3")" "Printer fix | Build the thing | Third one"
out=$(aw tag ae1 owner "Owner tag title" | cap); eq "tag with its own title" "$out" "tagged $E1 owner: \"Owner tag title\""
out=$(aw tag ae2 owner | cap); eq "tag without a title: the renamed title is the default" "$out" "tagged $E2 owner: \"Renamed two\""
out=$(aw tag ae3 owner | cap); eq "tag without a title, not renamed: the spawn description" "$out" "tagged $E3 owner: \"Third one\""
has "status OWNER TAG column: the renamed title wins over the tag's" "$(aw status | grep "$E1")" "  Printer fix  "
step3() { at "$1"; for x in "$E1" "$E2" "$E3"; do $FX grow "$T/proj" S1 "$x" tool_use "$(m "$1")"; done; aw run >/dev/null; }
step3 5; step3 10; step3 15
eq "progress lines use the renamed title" "$(grep -c 'Printer fix: still working (15 min)' "$T/tg.log")" "1"
has "a renamed agent tagged without a title: its title" "$(cat "$T/tg.log")" "⏳ Renamed two: still working (15 min)"
has "not renamed: the tag title (spawn description)" "$(cat "$T/tg.log")" "⏳ Third one: still working (15 min)"
hasnt "the tag's own title is overridden by the rename" "$(cat "$T/tg.log")" "Owner tag title"
echo 'not json {' > "$T/titles.json"
eq "broken titles file: spawn descriptions, no crash" "$(aw status | grep "$E1" | sed 's/.*  //')" "Build the thing"
printf '%s' '{"'"$E1"'": "flat string", "'"$E2"'": {"title": "   "}}' > "$T/titles.json"
eq "malformed entries are ignored" "$(aw status | grep -cE 'Build the thing$|Second one$')" "2"
at 20; aw run >/dev/null; eq "cron run with a malformed titles file: no crash note" "$(notes crash)" "0"

echo "# M. privacy + real files"
cat "$T"/inbox/* "$T"/tg.log "$T"/state/* 2>/dev/null >> "$ALL_OUT"
if grep -q 'SECRET-CONTENT-XYZZY' "$ALL_OUT"; then no "no transcript content in output/notes/texts/state"; else ok "no transcript content in output/notes/texts/state ($(wc -c < "$ALL_OUT") bytes checked)"; fi
real_after=$(real_snap)
if [ "$real_after" = "$real_before" ]; then ok "real state + real inbox untouched ($(grep -c . <<<"$real_before") files watched)"
else  # each file added, changed or gone: the host's live cron may write its own home meanwhile, never with test data
  marks='a0123456789abcdef|a[a-z0-9]{1,3}0{8,}1|aaa[12]000000000001x|SECRET-CONTENT-XYZZY'   # the fixture's ids (id b1 ...)
  leak=
  while read -r f; do
    [ -n "$f" ] || continue
    case $f in
      /home/node/.local/state/agent-watch/*|/home/node/inbox/*) { grep -qE "$marks" <<<"$f" || grep -qaE "$marks" "$f" 2>/dev/null; } && leak+="$f " ;;
      *) leak+="$f (no live writer there) " ;;
    esac
  done < <(comm -3 <(printf '%s\n' "$real_before") <(printf '%s\n' "$real_after") | sed 's/^\t//' | cut -d' ' -f1 | sort -u)
  if [ -n "$leak" ]; then no "real state + real inbox untouched" "written by this run: $leak"
  else ok "real state + real inbox untouched (changed by the live cron, no test data in it)"; fi
fi

eq "real titles file untouched" "$(titles_snap)" "$titles_before"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
