#!/usr/bin/env bash
# Offline tests for tg-cmd's /back catch-up card: fake remind/bills/media/gift-radar/calcache (TG_CMD_BIN), made-up
# fixtures in fixtures/back (recent.json + place-state.json next to a temp REMIND_DB, unraid-health reports via
# TG_CMD_UNRAID_REPORTS). Exit 0 = all pass. TGCMD_BIN: a test copy. Run alone or from run.sh.
set -uo pipefail
TGCMD=${TGCMD_BIN:-/home/node/bin/tg-cmd}
FIX=$(cd "$(dirname "$0")" && pwd)/fixtures/back
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
T=$(mktemp -d)
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
export USAGE_STATE="$T/usage"   # tg-cmd's exec records go to a scratch usage store: this suite runs with the real HOME
mkdir -p "$T/bin" "$T/state" "$T/empty"
fake() { # fake NAME FIXTURE-VAR: logs its args, fails when FAIL_<NAME> is set, prints the fixture
  cat > "$T/bin/$1" <<STUB
#!/usr/bin/env bash
printf '%s %s\n' "$1" "\$*" >> "$T/calls"
[ -n "\${FAIL_$2:-}" ] && { echo "$1: broken" >&2; exit \${FAIL_$2}; }
cat "\${FAKE_$2}"
STUB
  chmod +x "$T/bin/$1"; }
fake remind REMIND; fake bills BILLS; fake media MEDIA; fake gift-radar GIFT; fake calcache CAL
: > "$T/none.txt"; echo '[]' > "$T/none.json"
full() { export FAKE_REMIND=$FIX/remind-list.txt FAKE_BILLS=$FIX/bills.json FAKE_MEDIA=$FIX/media-recent.txt \
                FAKE_GIFT=$FIX/gift-radar.txt FAKE_CAL=$FIX/calcache.json REPORTS=$FIX/reports
         cp "$FIX/recent.json" "$T/state/recent.json"; find "$T/state" -name place-state.json -delete; }
empty() { export FAKE_REMIND=$T/none.txt FAKE_BILLS=$T/none.json FAKE_MEDIA=$T/none.txt FAKE_GIFT=$T/none.txt \
                 FAKE_CAL=$T/none.json REPORTS=$T/empty
          find "$T/state" -mindepth 1 -delete; }
place() { printf '%s' "$1" > "$T/state/place-state.json"; }
back() { : > "$T/calls"; TG_CMD_BIN="$T/bin" TG_CMD_NOW="${NOW:-2031-03-05 12:00}" REMIND_DB="$T/state/reminders.json" \
         TG_CMD_UNRAID_REPORTS="$REPORTS" "$TGCMD" back "$@"; }

echo "# /back: full card"
full
out=$(back 2); rc=$?
eq "full: rc 0" "$rc" "0"
want='👋 Catch-up · last 2 days
⏰ 4 reminders fired · open: Water the plants, Pick up parcel
🔜 Next 48 h: today 18:00 Take out the bins, tomorrow 09:30 Dentist check-up (+1)
💳 2 bills due ≤ 7 d: Phone Co overdue, Test Power AG Sat 08.03
🎬 New on Emby: 2 · Test Movie One, Test Show
🖥 Server: 1 critical + 2 warnings · disk3 is disabled, CPU is hot: 90°C (+1)
🎂 Bobby today, Testa Fri 07.03
📅 Next: tomorrow 19:30 Concert night · Testburg'
eq "full: the card" "$out" "$want"
eq "full: at most 10 lines" "$(( $(printf '%s\n' "$out" | wc -l) <= 10 ))" "1"
calls=$(sort "$T/calls")
has "calls: remind list" "$calls" "remind list"
has "calls: bills open json" "$calls" "bills list --open --json"
has "calls: media recent -n 40" "$calls" "media recent -n 40"
has "calls: gift-radar 7 days at TG_CMD_NOW" "$calls" "gift-radar list --days 7 --date 2031-03-05"
has "calls: calcache 14 days json" "$calls" "calcache show --days 14 --json"
eq "calls: five sources, read verbs only" "$(wc -l < "$T/calls" | tr -d ' ')" "5"
for bad in Example Person 80.00 45.50 CHF MYOWN "of mine" "Too far away" "Ancient" "Call the garage" "Return library" \
           "Far Later" "Older Movie" "Test Movie Two" "docker.img" "disk7" "disk9" "oldbox" "Stale old" "FCP" \
           "Past meeting" "Today marker" "Place one" "before noon" "tag home" "every Thu"; do
  hasnt "full: no '$bad'" "$out" "$bad"
