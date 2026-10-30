#!/usr/bin/env bash
# Tests for remind's location reminders (add --on home|leave|zone:NAME, place-due, list/cancel/snooze/done/buttons)
# against a stubbed `vault ha now --json` (REMIND_PLACE_CMD). Temp state only: REMIND_DB/INBOX, stub REMIND_TG and
# REMIND_TG_BUTTONS, REMIND_NOW. Exit 0 = all pass. REMIND_BIN: a test copy before installing. Run alone or from run.sh.
set -uo pipefail
R=${REMIND_BIN:-/home/node/bin/remind}
REAL_DIR=/home/node/.local/state/remind
T=$(mktemp -d "${TMPDIR:-/tmp}/remind-place-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
export REMIND_DB=$T/rem.json REMIND_INBOX=$T/inbox REMIND_TG=$T/tg REMIND_TG_BUTTONS=$T/tgb REMIND_PLACE_CMD=$T/vault
export REMIND_NOW="2026-10-20 10:00"
unset REMIND_BUTTONS REMIND_PLACE_DWELL REMIND_BACK_OFFER REMIND_BACK_HOURS REMIND_TG_CMD
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
cat > "$T/vault" <<STUB
#!/bin/sh
printf 'x\n' >> "$T/vault.calls"
[ -e "$T/vault-fail" ] && exit 1
cat "$T/place.json"
STUB
chmod +x "$T/tg" "$T/tgb" "$T/vault"
M=PLCTEST   # marker in every test text: must never reach the live store

pass=0 fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if grep -qF -- "$3" <<<"$2"; then ok "$1"; else no "$1" "$2"; fi; }
reset() { find "$T" -mindepth 1 -maxdepth 1 ! -name tg ! -name tgb ! -name vault -exec rm -rf {} +; }
lines() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }
py() { python3 -I -c "$@"; }
here() {   # here home|away|none|zone NAME → what the stubbed vault reports
  case $1 in
    home) printf '{"place": {"kind": "home", "zone": "Home", "band": null, "town": null, "flag": "home"}}' ;;
    away) printf '{"place": {"kind": "away", "zone": null, "band": "near", "town": "Testtown", "flag": "away"}}' ;;
    zone) printf '{"place": {"kind": "zone", "zone": "%s", "band": "region", "town": null, "flag": "away"}}' "$2" ;;
    none) printf '{"place": null, "activity": "still"}' ;;
  esac > "$T/place.json"; }
