#!/usr/bin/env bash
# Offline tests for tg-cmd's trip picture cards (TG_CMD_PICTURES=1): /train as a departure board and /trip as a
# countdown with flights, a place strip and today's dive estimate per place, plus cardimg's board/route/trip_strip/
# tiles/rich. Fake train-watch, flight-watch, trip-conditions and remind; made-up trip files; HOME, CARDIMG_DIR and
# the trip files are temp; the profile is the test fixture. TGCMD_BIN: a tg-cmd test copy (its lib/cardimg.py is used).
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE=${ASSISTANT_PROFILE:-$_pf/fixtures/profile.json}
TGCMD=${TGCMD_BIN:-/home/node/bin/tg-cmd}
CARDIMG="$(dirname "$(realpath "$TGCMD")")/lib/cardimg.py"
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
T=$(mktemp -d)
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
export USAGE_STATE="$T/usage"   # tg-cmd's exec records go to a scratch usage store: this suite runs with the real HOME
mkdir -p "$T/bin" "$T/tw" "$T/cards" "$T/home" "$T/trips"
J() { python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(eval(sys.argv[1]))' "$1"; }
size() { python3 -c 'from PIL import Image; import os, sys; i = Image.open(sys.argv[1]); print(i.width, i.height <= 1350, oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$1"; }

cat > "$T/bin/train-watch" <<'EOF'
#!/usr/bin/env bash
printf 'train-watch %s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  list) [ -n "${FAKE_TW_ERR:-}" ] && { echo "train-watch: $FAKE_TW_ERR" >&2; exit 1; }; cat "$FAKE_TW_LEGS" ;;
  status) [ -f "$FAKE_TW_DIR/$2.json" ] || { echo "train-watch: transport API unreachable" >&2; exit 1; }; cat "$FAKE_TW_DIR/$2.json" ;;
  *) exit 2 ;;
esac
EOF
cat > "$T/bin/flight-watch" <<'EOF'
#!/usr/bin/env bash
printf 'flight-watch %s\n' "$*" >> "$FAKE_LOG"
[ -n "${FAKE_FW_ERR:-}" ] && { echo "flight-watch: state unreadable" >&2; exit 1; }
cat "$FAKE_FW_JSON"
EOF
cat > "$T/bin/trip-conditions" <<'EOF'
#!/usr/bin/env bash
printf 'trip-conditions %s\n' "$*" >> "$FAKE_LOG"
[ -n "${FAKE_TC_SLOW:-}" ] && sleep 15
[ -n "${FAKE_TC_ERR:-}" ] && { echo "trip-conditions: forecast down" >&2; exit 1; }
cat "$FAKE_TC_JSON"
EOF
printf '#!/usr/bin/env bash\nprintf "remind %%s\\n" "$*" >> "$FAKE_LOG"\n' > "$T/bin/remind"
chmod +x "$T/bin/"*
printf 'raise RuntimeError("no pillow today")\n' > "$T/broken_cardimg.py"

cat > "$T/legs.json" <<'EOF'
[{"id": "b1", "from": "home", "to": "Basel SBB", "dep": "2031-03-05T13:10:00+01:00", "arr": "2031-03-05T14:05:00+01:00", "lines": ["IR36"], "title": "🚆 Home → Basel SBB"},
 {"id": "b2", "from": "home", "to": "Bern", "dep": "2031-03-05T14:00:00+01:00", "arr": "2031-03-05T15:00:00+01:00", "lines": ["IC1"], "title": "🚆 Home → Bern"},
 {"id": "b3", "from": "home", "to": "Lausanne", "dep": "2031-03-05T16:00:00+01:00", "arr": "2031-03-05T18:30:00+01:00", "lines": ["IC5", "IR15"], "title": "🚆 Home → Lausanne"},
 {"id": "b4", "from": "Basel SBB", "to": "home", "dep": "2031-03-06T01:40:00+01:00", "arr": "2031-03-06T02:50:00+01:00", "lines": ["SN3"], "title": "🚆 Basel SBB → Home (after Artist One)"},
 {"id": "b5", "from": "home", "to": "Olten", "dep": "2031-03-05T18:00:00+01:00", "arr": "2031-03-05T19:10:00+01:00", "lines": ["Bus 21", "IR16", "IC3", "Tram 9"], "title": "🚆 Home → Olten"},
 {"id": "b6", "from": "home", "to": "Chur", "dep": "2031-03-08T09:00:00+01:00", "arr": "2031-03-08T11:30:00+01:00", "lines": ["IC3"], "title": "🚆 Home → Chur"},
 {"id": "b7", "from": "home", "to": "Thun", "dep": "2031-03-20T10:00:00+01:00", "arr": "2031-03-20T11:00:00+01:00", "lines": [], "title": "🚆 Home → Thun"},
 {"id": "b0", "from": "home", "to": "Aarau", "dep": "2031-03-05T08:00:00+01:00", "arr": "2031-03-05T09:00:00+01:00", "lines": ["IR36"], "title": "🚆 past"}]
