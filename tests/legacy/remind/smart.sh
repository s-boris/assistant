#!/usr/bin/env bash
# Tests for Reminders 2.0 in ~/bin/remind: `when` (natural phrases, calendar anchors via a temp calcache), add with a
# phrase (time or place), move, aliases, help exit 0, list --json, instant press via $TELEGRAM_CALLBACK_DATA, and
# leave:zone:NAME location reminders. Temp state only (REMIND_DB/INBOX, stub tg/tgb/vault, CALCACHE_DIR, REMIND_NOW).
# REMIND_BIN: a copy under test (its lib/ next to it). Exit 0 = all pass; parser unit tests: when_test.py.
set -uo pipefail
R=${REMIND_BIN:-/home/node/bin/remind}
REAL_DIR=/home/node/.local/state/remind
T=$(mktemp -d "${TMPDIR:-/tmp}/remind-smart-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
export REMIND_DB=$T/rem.json REMIND_INBOX=$T/inbox REMIND_TG=$T/tg REMIND_TG_BUTTONS=$T/tgb REMIND_PLACE_CMD=$T/vault
export CALCACHE_DIR=$T/cal TRIPCLOCK_CONFIG=$T/trips.json TRIPCLOCK_FLIGHTS=$T/no-flights USAGE=off
export REMIND_NOW="2026-10-21 10:00"
unset REMIND_BUTTONS TELEGRAM_CALLBACK_DATA REMIND_VIA
mkdir -p "$T/cal"
cat > "$T/cal/events.json" <<'JSON'
{"version": 1, "events": [
 {"start": "2026-10-22T19:00:00+02:00", "end": "2026-10-22T22:30:00+02:00", "all_day": false, "title": "Big Band Live", "free": true, "flags": {"travel": null}},
 {"start": "2026-11-06T20:15:00+01:00", "end": "2026-11-06T21:45:00+01:00", "all_day": false, "title": "XY 153", "free": false, "flags": {"travel": "flight"}}]}
JSON
for s in tg tgb; do printf '#!/bin/sh\nprintf "%%s\\n" "$1" >> "%s/%s.log"\n' "$T" "$s" > "$T/$s"; chmod +x "$T/$s"; done
cat > "$T/vault" <<STUB
#!/bin/sh
cat "$T/where"
STUB
chmod +x "$T/vault"
M=SMARTTEST
pass=0 fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if grep -qF -- "$3" <<<"$2"; then ok "$1"; else no "$1" "$2"; fi; }
py() { python3 -I -c "$@"; }
at_of() { py 'import json,sys; print(" ".join(r["at"] for r in json.load(open(sys.argv[1])) if sys.argv[2] in r["text"]))' "$REMIND_DB" "$1"; }
idof() { py 'import json,sys; print(" ".join(r["id"] for r in json.load(open(sys.argv[1])) if sys.argv[2] in r["text"]))' "$REMIND_DB" "$1"; }

# A. when
eq "when: tomorrow evening" "$("$R" when tomorrow evening | cut -c1-16)" "2026-10-22 19:00"
eq "when: German day + time" "$("$R" when 'Freitag um 18 Uhr' | cut -c1-16)" "2026-10-23 18:00"
eq "when: calendar anchor" "$("$R" when after the concert | cut -c1-16)" "2026-10-22 22:30"
eq "when: flight default 3 h before" "$("$R" when before my flight | cut -c1-16)" "2026-11-06 17:15"
eq "when: place phrase" "$("$R" when when I get home | cut -d' ' -f1-2)" "--on home"
eq "when: leave work" "$("$R" when after work | cut -d' ' -f1-2)" "--on leave:zone:Work"
js=$("$R" when --json in 2 hours)
eq "when --json" "$(printf '%s' "$js" | py 'import json,sys; d=json.load(sys.stdin); print(d["ok"], d["at"], d["form"])')" "True 2026-10-21 12:00 relative"
"$R" when blah blah >/dev/null 2>&1; eq "when: unreadable = exit 2" "$?" "2"
eq "when --json error" "$("$R" when --json blah | py 'import json,sys; print(json.load(sys.stdin)["ok"])')" "False"

# B. add with a phrase
out=$("$R" add --owner 'tomorrow 7pm' "$M one"); has "add phrase: says what it read" "$out" "Thu 2026-10-22 19:00"
eq "add phrase: stored" "$(at_of "$M one")" "2026-10-22 19:00"
out=$("$R" add --owner --via voice 'after the concert' "$M two"); has "add anchor: event named" "$out" "Big Band Live"
eq "add anchor: stored" "$(at_of "$M two")" "2026-10-22 22:30"
eq "add: added + via stored" "$(py 'import json,sys; r=[r for r in json.load(open(sys.argv[1])) if "two" in r["text"]][0]; print(r["added"], r["via"])' "$REMIND_DB")" "2026-10-21 10:00 voice"
out=$("$R" add 'when I get home' "$M three"); has "add place phrase -> location reminder" "$out" "📍 when home"
out=$("$R" add 'after work' "$M four"); has "add after work -> leave:zone:Work" "$out" "when leaving Work"
"$R" add --owner 'gibberish time' "$M five" >/dev/null 2>&1; eq "add: bad phrase exit 2" "$?" "2"
eq "add: bad phrase stores nothing" "$(at_of "$M five")" ""
"$R" add --owner "2026-10-23 08:00" "$M six" >/dev/null; eq "add: exact time unchanged" "$(at_of "$M six")" "2026-10-23 08:00"