poll() { here "${@:2}"; REMIND_NOW="2026-10-20 $1" timeout 30 "$R" place-due; }   # poll HH:MM KIND [ZONE]
sent() { lines "$T/tgb.log"; }
last() { tail -n1 "$T/tgb.log"; }
plids() { py 'import json,sys
try: print(" ".join(r["id"] for r in json.load(open(sys.argv[1]))))
except FileNotFoundError: print("")' "$T/places.json"; }
rstate() { py 'import json,sys; o=json.load(open(sys.argv[1])).get(sys.argv[2]); print("none" if o is None else o["state"] + (" " + o["to"] if o.get("to") else ""))' "$T/recent.json" "$1"; }
addid() { "$R" add "$@" | sed -n 's/^added location reminder \([0-9a-f]\{8\}\):.*/\1/p'; }

# ---------------------------------------------------------------- add, list, validation
reset
out=$("$R" add --on home "$M fp2")
id=$(printf '%s' "$out" | sed -n 's/^added location reminder \([0-9a-f]\{8\}\):.*/\1/p')
eq "add --on home: ID + wording" "$out" "added location reminder $id: 📍 when home, next time only"
eq "list shows it as 📍 when home" "$("$R" list)" " 1. 📍 when home: $M fp2"
eq "list --ids" "$("$R" list --ids)" " 1. 📍 when home: $M fp2  [id $id]"
eq "timed store untouched" "$([ -e "$REMIND_DB" ] && grep -c "$M" "$REMIND_DB" || echo 0)" "0"
"$R" add --on garage "$M x" >/dev/null 2>&1; eq "bad --on: exit 2" "$?" "2"
"$R" add --on home --until 2026-11-01 "$M x" >/dev/null 2>&1; eq "--until with --on: exit 2" "$?" "2"
"$R" add --on home --every --once "$M x" >/dev/null 2>&1; eq "--every with --once: exit 2" "$?" "2"
"$R" add --on zone:Home --every "$M zh" >/dev/null
eq "zone:Home = home, every time" "$("$R" list | sed -n 2p)" " 2. 📍 when home: $M zh  (every time)"

# ---------------------------------------------------------------- home → away (dwell) → home, once
reset
REMIND_BACK_OFFER=0 poll 09:58 home; eq "no location reminders, welcome-back off: no vault call" "$(lines "$T/vault.calls")" "0"
id=$(addid --on home "$M fp2")
poll 10:02 home; eq "first reading = baseline, no fire" "$(sent)" "0"
poll 10:04 away; poll 10:08 away; poll 10:14 away; poll 10:15 home
eq "away 11 min, then home: fires once" "$(sent)" "1"
eq "message" "$(last)" "📍 Home: $M fp2"
has "buttons: next time home" "$(tail -n1 "$T/tgb.markup")" "\"rm:snxt:$id\""
eq "once: gone from places" "$(plids)" ""
eq "recent: fired" "$(rstate "$id")" "fired"
poll 10:30 away; poll 10:45 away; poll 10:50 home; eq "once: never again" "$(sent)" "1"
eq "fired.log counts it" "$(lines "$T/fired.log")" "1"

# ---------------------------------------------------------------- buttons, snooze next / +1h, done
has "remind buttons: place rows" "$("$R" buttons "$id")" "📍 Next time home"
out=$("$R" press "rm:snxt:$id"); eq "press snxt: back in places, same ID" "$(plids)" "$id"
has "press snxt: message" "$out" "snoozed to next time: 📍 when home"
has "press snxt twice: already waiting" "$("$R" press "rm:snxt:$id")" "already waiting"
"$R" press "rm:s1h:$id" >/dev/null
eq "then +1 h: a timed one-off with the same ID" "$(py 'import json,sys; print([r["at"] for r in json.load(open(sys.argv[1])) if r["id"]==sys.argv[2]])' "$REMIND_DB" "$id")" "['2026-10-20 11:00']"
eq "  and out of places" "$(plids)" ""
"$R" press "rm:snxt:$id" >/dev/null
eq "then next time again: back in places" "$(plids)" "$id"
eq "  and out of the timed store" "$(grep -c "$id" "$REMIND_DB")" "0"
has "done on the waiting one" "$("$R" press "rm:done:$id")" "done: $M fp2"
eq "  gone, recent done" "$(plids) $(rstate "$id")" " done"
p=$(addid --on leave "$M never")
"$R" snooze "$p" +1h >/dev/null 2>&1; eq "snooze before it fired: exit 1" "$?" "1"
s=$(addid --every --on home "$M series")
"$R" done "$s" >/dev/null 2>&1; eq "done on a location series: exit 1" "$?" "1"

# ---------------------------------------------------------------- leave + flapping, --every
reset
l=$(addid --on leave "$M bins"); e=$(addid --every --on home "$M every")
poll 10:01 home; poll 10:03 away; poll 10:06 home
eq "away 3 min (flap): nothing" "$(sent)" "0"
poll 10:08 away; poll 10:16 away; eq "away 8 min: not left yet" "$(sent)" "0"
poll 10:18 away; eq "away 10 min: leave fires" "$(sent)" "1"
eq "leave message" "$(last)" "📍 Left home: $M bins"
poll 10:20 home; eq "back home: every fires" "$(sent)" "2"
poll 10:23 away; poll 10:26 home; poll 10:28 away; poll 10:31 home
eq "flapping within 10 min = one fire" "$(sent)" "2"
poll 10:33 away; poll 10:45 away; poll 10:47 home
eq "real trip out and back: fires again" "$(sent)" "3"
eq "series stays" "$(plids)" "$e"
occ=$(tail -n1 "$T/tgb.markup" | sed -n 's/.*rm:done:\([0-9a-f]\{8\}\).*/\1/p')
[ -n "$occ" ] && [ "$occ" != "$e" ] && ok "occurrence ID differs from the series ID" || no "occurrence ID" "$occ"
eq "occurrence: snooze next = once" "$("$R" snooze "$occ" next >/dev/null; "$R" list | grep -c "📍 when home: $M every$")" "1"

# ---------------------------------------------------------------- quiet hours
reset
export REMIND_NOW="2026-10-20 23:00"
n=$(addid --on home "$M night"); u=$(addid --urgent --on home "$M urgent")
poll 23:02 away
here home; REMIND_NOW="2026-10-21 02:10" "$R" place-due
eq "02:10 arrival: only --urgent rings" "$(sent) $(last)" "1 📍 Home: $M urgent"
has "list: held until 08:00" "$("$R" list)" "📍 when home: $M night  (held until 08:00)"
here home; REMIND_NOW="2026-10-21 07:58" "$R" place-due; eq "07:58: still held" "$(sent)" "1"
here home; REMIND_NOW="2026-10-21 08:00" "$R" place-due
eq "08:00: sent with the arrival time" "$(sent) $(last)" "2 📍 Home (since 02:10): $M night"
eq "  and gone" "$(plids)" ""
export REMIND_NOW="2026-10-20 10:00"

# ---------------------------------------------------------------- trip clock: quiet hours on his clock (UTC+4)
reset
export TRIPCLOCK_CONFIG=$T/trips.json TRIPCLOCK_FLIGHTS=$T/no-flights
printf '{"trips": [{"name": "t", "tz": "Asia/Dubai", "from": "2026-10-19", "to": "2026-10-23"}]}' > "$T/trips.json"
export REMIND_NOW="2026-10-20 22:00"
n=$(addid --on zone:Hotel "$M hotel")
poll 22:01 away
here zone Hotel; REMIND_NOW="2026-10-20 22:30" "$R" place-due
eq "trip: 22:30 home = 00:30 his time: held" "$(sent)" "0"
has "trip: list: held until 08:00 his time = 06:00 home" "$("$R" list)" "(held until 06:00)"
here zone Hotel; REMIND_NOW="2026-10-21 05:58" "$R" place-due; eq "trip: 05:58 home (07:58 his): still held" "$(sent)" "0"
here zone Hotel; REMIND_NOW="2026-10-21 06:00" "$R" place-due
eq "trip: 06:00 home (08:00 his): sent" "$(sent)" "1"
rm -f "$T/trips.json"
reset
here home; REMIND_NOW="2026-10-20 21:58" "$R" add --on zone:Hotel "$M hotel2" >/dev/null
REMIND_NOW="2026-10-20 22:00" "$R" place-due >/dev/null
here zone Hotel; REMIND_NOW="2026-10-20 22:30" "$R" place-due
eq "no trip: 22:30 is not quiet, sent at once" "$(sent)" "1"
export REMIND_NOW="2026-10-20 10:00"

# ---------------------------------------------------------------- unknown zone, vault errors, zones
reset
w=$(addid --on "zone:work" "$M work"); g=$(addid --on zone:Gym "$M gym")
poll 10:01 away; poll 10:03 none
eq "unknown place: state kept" "$(py 'import json,sys; print(json.load(open(sys.argv[1]))["zone"])' "$T/place-state.json")" "away"
touch "$T/vault-fail"; poll 10:05 away; rc=$?; rm -f "$T/vault-fail"
eq "vault error: exit 0, nothing sent" "$rc $(sent)" "0 0"
poll 10:07 zone Work
eq "zone Work (case-insensitive) fires, Gym not" "$(sent) $(last)" "1 📍 At work: $M work"
eq "gym still waiting" "$(plids)" "$g"
st=$(cat "$T/place-state.json")
case $st in *Testtown*|*band*|*town*) no "state holds zone keys only" "$st" ;; *) ok "state holds zone keys only" ;; esac
eq "state is 0600" "$(stat -c %a "$T/place-state.json")" "600"
touch "$T/vault-fail"; rc=0
for k in $(seq 1 29); do REMIND_NOW="2026-10-20 11:00" "$R" place-due || rc=1; done
eq "29 vault errors in a row: still exit 0" "$rc" "0"
REMIND_NOW="2026-10-20 11:00" "$R" place-due 2>/dev/null; eq "30th: exit 1 (cron-fail)" "$?" "1"
rm -f "$T/vault-fail"; poll 11:02 away; eq "recovered: exit 0" "$?" "0"

