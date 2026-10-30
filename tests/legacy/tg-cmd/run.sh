#!/usr/bin/env bash
# Offline tests for ~/bin/tg-cmd's instant commands (bills, queue, space, reminders, home, list, shop, gym, train, rave, trip, warranty, back; /back in back.sh). Exit 0 = all pass.
# /home runs against a fake `ha` (TG_CMD_BIN) and the placeholder fixtures in fixtures/ (HA_HEALTH_DIR);
# /list, /shop and /gym against fake `lists`, `shop` and `gym` (made-up items); /train, /rave and /trip against fake
# `train-watch`, `rave-radar` and `remind` plus made-up trip files and a TODO.md; /warranty against a fake `contracts`; /pack in pack.sh; picture cards (/space, /gym, /cat, cardimg) in pictures.sh; the /train board and /trip card in trip-cards.sh.
# /memory and /recall were retired 2026-10-13; their 140 tests are in ~/attic/2026-10-13/opt/tg-cmd-tests/run.sh.
set -uo pipefail
TGCMD=${TGCMD_BIN:-/home/node/bin/tg-cmd}
T=$(mktemp -d)
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
export USAGE_STATE="$T/usage"   # tg-cmd's exec records go to a scratch usage store: this suite runs with the real HOME
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }

echo "# command table (instant commands are tg-bind rows on registry tools; menu lines, picture env and text args live in the registry)"
list=$("$TGCMD" --list); rc=$?
eq "--list rc 0" "$rc" "0"
eq "commands in table order" "$(printf '%s\n' "$list" | awk '{printf "%s ", $1}')" "/bills /queue /space /reminders /home /list /shop /parcels /gym /cat /train /gohome /rave /trip /warranty /back /week /pack /vibe /finish /doctor /party /crate /usage /sleep "
for c in gohome trip warranty back pack vibe party doctor gym parcels; do
  has "$c takes one argument (two → usage)" "$("$TGCMD" "$c" a b 2>&1)" "take one argument at most"
done
has "bills takes no argument" "$("$TGCMD" bills a 2>&1)" "take no arguments"
eq "exec records land in the scratch usage store, not the live one" \
  "$(cat "$T"/usage/spool/*.jsonl 2>/dev/null | grep -c '"f":"tg.gohome"')" "1"
TGBIND=${TG_BIND_BIN:-/home/node/bin/tg-bind}
if [ -z "${TG_BIND_BIN:-}" ] && [ -n "${NETGUARD_LOG:-}" ] \
   && { [ -n "${HUB_URL:-}" ] || [ -e "${HUB_TOKEN_FILE:-$HOME/.secrets/hub-token}" ]; }; then   # the rows live on the hub; a guarded run can't reach it
  echo "skip - tg-bind rows match tg-cmd (the rows are on the hub, out of a netguard run's reach)"
elif rows=$("$TGBIND" list --json 2>/dev/null) && [ -n "$rows" ]; then
  eq "every tg-bind command row on a tg-cmd tool names a tg-cmd command" "$(printf '%s' "$rows" | python3 -c 'import json,sys; known=set(sys.argv[1].split()); print([r["tool"] for r in json.load(sys.stdin) if r["kind"] == "command" and r["tool"].startswith("tg-cmd.") and "/" + r["tool"][7:] not in known])' "$(printf '%s\n' "$list" | awk '{print $1}')")" "[]"
else echo "skip - tg-bind rows match tg-cmd (no events store here; set TG_BIND_BIN to check one)"; fi
list=$("$TGCMD" --list)
has "--list shows /reminders" "$list" "/reminders  Your upcoming reminders"
has "--list shows /home" "$list" "/home       Home: lights, temperatures, media, HA health"
has "--list shows /shop" "$list" "/shop       Shopping list: what to buy where"
has "--list shows /gym" "$list" "/gym        Next gym session from your plan"
has "--list shows /train" "$list" "/train      Next trains with live status"
has "--list shows /gohome" "$list" "/gohome     Way home: next 3 connections + the last tonight (/gohome stop)"
has "--list shows /trip" "$list" "/trip       Next trip: dates, legs, open to-dos"
has "--list shows /warranty" "$list" "/warranty   Still under warranty? (/warranty tv filters)"
has "--list shows /back" "$list" "/back       Catch-up after time away (/back 3 = 3 days)"
has "--list shows /pack" "$list" "/pack       Packing list for the next event (/pack rave)"
eq "--list: 25 commands (/cat listed, in the menu only with pictures on)" "$(printf '%s\n' "$list" | grep -c '^/')" "25"
hasnt "--list: no /emby (retired)" "$list" "/emby "
hasnt "--list: no /dance (retired)" "$list" "/dance"
hasnt "--list: no /practice (retired)" "$list" "/practice"
hasnt "--list: no /memory" "$list" "/memory"
hasnt "--list: no /recall" "$list" "/recall"

echo "# retired and bad commands"
"$TGCMD" memory >/dev/null 2>&1; eq "memory: unknown, rc 2" "$?" "2"
for c in emby dance practice; do "$TGCMD" "$c" >/dev/null 2>&1; eq "$c: retired, rc 2" "$?" "2"; done
"$TGCMD" recall paperless >/dev/null 2>&1; eq "recall: rc 2" "$?" "2"
"$TGCMD" bogus >/dev/null 2>&1; eq "unknown command: rc 2" "$?" "2"
err=$("$TGCMD" bills extra 2>&1 >/dev/null); rc=$?
eq "arguments: rc 2" "$rc" "2"
has "arguments: says no arguments" "$err" "take no arguments"
"$TGCMD" --help >/dev/null; eq "--help rc 0" "$?" "0"

echo "# /home (fake ha, placeholder fixtures)"
FIX=$(cd "$(dirname "$0")" && pwd)/fixtures
cat > "$T/ha" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_HA_LOG"
if [ -n "${FAKE_HA_ERR:-}" ]; then echo "ha: $FAKE_HA_ERR" >&2; exit 1; fi
cat "$FAKE_HA_JSON"
EOF
chmod +x "$T/ha"
home() { # $1 = ha fixture, $2 = health dir
  : > "$T/calls"
  TG_CMD_BIN="$T" TG_CMD_NOW="2031-03-05 12:00" FAKE_HA_LOG="$T/calls" FAKE_HA_JSON="$1" HA_HEALTH_DIR="$2" "$TGCMD" home
}
out=$(home "$FIX/home-full.json" "$FIX/health-new"); rc=$?
eq "home: rc 0" "$rc" "0"
want='🏠 Home · 12:00
💡 3 lights on · Kitchen 2, Study 1
🌡 Kitchen 20.4° · Lounge 21.5°→22°🔥 · Study 19.0° · Terrace 8.3°
🧹 Vacuum: docked · 5%
🎵 Playing: Speaker One (Radio App) · Tablet (mail me at <email>) · TV Box on (HDMI 1)
🩺 HA health: 4 open (1 new) · 2 known offline
↳ Study Desk Lamp 🆕, Study Old Sensor, select.extra_mode, +1 more
🔋 Low: Window low, Door Sensor 9%'
eq "home: full answer" "$out" "$want"
eq "home: at most 10 lines" "$(( $(printf '%s\n' "$out" | wc -l) <= 10 ))" "1"
eq "home: one ha read, nothing else" "$(cat "$T/calls")" "list --json --limit 5000"
printf '#!/usr/bin/env bash\n[ "$1" = line ] && echo "💧 ⚠️ Lounge humid 26 h (RH 68 %%) · air 5–10 min"\n' > "$T/damp-watch"; chmod +x "$T/damp-watch"
out2=$(home "$FIX/home-full.json" "$FIX/health-new")
has "home: damp-watch line right after the temperatures" "$out2" $'Terrace 8.3°\n💧 ⚠️ Lounge humid 26 h (RH 68 %) · air 5–10 min\n🧹'
eq "home: with the damp line still at most 10 lines" "$(( $(printf '%s\n' "$out2" | wc -l) <= 10 ))" "1"
eq "home: damp-watch adds no ha read" "$(cat "$T/calls")" "list --json --limit 5000"
find "$T" -maxdepth 1 -name damp-watch -delete
for bad in "Test Person" "Workplace" "not_home" "Tracker" "Proximity" "@example.com" "Phone One" "3%" \
           "Robo Vac Battery" "Fridge" "CPU" "55.0" "Loose" "25.0" "Speaker Two" "Idle Speaker" "String Lights" "Kitchen Lights"; do
  hasnt "home: no '$bad'" "$out" "$bad"
