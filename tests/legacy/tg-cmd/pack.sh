#!/usr/bin/env bash
# Offline tests for tg-cmd's /pack card and ~/bin/pack --todo: fake calcache/rave-radar/ha (TG_CMD_BIN), made-up
# events, artists, trips and templates written below (fictional names only). Exit 0 = all pass.
# TGCMD_BIN / PACK_BIN: test copies. Run alone or from run.sh.
set -uo pipefail
TGCMD=${TGCMD_BIN:-/home/node/bin/tg-cmd}
PACK=${PACK_BIN:-/home/node/bin/pack}
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
T=$(mktemp -d)
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
export USAGE_STATE="$T/usage"   # tg-cmd's exec records go to a scratch usage store: this suite runs with the real HOME
mkdir -p "$T/bin"
cat > "$T/bin/calcache" <<STUB
#!/usr/bin/env bash
[ -n "\${FAIL_CAL:-}" ] && { echo "calcache: broken" >&2; exit \$FAIL_CAL; }
cat "$T/cal.json"
STUB
cat > "$T/bin/rave-radar" <<STUB
#!/usr/bin/env bash
[ -n "\${FAIL_RR:-}" ] && { echo "rave-radar: broken" >&2; exit 1; }
echo '[{"name": "Kalia Dunes", "aliases": ["Dunes"], "source": "x", "added": "2026-01-01"}]'
STUB
cat > "$T/bin/ha" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/calls"
case "\$1 \$2" in
  "list todo") cat "\${FAKE_HA_LIST}" ;;
  "todo items") [ -n "\${FAKE_HA_DENY:-}" ] && { echo "ha: refused: \$4 is not an allowed to-do list" >&2; exit 1; }
                echo '[{"uid": "u1", "summary": "🎧 Headphones", "status": "needs_action"}]' ;;
  "todo add") echo "added" ;;
  *) exit 2 ;;
esac
STUB
chmod +x "$T/bin/calcache" "$T/bin/rave-radar" "$T/bin/ha"
fl() { printf '{"travel": %s, "busy_evening": false, "away_overnight": %s, "abroad": %s, "id_check": %s}' "$1" "$2" "$3" "$4"; }
cat > "$T/cal.json" <<EOF
[
 {"id": "e1", "start": "2026-03-03", "end": "2026-03-04", "all_day": true, "title": "@Werkstadt", "town": "Werkstadt", "free": true, "flags": $(fl null false false false)},
 {"id": "e2", "start": "2026-03-03T19:00:00+01:00", "end": "2026-03-03T21:00:00+01:00", "all_day": false, "title": "Climbing", "town": null, "free": false, "flags": $(fl null false false false)},
 {"id": "e3", "start": "2026-03-06T20:00:00+01:00", "end": "2026-03-06T21:10:00+01:00", "all_day": false, "title": "🚆 Home → Basel SBB (Kalia Dunes)", "town": "Bachwil", "free": false, "flags": $(fl '"train"' false false false), "train": {"from": "home", "to": "Basel SBB", "lines": []}},
 {"id": "e4", "start": "2026-03-06T23:00:00+01:00", "end": "2026-03-07T06:00:00+01:00", "all_day": false, "title": "Kalia Dunez", "town": "Basel", "free": true, "flags": $(fl null true false true)},
 {"id": "e5", "start": "2026-03-07T05:10:00+01:00", "end": "2026-03-07T06:30:00+01:00", "all_day": false, "title": "🚆 Basel SBB → Home", "town": "Basel", "free": false, "flags": $(fl '"train"' false false false), "train": {"from": "Basel SBB", "to": "home", "lines": []}},
 {"id": "e6", "start": "2026-03-07T16:00:00+01:00", "end": "2026-03-08T01:00:00+01:00", "all_day": false, "title": "Mira Hochziit", "town": "Werkstadt", "free": true, "flags": $(fl null false false false)},
 {"id": "e7", "start": "2026-03-07T23:20:00+01:00", "end": "2026-03-08T00:10:00+01:00", "all_day": false, "title": "🚆 Werkstadt → Home", "town": "Werkstadt", "free": false, "flags": $(fl '"train"' false false false), "train": {"from": "Werkstadt", "to": "home", "lines": []}},
 {"id": "e8", "start": "2026-03-20", "end": "2026-03-21", "all_day": true, "title": "🇧🇪 Customer visit (Gent)", "town": "Gent", "free": false, "flags": $(fl null true true false)}
]
EOF
cat > "$T/tpl.json" <<'EOF'
{"types": {
 "rave": {"label": "Rave / club night", "emoji": "🪩", "items": ["🎟 Ticket", "👂 Earplugs", "💳 Card"]},
 "wedding": {"label": "Wedding / gig", "emoji": "💍", "items": ["🎧 Headphones", "👔 Suit", "🎁 Gift"]},
 "business": {"label": "Business day abroad", "emoji": "💼", "items": ["🛂 Passport / ID", "💶 EUR", "🔌 Adapter", "🔋 Phone charger", "💻 Laptop"]},
 "beach": {"label": "Beach / long trip", "emoji": "🏝", "items": ["🩱 Swimwear", "🧴 Sunscreen", "s1", "s2", "s3", "s4", "s5"]}
}}
EOF
echo '[{"name": "Mira'"'"'s wedding", "from": "2026-03-07", "to": "2026-03-08", "match": ["Mira"]}]' > "$T/trips.json"
echo '{"trips": {"isl": {"name": "Island trip", "from": "2026-03-25", "to": "2026-04-08", "legs": [{"name": "Isla", "from": "2026-03-26", "to": "2026-04-07"}]}}}' > "$T/tc.json"
echo '[{"entity_id": "todo.shopping_list", "name": "Shopping list", "state": "1"}]' > "$T/ha-none.json"
echo '[{"entity_id": "todo.shopping_list", "name": "Shopping list"}, {"entity_id": "todo.packing", "name": "Packing"}]' > "$T/ha-pack.json"
export TG_CMD_BIN="$T/bin" TG_CMD_NOW="2026-03-02 09:00" TG_CMD_PACK_TEMPLATES="$T/tpl.json" TG_CMD_TRIPS="$T/trips.json" \
       TG_CMD_TRIP_CONDITIONS="$T/tc.json" TG_CMD_PRETRIP="$T/none.json" PACK_TG_CMD="$TGCMD" TZ=Europe/Zurich
