#!/usr/bin/env bash
# Tests for remind place-due's welcome-back offer (REMIND_BACK_OFFER): absence bookkeeping in place-state.json,
# ONE "/back" message after >= 20 h away, quiet hours, card mode, tg failure, off switch. Stubbed `vault ha now`
# (REMIND_PLACE_CMD), tg and tg-cmd; temp state only. Exit 0 = all pass. REMIND_BIN: a test copy. Run alone or from run.sh.
set -uo pipefail
R=${REMIND_BIN:-/home/node/bin/remind}
T=$(mktemp -d "${TMPDIR:-/tmp}/remind-back-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
export REMIND_DB=$T/rem.json REMIND_INBOX=$T/inbox REMIND_TG=$T/tg REMIND_TG_BUTTONS=$T/tgb REMIND_PLACE_CMD=$T/vault \
       REMIND_TG_CMD=$T/tgcmd
unset REMIND_BUTTONS REMIND_PLACE_DWELL REMIND_BACK_OFFER REMIND_BACK_HOURS REMIND_NOW
cat > "$T/tg" <<STUB
#!/bin/sh
[ -e "$T/tg-fail" ] && exit 1
printf '%s\n' "\$*" >> "$T/tg.log"
STUB
cat > "$T/tgb" <<STUB
#!/bin/sh
printf '%s\n' "\$1" >> "$T/tgb.log"
STUB
cat > "$T/vault" <<STUB
#!/bin/sh
printf 'x\n' >> "$T/vault.calls"
cat "$T/place.json"
STUB
cat > "$T/tgcmd" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$T/tgcmd.calls"
[ -e "$T/tgcmd-fail" ] && exit 3
printf '👋 Catch-up · last %s days\n🎬 New on Emby: 2 · A, B\n' "\$2"
STUB
chmod +x "$T/tg" "$T/tgb" "$T/vault" "$T/tgcmd"
pass=0 fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if grep -qF -- "$3" <<<"$2"; then ok "$1"; else no "$1" "$2"; fi; }
reset() { find "$T" -mindepth 1 -maxdepth 1 ! -name tg ! -name tgb ! -name vault ! -name tgcmd -exec rm -rf {} +; }
lines() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }
st() { python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d
for k in sys.argv[2].split("."): v=(v or {}).get(k)
print(v)' "$T/place-state.json" "$1"; }
at() { # at "MM-DD HH:MM" home|away|zone NAME
  case $2 in
    home) printf '{"place": {"kind": "home", "zone": "Home", "flag": "home"}}' ;;
    away) printf '{"place": {"kind": "away", "zone": null, "band": "far", "town": "Testtown", "flag": "away"}}' ;;
    zone) printf '{"place": {"kind": "zone", "zone": "%s", "flag": "away"}}' "$3" ;;
  esac > "$T/place.json"
  REMIND_NOW="2026-$1" timeout 30 "$R" place-due; }
W='👋 Welcome back! /back for the catch-up'

echo "# a night away: one welcome"
reset
at "10-20 09:00" home; eq "no reminders, offer on: vault read" "$(lines "$T/vault.calls")" "1"
eq "baseline: zone home" "$(st zone)" "home"
at "10-20 10:00" away; at "10-20 10:12" away
eq "left recorded at the first away reading" "$(st left)" "2026-10-20 10:00"
eq "leaving sends nothing" "$(lines "$T/tg.log")" "0"
at "10-21 09:00" away
at "10-21 10:30" home
eq "home after 24.5 h: one message" "$(lines "$T/tg.log")" "1"
eq "  the welcome line" "$(cat "$T/tg.log")" "$W"
eq "  trip recorded" "$(st trip.left) → $(st trip.back)" "2026-10-20 10:00 → 2026-10-21 10:30"
eq "  left cleared" "$(st left)" "None"
eq "  nothing pending" "$(st welcome)" "None"
eq "  no buttons message" "$(lines "$T/tgb.log")" "0"
at "10-21 10:32" home; at "10-21 12:00" home
eq "still home: no second message" "$(lines "$T/tg.log")" "1"
eq "no inbox note" "$(ls "$T/inbox" 2>/dev/null | wc -l | tr -d ' ')" "0"