done
out=$(home "$FIX/home-empty.json" "$FIX/health-clean"); rc=$?
eq "home quiet: rc 0" "$rc" "0"
want='🏠 Home · 12:00
💡 All lights off
🎵 Nothing playing
🩺 HA health: all ok · 2 known offline
🔋 Batteries ok'
eq "home quiet: no temperature/vacuum lines, all ok" "$out" "$want"
out=$(home "$FIX/home-full.json" "$FIX/health-base")
has "home baseline: nothing new" "$out" "🩺 HA health: 4 open · 2 known offline"
hasnt "home baseline: no new marker" "$out" "🆕"
out=$(home "$FIX/home-full.json" "$FIX/health-old")
has "home old snapshot: age + failing watch" "$out" "🩺 HA health: 4 open · 2 known offline (snapshot 4 d old) · watch failing"
out=$(home "$FIX/home-full.json" "$T/no-such-dir")
has "home no snapshot: says so" "$out" "🩺 HA health: no ha-health-watch snapshot yet"
has "home no snapshot: rest still shown" "$out" "💡 3 lights on"
out=$(FAKE_HA_ERR="HA unreachable at http://198.51.100.7:8123" home "$FIX/home-full.json" "$FIX/health-new"); rc=$?
eq "home ha down: rc 3" "$rc" "3"
has "home ha down: fallback" "$out" "⚠️ /home failed: Home Assistant"
has "home ha down: hint" "$out" "what's on at home?"
hasnt "home ha down: IP masked" "$out" "198.51.100.7"
printf 'not json' > "$T/garbage.json"
out=$(home "$T/garbage.json" "$FIX/health-new"); rc=$?
eq "home garbage: rc 3" "$rc" "3"
has "home garbage: unreadable" "$out" "unreadable answer"
printf '{"a": 1}' > "$T/obj.json"
home "$T/obj.json" "$FIX/health-new" >/dev/null; eq "home non-list: rc 3" "$?" "3"
"$TGCMD" home extra >/dev/null 2>&1; eq "home with arguments: rc 2" "$?" "2"

echo "# /list (fake lists + shop)"
cat > "$T/lists" <<'EOF3'
#!/usr/bin/env bash
printf 'lists %s\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_LISTS_ERR:-}" ]; then echo "lists: $FAKE_LISTS_ERR" >&2; exit 1; fi
printf '%s' "${FAKE_LISTS_OUT-(no lists)}"; echo
EOF3
cat > "$T/shop" <<'EOF3'
#!/usr/bin/env bash
printf 'shop %s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  list) [ -n "${FAKE_SHOP_STALE:-}" ] && echo "shop: HA not reachable, showing the last state (ha todo items: timeout)" >&2
        if [ -n "${FAKE_SHOP_ERR:-}" ]; then echo "shop: $FAKE_SHOP_ERR" >&2; exit 2; fi
        printf '%s\n' "${FAKE_SHOP_JSON-[]}" ;;
  sync) if [ -n "${FAKE_SYNC_ERR:-}" ]; then echo "shop: ha todo items: $FAKE_SYNC_ERR" >&2; exit 1; fi
        echo '{"new": []}' ;;
  card) if [ -n "${FAKE_CARD_ERR:-}" ]; then echo "shop: $FAKE_CARD_ERR" >&2; exit 1; fi
        printf '%s' "${FAKE_CARD-}" ;;
  *) exit 2 ;;
esac
EOF3
chmod +x "$T/lists" "$T/shop"
two='[{"id": 1, "text": "Oat milk", "qty": 2, "status": "pending"}, {"id": 4, "text": "Batteries AA", "qty": 1, "status": "researched"}]'
tcmd() { : > "$T/log"; TG_CMD_BIN="$T" FAKE_LOG="$T/log" "$TGCMD" "$@"; }
out=$(FAKE_SHOP_JSON="$two" FAKE_LISTS_OUT=$'🛒 Shopping (1)\n1. Old milk\n\n☑️ Todo (1)\n1. Call the plumber\n✓ Pay rent' tcmd list); rc=$?
eq "list: rc 0" "$rc" "0"
want='🛒 Shopping (2)
1. 2× Oat milk
2. Batteries AA
→ /shop: where to buy
🗒️ +1 on my old local shopping list (ask me to move them)

☑️ Todo (1)
1. Call the plumber
✓ Pay rent'
eq "list: HA shopping first, local shopping replaced, todo kept" "$out" "$want"
eq "list: one synced shop read + one lists read" "$(cat "$T/log")" $'shop list --json\nlists show'
out=$(FAKE_SHOP_JSON="[]" FAKE_LISTS_OUT=$'🛒 Shopping: empty\n\n🗒️ Packing (1)\n1. Charger' tcmd list)
eq "list: empty HA list, empty legacy block not counted" "$out" $'🛒 Shopping list is empty\n\n🗒️ Packing (1)\n1. Charger'
out=$(FAKE_SHOP_JSON="[]" tcmd list)
eq "list: nothing anywhere" "$out" "🛒 Shopping list is empty"
out=$(FAKE_SHOP_STALE=1 FAKE_SHOP_JSON="$two" tcmd list); rc=$?
eq "list: HA down -> rc 0" "$rc" "0"
has "list: HA down -> last synced state" "$out" $'2. Batteries AA\n⚠️ HA not reachable: last synced list'
out=$(FAKE_SHOP_ERR="state file unreadable" FAKE_LISTS_OUT=$'☑️ Todo (1)\n1. Call the plumber' tcmd list); rc=$?
eq "list: shop broken, other lists still shown (rc 0)" "$rc" "0"
eq "list: shop broken -> one warning line" "$out" $'🛒 Shopping: ⚠️ couldn\'t read it (state file unreadable)\n\n☑️ Todo (1)\n1. Call the plumber'
out=$(FAKE_SHOP_JSON="$two" FAKE_LISTS_ERR="lock busy" tcmd list); rc=$?
eq "list: lists broken -> rc 0" "$rc" "0"
has "list: lists broken -> shopping kept + warning" "$out" $'→ /shop: where to buy\n\n⚠️ Other lists: lists: lock busy'
out=$(FAKE_SHOP_JSON="not json" FAKE_LISTS_OUT=$'☑️ Todo (1)\n1. Call the plumber' tcmd list)
has "list: unreadable shop JSON" "$out" "🛒 Shopping: ⚠️ couldn't read it (unreadable answer)"
out=$(FAKE_SHOP_JSON="not json" FAKE_LISTS_ERR="lock busy" tcmd list); rc=$?
eq "list: shop unreadable + lists broken -> rc 3" "$rc" "3"
has "list: both reasons" "$out" "⚠️ /list failed: shopping list: unreadable answer; lists: lock busy."
mv "$T/shop" "$T/shop.off"
out=$(FAKE_LISTS_ERR="lock busy" tcmd list); rc=$?
eq "list: shop missing + lists broken -> rc 3" "$rc" "3"
has "list: both broken -> fallback hint" "$out" "what's on my lists?"
mv "$T/shop.off" "$T/shop"

echo "# /shop (fake shop)"
card=$'🛒 Shopping list · 2 to buy\n\n📦 Online\nShopA · CHF 12.50\n  2× Oat milk, Batteries AA'
out=$(FAKE_CARD="$card" tcmd shop); rc=$?
eq "shop: rc 0" "$rc" "0"
eq "shop: card text passed through" "$out" "$card"
eq "shop: sync, then the text card (no --json)" "$(cat "$T/log")" $'shop sync --json\nshop card'
out=$(FAKE_CARD="🛒 Shopping list is empty" tcmd shop)
eq "shop: empty list" "$out" "🛒 Shopping list is empty"
out=$(FAKE_CARD="" tcmd shop)
eq "shop: no output -> empty message" "$out" "🛒 Shopping list is empty"
out=$(FAKE_SYNC_ERR="timeout" FAKE_CARD="$card" tcmd shop); rc=$?
eq "shop: HA down -> rc 0" "$rc" "0"
eq "shop: HA down -> last card + note" "$out" "$card"$'\n\n⚠️ HA not reachable: showing the last synced list'
out=$(FAKE_CARD_ERR="state file unreadable" tcmd shop); rc=$?
eq "shop: card fails -> rc 3" "$rc" "3"
has "shop: card fails -> fallback" "$out" "⚠️ /shop failed: shopping list: state file unreadable."
has "shop: fallback hint" "$out" "what's on my shopping list?"

