#!/usr/bin/env bash
# Tests for ~/bin/sleepin (the sleep-in guard), its gate as `tg --soft` runs it and remind's sleep-in hold. Exit 0 =
# all pass. (`tg --soft` itself: the repo's tests/tg/test_tg_flags.py.)
# Isolated: HOME is a temp dir (every sleepin default path lives under it; no profile, so the profile's fallback
# zone), the clock is faked (--at, SLEEPIN_NOW, REMIND_NOW), `tg` is a stub first on PATH, remind runs on temp
# state with a stub guard (REMIND_SLEEPIN_CMD). The real sleepin state is checked: a run never creates or changes it.
# SLEEPIN_BIN / REMIND_BIN: test copies before installing.
set -uo pipefail
S_BIN=${SLEEPIN_BIN:-/home/node/bin/sleepin}
R_BIN=${REMIND_BIN:-/home/node/bin/remind}
REAL=/home/node/.local/state/sleepin
real_sig() { find "$REAL" -type f -printf '%P %s %T@\n' 2>/dev/null | sort | md5sum; }
BEFORE=$(real_sig)
T=$(mktemp -d "${TMPDIR:-/tmp}/sleepin-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
soft() { TG_SOFT=1 "$S_BIN" gate -- tg "$@"; }   # what `tg --soft TEXT` runs (the stub tg first on PATH)
export HOME=$T/home
unset SLEEPIN_STATE SLEEPIN_NOW SLEEPIN_CALCACHE SLEEPIN_PLACE SLEEPIN_JOURNAL SLEEPIN_INBOX ASSISTANT_PROFILE
unset REMIND_DB REMIND_TG REMIND_NOW REMIND_SLEEPIN_CMD
CAL=$HOME/.local/state/calcache/events.json
PL=$HOME/.local/state/remind/place-state.json
JR=$HOME/.claude/channels/telegram/inbound-journal.jsonl
ST=$HOME/.local/state/sleepin
mkdir -p "$(dirname "$CAL")" "$(dirname "$PL")" "$(dirname "$JR")" "$T/bin"
cat > "$T/bin/tg" <<STUB
#!/bin/sh
[ -e "$T/tg-fail" ] && exit 1
printf '%s\n' "\$*" >> "$T/tg.log"
STUB
chmod +x "$T/bin/tg"
export PATH="$T/bin:$PATH"
pass=0 fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && echo "       $2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "got [$2] want [$3]"; fi; }
has() { case "$2" in *"$3"*) ok "$1" ;; *) no "$1" "[$3] not in [$2]" ;; esac; }
act() { "$S_BIN" active --at "$1" >/dev/null 2>&1; echo $?; }   # 0 = hold, 1 = send
sent() { if [ -f "$T/tg.log" ]; then wc -l < "$T/tg.log" | tr -d ' '; else echo 0; fi; }
qlen() { python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$ST/queue.json" 2>/dev/null || echo 0; }
place() { printf '%s' "$1" > "$PL"; }
journal() { printf '{"id":"x%s","at":"%s","kind":"message","owner":%s}\n' "$RANDOM" "$1" "${2:-true}" >> "$JR"; }

cat > "$CAL" <<'JSON'
{"version": 1, "events": [
 {"id": "a", "start": "2026-10-21T21:00:00+02:00", "end": "2026-10-21T22:00:00+02:00", "all_day": false, "title": "Evening class", "flags": {}},
 {"id": "b", "start": "2026-10-22T19:00:00+02:00", "end": "2026-10-22T22:30:00+02:00", "all_day": false, "title": "Show", "flags": {}},
 {"id": "c", "start": "2026-10-22T23:07:00+02:00", "end": "2026-10-23T00:14:00+02:00", "all_day": false, "title": "🚆 City → Home", "flags": {"travel": "train"}},
 {"id": "d", "start": "2026-10-24T16:00:00+02:00", "end": "2026-10-25T02:00:00+02:00", "all_day": false, "title": "Party", "flags": {}},
 {"id": "e", "start": "2026-10-30T20:00:00+01:00", "end": "2026-10-31T06:00:00+01:00", "all_day": false, "title": "Night out", "flags": {}},
 {"id": "f", "start": "2026-10-31T05:14:00+01:00", "end": "2026-10-31T07:30:00+01:00", "all_day": false, "title": "🚆 Town → Home", "flags": {"travel": "train"}},
 {"id": "g", "start": "2026-11-02", "end": "2026-11-04", "all_day": true, "title": "Trip", "flags": {}},
 {"id": "h", "start": "2026-11-05T06:00:00+01:00", "end": "2026-11-05T08:00:00+01:00", "all_day": false, "title": "Early start", "flags": {}},
 {"id": "i", "start": "2026-11-06T22:00:00+01:00", "end": "2026-11-06T23:00:00+01:00", "all_day": false, "title": "Ends at 23", "flags": {}},
 {"id": "j", "start": "2026-11-08T20:30:00+01:00", "end": "2026-11-09T06:45:00+01:00", "all_day": false, "title": "✈️ Overnight flight", "flags": {"travel": "flight"}},
 {"id": "k", "start": "2026-11-10T09:00:00+01:00", "end": "2026-11-11T08:00:00+01:00", "all_day": false, "title": "Long event", "flags": {}}
]}
JSON
place '{"zone": "home", "since": "2026-10-20 18:00", "trip": {"left": "2026-10-20 07:50", "back": "2026-10-20 18:00"}}'

# ---------------------------------------------------------------- calendar rule + window
eq "no late night (class ends 22:00): send" "$(act '2026-10-22 09:00')" 1
eq "train home 00:14: 05:59 send (window from 06:00)" "$(act '2026-10-23 05:59')" 1
eq "train home 00:14: 06:00 hold" "$(act '2026-10-23 06:00')" 0
eq "train home 00:14: 12:29 hold" "$(act '2026-10-23 12:29')" 0
eq "train home 00:14: 12:30 send" "$(act '2026-10-23 12:30')" 1
eq "all-day event: no guard" "$(act '2026-11-03 09:00')" 1
eq "early start (06:00-08:00) is not a late night" "$(act '2026-11-05 09:00')" 1
eq "event ending exactly 23:00: no guard" "$(act '2026-11-07 09:00')" 1
eq "overnight flight landing 06:45: hold" "$(act '2026-11-09 09:00')" 0
eq "23 h event: no guard" "$(act '2026-11-11 09:00')" 1
has "active -v says why" "$("$S_BIN" active -v --at '2026-10-23 09:00')" "HOLD until 12:30"

# ---------------------------------------------------------------- DST: the clocks go back on 25.10
eq "DST morning, party until 02:00 CEST: 09:00 hold" "$(act '2026-10-25 09:00')" 0
eq "DST morning: 12:29 hold" "$(act '2026-10-25 12:29')" 0
eq "DST morning: 12:30 send" "$(act '2026-10-25 12:30')" 1
e1=$(date -d '2026-10-25T10:31:00Z' +%s) e2=$(date -d '2026-10-25T11:29:00Z' +%s) e3=$(date -d '2026-10-25T11:30:00Z' +%s)
eq "DST epoch 10:31Z = 11:31 CET: hold (CEST math would say 12:31)" "$(SLEEPIN_NOW=@$e1 "$S_BIN" active; echo $?)" 0
eq "DST epoch 11:29Z = 12:29 CET: hold" "$(SLEEPIN_NOW=@$e2 "$S_BIN" active; echo $?)" 0
eq "DST epoch 11:30Z = 12:30 CET: send" "$(SLEEPIN_NOW=@$e3 "$S_BIN" active; echo $?)" 1
eq "DST night: wall 02:30 (ambiguous hour) is before the window" "$(act '2026-10-25 02:30')" 1

# ---------------------------------------------------------------- his message ends it (after the night is over)
eq "late night ending 07:30 (train home): 09:00 hold" "$(act '2026-10-31 09:00')" 0
journal "2026-10-31T07:00:00.000Z"          # 08:00 local: still before the train home + 60 min (08:30)
journal "2026-10-31T07:20:00.000Z" false    # not his: never counts
eq "his message 08:00 (before train home + 60 min): still hold" "$(act '2026-10-31 08:10')" 0
journal "2026-10-31T07:40:00.000Z"          # 08:40 local
eq "before his 08:40 message: hold" "$(act '2026-10-31 08:35')" 0
eq "after his 08:40 message: send" "$(act '2026-10-31 08:45')" 1
has "status names the end" "$(SLEEPIN_NOW='2026-10-31 08:45' "$S_BIN" status)" "ended 08:40 (his message"
: > "$JR"
printf '{"id":"v1","at":"2026-10-31T07:50:00.000Z","kind":"instant","owner":false,"meta":{"chat_id":"voice","via":"voice","ts":"2026-10-31T07:50:00.000Z"}}\n' >> "$JR"
eq "his live voice via the relay (08:50) ends it too: send" "$(act '2026-10-31 08:55')" 1
eq "before his voice event: hold" "$(act '2026-10-31 08:45')" 0
: > "$JR"

# ---------------------------------------------------------------- place rule (remind's place-state.json)
place '{"zone": "home", "since": "2026-10-28 02:30", "trip": {"left": "2026-10-27 20:00", "back": "2026-10-28 02:30"}}'
eq "home at 02:30 after a night out: hold" "$(act '2026-10-28 09:00')" 0
journal "2026-10-28T05:05:00.000Z"          # 06:05 local, after home + 60 min
eq "home 02:30, his message 06:05: send" "$(act '2026-10-28 06:10')" 1
: > "$JR"
place '{"zone": "home", "since": "2026-10-28 07:45", "trip": {"left": "2026-10-28 07:00", "back": "2026-10-28 07:45"}}'
eq "morning errand (left 07:00, back 07:45): no guard" "$(act '2026-10-28 09:00')" 1
place '{"zone": "away", "since": "2026-10-23 10:00", "left": "2026-10-23 10:00"}'
eq "late night, still home 09:59: hold" "$(act '2026-10-23 09:59')" 0
eq "late night, left home 10:00: send" "$(act '2026-10-23 10:01')" 1
place '{"zone": "away", "since": "2026-10-30 19:00", "left": "2026-10-30 16:40"}'
eq "still out (left the evening before): hold" "$(act '2026-10-31 07:00')" 0

# ---------------------------------------------------------------- hold / release
out=$(SLEEPIN_NOW="2026-10-26 20:00" "$S_BIN" hold --reason "test")
eq "hold in the evening = the next morning" "$out" "hold: Tue 27.10 06:00–12:30"
eq "held morning: 09:00 hold" "$(act '2026-10-27 09:00')" 0
eq "hold file is 0600" "$(stat -c %a "$ST/holds.json")" 600
SLEEPIN_NOW="2026-10-28 20:00" "$S_BIN" hold 2026-10-29 --until 10:00 >/dev/null
eq "hold --until 10:00: 09:59 hold" "$(act '2026-10-29 09:59')" 0
eq "hold --until 10:00: 10:00 send" "$(act '2026-10-29 10:00')" 1
out=$(SLEEPIN_NOW="2026-10-24 20:00" "$S_BIN" release 2026-10-25)
eq "release a later morning" "$out" "released Sun 25.10: no guard that morning"
eq "released morning: no guard" "$(act '2026-10-25 09:00')" 1
has "status lists it as off" "$(SLEEPIN_NOW='2026-10-24 20:00' "$S_BIN" status)" "Sun 25.10 off"
eq "bad date: usage error" "$("$S_BIN" hold 2026-13-01 >/dev/null 2>&1; echo $?)" 2

# ---------------------------------------------------------------- gate / soft texts / flush
place '{"zone": "home", "since": "2026-10-20 18:00"}'
eq "gate, no guard: runs now" "$(SLEEPIN_NOW='2026-10-22 09:00' soft "plain now"; echo $?):$(sent)" "0:1"
eq "gate passes the exit code" "$(SLEEPIN_NOW='2026-10-22 09:00' "$S_BIN" gate -- sh -c 'exit 3'; echo $?)" 3
eq "gate, missing command: 127" "$(SLEEPIN_NOW='2026-10-22 09:00' "$S_BIN" gate -- /nonexistent/x 2>/dev/null; echo $?)" 127
eq "soft text on a guarded morning: queued, exit 0" "$(SLEEPIN_NOW='2026-10-23 08:53' soft "deal A" 2>/dev/null; echo $?):$(sent)" "0:1"
SLEEPIN_NOW='2026-10-23 09:23' "$S_BIN" gate --tag radar -- tg "report B" 2>/dev/null
eq "two waiting" "$(qlen)" 2
eq "queue file is 0600" "$(stat -c %a "$ST/queue.json")" 600
has "status shows the queue" "$(SLEEPIN_NOW='2026-10-23 09:30' "$S_BIN" status)" "2 waiting"
SLEEPIN_NOW='2026-10-23 09:30' "$S_BIN" flush
eq "flush while it holds: nothing sent" "$(sent):$(qlen)" "1:2"
SLEEPIN_NOW='2026-10-23 12:30' "$S_BIN" flush >/dev/null
eq "flush at 12:30: both sent, in order" "$(sed -n '2p;3p' "$T/tg.log" | tr '\n' '|')" "deal A|report B|"
eq "queue empty" "$(qlen)" 0
SLEEPIN_NOW='2026-10-23 12:35' "$S_BIN" flush >/dev/null
eq "second flush: no duplicate" "$(sent)" 3
eq "end recorded" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["2026-10-23"]["ended"]["why"])' "$ST/holds.json")" time
eq "a recorded end does not leak into the morning itself" "$(act '2026-10-23 09:00')" 0

