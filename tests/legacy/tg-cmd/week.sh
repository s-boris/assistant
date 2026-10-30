#!/usr/bin/env bash
# Offline tests for tg-cmd's /week card (text, and the picture under TG_CMD_PICTURES=1): fake calcache, train-watch,
# sleepin, bills and gift-radar (TG_CMD_BIN), an open-meteo stand-in file (TG_CMD_WEATHER), the weather cache in a temp
# TG_CMD_STATE; HOME is a temp dir and the profile the test fixture (work town "Testcity"). Made-up data only.
# TGCMD_BIN: a test copy (its lib/cardimg.py is used). Exit 0 = all pass. Run alone.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE=${ASSISTANT_PROFILE:-$_pf/fixtures/profile.json}
TGCMD=${TGCMD_BIN:-/home/node/bin/tg-cmd}
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
T=$(mktemp -d)
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
export USAGE_STATE="$T/usage"   # tg-cmd's exec records go to a scratch usage store: this suite runs with the real HOME
mkdir -p "$T/bin" "$T/home" "$T/state" "$T/cards" "$T/fx"
J() { python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(eval(sys.argv[1]))' "$1"; }

fake() { # fake NAME VAR: logs its args, exits FAIL_<VAR> (with stderr) when set, prints $FAKE_<VAR>
  cat > "$T/bin/$1" <<STUB
#!/usr/bin/env bash
printf '%s %s\n' "$1" "\$*" >> "$T/calls"
[ -n "\${FAIL_$2:-}" ] && { echo "$1: broken" >&2; exit \${FAIL_$2}; }
cat "\${FAKE_$2}"
STUB
  chmod +x "$T/bin/$1"; }
fake calcache CAL; fake train-watch LEGS; fake sleepin SLEEP; fake bills BILLS; fake gift-radar GIFT

cat > "$T/fx/cal.json" <<'EOF'
[{"id": "a1", "title": "@Testcity", "start": "2031-03-05", "end": "2031-03-06", "all_day": true, "flags": {}},
 {"id": "a2", "title": "@Otherburg", "start": "2031-03-06", "end": "2031-03-07", "all_day": true, "flags": {}},
 {"id": "a3", "title": "Past meeting", "start": "2031-03-05T09:00:00+01:00", "end": "2031-03-05T10:00:00+01:00", "all_day": false, "flags": {}},
 {"id": "a4", "title": "Long workshop", "start": "2031-03-04T20:00:00+01:00", "end": "2031-03-05T14:00:00+01:00", "all_day": false, "flags": {}},
 {"id": "a5", "title": "🚆 Home → Testburg (Concert)", "start": "2031-03-05T18:10:00+01:00", "end": "2031-03-05T19:00:00+01:00", "all_day": false, "flags": {"travel": "train"}},
 {"id": "a6", "title": "Concert night", "start": "2031-03-05T19:30:00+01:00", "end": "2031-03-05T23:00:00+01:00", "all_day": false, "town": "Testburg", "flags": {}},
 {"id": "a7", "title": "🚆 Testburg → Home (after Concert)", "start": "2031-03-05T23:40:00+01:00", "end": "2031-03-06T00:30:00+01:00", "all_day": false, "flags": {"travel": "train"}},
 {"id": "a8", "title": "Ski week", "start": "2031-03-08", "end": "2031-03-11", "all_day": true, "flags": {}},
 {"id": "a9", "title": "✈️ Testport → Skiville", "start": "2031-03-09T07:00:00+01:00", "end": "2031-03-09T08:30:00+01:00", "all_day": false, "flags": {"travel": "flight"}},
 {"id": "a10", "title": "Car to the garage", "start": "2031-03-10T08:00:00+01:00", "end": "2031-03-10T08:30:00+01:00", "all_day": false, "flags": {"travel": "drive"}},
 {"id": "a11", "title": "Too far ahead", "start": "2031-03-12T10:00:00+01:00", "end": "2031-03-12T11:00:00+01:00", "all_day": false, "flags": {}}]
EOF
cat > "$T/fx/legs.json" <<'EOF'
[{"id": "l1", "from": "home", "to": "Testburg", "dep": "2031-03-05T18:10:00+01:00", "arr": "2031-03-05T19:00:00+01:00", "title": "🚆 Home → Testburg (Concert)"},
 {"id": "l2", "from": "home", "to": "Testcity", "dep": "2031-03-06T07:15:00+01:00", "arr": "2031-03-06T08:05:00+01:00", "title": "🚆 Home → Testcity"},
 {"id": "l3", "from": "home", "to": "Oldtown", "dep": "2031-03-04T07:15:00+01:00", "arr": "2031-03-04T08:05:00+01:00", "title": "🚆 Home → Oldtown"}]
EOF
cat > "$T/fx/sleep.json" <<'EOF'
{"now": "2031-03-05 12:00", "active": false, "date": "2031-03-05", "why": "send", "until": null, "ended": null, "sources": [],
 "queue": {"count": 0, "tags": {}, "oldest": null},
 "ahead": [{"date": "2031-03-06", "off": false, "until": "12:30", "sources": [{"kind": "calendar", "what": "Testburg → Home", "end": "2031-03-06 00:30"}]},
           {"date": "2031-03-08", "off": true, "until": "12:30", "sources": [{"kind": "calendar", "what": "x", "end": "2031-03-08 01:00"}]},
           {"date": "2031-03-12", "off": false, "until": "11:00", "sources": [{"kind": "hold", "what": "by hand", "end": "2031-03-12 11:00"}]}]}
EOF
cat > "$T/fx/bills.json" <<'EOF'
[{"id": 1, "payee": "Phone Co", "due": "2031-03-01", "amount": "12.00", "currency": "CHF"},
 {"id": 2, "payee": "Test Power AG", "due": "2031-03-07", "amount": "80.00", "currency": "CHF"},
 {"id": 3, "payee": "Water Works", "due": "2031-03-07", "amount": "45.50", "currency": "CHF"},
 {"id": 4, "payee": "Far Later Ltd", "due": "2031-04-01", "amount": "9.00", "currency": "CHF"}]
EOF
printf '%s\n' 'Fri 07.03.2031  in   2 d  Testa Person                 notified' 'Thu 13.03.2031  in   8 d  Bobby Far' > "$T/fx/gift.txt"
cat > "$T/fx/wx.json" <<'EOF'
{"daily": {"time": ["2031-03-05", "2031-03-06", "2031-03-07", "2031-03-08", "2031-03-09", "2031-03-10", "2031-03-11", "2031-03-12"],
 "weather_code": [0, 2, 3, 61, 71, 95, 45, 1], "temperature_2m_max": [12.4, 10.0, 8.6, 6.0, -1.2, 9.5, 7.0, 11.0],
 "temperature_2m_min": [3.0, 2.2, 1.0, 0.4, -6.6, 4.0, 2.0, 3.0], "precipitation_probability_max": [0, 10, 20, 80, 60, 90, 5, 0]}}
EOF
printf '%s\n' '{"daily": {"time": ["2031-03-05"], "weather_code": [61], "temperature_2m_max": [1.0], "temperature_2m_min": [0.0], "precipitation_probability_max": [99]}}' > "$T/fx/wx-other.json"
: > "$T/fx/none.txt"; echo '[]' > "$T/fx/none.json"
full() { export FAKE_CAL=$T/fx/cal.json FAKE_LEGS=$T/fx/legs.json FAKE_SLEEP=$T/fx/sleep.json FAKE_BILLS=$T/fx/bills.json FAKE_GIFT=$T/fx/gift.txt
         unset FAIL_CAL FAIL_LEGS FAIL_SLEEP FAIL_BILLS FAIL_GIFT; find "$T/state" -mindepth 1 -delete; }
week() { : > "$T/calls"; HOME="$T/home" TG_CMD_BIN="$T/bin" TG_CMD_NOW="${NOW:-2031-03-05 12:00}" TG_CMD_STATE="$T/state" \
         TG_CMD_WEATHER="${WX-$T/fx/wx.json}" CARDIMG_DIR="$T/cards" "$TGCMD" week "$@"; }
pic() { TG_CMD_PICTURES=1 week "$@"; }
cache_age() { python3 -c 'import json,sys,time; p=sys.argv[1]; d=json.load(open(p)); d["ts"]=time.time()-float(sys.argv[2]); json.dump(d, open(p,"w"))' "$T/state/week-weather.json" "$1"; }

echo "# /week: text card"
full
out=$(week); rc=$?
eq "text: rc 0" "$rc" "0"
want='🗓 Week · Wed 05.03 – Tue 11.03
Wed 05.03 ☀️ 12°/3°: 🏢 office · till 14:00 Long workshop · 🚆 18:10 Home → Testburg (Concert) · 19:30 Concert night · 🚆 23:40 Testburg → Home (after Concert) · 🌙 late night · 💳 1 bill overdue
Thu 06.03 ⛅ 10°/2°: 📍 Otherburg · 😴 sleep-in till 12:30 · 🚆 07:15 Home → Testcity
Fri 07.03 ☁️ 9°/1°: 🌙 late night · 🎂 Testa · 💳 2 bills due
Sat 08.03 🌧 6°/0° 💧80%: Ski week (to 10.03)
Sun 09.03 🌨 -1°/-7° 💧60%: ✈️ 07:00 Testport → Skiville
Mon 10.03 ⛈ 10°/4° 💧90%: 🚗 08:00 Car to the garage
Tue 11.03 🌫 7°/2°: 🌙 late night'
eq "text: the card" "$out" "$want"
calls=$(sort "$T/calls")
has "calls: calcache 8 days json" "$calls" "calcache show --days 8 --json"
has "calls: train-watch list json" "$calls" "train-watch list --json"
has "calls: sleepin status at TG_CMD_NOW" "$calls" "sleepin status --days 7 --json --at 2031-03-05 12:00"
has "calls: bills open json" "$calls" "bills list --open --json"
has "calls: gift-radar 7 days at TG_CMD_NOW" "$calls" "gift-radar list --days 7 --date 2031-03-05"
eq "calls: five sources, read verbs only" "$(wc -l < "$T/calls" | tr -d ' ')" "5"
for bad in "Past meeting" "Too far ahead" "Oldtown" "Phone Co" "Test Power" "Water Works" "Far Later" "80.00" "Person" "Bobby" "Testcity (" "@"; do
  hasnt "text: no '$bad'" "$out" "$bad"
done
eq "text: the calendar leg isn't doubled by train-watch's copy" "$(printf '%s' "$out" | grep -o '18:10' | wc -l | tr -d ' ')" "1"
eq "weather cache written 0600 in TG_CMD_STATE" "$(stat -c %a "$T/state/week-weather.json")" "600"
eq "nothing written under HOME" "$(find "$T/home" -type f | wc -l | tr -d ' ')" "0"

echo "# weather: cache, stale fallback, quiet without"
out2=$(WX=$T/fx/wx-other.json week)
has "fresh cache (< 3 h) is reused, no fetch" "$out2" "Wed 05.03 ☀️ 12°/3°:"
cache_age 36000
out2=$(WX=$T/fx/missing.json week)
has "open-meteo down, cache 10 h old → still the cached forecast" "$out2" "Sun 09.03 🌨 -1°/-7° 💧60%:"
out2=$(WX=$T/fx/wx-other.json week)
has "cache 10 h old + open-meteo up → refreshed" "$out2" "Wed 05.03 🌧 1°/0° 💧99%:"
has "refreshed forecast lacks the other days → no weather there" "$out2" "Thu 06.03: 📍 Otherburg"
cache_age $((80 * 3600))
out2=$(WX=$T/fx/missing.json week); rc=$?
eq "open-meteo down, cache 80 h old → rc 0, no weather, no warning" "$rc $(printf '%s' "$out2" | grep -c -E '°|⚠️')" "0 0"
has "  …the events still there" "$out2" "Wed 05.03: 🏢 office · till 14:00 Long workshop"
find "$T/state" -mindepth 1 -delete
out2=$(WX=$T/fx/missing.json week)
eq "open-meteo down, no cache → no weather, quiet" "$(printf '%s' "$out2" | grep -c -E '°|⚠️')" "0"
out2=$(WX=off week)
eq "TG_CMD_WEATHER=off → no weather, nothing cached" "$(printf '%s' "$out2" | grep -c '°') $(find "$T/state" -type f | wc -l | tr -d ' ')" "0 0"

echo "# failing sources"
full
out=$(FAIL_BILLS=1 week); rc=$?
eq "bills down: rc 0 + Not checked" "$rc $(printf '%s' "$out" | tail -1)" "0 ⚠️ Not checked: bills"
hasnt "bills down: no 💳" "$out" "💳"
out=$(FAIL_SLEEP=1 FAIL_GIFT=2 week)
eq "sleepin + gift-radar down: one Not checked line" "$(printf '%s' "$out" | tail -1)" "⚠️ Not checked: late nights, birthdays"
out=$(FAIL_LEGS=1 week); rc=$?
eq "train-watch down: quiet (the calendar has the legs)" "$rc $(printf '%s' "$out" | grep -c -E 'Not checked|07:15')" "0 0"
out=$(FAIL_CAL=1 week); rc=$?
has "calcache broken: rc 3 + ⚠️" "$rc $out" "3 ⚠️ /week failed: calendar: calcache failed."
out=$(FAIL_CAL=3 week); rc=$?
eq "calcache exit 3 (no cache yet): rc 0, flagged" "$rc $(printf '%s' "$out" | tail -1)" "0 ⚠️ Not checked: calendar (no cache yet)"
has "  …legs from train-watch still shown" "$out" "🚆 18:10 Home → Testburg (Concert)"
out=$(FAKE_CAL=$T/fx/none.json FAKE_LEGS=$T/fx/none.json FAKE_SLEEP=$T/fx/none.json FAKE_BILLS=$T/fx/none.json FAKE_GIFT=$T/fx/none.txt WX=off week); rc=$?
eq "empty week: rc 0, every day free (sleepin's [] → Not checked)" "$rc $(printf '%s' "$out" | grep -c ': free$') $(printf '%s' "$out" | tail -1)" "0 7 ⚠️ Not checked: late nights"

echo "# clock change"
out=$(NOW="2031-03-27 12:00" WX=off week)
has "DST start in the window → Sun 30.03 clocks forward" "$out" "Sun 30.03: 🕒 clocks forward 1 h"
eq "  …only that day" "$(printf '%s' "$out" | grep -c '🕒')" "1"

echo "# picture (TG_CMD_PICTURES=1)"
full
text=$(week)
s=$(date +%s%N); out=$(pic); rc=$?; ms=$(( ($(date +%s%N) - s) / 1000000 ))
eq "picture: rc 0" "$rc" "0"
photo=$(printf '%s' "$out" | J 'd["photo"]')
eq "picture: photo in CARDIMG_DIR" "$photo" "$T/cards/week.png"
eq "picture: 1080 wide, ≤ 1350 high, 0600" "$(python3 -c 'import sys; from PIL import Image; i=Image.open(sys.argv[1]); print(i.width, 700 <= i.height <= 1350)' "$photo") $(stat -c %a "$photo")" "1080 True 600"
eq "picture: caption = the text card (it fits)" "$(printf '%s' "$out" | J 'd["caption"]')" "$text"
eq "picture: text = the text card" "$(printf '%s' "$out" | J 'd["text"]')" "$text"
eq "picture: under 2 s" "$(( ms < 2000 ))" "1"
echo "       (picture run: $ms ms)"
python3 - "$T/fx/cal.json" "$T/fx/busy.json" <<'EOF'
import json, sys
evs = json.load(open(sys.argv[1]))
for d in range(5, 12):
    for h in range(8, 22):
        evs.append({"id": f"b{d}{h}", "title": f"Busy slot {d}-{h} with a really long and winding title to cut", "start": f"2031-03-{d:02d}T{h:02d}:15:00+01:00",
                    "end": f"2031-03-{d:02d}T{h:02d}:45:00+01:00", "all_day": False, "flags": {}})
json.dump(evs, open(sys.argv[2], "w"))
EOF
out=$(FAKE_CAL=$T/fx/busy.json pic); rc=$?
eq "busy week: rc 0, caption ≤ 1024" "$rc $(printf '%s' "$out" | J 'len(d["caption"]) <= 1024')" "0 True"
eq "busy week: card still 1080x1350 at most" "$(python3 -c 'import sys; from PIL import Image; i=Image.open(sys.argv[1]); print(i.width, i.height <= 1350)' "$T/cards/week.png")" "1080 True"
has "busy week: text keeps every event (cut at 3500)" "$(printf '%s' "$out" | J 'd["text"]')" "Busy slot 5-21"
printf 'raise ImportError("no pillow here")\n' > "$T/broken_cardimg.py"
out=$(TG_CMD_CARDIMG="$T/broken_cardimg.py" pic 2>"$T/err"); rc=$?
eq "render fails → the text card, rc 0" "$rc $out" "0 $text"
has "  …and says why on stderr" "$(cat "$T/err")" "/week picture failed"
out=$(FAIL_CAL=1 pic); rc=$?
has "picture, calcache broken: rc 3 + ⚠️ text" "$rc $out" "3 ⚠️ /week failed: calendar: calcache failed."

echo "# menu"
has "--list: /week right after /back" "$("$TGCMD" --list | awk '{printf "%s ", $1}')" "/back /week "
has "--list: /week's line" "$("$TGCMD" --list)" "/week       Your week: calendar, trains, weather, late nights"
out=$("$TGCMD" week extra 2>&1); rc=$?
eq "an argument → usage, rc 2" "$rc" "2"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