echo "# /parcels (fake parcels) + its line under /shop"
cat > "$T/parcels" <<'EOFP'
#!/bin/sh
printf 'parcels %s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  line) [ -n "${FAKE_PARCELS_LINE:-}" ] && echo "$FAKE_PARCELS_LINE"; exit 0;;
  picked) echo "✅ Marked 1 parcel as picked up; pickup reminder cancelled.";;
  "") [ -n "${FAKE_PARCELS_ERR:-}" ] && { echo "boom" >&2; exit 1; }; printf '📦 Parcels · as of 09:00\n🧾 Testshop: Fri 13.3 · Test Pot\n';;
esac
EOFP
chmod +x "$T/parcels"
out=$(FAKE_CARD="$card" tcmd shop)
eq "shop: no parcels line when none are coming" "$out" "$card"
out=$(FAKE_CARD="$card" FAKE_PARCELS_LINE="📦 2 on the way → /parcels" tcmd shop)
eq "shop: parcels line under the card" "$out" "$card"$'\n\n📦 2 on the way → /parcels'
out=$(tcmd parcels); rc=$?
eq "parcels: rc 0" "$rc" "0"
eq "parcels: card passed through" "$out" $'📦 Parcels · as of 09:00\n🧾 Testshop: Fri 13.3 · Test Pot'
out=$(tcmd parcels picked)
eq "parcels picked: mark, then the card" "$out" $'✅ Marked 1 parcel as picked up; pickup reminder cancelled.\n\n📦 Parcels · as of 09:00\n🧾 Testshop: Fri 13.3 · Test Pot'
eq "parcels picked: station only" "$(head -1 "$T/log")" "parcels picked station"
out=$(tcmd parcels whatever)
has "parcels: unknown word -> hint, nothing run" "$out" "/parcels takes one word: picked"
out=$(FAKE_PARCELS_ERR=1 tcmd parcels); rc=$?
eq "parcels: failure -> rc 3" "$rc" "3"
has "parcels: fallback hint" "$out" "where are my parcels?"
rm -f "$T/parcels"

echo "# /gym (fake gym)"
cat > "$T/gym" <<'EOF3'
#!/usr/bin/env bash
printf 'gym %s\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_GYM_ERR:-}" ]; then echo "gym: $FAKE_GYM_ERR" >&2; exit 1; fi
printf '%s\n' "${FAKE_GYM_OUT-}"
EOF3
chmod +x "$T/gym"
gcard=$'🏋️ Next: B · Pull (~45 min)\nLast session 01.03. (A, 4 d ago) · streak 3 wk\n\n1  Row  3×8–12 @ 50 kg'
out=$(FAKE_GYM_OUT="$gcard" tcmd gym); rc=$?
eq "gym: rc 0" "$rc" "0"
eq "gym: card passed through" "$out" "$gcard"
eq "gym: exactly one local 'gym plan next' (no sync)" "$(cat "$T/log")" "gym plan next"
out=$(FAKE_GYM_OUT=$'🏋️ Gym · no plan yet\n6 sessions in 4 wk (1.5/wk) · streak 3 wk · last 01.03. (4 d ago)' tcmd gym)
has "gym: no plan -> summary line" "$out" "streak 3 wk · last 01.03."
out=$(FAKE_GYM_OUT="" tcmd gym)
eq "gym: no output -> fallback text" "$out" "🏋️ Gym · nothing logged yet"
out=$(FAKE_GYM_ERR="database is locked" tcmd gym); rc=$?
eq "gym: failure -> rc 3" "$rc" "3"
has "gym: failure -> fallback" "$out" "⚠️ /gym failed: gym: database is locked."
has "gym: fallback hint" "$out" "what's my next gym session?"
: > "$T/log"; out=$(FAKE_GYM_OUT=$'🏋️ Recap · A · Squat day' tcmd gym recap)
eq "gym recap: one 'gym recap' call" "$(cat "$T/log")" "gym recap"
: > "$T/log"; out=$(tcmd gym next)
has "gym next (retired coach word): the hint" "$out" "/gym recap = last session"
eq "gym next: no gym call" "$(cat "$T/log")" ""

echo "# /train (fake train-watch: legs + per-leg status JSON)"
cat > "$T/train-watch" <<'EOF4'
#!/usr/bin/env bash
printf 'train-watch %s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  list) if [ -n "${FAKE_TW_ERR:-}" ]; then echo "train-watch: $FAKE_TW_ERR" >&2; exit 1; fi; cat "$FAKE_TW_LEGS" ;;
  status) [ "${FAKE_TW_SLOW:-}" = "$2" ] && sleep 15
          [ -f "$FAKE_TW_DIR/$2.json" ] || { echo "train-watch: transport API unreachable" >&2; exit 1; }
          cat "$FAKE_TW_DIR/$2.json" ;;
  *) exit 2 ;;
esac
EOF4
chmod +x "$T/train-watch"
mkdir -p "$T/tw" "$T/tw-none"
cat > "$T/legs.json" <<'EOF5'
[{"id": "a5", "from": "home", "to": "Basel SBB", "dep": "2031-03-05T08:00:00+01:00", "arr": "2031-03-05T09:00:00+01:00", "lines": ["IR36"], "title": "🚆 Home → Basel SBB"},
 {"id": "a2", "from": "Basel SBB", "to": "home", "dep": "2031-03-06T01:40:00+01:00", "arr": "2031-03-06T02:50:00+01:00", "lines": ["SN3"], "title": "🚆 Basel SBB → Home (after Artist One)"},
 {"id": "a1", "from": "home", "to": "Basel SBB", "dep": "2031-03-05T13:10:00+01:00", "arr": "2031-03-05T14:05:00+01:00", "lines": ["IR36"], "title": "🚆 Home → Basel SBB (Artist One)"},
 {"id": "a3", "from": "home", "to": "Lausanne", "dep": "2031-03-05T16:00:00+01:00", "arr": "2031-03-05T18:30:00+01:00", "lines": ["IC5", "IR15"], "title": "🚆 Home → Lausanne"},
 {"id": "a4", "from": "Lausanne", "to": "home", "dep": "2031-03-08T09:00:00+01:00", "arr": "2031-03-08T11:30:00+01:00", "lines": ["IC5"], "title": "🚆 Lausanne → Home (after Club Night)"},
 {"id": "a6", "from": "home", "to": "Bern", "dep": "2031-03-20T10:00:00+01:00", "arr": "2031-03-20T11:00:00+01:00", "lines": [], "title": "🚆 Home → Bern"}]
EOF5
echo '{"fine": true, "live": {"dep": "13:10", "arr": "14:05", "delay": 0, "platform": "7"}, "problems": [], "alt": null}' > "$T/tw/a1.json"
echo '{"fine": false, "live": {"dep": "16:06", "arr": "18:36", "delay": 6, "platform": "3"}, "problems": [{"key": "delay5", "text": "delayed"}], "alt": null}' > "$T/tw/a3.json"
echo '{"fine": false, "live": null, "problems": [{"key": "cancelled", "text": "gone"}], "alt": {"dep": "01:55", "arr": "03:05", "lines": ["SN3"]}}' > "$T/tw/a2.json"
echo '{"fine": false, "live": {"dep": "09:00", "arr": "11:30", "delay": 0}, "problems": [{"key": "platform:Lausanne:9", "text": "pl"}, {"key": "bus:Lausanne", "text": "bus"}], "alt": null}' > "$T/tw/a4.json"
echo '{"fine": true, "live": {"dep": "10:00", "arr": "11:00", "delay": 0, "platform": "4"}, "problems": [], "alt": null}' > "$T/tw/a6.json"
train() { FAKE_TW_LEGS="$T/legs.json" FAKE_TW_DIR="${TWD:-$T/tw}" TG_CMD_NOW="${NOW:-2031-03-05 12:00}" tcmd train; }
out=$(train); rc=$?
eq "train: rc 0" "$rc" "0"
want='🚆 Trains · next 4 of 5 · home = door to door
today 13:10 home → Basel SBB 14:05
   IR36 · ✅ on time · pl 7
today 16:00 home → Lausanne 18:30
   IC5 › IR15 · ⚠️ +6 min (arr 18:36)
tomorrow 01:40 Basel SBB → home 02:50
   SN3 · ❌ cancelled → alt 01:55 SN3 → 03:05
Sat 08.03 09:00 Lausanne → home 11:30
   IC5 · ⚠️ now pl 9 · 🚌 replacement bus'