EOF
echo '{"fine": true, "live": {"dep": "13:10", "arr": "14:05", "delay": 0, "platform": "7", "lines": ["IR36"]}, "problems": [], "alt": null}' > "$T/tw/b1.json"
echo '{"fine": true, "live": {"dep": "14:03", "arr": "15:03", "delay": 3, "platform": "4"}, "problems": [], "alt": null}' > "$T/tw/b2.json"
echo '{"fine": false, "live": {"dep": "16:06", "arr": "18:36", "delay": 6, "platform": "3"}, "problems": [{"key": "delay5", "text": "late"}], "alt": null}' > "$T/tw/b3.json"
echo '{"fine": false, "live": null, "problems": [{"key": "cancelled", "text": "gone"}], "alt": {"dep": "01:55", "arr": "03:05", "lines": ["SN3"]}}' > "$T/tw/b4.json"
echo '{"fine": false, "live": {"dep": "18:00", "arr": "19:10", "delay": 0, "platform": "9"}, "problems": [{"key": "transfer:Olten Nord", "text": "x"}, {"key": "platform:Zürich HB:9", "text": "y"}, {"key": "bus:Aarau", "text": "z"}], "alt": null}' > "$T/tw/b5.json"
echo '{"fine": true, "live": {"dep": "10:00", "arr": "11:00", "delay": 0, "platform": "2"}, "problems": [], "alt": null}' > "$T/tw/b7.json"

cat > "$T/trips/conditions.json" <<'EOF'
{"trips": {"island": {"name": "Island Trip", "emoji": "🌴", "tz": "UTC", "from": "2031-03-14", "to": "2031-03-22",
  "legs": [{"name": "North Isle", "spot": "Bay One", "from": "2031-03-15", "to": "2031-03-18", "dive_label": "Reef A"},
           {"name": "South Isle", "spot": "Bay Two", "from": "2031-03-18", "to": "2031-03-20", "dive_label": "Reef B", "crossing": "Fast ferry North Isle → South Isle"},
           {"name": "East Isle", "from": "2031-03-20", "to": "2031-03-21", "dive_label": "Reef C", "crossing": "Boat South Isle → East Isle"}]}}}
EOF
echo '{"name": "Island Trip", "from": "2031-03-14", "until": "2031-03-22", "legs": ["flight out Fri 14.03 21:35", "back Sat 22.03"]}' > "$T/trips/pretrip.json"
cat > "$T/trips/extra.json" <<'EOF'
{"trips": [{"name": "Island Trip", "link": "https://example.org/hub", "match": ["island"]},
           {"name": "Family Fest", "emoji": "💍", "from": "2031-03-07", "to": "2031-03-08", "legs": ["🚗 drive 15:00 → Fest Hall"], "match": ["fest"], "todo": ["Pack the gear"]}]}
EOF
printf '# TODO\n\n## Waiting on you\n- **Island Trip stays** — pick one (internal note)\n\n## Ideas\n' > "$T/trips/TODO.md"
cat > "$T/flights.json" <<'EOF'
[{"id": "f3", "no": "QZ18", "from": "CCC", "to": "BBB", "dep": "2031-03-21T20:30:00+04:00", "arr": "2031-03-22T01:05:00+04:00"},
 {"id": "f1", "no": "XY100", "from": "AAA", "to": "BBB", "dep": "2031-03-14T21:35:00+01:00", "arr": "2031-03-15T06:45:00+04:00"},
 {"id": "f2", "no": "QZ19", "from": "BBB", "to": "CCC", "dep": "2031-03-15T07:50:00+04:00", "arr": "2031-03-15T12:25:00+04:00"},
 {"id": "f4", "no": "XY101", "from": "BBB", "to": "AAA", "dep": "2031-03-22T02:35:00+04:00", "arr": "2031-03-22T06:45:00+01:00"},
 {"id": "f9", "no": "XY777", "from": "AAA", "to": "DDD", "dep": "2031-05-01T10:00:00+01:00", "arr": "2031-05-01T12:00:00+01:00"},
 {"id": "bad", "no": "XY1", "from": "AAA", "to": "BBB", "dep": "soon"}]