# ---------------------------------------------------------------- stale baseline: added while home
reset
export REMIND_NOW="2026-10-18 09:59"; x=$(addid --on leave "$M old")
here away; REMIND_NOW="2026-10-18 10:00" "$R" place-due; "$R" done "$x" >/dev/null
export REMIND_NOW="2026-10-20 12:00"; h=$(addid --on home "$M stale")
poll 12:01 home; eq "added at home, old state 'away': no fire" "$(sent)" "0"
poll 12:03 away; poll 12:15 away; poll 12:17 home; eq "next real arrival fires" "$(sent)" "1"
export REMIND_NOW="2026-10-20 10:00"

# ---------------------------------------------------------------- cancel, list numbering
reset
"$R" add --owner "2026-10-21 09:00" "$M timed" >/dev/null
addid --on home "$M a" >/dev/null; addid --on leave "$M b" >/dev/null
eq "list: timed first, places numbered on" "$("$R" list)" " 1. 2026-10-21 09:00  [owner] $M timed
 2. 📍 when home: $M a
 3. 📍 when leaving home: $M b"
eq "cancel 3" "$("$R" cancel 3)" "cancelled: 📍 when leaving home: $M b"
eq "cancel --match" "$("$R" cancel --match "$M a")" "cancelled: 📍 when home: $M a"
eq "skip on a place: exit 1" "$(addid --on home "$M c" >/dev/null; "$R" skip 2 >/dev/null; echo $?)" "1"

# ---------------------------------------------------------------- Telegram down → inbox note
reset
id=$(addid --on home "$M note")
touch "$T/tgb-fail" "$T/tg-fail"
poll 10:01 away; poll 10:11 away; poll 10:13 home
note=$(cat "$T"/inbox/reminder-place-*.md 2>/dev/null)
has "tg down: inbox note with the ID" "$note" "ID $id): $M note"
has "  and the place" "$note" "set for 📍 when home"

# ---------------------------------------------------------------- live store
leak=$(cat "$REAL_DIR/reminders.json" "$REAL_DIR/recent.json" "$REAL_DIR/places.json" 2>/dev/null | grep -c "$M")
eq "live stores: no test text" "$leak" "0"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