eq "train: full answer (past leg dropped, soonest first, 4 shown)" "$out" "$want"
eq "train: at most 10 lines" "$(( $(printf '%s\n' "$out" | wc -l) <= 10 ))" "1"
eq "train: one list + one status --json per shown leg" "$(sort "$T/log")" $'train-watch list --json\ntrain-watch status a1 --json\ntrain-watch status a2 --json\ntrain-watch status a3 --json\ntrain-watch status a4 --json'
out=$(NOW="2031-03-01 12:00" train)
has "train: far leg -> as planned, no platform" "$out" $'home → Basel SBB 14:05\n   IR36 · ✅ as planned'
hasnt "train: far leg -> no 'on time'" "$out" "on time"
out=$(TWD="$T/tw-none" train); rc=$?
eq "train: every status fails -> still rc 0" "$rc" "0"
eq "train: every status fails -> grey line per leg" "$(printf '%s\n' "$out" | grep -c '⚪ no live status right now')" "4"
start=$(date +%s); out=$(FAKE_TW_SLOW=a3 train); rc=$?; took=$(( $(date +%s) - start ))
eq "train: one slow leg -> rc 0" "$rc" "0"
has "train: slow leg -> grey line, others kept" "$out" $'IC5 › IR15 · ⚪ no live status right now\ntomorrow 01:40'
eq "train: slow leg killed within the plugin's 20 s" "$(( took < 15 ))" "1"
echo '[]' > "$T/legs-empty.json"
out=$(FAKE_TW_LEGS="$T/legs-empty.json" FAKE_TW_DIR="$T/tw" TG_CMD_NOW="2031-03-05 12:00" tcmd train); rc=$?
eq "train: no legs" "$rc:$out" "0:🚆 No train legs planned ahead."
out=$(FAKE_TW_ERR="legs file unreadable" train); rc=$?
eq "train: list fails -> rc 3" "$rc" "3"
has "train: list fails -> fallback" "$out" "⚠️ /train failed: train watch: legs file unreadable."
has "train: fallback hint" "$out" "are my trains on time?"

echo "# /rave (fake rave-radar + train legs + trips)"
cat > "$T/rave-radar" <<'EOF6'
#!/usr/bin/env bash
printf 'rave-radar %s\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_RR_ERR:-}" ]; then echo "rave-radar: $FAKE_RR_ERR" >&2; exit 1; fi
printf '%s\n' "${FAKE_RR_JSON-[]}"
EOF6
chmod +x "$T/rave-radar"
mkdir -p "$T/trips"
cat > "$T/trips/conditions.json" <<'EOF7'
{"trips": {"island": {"name": "Island Trip", "emoji": "🌴", "tz": "UTC", "from": "2031-03-14", "to": "2031-03-22",
  "legs": [{"name": "North Isle", "from": "2031-03-14", "to": "2031-03-18"}, {"name": "South Isle", "from": "2031-03-18", "to": "2031-04-02"}]}}}
EOF7
echo '{"name": "Island Trip", "from": "2031-03-14", "until": "2031-03-22", "legs": ["flight out Fri 14.03 21:35", "back Sat 22.03"]}' > "$T/trips/pretrip.json"
cat > "$T/trips/extra.json" <<'EOF8'
{"trips": [{"name": "Island Trip", "link": "https://example.org/hub"},
           {"name": "Unknown Trip", "link": "https://example.org/x"},
           {"name": "Family Fest", "emoji": "💍", "from": "2031-03-07", "to": "2031-03-08", "legs": ["🚗 drive 15:00"], "match": ["fest"], "todo": ["Pack the gear"]}]}
EOF8
export TG_CMD_TRIP_CONDITIONS="$T/trips/conditions.json" TG_CMD_PRETRIP="$T/trips/pretrip.json" TG_CMD_TRIPS="$T/trips/extra.json" TG_CMD_TODO="$T/trips/TODO.md"
rr='[{"kind": "show", "artist": "Artist One", "date": "2031-03-05", "city": "Basel", "venue": "Hall A", "presale": null, "soldout": false},
 {"kind": "show", "artist": "Artist Two", "date": "2031-03-15", "city": "Bern", "venue": "Club B", "soldout": true},
 {"kind": "lineup", "artist": "Artist Three", "date": null, "city": "Somewhere (DE)", "venue": "Big Fest", "presale": "2031-04-01"},
 {"kind": "show", "artist": "Artist Four", "date": "2031-03-08", "city": "Zürich", "venue": "Club C", "presale": "2031-03-01"}]'
rave() { FAKE_TW_LEGS="$T/legs.json" FAKE_TW_DIR="$T/tw" TG_CMD_NOW="2031-03-05 12:00" tcmd rave; }
out=$(FAKE_RR_JSON="$rr" rave); rc=$?
eq "rave: rc 0" "$rc" "0"
want='🎧 Raves · next 5
today · Artist One · Basel · 🎟 going
Fri 07.03 · Club Night · Lausanne · 🎟 going
Sat 08.03 · Artist Four · Zürich · ✈️ clashes with Family Fest
Sat 15.03 · Artist Two · Bern · ✈️ clashes with Island Trip · sold out
date tba · Artist Three · Big Fest lineup, Somewhere (DE) · presale 01.04'
eq "rave: full answer (going from 🚆 titles, return leg not doubled, clashes, past presale dropped)" "$out" "$want"
eq "rave: one radar read + one legs read, no status calls" "$(cat "$T/log")" $'rave-radar list --days 180 --json\ntrain-watch list --json'
many=$(python3 -c 'import json; print(json.dumps([{"kind": "show", "artist": f"Act {i}", "date": f"2031-04-{i+1:02d}", "city": "Bern"} for i in range(9)]))')
out=$(FAKE_RR_JSON="$many" FAKE_TW_LEGS="$T/legs-empty.json" TG_CMD_NOW="2031-03-05 12:00" tcmd rave)
has "rave: capped header" "$out" "🎧 Raves · next 8 of 9"
eq "rave: capped at 9 lines" "$(printf '%s\n' "$out" | wc -l)" "9"
out=$(FAKE_RR_JSON="$rr" FAKE_TW_ERR="locked" rave); rc=$?
eq "rave: train-watch down -> rc 0" "$rc" "0"
hasnt "rave: train-watch down -> no going marks" "$out" "🎟"
has "rave: train-watch down -> radar rows kept" "$out" "today · Artist One · Basel"
out=$(FAKE_RR_JSON="[]" FAKE_TW_LEGS="$T/legs-empty.json" TG_CMD_NOW="2031-03-05 12:00" tcmd rave)
eq "rave: nothing" "$out" "🎧 No upcoming raves on the radar."
out=$(FAKE_RR_ERR="state unreadable" rave); rc=$?
eq "rave: radar fails -> rc 3" "$rc" "3"
has "rave: fallback hint" "$out" "what raves are coming up?"

echo "# /trip (trip files, fake remind, TODO.md)"
cat > "$T/trips/TODO.md" <<'EOF9'
# TODO
## Due
- **Island prep call** — 06.03 18:00
## Waiting on you
- **Island Trip stays** — villas or resorts? (sent 11:10; internal note)
- **Other thing** — unrelated
## Ideas
- **Island Trip ideas** — not waiting
EOF9
cat > "$T/remind" <<'EOF10'
#!/usr/bin/env bash
printf 'remind %s\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_REMIND_ERR:-}" ]; then echo "remind: $FAKE_REMIND_ERR" >&2; exit 1; fi
printf '%s\n' "${FAKE_REMIND_OUT-}"
EOF10
chmod +x "$T/remind"
rem=' 1. 2031-03-06 18:00  [owner] 🌴 Island Trip: book all nights by 10.03 (the visa needs them)
 2. 2031-03-07 09:00  Ask owner about the Island Trip shuttle
 3. 2031-03-09 09:00  [owner] Pay the bill before the island trip  (tag trip)
 4. 2031-04-01 09:00  [owner] Island Trip photos album
 5. 2031-03-06 10:00  [owner] Dentist'