EOF
cat > "$T/cond.json" <<'EOF'
{"trip": "island", "days": [{"date": "2031-03-01", "mode": "preview", "legs": [
  {"leg": "North Isle", "wind": {"kn": 8.4}, "sea": {"wave_m": 0.8, "temp_c": 28.6}, "dive": {"long_range": "go", "visibility": "good", "why": [], "estimate": true}},
  {"leg": "South Isle", "wind": {"kn": 14.2}, "sea": {"wave_m": 1.5, "temp_c": 28.1}, "dive": {"long_range": "marginal", "visibility": "fair", "why": [], "estimate": true}}]}]}
EOF
export TG_CMD_TRIP_CONDITIONS="$T/trips/conditions.json" TG_CMD_PRETRIP="$T/trips/pretrip.json" TG_CMD_TRIPS="$T/trips/extra.json" TG_CMD_TODO="$T/trips/TODO.md"
run() { : > "$T/log"; HOME="$T/home" FAKE_LOG="$T/log" TG_CMD_BIN="$T/bin" CARDIMG_DIR="$T/cards" FAKE_TW_LEGS="${LEGS:-$T/legs.json}" \
  FAKE_TW_DIR="${TWD:-$T/tw}" FAKE_FW_JSON="$T/flights.json" FAKE_TC_JSON="$T/cond.json" TG_CMD_NOW="${NOW:-2031-03-05 12:00}" "$TGCMD" "$@"; }
pic() { TG_CMD_PICTURES=1 run "$@"; }
py() { HOME="$T/home" FAKE_TW_LEGS="$T/legs.json" FAKE_TW_DIR="$T/tw" TGCMD="$TGCMD" CARDIMG="$CARDIMG" TG_CMD_NOW="${NOW:-2031-03-05 12:00}" TG_CMD_BIN="$T/bin" FAKE_LOG="$T/log" \
  FAKE_FW_JSON="$T/flights.json" FAKE_TC_JSON="$T/cond.json" python3 -c '
import importlib.machinery, importlib.util, os, sys
sys.dont_write_bytecode = True
ld = importlib.machinery.SourceFileLoader("tgcmd", os.environ["TGCMD"])
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("tgcmd", ld)); ld.exec_module(m)
ci = m.cardimg()
exec(sys.argv[1])' "$1"; }