SLEEPIN_NOW='2026-10-31 09:00' soft "news C" 2>/dev/null
out=$(SLEEPIN_NOW='2026-10-31 09:10' "$S_BIN" release --reason "he is up")
eq "release now sends what waits" "$out:$(tail -n1 "$T/tg.log")" "released Sat 31.10: sent 1:news C"
eq "released: 09:15 send" "$(act '2026-10-31 09:15')" 1

SLEEPIN_NOW='2026-11-09 09:00' soft "ping D" 2>/dev/null
journal "2026-11-09T08:30:00.000Z"          # 09:30 local, after the landing + 60 min
SLEEPIN_NOW='2026-11-09 09:35' "$S_BIN" flush >/dev/null
eq "his message: the next flush sends" "$(tail -n1 "$T/tg.log"):$(qlen)" "ping D:0"
eq "end recorded as his message" "$(python3 -c 'import json,sys; e=json.load(open(sys.argv[1]))["2026-11-09"]["ended"]; print(e["why"], e["at"])' "$ST/holds.json")" "his message 2026-11-09 09:30"
: > "$JR"

touch "$T/tg-fail"
SLEEPIN_NOW='2026-11-09 08:00' soft "fails E" 2>/dev/null
eq "queued while it holds" "$(qlen)" 1
for m in 40 45 50; do SLEEPIN_NOW="2026-11-09 12:$m" "$S_BIN" flush >/dev/null; done
eq "3 failed sends: one inbox note, queue empty" "$(ls "$HOME/inbox" | grep -c '^sleepin-failed-'):$(qlen)" "1:0"
has "the note carries the text" "$(cat "$HOME"/inbox/sleepin-failed-*)" "fails E"
rm -f "$T/tg-fail"
js=$(SLEEPIN_NOW='2026-10-23 09:00' "$S_BIN" status --json)
eq "status --json parses" "$(printf '%s' "$js" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["active"], d["until"])')" "True 12:30"