trip() { FAKE_TW_LEGS="$T/legs.json" FAKE_TW_DIR="$T/tw" FAKE_REMIND_OUT="$rem" TG_CMD_NOW="$1" tcmd trip; }
out=$(trip "2031-03-05 12:00"); rc=$?
eq "trip: rc 0" "$rc" "0"
want='💍 Family Fest · Fri 7–8.3 · in 2 days
🚗 drive 15:00
🚆 Sat 08.03 09:00 Lausanne → home 11:30
Open:
☐ Pack the gear
Then: 🌴 Island Trip 14–22.3'
eq "trip: next = the short one, its own todo + train leg, then the next trip" "$out" "$want"
out=$(trip "2031-03-10 10:00")
want='🌴 Island Trip · Fri 14–22.3 (8 nights) · in 4 days
🏝 North Isle 14–18.3 › South Isle 18.3–2.4
✈️ flight out Fri 14.03 21:35
✈️ back Sat 22.03
🚆 Thu 20.03 10:00 home → Bern 11:00
Open:
☐ Island Trip stays: villas or resorts?
☐ 06.03 book all nights by 10.03 (the visa needs them)
☐ 09.03 Pay the bill before the island trip
🗺 https://example.org/hub'
eq "trip: files merged by name, waiting item + owner reminders up to the trip end, link" "$out" "$want"
eq "trip: exactly 10 lines" "$(printf '%s\n' "$out" | wc -l)" "10"
hasnt "trip: my own follow-ups stay out" "$out" "shuttle"
hasnt "trip: board internals stay out" "$out" "internal note"
hasnt "trip: dates-less extra trip ignored" "$out" "example.org/x"
has "trip: during the trip -> day N of M" "$(trip "2031-03-16 09:00" | head -1)" "· day 3 of 9"
has "trip: last day" "$(trip "2031-03-22 09:00" | head -1)" "· last day"
many_rem=$(for i in 1 2 3 4 5 6; do printf ' %d. 2031-03-0%d 09:00  [owner] Island Trip task %d\n' "$i" "$i" "$i"; done)
out=$(FAKE_TW_LEGS="$T/legs.json" FAKE_TW_DIR="$T/tw" FAKE_REMIND_OUT="$many_rem" TG_CMD_NOW="2031-03-10 10:00" tcmd trip)
eq "trip: overflow -> still 10 lines" "$(printf '%s\n' "$out" | wc -l)" "10"
has "trip: overflow -> + N more" "$out" "+ 5 more (ask me)"
out=$(FAKE_REMIND_ERR="db locked" FAKE_TW_ERR="locked" FAKE_TW_LEGS="$T/legs.json" TG_CMD_NOW="2031-03-10 10:00" tcmd trip); rc=$?
eq "trip: remind + train-watch down -> rc 0" "$rc" "0"
has "trip: remind down -> TODO item still listed" "$out" "☐ Island Trip stays: villas or resorts?"
out=$(trip "2031-04-03 10:00")
eq "trip: none ahead" "$out" "🧳 No trip planned."
echo 'not json' > "$T/trips/broken.json"
out=$(TG_CMD_TRIP_CONDITIONS="$T/trips/broken.json" TG_CMD_PRETRIP="$T/trips/none.json" trip "2031-03-10 10:00"); rc=$?
eq "trip: broken/missing trip files -> rc 0" "$rc" "0"
eq "trip: broken files -> only the remaining trip" "$out" "🧳 No trip planned."
echo "# /trip WORDS|list and the offline pack line"
printf '<!doctype html><p>pack</p>\n' > "$T/trips/velmora-pack.html"
python3 - "$T/trips/extra.json" "$T/trips/extra2.json" "$T/trips/velmora-pack.html" "$T/trips/missing-pack.html" <<'EOF11'
import json, sys
d = json.load(open(sys.argv[1]))
d["trips"] += [{"name": "Velmora Fair", "emoji": "🎪", "from": "2031-03-25", "to": "2031-03-26", "match": ["velmora", "castelluna"],
                "legs": ["✈️ Thu 26.3 Velmora → Castelluna (not booked yet)"], "pack": sys.argv[3]},
               {"name": "Nordby Hike", "from": "2031-04-05", "to": "2031-04-05", "pack": sys.argv[4]}]
