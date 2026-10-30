#!/usr/bin/env bash
# Tests for remind's IDs, snooze, done, buttons and press, and for ~/bin/tg-buttons (against a local mock Bot API).
# Temp state only: REMIND_DB/INBOX, stub REMIND_TG and REMIND_TG_BUTTONS, REMIND_NOW. Exit 0 = all pass.
# REMIND_BIN / TG_BUTTONS_BIN: test copies before installing. Run on its own or from run.sh.
set -uo pipefail
R=${REMIND_BIN:-/home/node/bin/remind}
TGB=${TG_BUTTONS_BIN:-/home/node/bin/tg-buttons}
REAL_DIR=/home/node/.local/state/remind
T=$(mktemp -d "${TMPDIR:-/tmp}/remind-snooze-tests.XXXXXX")
MOCK_PID=
trap '[ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2>/dev/null; rm -rf "${T:?}"' EXIT
export REMIND_DB=$T/rem.json REMIND_INBOX=$T/inbox REMIND_TG=$T/tg REMIND_TG_BUTTONS=$T/tgb
export REMIND_NOW="2026-10-20 10:00"
unset REMIND_BUTTONS
cat > "$T/tg" <<STUB
#!/bin/sh
[ -e "$T/tg-fail" ] && exit 1
printf '%s\n' "\$*" >> "$T/tg.log"
STUB
cat > "$T/tgb" <<STUB
#!/bin/sh
[ -e "$T/tgb-fail" ] && exit 1
printf '%s\n' "\$1" >> "$T/tgb.log"
printf '%s\n' "\$2" >> "$T/tgb.markup"
STUB
chmod +x "$T/tg" "$T/tgb"
M=SNZTEST   # marker in every test text: must never reach the live store