# ---------------------------------------------------------------- remind: my nudges wait, his reminders don't
RT=$T/remind; mkdir -p "$RT"
export REMIND_DB=$RT/rem.json REMIND_INBOX=$RT/inbox REMIND_TG=$RT/tg REMIND_SLEEPIN_CMD=$RT/guard REMIND_PLACE_CMD="cat $RT/here.json"
cat > "$RT/tg" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$RT/tg.log"
STUB
cat > "$RT/guard" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$RT/guard.log"
[ -e "$RT/asleep" ]
STUB
chmod +x "$RT/tg" "$RT/guard"
R() { REMIND_NOW="$1" "$R_BIN" "${@:2}"; }
R "2026-10-23 08:00" add --owner "2026-10-23 09:00" "his own A" >/dev/null
R "2026-10-23 08:00" add --owner --tag bill:1 "2026-10-23 09:00" "nudge B" >/dev/null
R "2026-10-23 08:00" add --owner --soft "2026-10-23 09:00" "soft C" >/dev/null
R "2026-10-23 08:00" add --owner --tag vp:x1 "2026-10-23 09:00" "voice D" >/dev/null
R "2026-10-23 08:00" add "2026-10-23 09:00" "for me E" >/dev/null
R "2026-10-23 08:00" add --owner --tag deadline:x --every daily "2026-10-23 09:00" "series F" >/dev/null
R "2026-10-23 08:00" add --call "2026-10-23 09:00" "call G" >/dev/null
touch "$RT/asleep"
R "2026-10-23 09:00" due
log=$(cat "$RT/tg.log" 2>/dev/null)
has "his own reminder goes out" "$log" "his own A"
has "his voice-plan reminder goes out" "$log" "voice D"
has "his call reminder goes out" "$log" "call G"
case "$log" in *"nudge B"*|*"soft C"*|*"series F"*) no "my nudges wait" "$log" ;; *) ok "my nudges wait (tagged, --soft, tagged series)" ;; esac
eq "reminder for me: inbox note as usual" "$(cat "$RT"/inbox/* 2>/dev/null | grep -c 'for me E')" 1
eq "the guard was asked with the fake clock" "$(sort -u "$RT/guard.log")" "--at 2026-10-23 09:00"
eq "waiting ones stay in the store" "$(R '2026-10-23 09:00' list | grep -c 'nudge B\|soft C\|series F')" 3
c0=$(wc -l < "$RT/tg.log" | tr -d ' ')
R "2026-10-23 09:01" due
eq "still asleep 09:01: nothing more" "$(wc -l < "$RT/tg.log" | tr -d ' ')" "$c0"
rm -f "$RT/asleep"
R "2026-10-23 12:30" due
log=$(cat "$RT/tg.log")
has "after the guard: nudge B" "$log" "nudge B"
has "after the guard: soft C" "$log" "soft C"
has "after the guard: series F" "$log" "series F"
has "series moves on to the next slot" "$(R '2026-10-23 12:30' list)" "2026-10-24 09:00  [owner] series F"
eq "nothing sent twice" "$(sort "$RT/tg.log" | uniq -d | wc -l | tr -d ' ')" 0