p() { "$TGCMD" pack "$@"; }
lines() { printf '%s\n' "$1" | wc -l | tr -d ' '; }

echo "# /pack: next event within 10 days"
out=$(p); rc=$?
eq "rc 0" "$rc" "0"
has "artist typo (fuzzy) -> rave, soonest typed event" "$(printf '%s' "$out" | head -1)" "🎒 🪩 Kalia Dunez · Fri 06.03 (in 4 d)"
has "18+/ID flag -> official photo ID" "$out" "🪪 Official photo ID"
has "train home 05:10 -> powerbank" "$out" "🔋 Powerbank"
eq "earplugs not doubled (template has them)" "$(printf '%s\n' "$out" | grep -c Earplugs)" "1"
hasnt "night train home is no night away (no toothbrush)" "$out" "Toothbrush"
hasnt "not abroad: no passport" "$out" "Passport"
hasnt "untyped earlier event skipped" "$out" "Climbing"
eq "extras first, then the template" "$(printf '%s\n' "$out" | sed -n 2p)" "🪪 Official photo ID"
out=$(FAIL_RR=1 p); rc=$?
eq "rave-radar down: still rc 0" "$rc" "0"
has "  no artist list -> next typed event is the wedding" "$(printf '%s' "$out" | head -1)" "Mira Hochziit"
out=$(TG_CMD_NOW="2026-03-09 09:00" p)
has "nothing typed within 10 days" "$out" "Nothing to pack for in the next 10 days"
out=$(TG_CMD_NOW="2026-03-10 09:00" p)
has "day 10 still counts (in 10 d)" "$(printf '%s' "$out" | head -1)" "Customer visit (Gent) · Fri 20.03 (in 10 d)"