done

echo "# /back N: the window"
out=$(back 5)
has "N=5: header" "$out" "👋 Catch-up · last 5 days"
has "N=5: older media counted" "$out" "🎬 New on Emby: 3 · Test Movie One, Test Show (+1)"
has "N=5: the snoozed one counts as fired, not open" "$out" "⏰ 4 reminders fired · open: Water the plants, Pick up parcel"
out=$(back 1)
has "N=1: header singular" "$out" "👋 Catch-up · last 1 day"
has "N=1: fewer fired" "$out" "⏰ 2 reminders fired · open: Pick up parcel"
has "N=1: server flags by day (since 04.03 counts)" "$out" "🖥 Server: 1 critical + 2 warnings"
hasnt "N=1: older since-day out" "$out" "disk7"
out=$(back 30); has "N=30: max ok" "$out" "last 30 days"
has "N=30: ancient fired one in" "$out" "⏰ 5 reminders fired"
for bad in 0 31 x -2 "2 3"; do
  out=$(back "$bad"); rc=$?
  eq "bad N '$bad': usage, rc 0" "$rc:$out" "0:Use /back or /back N (N = days, 1–30)."
done
out=$(back 3d); has "N=3d accepted" "$out" "last 3 days"
out=$(back " 2 "); has "N padded accepted" "$out" "last 2 days"

echo "# default window from place-state"
full
out=$(back); has "no place-state: 2 days" "$out" "👋 Catch-up · last 2 days"
place '{"zone": "home", "since": "2031-03-04 18:00", "trip": {"left": "2031-03-03 10:00", "back": "2031-03-04 18:00"}}'
out=$(back); has "trip back 18 h ago: since it began" "$out" "👋 Catch-up · since Mon 03.03 10:00"
has "  window: media from 09:00 that day out" "$out" "🎬 New on Emby: 2 ·"
has "  window: fired at 13:00 that day in" "$out" "⏰ 4 reminders fired"
place '{"zone": "home", "trip": {"left": "2031-02-25 10:00", "back": "2031-03-01 18:00"}}'
out=$(back); has "trip back 4 days ago: 2 days" "$out" "👋 Catch-up · last 2 days"
place '{"zone": "away", "since": "2031-03-04 07:10", "left": "2031-03-04 07:00", "trip": {"left": "2031-03-01 10:00", "back": "2031-03-02 10:00"}}'
out=$(back); has "away now: since he left" "$out" "👋 Catch-up · since Tue 04.03 07:00"
place '{"zone": "zone:Work", "since": "2031-03-05 08:00", "left": "2031-02-01 07:00"}'
out=$(back); has "away > 30 days: 2 days" "$out" "👋 Catch-up · last 2 days"
place 'not json'
out=$(back); rc=$?; eq "broken place-state: 2 days, rc 0" "$rc:$(printf '%s' "$out" | head -1)" "0:👋 Catch-up · last 2 days"
place '{"zone": "home", "trip": {"left": "2031-03-03 10:00", "back": "2031-03-04 18:00"}}'
out=$(back 1); has "N beats the trip" "$out" "last 1 day"

echo "# empty sources: every section left out"
empty
out=$(back 2); rc=$?
eq "empty: rc 0 + one-liner" "$rc:$out" "0:👋 Catch-up · last 2 days
Nothing new, nothing due soon."
full
for v in REMIND BILLS MEDIA GIFT CAL; do
  full; case $v in CAL) export FAKE_CAL=$T/none.json ;; *) export FAKE_$v=$T/none.txt ;; esac
  [ "$v" = BILLS ] && export FAKE_BILLS=$T/none.json
  out=$(back 2)
  eq "only $v empty: 7 lines" "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" "7"