json.dump(d, open(sys.argv[2], "w"))
EOF11
trip2() { TG_CMD_TRIPS="$T/trips/extra2.json" FAKE_TW_LEGS="$T/legs.json" FAKE_TW_DIR="$T/tw" FAKE_REMIND_OUT="${REM2-}" TG_CMD_NOW="$1" tcmd trip "${@:2}"; }
out=$(trip2 "2031-03-10 10:00" velmora); rc=$?
eq "trip velmora: rc 0" "$rc" "0"
eq "trip velmora: head" "$(printf '%s\n' "$out" | head -1)" "🎪 Velmora Fair · Tue 25–26.3 · in 15 days"
has "trip velmora: its legs" "$out" "✈️ Thu 26.3 Velmora → Castelluna (not booked yet)"
has "trip velmora: pack line with size, date and path" "$out" "📦 Offline pack 1 KB ("
vp="$T/trips/velmora-pack.html"; case $vp in "$HOME"/*) vp="~${vp#"$HOME"}" ;; esac   # tg-cmd shows home paths as ~/...
has "trip velmora: pack line names the file for the session" "$out" "“send the pack” → $vp"
hasnt "trip velmora: no Then line for a picked trip" "$out" "Then:"
has "trip by match word" "$(trip2 "2031-03-10 10:00" Castelluna | head -1)" "🎪 Velmora Fair"
has "trip by name word (case-insensitive)" "$(trip2 "2031-03-10 10:00" ISLAND | head -1)" "🌴 Island Trip"
out=$(trip2 "2031-03-10 10:00" atlantis); rc=$?
eq "trip unknown word: rc 0 + hint" "$rc|$out" "0|🧳 No trip ahead called “atlantis”.
/trip list shows them."
out=$(trip2 "2031-03-10 10:00" list)
eq "trip list" "$out" "🧳 Trips ahead (3)
🌴 Island Trip · 14–22.3 · in 4 d
🎪 Velmora Fair · 25–26.3 · in 15 d 📦
🧳 Nordby Hike · 5.4 · in 26 d
/trip WORDS shows one"
has "trip list: pack marker only where the file exists" "$(printf '%s\n' "$out" | grep Velmora)" "📦"
hasnt "trip list: missing pack file -> no marker" "$(printf '%s\n' "$out" | grep Nordby)" "📦"
has "trip: bare /trip -> Then line says the pack is ready" "$(trip2 "2031-03-20 12:00" | tail -1)" "Then: 🎪 Velmora Fair 25–26.3 · 📦 pack ready"
hasnt "trip nordby: missing pack file -> no pack line" "$(trip2 "2031-03-30 10:00" nordby)" "📦"
REM2=$(for i in 1 2 3 4 5 6 7 8 9; do printf ' %d. 2031-03-1%d 09:00  [owner] Velmora task %d\n' "$i" "$i" "$i"; done)
out=$(REM2="$REM2" trip2 "2031-03-10 10:00" velmora)
eq "trip velmora + 9 to-dos: still 10 lines" "$(printf '%s\n' "$out" | wc -l)" "10"
has "trip velmora + 9 to-dos: pack line kept" "$(printf '%s\n' "$out" | tail -1)" "📦 Offline pack"
"$TGCMD" trip a b >/dev/null 2>&1; eq "trip: two arguments -> rc 2" "$?" "2"
unset TG_CMD_TRIP_CONDITIONS TG_CMD_PRETRIP TG_CMD_TRIPS TG_CMD_TODO

echo "# /warranty (fake contracts)"
cat > "$T/contracts" <<'EOF4'
#!/usr/bin/env bash
printf 'contracts %s\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_CONTRACTS_ERR:-}" ]; then echo "contracts: $FAKE_CONTRACTS_ERR" >&2; exit 3; fi
printf '%s\n' "${FAKE_CONTRACTS_OUT-🛡 Warranty · 1 still covered
• Beispiel-Fernseher · Galaxus · until 12.09.2028
24 mo = Swiss legal default (OR 210); shop/maker terms may differ.}"
EOF4
chmod +x "$T/contracts"
export FAKE_LISTS_OUT='{"list": "warranty", "open": [], "done": []}'
LW='lists show warranty --json'
out=$(tcmd warranty); rc=$?
eq "warranty: rc 0" "$rc" "0"
has "warranty: card passed through" "$out" "• Beispiel-Fernseher · Galaxus · until 12.09.2028"
eq "warranty: logged list read, then one capped call, small card budget" "$(cat "$T/log")" "$LW"$'\n'"contracts warranty --max 7 --budget 6"
eq "warranty: no logged entries -> no block" "$([[ "$out" == *"Logged by you"* ]] && echo yes || echo no)" "no"
out=$(tcmd warranty "  Fernseher   55 "); rc=$?
eq "warranty WORDS: rc 0" "$rc" "0"
eq "warranty WORDS: words after --" "$(tail -1 "$T/log")" "contracts warranty --max 7 --budget 6 -- Fernseher 55"
out=$(tcmd /warranty "--all"); eq "warranty: a dash word stays a word" "$(tail -1 "$T/log")" "contracts warranty --max 7 --budget 6 -- --all"
out=$(tcmd warranty "   "); eq "warranty: blank arg = no filter" "$(tail -1 "$T/log")" "contracts warranty --max 7 --budget 6"
err=$(tcmd warranty a b 2>&1 >/dev/null); rc=$?
eq "warranty: two args rc 2" "$rc" "2"
has "warranty: two args says one at most" "$err" "one argument at most"
out=$(FAKE_CONTRACTS_OUT="" tcmd warranty); eq "warranty: empty output -> fallback" "$out" "🛡 Warranty · no answer"
out=$(FAKE_CONTRACTS_ERR="paperless metadata unavailable: timeout" tcmd warranty); rc=$?
eq "warranty: failure -> rc 3" "$rc" "3"
has "warranty: failure -> fallback" "$out" "⚠️ /warranty failed: paperless: paperless metadata unavailable: timeout."
has "warranty: fallback hint" "$out" "is my X still under warranty?"
lines=$(tcmd warranty | wc -l); eq "warranty: short card" "$([ "$lines" -le 10 ] && echo ok)" "ok"
# his own entries (voice-plan's warranty action -> `lists add warranty "product · shop · bought D · warranty to D"`)
wl='{"list": "warranty", "open": [
 {"n": 1, "text": "Beispiel-Kopfhörer · Digitec · bought 01.02.2030 · warranty to 01.02.2032", "added": "2030-02-01 10:00"},
 {"n": 2, "text": "Beispiel-Mixer · Brack · bought 01.06.2029 · warranty to 01.04.2031", "added": "2029-06-01 10:00"},
 {"n": 3, "text": "Alter Toaster · bought 01.01.2027 · warranty to 01.01.2029", "added": "2027-01-01 10:00"},
 {"n": 4, "text": "Velo-Lampe ohne Datum", "added": "2030-01-01 10:00"}], "done": [
 {"text": "Erledigt · warranty to 01.01.2035", "done": "2030-01-01 10:00"}]}'
out=$(FAKE_LISTS_OUT="$wl" TG_CMD_NOW="2031-03-05 12:00" tcmd warranty); rc=$?
eq "warranty logged: rc 0" "$rc" "0"
has "warranty logged: block after the paperless rows" "$out" $'• Beispiel-Fernseher · Galaxus · until 12.09.2028\n📝 Logged by you (3)'
has "warranty logged: soonest first, ⚠️ within 90 d" "$out" $'📝 Logged by you (3)\n• Beispiel-Mixer · Brack · ⚠️ until 01.04.2031 (27 d)\n• Beispiel-Kopfhörer · Digitec · until 01.02.2032\n• Velo-Lampe ohne Datum'
eq "warranty logged: expired + checked ones hidden" "$([[ "$out" == *Toaster* || "$out" == *Erledigt* ]] && echo shown || echo hidden)" "hidden"
eq "warranty logged: legal-default note stays last" "$(printf '%s\n' "$out" | tail -1)" "24 mo = Swiss legal default (OR 210); shop/maker terms may differ."
eq "warranty logged: paperless gets fewer rows (7 - 3 logged lines)" "$(tail -1 "$T/log")" "contracts warranty --max 4 --budget 6"
lines=$(printf '%s\n' "$out" | wc -l); eq "warranty logged: card stays short" "$([ "$lines" -le 12 ] && echo ok)" "ok"
out=$(FAKE_LISTS_OUT="$wl" TG_CMD_NOW="2031-03-05 12:00" tcmd warranty toaster)
has "warranty logged WORDS: expired match shown" "$out" $'📝 Logged by you · 1 match\n• Alter Toaster · expired 01.01.2029'
eq "warranty logged WORDS: words still go to contracts" "$(tail -1 "$T/log")" "contracts warranty --max 6 --budget 6 -- toaster"
out=$(FAKE_LISTS_OUT="$wl" TG_CMD_NOW="2031-03-05 12:00" tcmd warranty "kopfhorer")
has "warranty logged WORDS: accent-folded match" "$out" "• Beispiel-Kopfhörer · Digitec · until 01.02.2032"
out=$(FAKE_LISTS_OUT="$wl" TG_CMD_NOW="2031-03-05 12:00" FAKE_CONTRACTS_ERR="paperless metadata unavailable: timeout" tcmd warranty); rc=$?
eq "warranty logged + paperless down: rc 0" "$rc" "0"
has "warranty logged + paperless down: says so" "$out" "🛡 Warranty · ⚠️ paperless not reachable (paperless metadata unavailable: timeout)"
has "warranty logged + paperless down: logged rows kept" "$out" "• Beispiel-Mixer · Brack"
out=$(FAKE_LISTS_OUT="$wl" TG_CMD_NOW="2031-03-05 12:00" FAKE_CONTRACTS_OUT="" tcmd warranty)
eq "warranty logged + empty paperless answer: head + block" "$(printf '%s\n' "$out" | head -2)" $'🛡 Warranty\n📝 Logged by you (3)'
many='{"list": "warranty", "open": ['$(for i in 1 2 3 4 5 6; do printf '{"text": "Ding %s · warranty to 0%s.01.2033"}%s' "$i" "$i" "$([ "$i" -lt 6 ] && echo ,)"; done)'], "done": []}'
out=$(FAKE_LISTS_OUT="$many" TG_CMD_NOW="2031-03-05 12:00" tcmd warranty)
has "warranty logged: at most 4 rows + more" "$out" $'• Ding 4 · until 04.01.2033\n… +2 more'
eq "warranty logged: paperless keeps at least 3 rows" "$(tail -1 "$T/log")" "contracts warranty --max 3 --budget 6"
out=$(FAKE_LISTS_OUT="not json" tcmd warranty); rc=$?
eq "warranty: logged list unreadable -> rc 0" "$rc" "0"
has "warranty: logged list unreadable -> one warning line" "$out" "⚠️ logged warranties: unreadable answer"
has "warranty: logged list unreadable -> paperless card kept" "$out" "• Beispiel-Fernseher · Galaxus · until 12.09.2028"
out=$(FAKE_LISTS_ERR="lock busy" tcmd warranty tv); rc=$?
eq "warranty WORDS: logged list broken -> no warning on a search" "$([[ "$out" == *"⚠️"* ]] && echo warn || echo none)" "none"
unset FAKE_LISTS_OUT

# /reminders: location reminders (remind add --on) under "📍 By place"
prem=' 1. 2031-03-06 18:00  [owner] Dentist
 2. 📍 when home: Power-cycle the sensors
 3. 📍 when at Work: Bring the badge  (every time)  (tag x)'
out=$(FAKE_REMIND_OUT="$prem" TG_CMD_NOW="2031-03-05 12:00" tcmd reminders)
has "reminders: place section" "$out" "📍 By place"
has "reminders: when home line" "$out" "When home: Power-cycle the sensors"
has "reminders: every time = 🔁, tag dropped" "$out" "When at Work: Bring the badge  🔁"
has "reminders: timed ones still listed" "$out" "Dentist"
out=$(FAKE_REMIND_OUT=' 1. 📍 when leaving home: Take the bins' TG_CMD_NOW="2031-03-05 12:00" tcmd reminders)
has "reminders: a place one alone still lists" "$out" "When leaving home: Take the bins"

echo "# /back (back.sh)"
bk=$(TGCMD_BIN="$TGCMD" timeout 200 bash "$(dirname "$0")/back.sh" 2>&1)
printf '%s\n' "$bk" | grep -E '^(FAIL|       got)' || true
bp=$(printf '%s\n' "$bk" | sed -n 's/^pass=\([0-9]*\) fail=\([0-9]*\)$/\1 \2/p')
if [ -n "$bp" ]; then pass=$((pass + ${bp% *})); fail=$((fail + ${bp#* })); else no "back.sh ran to the end" "$(printf '%s' "$bk" | tail -n3)"; fi

echo
echo "# picture cards (pictures.sh)"
pc=$(TGCMD_BIN="$TGCMD" timeout 200 bash "$(dirname "$0")/pictures.sh" 2>&1)
printf '%s\n' "$pc" | grep -E '^(FAIL|       got)' || true
pcp=$(printf '%s\n' "$pc" | sed -n 's/^\([0-9]*\) passed, \([0-9]*\) failed$/\1 \2/p' | tail -n1)
if [ -n "$pcp" ]; then pass=$((pass + ${pcp% *})); fail=$((fail + ${pcp#* })); else no "pictures.sh ran to the end" "$(printf '%s' "$pc" | tail -n3)"; fi

echo "# /train board + /trip card (trip-cards.sh)"
tc=$(TGCMD_BIN="$TGCMD" timeout 200 bash "$(dirname "$0")/trip-cards.sh" 2>&1)
printf '%s\n' "$tc" | grep -E '^(FAIL|       got)' || true
tcp=$(printf '%s\n' "$tc" | sed -n 's/^\([0-9]*\) passed, \([0-9]*\) failed$/\1 \2/p' | tail -n1)
if [ -n "$tcp" ]; then pass=$((pass + ${tcp% *})); fail=$((fail + ${tcp#* })); else no "trip-cards.sh ran to the end" "$(printf '%s' "$tc" | tail -n3)"; fi

echo "# /pack + pack --todo (pack.sh)"
pk=$(TGCMD_BIN="$TGCMD" PACK_BIN="${PACK_BIN:-/home/node/bin/pack}" timeout 200 bash "$(dirname "$0")/pack.sh" 2>&1)
printf '%s\n' "$pk" | grep -v '^ok' | grep -v '^pass=' | grep -v '^$'
pp=$(printf '%s' "$pk" | sed -n 's/^pass=\([0-9]*\) fail=\([0-9]*\)$/\1 \2/p')
if [ -n "$pp" ]; then pass=$((pass + ${pp% *})); fail=$((fail + ${pp#* })); else no "pack.sh ran to the end" "$(printf '%s' "$pk" | tail -n3)"; fi

echo "# /gohome (fake gohome)"
G=$(mktemp -d)
cat > "$G/gohome" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$#:$*" >> "$FAKE_GH_LOG"
if [ -n "${FAKE_GH_ERR:-}" ]; then echo "gohome: transport API unreachable or broken: timed out" >&2; exit 1; fi
printf '🏠 Home from %s · 22:10\n22:37 Alpha Hbf Pl. 3 · S7 · direct → 🏠 23:40 https://app.sbbmobile.ch/timetable?from=Alpha%%20Hbf&to=X&time=1\n' "${1:-Alpha Hbf (phone)}"
EOF
chmod +x "$G/gohome"
out=$(TG_CMD_BIN="$G" FAKE_GH_LOG="$G/log" "$TGCMD" gohome); rc=$?
eq "gohome: rc 0" "$rc" "0"
eq "gohome: the card as gohome prints it" "$(printf '%s\n' "$out" | head -1)" "🏠 Home from Alpha Hbf (phone) · 22:10"
has "gohome: SBB link passes through" "$out" "https://app.sbbmobile.ch/timetable?from=Alpha%20Hbf"
out=$(TG_CMD_BIN="$G" FAKE_GH_LOG="$G/log" "$TGCMD" gohome "Alpha Hbf Nord"); rc=$?
eq "gohome STOP: rc 0" "$rc" "0"
eq "gohome: no arg = no argv, a stop with spaces = one argv" "$(cat "$G/log")" "0:
1:Alpha Hbf Nord"
out=$(TG_CMD_BIN="$G" FAKE_GH_LOG="$G/log" FAKE_GH_ERR=1 "$TGCMD" gohome); rc=$?
eq "gohome failing: rc 3" "$rc" "3"
eq "gohome failing: reason + fallback hint" "$out" "⚠️ /gohome failed: way home: transport API unreachable or broken: timed out.
Ask me in a normal message (\"how do I get home from here?\") and I'll look."
find "$G" -mindepth 1 -delete; rmdir "$G"

echo "# /vibe (fake media)"
VB=$(mktemp -d)
cat > "$VB/media" <<'FAKEMEDIA'
#!/usr/bin/env bash
printf '%s\n' "$#:$*" >> "$FAKE_VB_LOG"
if [ -n "${FAKE_VB_ERR:-}" ]; then echo "media: vibe: no index yet and Emby is unreachable" >&2; exit 1; fi
if [ -n "${FAKE_VB_BAD:-}" ]; then echo "not json"; exit 0; fi
printf '{"text": "🎬 Vibe · cozy rainy\\n\\n1️⃣ Cozy Little Cafe (2022) · 84 min · ★6.9\\nComedy · cozy\\n▶ https://emby.example.test/web/index.html#!/item?id=115", "message": "**x**", "buttons": []}\n'
FAKEMEDIA
chmod +x "$VB/media"
vb() { TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" "$TGCMD" vibe "$@"; }
out=$(vb); rc=$?
eq "vibe: bare rc 0" "$rc" "0"
has "vibe: bare = hint" "$out" "/vibe cozy rainy sunday"
eq "vibe: bare runs nothing" "$(cat "$VB/log" 2>/dev/null)" ""
out=$(vb "cozy  rainy"); rc=$?
eq "vibe: rc 0" "$rc" "0"
eq "vibe: the plain text with ▶ links" "$out" "🎬 Vibe · cozy rainy

1️⃣ Cozy Little Cafe (2022) · 84 min · ★6.9
Comedy · cozy
▶ https://emby.example.test/web/index.html#!/item?id=115"
eq "vibe: media vibe --json -- WORDS" "$(cat "$VB/log")" "5:vibe --json -- cozy rainy"
: > "$VB/log"; vb "-n 9 like Arrival" >/dev/null
eq "vibe: dashes stay words" "$(cat "$VB/log")" "7:vibe --json -- -n 9 like Arrival"
has "--list shows /vibe" "$("$TGCMD" --list)" "/vibe       4 movies from your library for a mood"
out=$(FAKE_VB_ERR=1 vb cozy); rc=$?
eq "vibe failing: rc 3" "$rc" "3"
has "vibe failing: fallback hint" "$out" "a movie from my library for a cozy evening"
out=$(FAKE_VB_BAD=1 vb cozy); rc=$?
eq "vibe: unreadable answer rc 3" "$rc" "3"
TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" "$TGCMD" vibe cozy rainy >/dev/null 2>&1; eq "vibe: two args = usage" "$?" "2"
find "$VB" -mindepth 1 -delete; rmdir "$VB"

echo "# /finish (fake media)"
VB=$(mktemp -d)
cat > "$VB/media" <<'FAKEMEDIA'
#!/usr/bin/env bash
printf '%s\n' "$#:$*" >> "$FAKE_VB_LOG"
if [ -n "${FAKE_FIN_ERR:-}" ]; then echo "media: Seerr user 'x' has no linked Emby account" >&2; exit 1; fi
if [ -n "${FAKE_FIN_BAD:-}" ]; then echo "not json"; exit 0; fi
printf '{"text": "🏁 Finish next\\nStarted, paused 14+ days, episodes left\\n\\n📺 Hijack · 3 of 8 left · ★7.4\\n   next S01E06 · last watched 21.08.\\n   ▶ https://emby.example.test/web/index.html#!/item?id=77", "message": "**x**", "buttons": []}\n'
[ -n "${FAKE_FIN_PART:-}" ] && exit 3
exit 0
FAKEMEDIA
chmod +x "$VB/media"
: > "$VB/log"
out=$(TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" "$TGCMD" finish); rc=$?
eq "finish: rc 0" "$rc" "0"
eq "finish: the plain text" "$out" "🏁 Finish next
Started, paused 14+ days, episodes left

📺 Hijack · 3 of 8 left · ★7.4
   next S01E06 · last watched 21.08.
   ▶ https://emby.example.test/web/index.html#!/item?id=77"
eq "finish: media finish --json" "$(cat "$VB/log")" "2:finish --json"
out=$(TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" FAKE_FIN_PART=1 "$TGCMD" finish); rc=$?
eq "finish: media exit 3 (a show failed) still answers" "$rc $(printf '%s' "$out" | sed -n 4p)" "0 📺 Hijack · 3 of 8 left · ★7.4"
out=$(TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" FAKE_FIN_ERR=1 "$TGCMD" finish); rc=$?
eq "finish failing: rc 3" "$rc" "3"
has "finish failing: reason + fallback hint" "$out" "no linked Emby account"
has "finish failing: fallback hint" "$out" "which shows should I finish"
out=$(TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" FAKE_FIN_BAD=1 "$TGCMD" finish); rc=$?
has "finish: unreadable answer" "$out" "Emby finish: unreadable answer"
out=$(TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" "$TGCMD" finish x 2>&1); rc=$?
eq "finish takes no argument" "$rc" "2"
has "--list shows /finish" "$("$TGCMD" --list)" "/finish     Shows you started"
find "$VB" -mindepth 1 -delete; rmdir "$VB"

echo "# /doctor (fake media)"
VB=$(mktemp -d)
cat > "$VB/media" <<'FAKEMEDIA'
#!/usr/bin/env bash
printf '%s\n' "$#:$*" >> "$FAKE_VB_LOG"
if [ -n "${FAKE_DR_ERR:-}" ]; then echo "media: no Seerr match for 'Nope'" >&2; exit 1; fi
if [ -n "${FAKE_DR_BAD:-}" ]; then echo "not json"; exit 0; fi
printf '{"text": "🩺 Request doctor · 2 open requests\\n🔤 Mismatch Movie: name mismatch", "message": "**x**", "items": []}\n'
[ -n "${FAKE_DR_PART:-}" ] && exit 3
exit 0
FAKEMEDIA
chmod +x "$VB/media"
: > "$VB/log"
out=$(TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" "$TGCMD" doctor); rc=$?
eq "doctor: rc 0" "$rc" "0"
eq "doctor: the plain text" "$out" "🩺 Request doctor · 2 open requests
🔤 Mismatch Movie: name mismatch"
eq "doctor: bare = media doctor --json --no-probe" "$(cat "$VB/log")" "3:doctor --json --no-probe"
: > "$VB/log"
TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" "$TGCMD" doctor "The Last  Mountain" >/dev/null
eq "doctor: one title after --" "$(cat "$VB/log")" "7:doctor --json --no-probe -- The Last Mountain"
out=$(TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" FAKE_DR_PART=1 "$TGCMD" doctor); rc=$?
eq "doctor: media exit 3 still answers" "$rc $(printf '%s' "$out" | head -1)" "0 🩺 Request doctor · 2 open requests"
out=$(TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" FAKE_DR_ERR=1 "$TGCMD" doctor Nope); rc=$?
eq "doctor failing: rc 3" "$rc" "3"
has "doctor failing: reason" "$out" "no Seerr match for 'Nope'"
has "doctor failing: fallback hint" "$out" "why isn't X on Emby yet"
out=$(TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" FAKE_DR_BAD=1 "$TGCMD" doctor); rc=$?
has "doctor: unreadable answer" "$out" "Request doctor: unreadable answer"
TG_CMD_BIN="$VB" FAKE_VB_LOG="$VB/log" "$TGCMD" doctor a b >/dev/null 2>&1; eq "doctor: two args = usage" "$?" "2"
has "--list shows /doctor" "$("$TGCMD" --list)" "/doctor     Why a Seerr request"
find "$VB" -mindepth 1 -delete; rmdir "$VB"

echo "# /crate (fake crate-now)"
CR=$(mktemp -d)
cat > "$CR/crate-now" <<'FAKE'
#!/bin/sh
[ -n "$FAKE_CR_LOG" ] && echo "crate-now $*" >> "$FAKE_CR_LOG"
case "$FAKE_CR" in
  add) echo "➕ Crate: Artist – Title" ;;
  fail) echo "⚠️ Couldn't save the track (Home Assistant: down)."; exit 3 ;;
  crash) echo "boom" >&2; exit 1 ;;
esac
FAKE
chmod +x "$CR/crate-now"
out=$(TG_CMD_BIN="$CR" FAKE_CR=add FAKE_CR_LOG="$CR/log" "$TGCMD" crate); rc=$?
eq "crate: rc 0" "$rc" "0"
eq "crate: crate-now's line passed through" "$out" "➕ Crate: Artist – Title"
eq "crate: crate-now called without args" "$(cat "$CR/log")" "crate-now "
out=$(TG_CMD_BIN="$CR" FAKE_CR=fail "$TGCMD" crate); rc=$?
eq "crate: exit-3 fallback line shown" "$out" "⚠️ Couldn't save the track (Home Assistant: down)."
out=$(TG_CMD_BIN="$CR" FAKE_CR=crash "$TGCMD" crate); rc=$?
eq "crate: crash rc 3" "$rc" "3"
has "crate: crash → the normal-message hint" "$out" "add the playing track to my crate"
find "$CR" -mindepth 1 -delete; rmdir "$CR"

echo "# /party (fake party)"
PT=$(mktemp -d)
cat > "$PT/party" <<'FAKE'
#!/usr/bin/env bash
echo "$#:$*" >> "$FAKE_PT_LOG"
if [ -n "${FAKE_PT_CRASH:-}" ]; then echo "Traceback: boom" >&2; exit 2; fi
case "$1" in
  on) echo "🎉 Party on · Kitchen + Shelf"; echo "♪ 128 BPM (Deezer) · Party palette" ;;
  off) if [ -n "${FAKE_PT_FAIL:-}" ]; then echo "⚠️ Party off: nothing restored yet"; exit 1; fi; echo "💡 Party off · Kitchen + Shelf restored" ;;
  status) echo "No party running." ;;
esac
FAKE
chmod +x "$PT/party"
pt() { TG_CMD_BIN="$PT" FAKE_PT_LOG="$PT/log" "$TGCMD" party "$@"; }
out=$(pt); rc=$?
eq "party: rc 0" "$rc" "0"
eq "party: bare = party on, its text" "$out" "🎉 Party on · Kitchen + Shelf
♪ 128 BPM (Deezer) · Party palette"
eq "party: runs party on" "$(cat "$PT/log")" "1:on"
: > "$PT/log"; out=$(pt " Off "); eq "party off: restored text" "$out" "💡 Party off · Kitchen + Shelf restored"
eq "party off: runs party off" "$(cat "$PT/log")" "1:off"
: > "$PT/log"; pt stop >/dev/null; pt status >/dev/null; eq "party: stop = off, status" "$(cat "$PT/log" | tr '\n' ' ')" "1:off 1:status "
: > "$PT/log"; out=$(pt dance); has "party: other words = hint" "$out" "/party off restores the lights"
eq "party: hint runs nothing" "$(cat "$PT/log" 2>/dev/null)" ""
out=$(FAKE_PT_FAIL=1 pt off); rc=$?
eq "party: party's exit 1 keeps its own text, rc 0" "$rc|$out" "0|⚠️ Party off: nothing restored yet"
out=$(FAKE_PT_CRASH=1 pt); rc=$?
eq "party crashing: rc 3" "$rc" "3"
has "party crashing: fallback hint" "$out" "party lights on (or off)"
has "--list shows /party" "$("$TGCMD" --list)" "/party      Party lights: WLEDs follow the music (/party off restores)"
find "$PT" -mindepth 1 -delete; rmdir "$PT"

echo "# /home panel (keyboard): home-panel's JSON under TELEGRAM_INSTANT_KEYBOARD=1, the plain card otherwise"
KP=$(mktemp -d)
printf '#!/bin/sh\necho "{\\"text\\": \\"panel $*\\"}"\n' > "$KP/home-panel"; chmod +x "$KP/home-panel"
has "home + TELEGRAM_INSTANT_KEYBOARD=1 → still the plain card (panel retired)" "$(TELEGRAM_INSTANT_KEYBOARD=1 TG_CMD_HOME_PANEL="$KP/home-panel" TG_CMD_BIN="$T" TG_CMD_NOW="2031-03-05 12:00" FAKE_HA_LOG="$T/calls" FAKE_HA_JSON="$FIX/home-full.json" HA_HEALTH_DIR="$FIX/health-new" "$TGCMD" home)" "🏠 Home · 12:00"
has "home without the env → the plain card" "$(TG_CMD_BIN="$T" TG_CMD_NOW="2031-03-05 12:00" FAKE_HA_LOG="$T/calls" FAKE_HA_JSON="$FIX/home-full.json" HA_HEALTH_DIR="$FIX/health-new" TG_CMD_HOME_PANEL="$KP/home-panel" "$TGCMD" home)" "🏠 Home · 12:00"
has "home + env but no home-panel → the plain card" "$(TELEGRAM_INSTANT_KEYBOARD=1 TG_CMD_HOME_PANEL="$KP/missing" TG_CMD_BIN="$T" TG_CMD_NOW="2031-03-05 12:00" FAKE_HA_LOG="$T/calls" FAKE_HA_JSON="$FIX/home-full.json" HA_HEALTH_DIR="$FIX/health-new" "$TGCMD" home)" "🏠 Home · 12:00"
find "$KP" -mindepth 1 -delete; rmdir "$KP"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