echo "# /pack TYPE, WORDS, list"
out=$(p wedding)
has "wedding: the calendar event, not the trip file's copy" "$(printf '%s' "$out" | head -1)" "🎒 💍 Mira Hochziit · Sat 07.03 (in 5 d)"
hasnt "  train home 23:20 is no night train" "$out" "Powerbank"
hasnt "  ends 01:00: no toothbrush" "$out" "Toothbrush"
out=$(p business)
has "business: flag-emoji visit abroad" "$(printf '%s' "$out" | head -1)" "🎒 💼 🇧🇪 Customer visit (Gent) · Fri 20.03"
has "  night away -> toothbrush" "$out" "🪥 Toothbrush"
eq "  passport not doubled" "$(printf '%s\n' "$out" | grep -c Passport)" "1"
eq "  EUR not doubled" "$(printf '%s\n' "$out" | grep -c EUR)" "1"
hasnt "  template charger covers the extra" "$out" "🔌 Phone charger"
out=$(p beach)
has "beach: the long trip from trip-conditions" "$(printf '%s' "$out" | head -1)" "🎒 🏝 Island trip · Wed 25.03 (in 23 d)"
has "  abroad -> passport" "$out" "🛂 Passport"
has "  abroad -> EUR" "$out" "💶 EUR cash"
eq "  11 items fit in 10 lines (paired)" "$(lines "$out")" "7"
has "  paired with ·" "$out" " · "
out=$(p club)
has "alias club -> rave" "$(printf '%s' "$out" | head -1)" "🪩 Kalia Dunez"
out=$(p dunes)
has "WORDS find an event by its leg (artist name)" "$(printf '%s' "$out" | head -1)" "Kalia Dunez"
out=$(p Climbing)
has "WORDS: event without a type" "$out" "Climbing: no list fits it"
out=$(p zzz)
has "unknown words" "$out" "No list or event called “zzz”"
out=$(p list)
has "list: types" "$out" "🪩 rave · Rave / club night (3)"
has "list: hint" "$out" "/pack TYPE shows one"
eq "list: 6 lines" "$(lines "$out")" "6"
out=$(FAIL_CAL=3 p business)
has "no calendar cache, no such trip: template only" "$(printf '%s' "$out" | head -1)" "🎒 💼 Business day abroad"
out=$(FAIL_CAL=3 p wedding)
has "no calendar cache: the trip file's wedding" "$(printf '%s' "$out" | head -1)" "🎒 💍 Mira's wedding"
out=$(FAIL_CAL=3 p beach)
has "no calendar cache: trips still count" "$out" "Island trip"
out=$(FAIL_CAL=1 p); rc=$?
eq "calcache broken: rc 3" "$rc" "3"
has "  fallback text" "$out" "⚠️ /pack failed: calendar"
out=$(TG_CMD_PACK_TEMPLATES="$T/missing.json" p); rc=$?
eq "no templates: rc 3" "$rc" "3"
has "  says so" "$out" "no packing templates yet"
for a in "" wedding business beach list; do
  n=$(lines "$(p $a)"); if [ "$n" -le 10 ]; then ok "≤ 10 lines (/pack $a: $n)"; else no "≤ 10 lines (/pack $a)" "$n"; fi
done

echo "# pack --todo (fake ha)"
: > "$T/calls"
out=$(FAKE_HA_LIST="$T/ha-none.json" "$PACK" --todo wedding); rc=$?
eq "no Packing list: rc 4" "$rc" "4"
has "  says it" "$out" "No HA to-do list \"Packing\" exists"
eq "  nothing added" "$(grep -c '^todo add' "$T/calls")" "0"
: > "$T/calls"
out=$(FAKE_HA_LIST="$T/ha-pack.json" "$PACK" --todo wedding --dry-run); rc=$?
eq "dry run: rc 0" "$rc" "0"
has "  would add 2, 1 already there" "$out" "todo.packing: would add 2, 1 already there."
eq "  dry run adds nothing" "$(grep -c '^todo add' "$T/calls")" "0"
: > "$T/calls"
out=$(FAKE_HA_LIST="$T/ha-pack.json" "$PACK" --todo wedding); rc=$?
eq "todo: rc 0" "$rc" "0"
has "  added 2" "$out" "todo.packing: added 2, 1 already there."
eq "  two adds, to todo.packing only" "$(grep -c '^todo add .* --list todo.packing$' "$T/calls")" "2"
has "  suit added" "$(cat "$T/calls")" "todo add 👔 Suit --list todo.packing"
out=$(FAKE_HA_LIST="$T/ha-pack.json" FAKE_HA_DENY=1 "$PACK" --todo wedding); rc=$?
eq "list not allowed for ha: rc 5" "$rc" "5"
has "  names the config line" "$out" "todo-lists.json"
out=$("$PACK" wedding); rc=$?
eq "pack without --todo = the card" "$(printf '%s' "$out" | head -1)" "🎒 💍 Mira Hochziit · Sat 07.03 (in 5 d)"
"$PACK" --dry-run >/dev/null 2>&1; eq "--dry-run without --todo: usage rc 2" "$?" "2"

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