pass=0 fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if grep -qF -- "$3" <<<"$2"; then ok "$1"; else no "$1" "$2"; fi; }
reset() { find "$T" -mindepth 1 -maxdepth 1 ! -name tg ! -name tgb ! -name mock.py -exec rm -rf {} +; }
py() { python3 -I -c "$@"; }
idof() { py 'import json,sys; print(" ".join(r["id"] for r in json.load(open(sys.argv[1])) if sys.argv[2] in r["text"]))' "$REMIND_DB" "$1"; }
store() { py 'import json,sys
d = json.load(open(sys.argv[1]))
print(" | ".join(" ".join([r["id"], r["at"], "every" if r.get("every") else "once"] + (["tag=" + r["tag"]] if r.get("tag") else [])) for r in d))' "$REMIND_DB"; }
nstore() { py 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$REMIND_DB"; }
rstate() { py 'import json,sys; d=json.load(open(sys.argv[1])); o=d.get(sys.argv[2]); print("none" if o is None else o["state"] + (" " + o["to"] if o.get("to") else ""))' "$T/recent.json" "$1"; }
due() { REMIND_NOW="$1" timeout 20 "$R" due; }
at() { REMIND_NOW="$1" timeout 20 "$R" "${@:2}"; }
lines() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }

# ---------------------------------------------------------------- IDs
reset
"$R" add --owner --tag bill:9 "2026-10-20 10:00" "$M pay rent" >/dev/null
id=$(idof "pay rent")
if grep -qE '^[0-9a-f]{8}$' <<<"$id"; then ok "add gives an 8-hex ID"; else no "add gives an 8-hex ID" "$id"; fi
eq "list unchanged (no ID)" "$("$R" list)" " 1. 2026-10-20 10:00  [owner] $M pay rent  (tag bill:9)"
eq "list --ids appends it" "$("$R" list --ids)" " 1. 2026-10-20 10:00  [owner] $M pay rent  (tag bill:9)  [id $id]"
"$R" add "2026-10-21 08:00" "$M other" >/dev/null
o=$(idof "other"); [ "$o" != "$id" ] && ok "IDs differ" || no "IDs differ" "$o $id"

reset
printf '[{"at": "2026-10-21 09:00", "text": "%s legacy A", "owner": true}, {"at": "2026-10-21 09:00", "text": "%s legacy A", "owner": true}]' "$M" "$M" > "$REMIND_DB"
out=$("$R" list)
eq "legacy store: list output as before" "$out" " 1. 2026-10-21 09:00  [owner] $M legacy A
 2. 2026-10-21 09:00  [owner] $M legacy A"
eq "  and both entries got distinct IDs, saved" "$(py 'import json,sys; d=json.load(open(sys.argv[1])); print(len({r["id"] for r in d}))' "$REMIND_DB")" "2"

reset
printf '[{"at": "2026-10-20 10:00", "text": "%s journaled", "owner": true}]' "$M" > "$REMIND_DB"
key=$(py 'import hashlib,json,sys; r=json.load(open(sys.argv[1]))[0]; print("202610201000-" + hashlib.sha1(json.dumps(r, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12])' "$REMIND_DB")
printf '%s\n' "$key" > "$REMIND_DB.journal"
due "2026-10-20 10:00"
eq "a journal key from before IDs still stops a second delivery" "$(lines "$T/tgb.log") $(lines "$T/tg.log") $(nstore)" "0 0 0"

# ---------------------------------------------------------------- owner fire with buttons
reset
"$R" add --owner --tag bill:9 "2026-10-20 10:00" "$M pay rent" >/dev/null
id=$(idof "pay rent")
want="[[{\"text\": \"+1 h\", \"data\": \"rm:s1h:$id\"}, {\"text\": \"Tonight 19:00\", \"data\": \"rm:ston:$id\"}], [{\"text\": \"Tomorrow 09:00\", \"data\": \"rm:stom:$id\"}, {\"text\": \"Done\", \"data\": \"rm:done:$id\"}]]"
eq "buttons ID: exact rows" "$("$R" buttons "$id")" "$want"
eq "  callback data <= 64 bytes" "$("$R" buttons "$id" | py 'import json,sys; print(max(len(b["data"].encode()) for row in json.load(sys.stdin) for b in row) <= 64)')" "True"
due "2026-10-20 10:00"
eq "owner fire goes out with the buttons" "$(cat "$T/tgb.log") | $(cat "$T/tgb.markup") | $(lines "$T/tg.log")" "⏰ $M pay rent | $want | 0"
eq "  one-off left the store, fired occurrence kept" "$(nstore) $(rstate "$id")" "0 fired"
eq "  recent.json is 0600" "$(stat -c %a "$T/recent.json")" "600"
eq "buttons work for the fired ID" "$("$R" buttons "$id")" "$want"

# ---------------------------------------------------------------- snooze / press / double press
out=$(at "2026-10-20 10:05" press "rm:s1h:$id"); rc=$?
eq "press rm:s1h: back in the store, same ID, text and tag" "$rc | $out | $(store)" \
   "0 | snoozed to Tue 2026-10-20 11:05: $M pay rent | $id 2026-10-20 11:05 once tag=bill:9"
eq "  recent says snoozed" "$(rstate "$id")" "snoozed 2026-10-20 11:05"
out=$(at "2026-10-20 10:05" press "rm:s1h:$id"); rc=$?
eq "double press +1h: no duplicate, no error" "$rc | $(nstore) | $out" "0 | 1 | already snoozed to Tue 2026-10-20 11:05: $M pay rent"
at "2026-10-20 10:07" press "rm:s1h:$id" >/dev/null
eq "  a later second press moves the same entry" "$(store)" "$id 2026-10-20 11:07 once tag=bill:9"
due "2026-10-20 11:07"
eq "snoozed copy fires again with the same ID's buttons" "$(lines "$T/tgb.log") $(tail -n1 "$T/tgb.markup" | grep -c "rm:done:$id") $(nstore) $(rstate "$id")" "2 1 0 fired"
out=$(at "2026-10-20 11:10" press "rm:done:$id"); rc=$?
eq "press rm:done: done" "$rc | $out | $(rstate "$id") | $(nstore)" "0 | done: $M pay rent | done | 0"
out=$(at "2026-10-20 11:11" press "rm:done:$id"); rc=$?
eq "double press done: already done, exit 0, nothing added" "$rc | $out | $(nstore)" "0 | already done: $M pay rent | 0"
out=$(at "2026-10-20 11:12" snooze "$id" tomorrow); rc=$?
eq "snooze after done brings it back (he changed his mind)" "$rc | $(store)" "0 | $id 2026-10-21 09:00 once tag=bill:9"
out=$(at "2026-10-20 11:13" done "$id"); rc=$?
eq "done on a pending snoozed copy removes it" "$rc | $(nstore) | $(rstate "$id")" "0 | 0 | done"
out=$(at "2026-10-20 11:14" done "$id"); rc=$?
eq "  done again: already done" "$rc | $out" "0 | already done: $M pay rent"

reset
"$R" add --owner "2026-10-20 12:00" "$M pending" >/dev/null
p=$(idof pending)
eq "snooze a not-yet-fired one-off moves it" "$(at "2026-10-20 10:00" snooze "$p" +30m) | $(store)" \
   "snoozed to Tue 2026-10-20 10:30: $M pending | $p 2026-10-20 10:30 once"
eq "done on a not-yet-fired one-off removes it" "$(at "2026-10-20 10:00" done "$p") | $(nstore) | $(rstate "$p")" \
   "done: $M pending | 0 | done"

# ---------------------------------------------------------------- times (Europe/Zurich wall time)
reset
"$R" add --owner "2026-12-31 23:00" "$M times" >/dev/null
t=$(idof times)
s() { at "$1" snooze "$t" "$2" >/dev/null; store | cut -d' ' -f2-3; }
eq "tonight before 19:00 = today 19:00" "$(s "2026-10-20 10:00" tonight)" "2026-10-20 19:00"
eq "tonight at 19:00 = tomorrow 19:00" "$(s "2026-10-20 19:00" tonight)" "2026-10-21 19:00"
eq "tonight after 19:00 = tomorrow 19:00" "$(s "2026-10-20 20:30" tonight)" "2026-10-21 19:00"
eq "tonight on New Year's Eve 20:00 rolls into the next year" "$(s "2026-12-31 20:00" tonight)" "2027-01-01 19:00"
eq "tomorrow = tomorrow 09:00" "$(s "2026-10-20 23:59" tomorrow)" "2026-10-21 09:00"
eq "tomorrow at 00:30 = that next day 09:00" "$(s "2026-10-21 00:30" tomorrow)" "2026-10-22 09:00"
eq "tomorrow at month end" "$(s "2026-10-31 18:00" tomorrow)" "2026-11-01 09:00"
eq "+1h at 23:30 crosses midnight" "$(s "2026-10-20 23:30" +1h)" "2026-10-21 00:30"
eq "+1h on the DST night (wall time)" "$(s "2026-10-25 01:30" +1h)" "2026-10-25 02:30"
eq "+2d" "$(s "2026-10-20 10:00" +2d)" "2026-10-22 10:00"
eq "ISO time" "$(s "2026-10-20 10:00" "2026-10-23 08:15")" "2026-10-23 08:15"
eq "ISO with T" "$(s "2026-10-20 10:00" "2026-10-24T07:45")" "2026-10-24 07:45"
before=$(store)
out=$(at "2026-10-20 10:00" snooze "$t" "2026-10-19 08:00" 2>&1); rc=$?
eq "ISO in the past: exit 2, unchanged" "$rc $(store | cut -d' ' -f2-3)" "2 2026-10-24 07:45"
out=$(at "2026-10-20 10:00" snooze "$t" soon 2>&1); rc=$?
eq "bad WHEN: exit 2" "$rc" "2"
out=$(at "2026-10-20 10:00" snooze "$t" +0h 2>&1); rc=$?
eq "+0h: exit 2" "$rc" "2"

# ---------------------------------------------------------------- unknown IDs, bad data
out=$("$R" snooze deadbeef +1h 2>&1); rc=$?; eq "snooze unknown ID: exit 1" "$rc | $out" "1 | remind: no reminder with ID deadbeef"
out=$("$R" done deadbeef 2>&1); rc=$?; eq "done unknown ID: exit 1" "$rc" "1"
out=$("$R" buttons deadbeef 2>&1); rc=$?; eq "buttons unknown ID: exit 1, no JSON" "$rc | $out" "1 | remind: no reminder with ID deadbeef"
out=$("$R" press rm:done:deadbeef 2>&1); rc=$?; eq "press unknown ID: exit 1" "$rc" "1"
out=$("$R" done xyz 2>&1); rc=$?; eq "malformed ID: exit 2" "$rc" "2"
for d in rm:zzz:12345678 rm:done:XYZ shop:order "rm:s1h:$t:extra" ""; do
  "$R" press "$d" >/dev/null 2>&1; rc=$?; eq "press bad data '$d': exit 2" "$rc" "2"
done
"$R" press >/dev/null 2>&1; eq "press without data: exit 2" "$?" "2"
eq "  store untouched by all of these" "$(store | cut -d' ' -f2-3)" "2026-10-24 07:45"

# ---------------------------------------------------------------- recurring series
reset
"$R" add --owner --every daily "2026-10-20 10:00" "$M vitamins" >/dev/null
sid=$(idof vitamins)
due "2026-10-20 10:00"
occ=$(tail -n1 "$T/tgb.markup" | py 'import json,sys; print(json.load(sys.stdin)[1][1]["data"].split(":")[2])')
[ -n "$occ" ] && [ "$occ" != "$sid" ] && ok "series occurrence has its own ID" || no "series occurrence has its own ID" "$occ $sid"
eq "  series moved on, occurrence recorded" "$(store) | $(rstate "$occ")" "$sid 2026-10-21 10:00 every | fired"
out=$(at "2026-10-20 10:02" press "rm:ston:$occ"); rc=$?
eq "snooze an occurrence: one-off copy, series intact" "$rc | $(store)" "0 | $occ 2026-10-20 19:00 once | $sid 2026-10-21 10:00 every"
at "2026-10-20 10:03" press "rm:ston:$occ" >/dev/null
eq "  double press: still one copy" "$(nstore)" "2"
eq "done on the occurrence removes only the copy" "$(at "2026-10-20 10:04" press "rm:done:$occ") | $(store)" \
   "done: $M vitamins | $sid 2026-10-21 10:00 every"
out=$("$R" snooze "$sid" +1h 2>&1); rc=$?; eq "snooze a series ID: exit 1" "$rc" "1"
out=$("$R" done "$sid" 2>&1); rc=$?; eq "done a series ID: exit 1, series kept" "$rc $(nstore)" "1 1"
out=$("$R" buttons "$sid" 2>&1); rc=$?; eq "buttons for a series ID: exit 1" "$rc" "1"
due "2026-10-21 10:00"
occ2=$(tail -n1 "$T/tgb.markup" | py 'import json,sys; print(json.load(sys.stdin)[0][0]["data"].split(":")[2])')
[ "$occ2" != "$occ" ] && ok "next occurrence gets a new ID" || no "next occurrence gets a new ID" "$occ2"

# ---------------------------------------------------------------- fallbacks and notes
reset
"$R" add --owner "2026-10-20 10:00" "$M fb" >/dev/null; f=$(idof fb)
touch "$T/tgb-fail"; due "2026-10-20 10:00"
eq "buttons sender fails: plain tg" "$(cat "$T/tg.log") $(lines "$T/tgb.log")" "⏰ $M fb 0"
reset
"$R" add --owner "2026-10-20 10:00" "$M fb2" >/dev/null; f=$(idof fb2)
touch "$T/tgb-fail" "$T/tg-fail"; due "2026-10-20 10:00"
note=$(cat "$T"/inbox/reminder-*.md 2>/dev/null)
has "both fail: the inbox note carries the ID" "$note" "ID $f): $M fb2"
has "  and says to send it with remind buttons" "$note" "remind buttons ID"
eq "  its buttons still work" "$("$R" buttons "$f" | grep -c "rm:done:$f")" "1"
reset
"$R" add --owner "2026-10-20 10:00" "$M plain" >/dev/null
REMIND_BUTTONS=0 due "2026-10-20 10:00"
eq "REMIND_BUTTONS=0: plain tg only" "$(lines "$T/tg.log") $(lines "$T/tgb.log")" "1 0"
reset
"$R" add --owner "2026-10-20 10:00" "$M stubbed" >/dev/null
env -u REMIND_TG_BUTTONS REMIND_NOW="2026-10-20 10:00" "$R" due
eq "stubbed REMIND_TG without REMIND_TG_BUTTONS: never the real buttons sender" "$(lines "$T/tg.log")" "1"
reset
"$R" add "2026-10-20 10:00" "$M my own" >/dev/null; me=$(idof "my own")
due "2026-10-20 10:00"
has "my own reminder: note with ID" "$(cat "$T"/inbox/reminder-*.md)" "ID $me): $M my own"
eq "  snooze after firing restores it as mine" "$(at "2026-10-20 10:01" snooze "$me" +1h) | $(py 'import json,sys; print(json.load(open(sys.argv[1]))[0]["owner"])' "$REMIND_DB")" \
   "snoozed to Tue 2026-10-20 11:01: $M my own | False"

# ---------------------------------------------------------------- recent.json pruning
reset
printf '{"0000000a": {"at": "2026-09-01 10:00", "text": "%s old", "state": "fired", "seen": "2026-09-01 10:00"}}' "$M" > "$T/recent.json"
"$R" add --owner "2026-10-20 10:00" "$M new" >/dev/null
due "2026-10-20 10:00"
eq "occurrences untouched for 30 days are pruned" "$(rstate 0000000a) $(py 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$T/recent.json")" "none 1"