echo "# /train board"
text=$(run train); rc=$?
eq "train text: rc 0, still 4 legs without the flag (voice relay)" "$rc:$(printf '%s\n' "$text" | head -1)" "0:🚆 Trains · next 4 of 7 · home = door to door"
out=$(pic train 2>"$T/err"); rc=$?
eq "train board: rc 0, no stderr" "$rc:$(cat "$T/err")" "0:"
eq "train board: the card under CARDIMG_DIR" "$(printf '%s' "$out" | J 'd["photo"]')" "$T/cards/train.png"
eq "train board: 1080 wide, ≤ 1350 tall, 0600" "$(size "$T/cards/train.png")" "1080 True 0o600"
eq "train board: caption = text = the 6 board legs" "$(printf '%s' "$out" | J 'd["caption"].splitlines()[0] + " | " + d["text"].splitlines()[0]')" "🚆 Trains · next 6 of 7 · home = door to door | 🚆 Trains · next 6 of 7 · home = door to door"
has "train board: caption keeps the verdicts" "$(printf '%s' "$out" | J 'd["caption"]')" "❌ cancelled → alt 01:55 SN3 → 03:05"
eq "train board: caption ≤ 1024" "$(printf '%s' "$out" | J 'len(d["caption"]) <= 1024')" "True"
eq "train board: one list + 6 status calls, no other source" "$(sort "$T/log" | tr '\n' ' ')" "train-watch list --json train-watch status b1 --json train-watch status b2 --json train-watch status b3 --json train-watch status b4 --json train-watch status b5 --json train-watch status b6 --json "
rows=$(py '
L = m.legs_ahead(); S = [m.leg_status(g) for g in L[:6]]
for g, s in zip(L, S):
    r = m.board_row(g, s, ci)
    col = {ci.GOOD: "good", ci.WARN: "warn", ci.BAD: "bad", ci.ACCENT: "accent", ci.TEXT2: "grey"}[r["color"]]
    print("|".join([r["time"], r["day"], r["dest"], r["origin"], "+".join(r["lines"]), r["arr"], r["platform"] + ("!" if r.get("platform_new") else ""), r["status"], col, r["note"]]))')
eq "train board rows: status, colour, platform, note per leg" "$rows" "13:10|today|Basel SBB|Home|IR36|14:05|7|On time|good|
14:00|today|Bern|Home|IC1|15:00|4|+3 min|warn|
16:00|today|Lausanne|Home|IC5+IR15|18:36|3|+6 min|warn|
18:00|today|Olten|Home|Bus 21+IR16+IC3+Tram 9|19:10|9!|Tight change|bad|Change at Olten Nord at risk · replacement bus from Aarau
01:40|tomorrow|Home|Basel SBB|SN3|02:50||Cancelled|bad|Alt 01:55 SN3 → 03:05
09:00|Sat 08.03|Chur|Home|IC3|11:30||No status|grey|"
eq "train board row: a far leg = as planned, no platform" "$(NOW="2031-03-19 08:00" py '
g = m.legs_ahead()[0]; r = m.board_row(g, m.leg_status(g), ci); print(r["dest"], r["status"], repr(r["platform"]), r["color"] == ci.ACCENT)')" "Thun As planned '' True"
eq "train board row: cancelled without alternative" "$(py '
g = m.legs_ahead()[0]; r = m.board_row(g, {"fine": False, "problems": [{"key": "cancelled"}]}, ci); print(r["status"], "|", r["note"])')" "Cancelled | No alternative found, check SBB"
out=$(TWD="$T/none" pic train); rc=$?
eq "train board, every status fails: still a card, rc 0" "$rc:$(printf '%s' "$out" | J 'd["photo"]')" "0:$T/cards/train.png"
eq "train board, every status fails: grey lines in the caption" "$(printf '%s' "$out" | J 'd["caption"].count("⚪ no live status right now")')" "6"
out=$(FAKE_TW_ERR="legs file unreadable" pic train 2>/dev/null); rc=$?
eq "train board, list fails: the ⚠️ fallback, rc 3" "$rc:$(printf '%s\n' "$out" | head -1)" "3:⚠️ /train failed: train watch: legs file unreadable."
echo '[]' > "$T/legs-empty.json"
eq "train board, no legs: the plain text, no card" "$(LEGS="$T/legs-empty.json" pic train)" "🚆 No train legs planned ahead."
out=$(TG_CMD_CARDIMG="$T/broken_cardimg.py" pic train 2>"$T/err"); rc=$?
eq "train board, card broken: the text card, rc 0" "$rc:$(printf '%s\n' "$out" | head -1)" "0:🚆 Trains · next 6 of 7 · home = door to door"
has "train board, card broken: logged" "$(cat "$T/err")" "/train picture failed (RuntimeError"

echo "# /trip card"
NOW="2031-03-01 10:00"; export NOW
text=$(run trip island); rc=$?
eq "trip text: rc 0, unchanged head" "$rc:$(printf '%s\n' "$text" | head -1)" "0:🌴 Island Trip · Fri 14–22.3 (8 nights) · in 13 days"
out=$(pic trip island 2>"$T/err"); rc=$?
eq "trip card: rc 0, no stderr" "$rc:$(cat "$T/err")" "0:"
eq "trip card: the card under CARDIMG_DIR" "$(printf '%s' "$out" | J 'd["photo"]')" "$T/cards/trip.png"
eq "trip card: 1080 wide, ≤ 1350 tall, 0600" "$(size "$T/cards/trip.png")" "1080 True 0o600"
eq "trip card: caption = text = the text card" "$(printf '%s' "$out" | J 'd["caption"] == d["text"]'):$(printf '%s' "$out" | J 'd["text"]' | head -1)" "True:🌴 Island Trip · Fri 14–22.3 (8 nights) · in 13 days"
has "trip card: conditions asked for today, without the advisory lookups" "$(cat "$T/log")" "trip-conditions island --json --days 1 --no-advisory"
has "trip card: flights from flight-watch" "$(cat "$T/log")" "flight-watch list --json"
eq "trip journeys: out (2 legs) and back (2 legs); bad and far flights dropped" "$(py '
t = m.trip_select("island")[1]
print([[f["no"] for f in j] for j in m.trip_flights(t)])')" "[['XY100', 'QZ19'], ['QZ18', 'XY101']]"
eq "trip route: stops in each airport's local time + flight numbers" "$(py '
t = m.trip_select("island")[1]
print(m.journey_route(m.trip_flights(t)[0], "Out"))')" "(('Out', 'Fri 14.03'), [('AAA', 'Fri 21:35'), ('BBB', '06:45–07:50'), ('CCC', 'Sat 12:25')], ['XY 100', 'QZ 19'])"
cd_() { NOW="$1" py '
t = m.trip_select("island")[1]
print(" | ".join(m.trip_countdown(t, m.trip_flights(t), "North Isle")))'; }
eq "countdown: days to the first flight" "$(cd_ "2031-03-01 10:00")" "13 | days to go · XY 100 Fri 14.03 21:35"
eq "countdown: hours on the last day before" "$(cd_ "2031-03-14 10:00")" "12 h | to go · XY 100 Fri 14.03 21:35"
eq "countdown: one day before = hours too" "$(cd_ "2031-03-13 22:00")" "24 h | to go · XY 100 Fri 14.03 21:35"
eq "countdown: during the trip = the day and the place" "$(cd_ "2031-03-16 09:00")" "Day 3 | of 9 · North Isle"
eq "countdown: last day" "$(cd_ "2031-03-22 09:00")" "Last day | of 9 days"
eq "countdown: no flights = to the start date" "$(py '
t = m.trip_select("island")[1]; print(" | ".join(m.trip_countdown(t, [])))')" "13 | days to go · starts Fri 14.03"
eq "tiles: one per place, today's dive estimate, the clearly best one starred" "$(py '
t = m.trip_select("island")[1]; k, conf = m.trip_conf(t)
from datetime import date
legs = [{**l, "from": date.fromisoformat(l["from"]), "to": date.fromisoformat(l["to"])} for l in conf["legs"]]
for it in m.trip_tiles(legs, m.trip_conditions(k), ci):
    print(it["title"], it.get("star", False), it["sub"], it["pill"] and it["pill"][0], [x if isinstance(x, str) else x[0] for x in it["lines"]])')" "North Isle True ['15–18.3 · 3 nights', 'Bay One'] Long-range: go ['Vis good', '8 kn · waves 0.8 m', 'Sea 29°']
South Isle False ['18–20.3 · 2 nights', 'Bay Two'] Long-range: maybe ['Vis fair', '14 kn · waves 1.5 m', 'Sea 28°']
East Isle False ['20–21.3 · 1 night'] None ['Reef C']"
for n in "2031-03-14 10:00" "2031-03-16 09:00" "2031-03-22 09:00"; do
  out=$(NOW="$n" pic trip island 2>"$T/err"); rc=$?
  eq "trip card on $n: rendered" "$rc:$(printf '%s' "$out" | J 'd["photo"]'):$(cat "$T/err")" "0:$T/cards/trip.png:"
done
cp "$T/cards/trip.png" "$T/cards/trip-last.png"
out=$(FAKE_TC_ERR=1 FAKE_FW_ERR=1 pic trip island 2>"$T/err"); rc=$?
eq "trip card, conditions + flights down: still a card" "$rc:$(printf '%s' "$out" | J 'd["photo"]'):$(cat "$T/err")" "0:$T/cards/trip.png:"
start=$(date +%s); out=$(FAKE_TC_SLOW=1 pic trip island); rc=$?; took=$(( $(date +%s) - start ))
eq "trip card, slow conditions: killed, card anyway, within the plugin's 20 s" "$rc:$(printf '%s' "$out" | J 'd["photo"]'):$(( took < 12 ))" "0:$T/cards/trip.png:1"
out=$(pic trip fest 2>"$T/err"); rc=$?
eq "trip card without places (no conditions config): rendered, no conditions call" "$rc:$(printf '%s' "$out" | J 'd["photo"]'):$(grep -c trip-conditions "$T/log")" "0:$T/cards/trip.png:0"
eq "no trip named: the soonest (here the short one without places)" "$(pic trip | J 'd["caption"]' | head -1)" "💍 Family Fest · Fri 7–8.3 · in 6 days"
eq "trip card without places: caption = its text card" "$(printf '%s' "$out" | J 'd["caption"]' | head -1)" "💍 Family Fest · Fri 7–8.3 · in 6 days"
eq "trip list: text only" "$(pic trip list | head -1)" "🧳 Trips ahead (2)"
eq "trip, unknown name: text only" "$(pic trip moon | head -1)" "🧳 No trip ahead called “moon”."
eq "no trip ahead: text only" "$(NOW="2031-06-01 10:00" pic trip)" "🧳 No trip planned."
out=$(TG_CMD_CARDIMG="$T/broken_cardimg.py" pic trip island 2>"$T/err"); rc=$?
eq "trip card broken: the text card, rc 0" "$rc:$(printf '%s\n' "$out" | head -1)" "0:🌴 Island Trip · Fri 14–22.3 (8 nights) · in 13 days"
unset NOW

echo "# cardimg: drawn glyphs, board, route, strip, tiles"
eq "cardimg: rich() draws → and ★ (no tofu), width = rich_w, cut at maxw" "$(py '
c = ci.Card("t"); f = ci.font(30)
w = c.rich(56, 300, "A → B ★4.5", f); print(round(w) == round(c.rich_w("A → B ★4.5", f)), c.rich(56, 400, "Alpha → Beta → Gamma → Delta", f, maxw=200) <= 200)
print([p for p in c._rich_parts("A → B ★4.5")])')" "True True
['A', '→', 'B ', '★', '4.5']"
eq "cardimg: board stops at the bottom, returns rows drawn" "$(py '
c = ci.Card("t"); r = {"time": "10:00", "day": "today", "dest": "X", "lines": ["S1"], "status": "On time", "color": ci.GOOD, "note": "n"}
print(c.board([r] * 12), c.y <= c.h - ci.PAD)')" "5 True"
eq "cardimg: strip labels = place cuts first, overlapping trip ends dropped" "$(py '
from datetime import date
c = ci.Card("t"); seen = []
orig = c.d.text
c.d.text = lambda xy, s, **k: (seen.append(s), orig(xy, s, **k))
c.trip_strip([{"name": "A", "from": date(2031, 3, 2), "to": date(2031, 3, 9)}, {"name": "B", "from": date(2031, 3, 9), "to": date(2031, 3, 16)}], date(2031, 3, 1), date(2031, 3, 17), today=date(2031, 3, 5))
print([s for s in seen if "." in s or s == "today"])')" "['2.3', '9.3', '16.3', '1.3', 'today']"
eq "cardimg: route + tiles draw and advance y" "$(py '
c = ci.Card("t"); y0 = c.y
c.route(("Out", "Fri"), [("AAA", "21:35"), ("BBB", "06:45–07:50"), ("CCC", "12:25")], ["XY 1", "QZ 2"])
c.tiles([{"title": "One", "star": True, "sub": ["a"], "pill": ("Long-range: go", (0, 0, 0), ci.GOOD), "lines": ["b", ("c", ci.BAD)]}, {"title": "Two"}])
print(c.y > y0 + 300, os.path.basename(c.save("t", os.environ["HOME"] + "/../cards")))')" "True t.png"

echo "# menu"
has "--list names /train" "$("$TGCMD" --list)" "/train      Next trains with live status"
has "--list names /trip" "$("$TGCMD" --list)" "/trip       Next trip: dates, legs, open to-dos"
eq "nothing written under HOME" "$(find "$T/home" -type f | wc -l)" "0"

[ -n "${KEEP_CARDS:-}" ] && mkdir -p "$KEEP_CARDS" && cp "$T/cards/"*.png "$KEEP_CARDS/"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