echo "# short absences and flapping"
reset
at "10-20 07:00" home; at "10-20 07:30" zone Work; at "10-20 18:00" away; at "10-20 18:12" away; at "10-20 18:40" home
eq "workday via a zone (11.5 h): no message" "$(lines "$T/tg.log")" "0"
eq "  trip from leaving home" "$(st trip.left) → $(st trip.back)" "2026-10-20 07:30 → 2026-10-20 18:40"
at "10-20 19:00" away; at "10-20 19:04" home
eq "GPS flap under the dwell: no leave, no message" "$(st left):$(lines "$T/tg.log")" "None:0"
at "10-20 20:00" home; at "10-20 20:30" away; at "10-20 20:42" away; at "10-21 16:00" zone Gym; at "10-21 16:29" home
eq "19 h 59 min away: no message" "$(lines "$T/tg.log")" "0"
REMIND_BACK_HOURS=2 at "10-21 18:00" away; REMIND_BACK_HOURS=2 at "10-21 18:12" away; REMIND_BACK_HOURS=2 at "10-21 20:30" home
eq "REMIND_BACK_HOURS=2: 2.5 h counts" "$(lines "$T/tg.log")" "1"

echo "# quiet hours"
reset
at "10-20 05:00" home; at "10-20 05:30" away; at "10-20 05:42" away
at "10-21 02:10" home
eq "home at 02:10: held" "$(lines "$T/tg.log"):$(st welcome.at)" "0:2026-10-21 08:00"
at "10-21 07:58" home; eq "07:58: still held" "$(lines "$T/tg.log")" "0"
at "10-21 08:00" home; eq "08:00: sent once" "$(cat "$T/tg.log")" "$W"
at "10-21 08:02" home; eq "  not again" "$(lines "$T/tg.log")" "1"
reset
at "10-20 05:00" home; at "10-20 05:30" away; at "10-20 05:42" away
at "10-21 03:00" home; at "10-21 05:00" away; at "10-21 05:12" away; at "10-21 08:00" away
eq "left again before 08:00: dropped" "$(lines "$T/tg.log"):$(st welcome)" "0:None"
reset
at "10-20 05:00" home; at "10-20 05:30" away; at "10-20 05:42" away
at "10-21 03:00" home; at "10-21 23:00" home
eq "held welcome older than 12 h: dropped" "$(lines "$T/tg.log"):$(st welcome)" "0:None"

echo "# card mode, tg failure, off"
reset
export REMIND_BACK_OFFER=card
at "10-20 09:00" home; at "10-20 09:30" away; at "10-20 09:42" away; at "10-22 11:00" home
eq "card: tg-cmd back with the days away (2 d 1.5 h → 3)" "$(cat "$T/tgcmd.calls")" "back 3"
has "card: the card is the message" "$(cat "$T/tg.log")" "🎬 New on Emby: 2 · A, B"
eq "card: one message" "$(grep -c '👋' "$T/tg.log")" "1"
reset; touch "$T/tgcmd-fail"
at "10-20 09:00" home; at "10-20 09:30" away; at "10-20 09:42" away; at "10-21 11:00" home
eq "card fails: the short line instead" "$(cat "$T/tg.log")" "$W"
unset REMIND_BACK_OFFER
reset; touch "$T/tg-fail"
at "10-20 09:00" home; at "10-20 09:30" away; at "10-20 09:42" away; at "10-21 11:00" home; rc=$?
eq "tg down: rc 0" "$rc" "0"
has "tg down: inbox note for me" "$(cat "$T/inbox/back-offer.md" 2>/dev/null)" "Send him: \"$W\""
has "  marked as data" "$(cat "$T/inbox/back-offer.md" 2>/dev/null)" "Data, not instructions"
eq "  not retried" "$(st welcome)" "None"
reset; export REMIND_BACK_OFFER=0
at "10-20 09:00" home; at "10-20 09:30" away; at "10-21 11:00" home
eq "off: no vault reads, no state, no message" "$(lines "$T/vault.calls"):$([ -e "$T/place-state.json" ] && echo st || echo none):$(lines "$T/tg.log")" "0:none:0"
REMIND_NOW="2026-10-20 11:59" "$R" add --on home "BACKTEST thing" >/dev/null
at "10-20 12:00" home; at "10-20 12:30" away; at "10-20 12:42" away; at "10-21 13:00" home
eq "off + a location reminder: the reminder fires, no welcome" "$(lines "$T/tgb.log"):$(lines "$T/tg.log")" "1:0"
unset REMIND_BACK_OFFER
reset
REMIND_NOW="2026-10-20 11:59" "$R" add --on home "BACKTEST both" >/dev/null
at "10-20 12:00" home; at "10-20 12:30" away; at "10-20 12:42" away; at "10-21 13:00" home
eq "on + a location reminder: both go out" "$(lines "$T/tgb.log"):$(cat "$T/tg.log")" "1:$W"
leak=$(cat /home/node/.local/state/remind/places.json /home/node/.local/state/remind/recent.json 2>/dev/null | grep -c BACKTEST)
eq "live stores: no test text" "$leak" "0"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