# place-due: a location reminder held for quiet hours stays held on a sleep-in morning
: > "$RT/tg.log"
printf '{"place": {"kind": "away", "zone": null, "band": "near", "town": "Testtown", "flag": "away"}}' > "$RT/here.json"
R "2026-10-24 20:00" place-due; R "2026-10-24 20:11" place-due
R "2026-10-24 20:12" add --on home "place P" >/dev/null
R "2026-10-24 20:14" place-due
printf '{"place": {"kind": "home", "zone": "Home", "band": null, "town": null, "flag": "home"}}' > "$RT/here.json"
R "2026-10-25 02:10" place-due
eq "arrival 02:10: held (quiet hours)" "$(wc -l < "$RT/tg.log" | tr -d ' ')" 0
touch "$RT/asleep"
R "2026-10-25 08:00" place-due
eq "08:00 on a sleep-in morning: still held" "$(wc -l < "$RT/tg.log" | tr -d ' ')" 0
rm -f "$RT/asleep"
R "2026-10-25 12:30" place-due
has "guard over: the held location reminder goes out" "$(cat "$RT/tg.log")" "place P"
unset REMIND_DB REMIND_INBOX REMIND_TG REMIND_SLEEPIN_CMD REMIND_PLACE_CMD

# ---------------------------------------------------------------- trip clock: the guard follows his zone
# A night out abroad (UTC+4) ending 02:00 there = 23:00 at home: a late night only on his clock.
python3 - "$CAL" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d['events'].append({"id": "t1", "start": "2027-01-20T20:00:00+04:00", "end": "2027-01-21T02:00:00+04:00",
                    "all_day": False, "title": "Night out abroad", "flags": {}})