# C. move, aliases, help
i=$(idof "$M one")
out=$("$R" move "$i" friday 18:00); has "move ID phrase" "$out" "moved to Fri 2026-10-23 18:00"
eq "move: stored" "$(at_of "$M one")" "2026-10-23 18:00"
n=$("$R" list | grep -n "$M six" | cut -d. -f1 | tr -d ' ' | cut -d: -f2)
out=$("$R" reschedule "$n" +2h); has "reschedule N (alias)" "$out" "moved to Wed 2026-10-21 12:00"
"$R" move 99 +1h >/dev/null 2>&1; eq "move: unknown N exit 1" "$?" "1"
"$R" help >/dev/null; eq "help exits 0" "$?" "0"
eq "ls = list" "$("$R" ls | grep -c "$M")" "$("$R" list | grep -c "$M")"
out=$("$R" rm --match "$M six"); has "rm = cancel" "$out" "cancelled"
out=$("$R" snooze "$i" 'tomorrow evening'); has "snooze takes a phrase" "$out" "snoozed to Thu 2026-10-22 19:00"

# C2. cancel / skip by ID
"$R" add --owner --every weekly "2026-10-26 09:00" "$M series" >/dev/null
si=$(idof "$M series")
out=$("$R" skip "$si"); has "skip by ID" "$out" "next: Mon 2026-11-02 09:00"
"$R" add --owner "2026-10-30 09:00" "$M byid" >/dev/null
out=$("$R" cancel "$(idof "$M byid")"); has "cancel by ID" "$out" "cancelled: 2026-10-30 09:00"
out=$("$R" cancel "$si"); has "cancel series by ID" "$out" "cancelled"
"$R" cancel deadbeef >/dev/null; eq "cancel unknown ID exit 1" "$?" "1"

# D. list --json
js=$("$R" list --json --mine)
eq "list --json: kinds" "$(printf '%s' "$js" | py 'import json,sys; d=json.load(sys.stdin); print(sorted({x["kind"] for x in d["items"]}), d["now"])')" "['place', 'timed'] 2026-10-21 10:00"
eq "list --json: place where" "$(printf '%s' "$js" | py 'import json,sys; d=json.load(sys.stdin); print([x["where"] for x in d["items"] if x["kind"]=="place"])')" "['when home', 'when leaving Work']"

# E. instant press via $TELEGRAM_CALLBACK_DATA
i2=$(idof "$M two")
out=$(TELEGRAM_CALLBACK_DATA="rm:stom:$i2" "$R" press); eq "instant press: one short line" "$out" "⏰ Snoozed to Thu 22.10. 09:00"
out=$(TELEGRAM_CALLBACK_DATA="rm:done:$i2" "$R" press); eq "instant done" "$out" "✅ Done"
out=$(TELEGRAM_CALLBACK_DATA="rm:done:$i2" "$R" press); eq "instant done twice" "$out" "✅ Already done"
out=$(TELEGRAM_CALLBACK_DATA="rm:done:deadbeef" "$R" press); rc=$?
eq "instant unknown ID exit 1" "$rc" "1"; has "instant unknown ID: warning line" "$out" "⚠️"
grep -q "$M" <<<"$out" && no "instant reply never carries the text" "$out" || ok "instant reply never carries the text"

# F. leave:zone:NAME fires after leaving the zone (dwell 10 min) and not on arrival
here() { case "$1" in
  home) printf '{"place": {"kind": "home", "zone": "Home"}}' ;;
  away) printf '{"place": {"kind": "away", "zone": null}}' ;;
  zone) printf '{"place": {"kind": "zone", "zone": "%s"}}' "$2" ;; esac > "$T/where"; }
poll() { here "${@:2}"; REMIND_NOW="2026-10-21 $1" "$R" place-due >/dev/null 2>&1; }
: > "$T/tgb.log"
poll 10:02 zone Work; poll 10:04 zone Work
eq "leave:zone: nothing while at Work" "$(grep -c 'Left Work' "$T/tgb.log")" "0"
poll 10:06 away; poll 10:10 away
eq "leave:zone: waits for the away dwell" "$(grep -c 'Left Work' "$T/tgb.log")" "0"
poll 10:17 away
eq "leave:zone: fires once after 10 min away" "$(grep -c "📍 Left Work: $M four" "$T/tgb.log")" "1"
poll 10:19 away; eq "leave:zone: once only" "$(grep -c 'Left Work' "$T/tgb.log")" "1"
"$R" add --on leave:zone:Work --every "$M seven" >/dev/null
poll 10:21 zone Work; poll 10:23 home
eq "leave:zone: Work -> home counts at once" "$(grep -c "📍 Left Work: $M seven" "$T/tgb.log")" "1"

leak=$(cat "$REAL_DIR/reminders.json" "$REAL_DIR/recent.json" "$REAL_DIR/places.json" 2>/dev/null | grep -c "$M")
eq "live store: no test text" "$leak" "0"
python3 -I "$(dirname "$0")/when_test.py" >/dev/null 2>&1; eq "when_test.py (parser units) passes" "$?" "0"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