done
full; out=$(FAKE_REMIND=$T/none.txt back 2); hasnt "no upcoming: no 🔜" "$out" "🔜"
full; out=$(FAKE_BILLS=$T/none.json back 2); hasnt "no bills: no 💳" "$out" "💳"
full; out=$(FAKE_MEDIA=$T/none.txt back 2); hasnt "no media: no 🎬" "$out" "🎬"
full; out=$(FAKE_GIFT=$T/none.txt back 2); hasnt "no birthdays: no 🎂" "$out" "🎂"
full; out=$(FAKE_CAL=$T/none.json back 2); hasnt "no events: no 📅" "$out" "📅"
full; out=$(REPORTS=$T/empty back 2); hasnt "no report: no 🖥" "$out" "🖥"
full; echo '{}' > "$T/state/recent.json"; out=$(back 2); hasnt "nothing fired: no ⏰" "$out" "⏰"
full; out=$(NOW="2031-03-12 12:00" back 2); hasnt "old report only: no 🖥" "$out" "🖥"
full; out=$(FAIL_CAL=3 back 2); rc=$?
eq "calcache exit 3 (no cache): no 📅, nothing flagged" "$rc:$(printf '%s' "$out" | grep -c -E '📅|Not checked')" "0:0"
full; python3 - "$T/state/recent.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
for k in d: d[k]["state"] = "done" if d[k].get("state") == "fired" else d[k]["state"]
json.dump(d, open(p, "w"))
PY
out=$(back 2); has "all fired handled" "$out" "⏰ 4 reminders fired · all handled"
full; out=$(FAKE_BILLS=$FIX/bills.json NOW="2031-03-05 12:00" back 2)
full; printf '[{"payee": "Solo Bill", "due": "2031-03-05", "amount": "1", "currency": "CHF"}]' > "$T/one.json"
out=$(FAKE_BILLS=$T/one.json back 2); has "one bill: singular, today" "$out" "💳 1 bill due ≤ 7 d: Solo Bill today"

echo "# calendar picks"
full; printf '[{"start": "2031-03-06", "end": "2031-03-07", "all_day": true, "title": "Holiday", "town": null},
 {"start": "2031-03-06T19:30:00+01:00", "all_day": false, "title": "Concert night", "town": "Testburg"}]' > "$T/cal.json"
out=$(FAKE_CAL=$T/cal.json back 2); has "all-day tomorrow first: no time" "$out" "📅 Next: tomorrow Holiday"
printf '[{"start": "2031-03-05T12:30:00Z", "all_day": false, "title": "Lunch", "town": null}]' > "$T/cal.json"
out=$(FAKE_CAL=$T/cal.json back 2); has "UTC start shown in local time, no town" "$out" "📅 Next: today 13:30 Lunch"
hasnt "  no town dot" "$(printf '%s' "$out" | grep '📅')" " · "

echo "# media cap"
full; for i in $(seq 1 40); do printf 'Wed 05.03 11:%02d  movie  Bulk Movie %02d (2030)\n' "$((i % 60))" "$i"; done > "$T/many.txt"
out=$(FAKE_MEDIA=$T/many.txt back 2); has "40 lines in the window: count marked +" "$out" "🎬 New on Emby: 40+ · Bulk Movie 01, Bulk Movie 02 (+38)"

echo "# failures"
full; out=$(FAIL_BILLS=1 back 2); rc=$?
eq "bills down: rc 0" "$rc" "0"
has "bills down: flagged" "$out" "⚠️ Not checked: bills"
has "bills down: the rest still there" "$out" "🎬 New on Emby: 2"
full; out=$(FAIL_REMIND=1 FAIL_MEDIA=1 back 2)
has "remind + media down: both flagged" "$out" "⚠️ Not checked: reminders, Emby"
has "remind down: fired ones still listed (recent.json)" "$out" "⏰ 4 reminders fired"
full; printf 'not json' > "$T/bad.json"; out=$(FAKE_BILLS=$T/bad.json back 2); has "bad bills JSON: flagged" "$out" "⚠️ Not checked: bills"
full; mkdir -p "$T/badrep/2031-03-05.md"
out=$(FAIL_REMIND=1 FAIL_BILLS=1 FAIL_MEDIA=1 FAIL_GIFT=1 FAIL_CAL=1 REPORTS=$T/badrep back 2); rc=$?
eq "all sources down: rc 3" "$rc" "3"
has "all down: fallback hint" "$out" "⚠️ /back failed: no data source answered."
has "all down: ask me" "$out" "what did I miss while I was away"
full; out=$(TG_CMD_BIN="$T/nowhere" TG_CMD_NOW="2031-03-05 12:00" REMIND_DB="$T/state/reminders.json" TG_CMD_UNRAID_REPORTS="$FIX/reports" "$TGCMD" back 2); rc=$?
eq "missing binaries: rc 0, partial card" "$rc" "0"
has "  fired + server still shown" "$out" "🖥 Server: 1 critical"

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