json.dump(d, open(sys.argv[1], 'w'))
PY
mkdir -p "$HOME/.config/tripclock"
printf '{"trips": [{"name": "t", "tz": "Asia/Dubai", "from": "2027-01-15", "to": "2027-01-30"}]}' > "$HOME/.config/tripclock/trips.json"
eq "trip: 05:59 his time (02:59 home) send" "$(act '2027-01-21 02:59')" 1
eq "trip: 06:30 his time (03:30 home) hold" "$(act '2027-01-21 03:30')" 0
eq "trip: 12:29 his time (09:29 home) hold" "$(act '2027-01-21 09:29')" 0
eq "trip: 12:30 his time (09:30 home) send" "$(act '2027-01-21 09:30')" 1
has "trip: -v shows his time" "$("$S_BIN" active -v --at '2027-01-21 05:00')" "HOLD until 12:30"
mv "$HOME/.config/tripclock/trips.json" "$HOME/.config/tripclock/trips.off"
eq "no trip: the same night ends 23:00 at home, no guard" "$(act '2027-01-21 05:00')" 1

# ---------------------------------------------------------------- the hub's Telegram rows instead of a journal
# A fake hub (POST /tg/q kind_payloads) on 127.0.0.1; the owner = the access file's first allowFrom.
cat > "$T/fakehub.py" <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
rows = {"tg.message": [{"id": 7, "payload": json.dumps({"user_id": 42, "chat_id": 42, "message_id": 5, "text": "x",
                                                        "event_type": "message", "ts": "2026-10-31T07:40:00.000Z"})},
                       {"id": 8, "payload": json.dumps({"user_id": 99, "chat_id": 99, "message_id": 6,
                                                        "ts": "2026-10-31T07:30:00.000Z"})}]}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        with open(sys.argv[2], "a") as f:
            f.write(json.dumps({"path": self.path, "body": b}) + "\n")
        out = json.dumps({"rows": rows.get((b.get("params") or {}).get("kind"), []), "truncated": False}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
srv = HTTPServer(("127.0.0.1", 0), H)
open(sys.argv[1], "w").write(str(srv.server_address[1]))
srv.serve_forever()
PY
python3 "$T/fakehub.py" "$T/hub.port" "$T/hub.calls" & HUBPID=$!
trap 'kill $HUBPID 2>/dev/null; rm -rf "$T"' EXIT
for _ in $(seq 300); do [ -s "$T/hub.port" ] && break; sleep 0.1; done  # 30 s: python start under load
mkdir -p "$HOME/.config/assistant" "$HOME/.secrets"
printf '{"allowFrom": ["42"]}' > "$(dirname "$JR")/access.json"
printf 't%.0s' $(seq 40) > "$HOME/.secrets/hub-token"; chmod 600 "$HOME/.secrets/hub-token"
export HUB_URL="http://127.0.0.1:$(cat "$T/hub.port")"
: > "$JR"
eq "hub: an unguarded morning: send" "$(act '2026-10-22 09:00')" 1
eq "hub: a guarded morning before its floor (08:30): hold" "$(act '2026-10-31 07:00')" 0
eq "hub: neither asks the hub" "$(cat "$T/hub.calls" 2>/dev/null | wc -l | tr -d ' ')" 0
eq "hub: his 08:40 hub message ends it: send" "$(act '2026-10-31 08:45')" 1
eq "hub: before it: hold (the 08:30 row is not his)" "$(act '2026-10-31 08:35')" 0
has "hub: reads kind_payloads" "$(head -1 "$T/hub.calls")" '"name": "kind_payloads"'
eq "SLEEPIN_HUB=off: hold" "$(SLEEPIN_HUB=off "$S_BIN" active --at '2026-10-31 08:45' >/dev/null 2>&1; echo $?)" 0
eq "SLEEPIN_JOURNAL set: no hub (hold)" "$(SLEEPIN_JOURNAL="$JR" "$S_BIN" active --at '2026-10-31 08:45' >/dev/null 2>&1; echo $?)" 0
eq "SLEEPIN_JOURNAL + SLEEPIN_HUB=on: send" "$(SLEEPIN_HUB=on SLEEPIN_JOURNAL="$JR" "$S_BIN" active --at '2026-10-31 08:45' >/dev/null 2>&1; echo $?)" 1
eq "hub down: the file alone (hold), no crash" "$(HUB_URL=http://127.0.0.1:9 "$S_BIN" active --at '2026-10-31 08:45' >/dev/null 2>&1; echo $?)" 0
has "hub down: logged" "$(tail -1 "$ST/sleepin.log")" "hub journal: HubDown"
unset HUB_URL
kill $HUBPID 2>/dev/null

# ---------------------------------------------------------------- isolation
eq "real sleepin state untouched" "$(real_sig)" "$BEFORE"
echo "pass=$pass fail=$fail"
[ "$fail" = 0 ]