# ---------------------------------------------------------------- tg-buttons against a mock Bot API
cat > "$T/mock.py" <<'PY'
import http.server, sys, urllib.parse, os
d = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()
        with open(os.path.join(d, 'mock.log'), 'a') as f:
            f.write(self.path + '\n' + repr(urllib.parse.parse_qs(body)) + '\n')
        code = 400 if os.path.exists(os.path.join(d, 'mock-400')) else 200
        self.send_response(code); self.end_headers(); self.wfile.write(b'{"ok": %s}' % (b'true' if code == 200 else b'false'))
    def log_message(self, *a): pass
s = http.server.HTTPServer(('127.0.0.1', 0), H)
open(os.path.join(d, 'port.tmp'), 'w').write(str(s.server_port)); os.replace(os.path.join(d, 'port.tmp'), os.path.join(d, 'port'))
s.serve_forever()
PY
timeout 120 python3 -I "$T/mock.py" "$T" & MOCK_PID=$!
n=0; while [ ! -s "$T/port" ] && [ $n -lt 50 ]; do sleep 0.1; n=$((n + 1)); done
printf 'TELEGRAM_BOT_TOKEN=123:FAKE\n' > "$T/fake.env"; printf '{"allowFrom": ["4242"]}' > "$T/fake-access.json"
export TG_BUTTONS_ENV=$T/fake.env TG_BUTTONS_ACCESS=$T/fake-access.json TG_BUTTONS_API="http://127.0.0.1:$(cat "$T/port")"
"$TGB" "⏰ $M hi" "$want"; rc=$?
log=$(cat "$T/mock.log" 2>/dev/null)
eq "tg-buttons: sent" "$rc" "0"
has "  to sendMessage with the fake token" "$log" "/bot123:FAKE/sendMessage"
has "  chat id from access.json" "$log" "'chat_id': ['4242']"
has "  text kept" "$log" "⏰ $M hi"
mk=$(printf '%s' "$log" | py 'import ast,sys; l=sys.stdin.read().splitlines(); print(ast.literal_eval(l[1])["reply_markup"][0])')
eq "  reply_markup = inline_keyboard with callback_data" "$mk" \
   "{\"inline_keyboard\":[[{\"text\":\"+1 h\",\"callback_data\":\"rm:s1h:$id\"},{\"text\":\"Tonight 19:00\",\"callback_data\":\"rm:ston:$id\"}],[{\"text\":\"Tomorrow 09:00\",\"callback_data\":\"rm:stom:$id\"},{\"text\":\"Done\",\"callback_data\":\"rm:done:$id\"}]]}"
touch "$T/mock-400"; "$TGB" "x" "$want" 2>/dev/null; rc=$?
[ "$rc" -ne 0 ] && ok "tg-buttons: Telegram refuses → non-zero" || no "tg-buttons: Telegram refuses → non-zero" "$rc"
n0=$(lines "$T/mock.log")
"$TGB" "x" 'not json' 2>/dev/null; eq "tg-buttons: bad JSON → exit 2, nothing sent" "$? $(lines "$T/mock.log")" "2 $n0"
"$TGB" "x" "[[{\"text\":\"a\",\"data\":\"$(printf 'x%.0s' $(seq 65))\"}]]" 2>/dev/null; eq "tg-buttons: data over 64 bytes → exit 2" "$?" "2"
"$TGB" "x" 2>/dev/null; eq "tg-buttons: one argument → exit 2" "$?" "2"

# ---------------------------------------------------------------- live store
leak=$(cat "$REAL_DIR/reminders.json" "$REAL_DIR/recent.json" 2>/dev/null | grep -c "$M")
eq "live reminders.json / recent.json: no test text" "$leak" "0"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
