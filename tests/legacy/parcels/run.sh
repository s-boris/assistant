#!/usr/bin/env bash
# Offline tests for ~/bin/parcels (PARCELS_BIN=/copy to test a copy): fake shop pages (made-up items, fictional towns),
# fake vault / remind / shop / profile-get on PATH, a fixed clock. Exit 0 = all pass.
set -uo pipefail
BIN=${PARCELS_BIN:-/home/node/bin/parcels}
T=$(mktemp -d); trap 'rm -rf "${T:?}"' EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
mkdir -p "$T/bin" "$T/state" "$T/inbox"
export PARCELS_STATE="$T/state" PARCELS_CONFIG="$T/config.json" PARCELS_FETCH="$T/bin/fetch" PARCELS_INBOX="$T/inbox"
export PARCELS_NOW="2026-03-10T10:00:00" USAGE=off PATH="$T/bin:$PATH" LOG="$T/log"
cat > "$T/config.json" <<'J'
{"station": {"town": "Stationdorf", "hold_days": 14}, "home_town": "Hometown am See",
 "mail": {"queries": ["post.example"], "station_queries": ["stationmail"], "max_new": 4, "budget_s": 60}}
J
# --- fake shop pages
cat > "$T/amazon.json" <<'J'
{"ok": true, "status": 200, "login": false, "challenge": false, "text": "", "cards": [
 {"text": "ORDER PLACED 1 March 2026 TOTAL €9.99 DISPATCH TO Test Person Teststr 1 79999 Stationdorf Germany ORDER # 111-2222222-3333333 View order details Delivered 3 March Your package was left in a secure location.", "titles": ["ACME Pack of 10 5 x 5 cm Blue Widget Squares, for crafts", "BRAND - Strong Glue, 123"]},
 {"text": "ORDER PLACED 8 March 2026 TOTAL €5.00 DISPATCH TO Test Person 9999 Hometown am See Switzerland ORDER # 111-4444444-5555555 Arriving tomorrow", "titles": ["Plain Test Mug"]},
 {"text": "ORDER PLACED 2 March 2026 ORDER # 111-6666666-7777777 Cancelled", "titles": ["Gone Thing"]},
 {"text": "ORDER PLACED 1 January 2026 ORDER # 111-8888888-9999999 Delivered 3 January", "titles": ["Old Thing"]}]}
J
cat > "$T/galaxus.json" <<'J'
{"ok": true, "status": 200, "login": false, "challenge": false, "text": "Alle Bestellungen\nBestellung 12345678 vom 7.3.2026\nStatus Nicht bezahlt\n2×\nTest Plant Pot\nLieferung zwischen Fr, 13.3. und Mo, 16.3.\n1×\nTest Fan\nVersandt\nBestellung 12345000 vom 1.3.2026\nStatus Bezahlt\n1×\nOld Lamp\nZugestellt am 9.3.2026\nBestellung 12340000 vom 1.2.2026\n1×\nVery Old\nZugestellt am 3.2.2026\n"}
J
echo '{"ok": true, "status": 200, "login": true, "challenge": false, "text": ""}' > "$T/brack.json"
cat > "$T/bin/fetch" <<'S'
#!/bin/sh
case "$1" in *amazon*) f=amazon;; *galaxus*) f=galaxus;; *brack*) f=brack;; *) exit 2;; esac
[ -n "${FAKE_FETCH_FAIL:-}" ] && [ "$f" = "$FAKE_FETCH_FAIL" ] && { echo '{"ok": false, "error": "browser gone"}'; exit 4; }
tr -d '\n' < "$(dirname "$0")/../$f.json"; echo
S
# fake vault: search hits (sender/date/category), quick cards (action), `when` answers. FAKE_MAIL=pickup: pickup
# notices (a shop's pick_up notice at a made-up town, a carrier's at a misspelled home town, a shop's other mails).
cat > "$T/bin/vault" <<'S'
#!/bin/sh
echo "vault $*" >> "$LOG"
q=$(eval echo "\${$#}")
case "$1 $2" in
 "gmail search")
   if [ "${FAKE_MAIL:-}" = pickup ]; then case "$q" in
     post.example) echo '{"results": [{"ref": "main:30", "date": "2026-03-08T09:00:00+01:00", "from": "Post Example (post.ch)", "category": "delivery"}]}';;
     from:notice.aliexpress.com) echo '{"results": [{"ref": "main:20", "date": "2026-03-09T11:00:00+01:00", "from": "AliExpress (notice.aliexpress.com)", "category": "delivery"}, {"ref": "main:21", "date": "2026-03-09T10:00:00+01:00", "from": "AliExpress (notice.aliexpress.com)", "category": "delivery"}, {"ref": "main:22", "date": "2026-03-08T10:00:00+01:00", "from": "AliExpress (notice.aliexpress.com)", "category": "newsletter"}]}';;
     *) echo '{"results": []}';; esac
   elif [ "$q" = "post.example" ]; then echo '{"results": [{"ref": "main:1", "date": "2026-03-10T07:00:00+01:00", "from": "Post Example (post.ch)", "category": "delivery"}, {"ref": "main:2", "date": "2026-03-09T07:00:00+01:00", "from": "Shop News (post.ch)", "category": "newsletter"}, {"ref": "main:3", "date": "2026-02-01T07:00:00+01:00", "from": "Post (post.ch)", "category": "delivery"}]}'
   elif [ "$q" = "stationmail" ]; then echo '{"results": [{"ref": "main:9", "date": "2026-03-04T07:00:00+01:00", "from": "Station Mail", "category": "?"}]}'
   else echo '{"results": []}'; fi;;
 "gmail get") case "$q" in
   main:20|main:30) echo '{"kind": "mail_card", "ref": "'"$q"'", "category": "delivery", "actionable": true, "action": "pick_up", "wants": "pickup"}';;
   *) echo '{"kind": "mail_card", "ref": "'"$q"'", "category": "delivery", "actionable": false, "action": "none"}';; esac;;
 "ask gmail") case "$3" in main:1) echo '{"answer": {"date": "2026-03-10", "venue": "Somestreet 1 9999 Hometown", "status": "planned"}}';;
   main:20) echo '{"answer": {"date": "2026-03-09", "venue": "Test Kiosk", "address": "Kioskweg 2, 8888 Pickdorf am Testsee Testkanton", "status": "confirmed"}}';;
   main:30) echo '{"answer": {"date": "2026-03-08", "venue": "Test Shop", "address": "Shopweg 5, 9999 Hometwn am See", "status": "confirmed"}}';;
                          *) echo '{"answer": {"date": "2026-03-04", "venue": "", "status": "planned"}}';; esac;;
esac
S
cat > "$T/bin/remind" <<'S'
#!/bin/sh
echo "remind $*" >> "$LOG"
[ "$1" = list ] && cat "$REMIND_LIST" 2>/dev/null
exit 0
S
cat > "$T/bin/shop" <<'S'
#!/bin/sh
echo "shop $*" >> "$LOG"
echo '{"town": "Stationdorf", "stops": [{"store": "Testmarkt", "items": [{"id": "a"}, {"id": "b"}]}]}'
S
printf '#!/bin/sh\necho "Hometown am See"\n' > "$T/bin/profile-get"
chmod +x "$T/bin/"*
export REMIND_LIST="$T/remind.txt"; : > "$REMIND_LIST"

echo "# refresh"
out=$("$BIN" refresh); rc=$?
eq "refresh rc 0" "$rc" "0"
has "station pickup with deadline (delivered 3.3 + 14 d)" "$out" "🏪 Stationdorf station: 1 to pick up by Tue 17.3 (Amazon: Blue Widget Squares, Strong Glue)"
has "amazon arriving tomorrow" "$out" "🚚 Amazon: tomorrow · Plain Test Mug"
has "galaxus window, least advanced item" "$out" "🧾 Galaxus: Fri 13.3–Mon 16.3 · Test Plant Pot, Test Fan"
has "recent galaxus delivery" "$out" "✅ Delivered: Galaxus yesterday"
has "carrier notice from vault flags" "$out" "📬 Carrier notices: Post today"
has "brack sign-in → not checked" "$out" "⚠️ Not checked: Brack (sign-in needed)"
hasnt "cancelled order hidden" "$out" "Gone Thing"
hasnt "old orders hidden" "$out" "Old Thing"
hasnt "no order numbers" "$(cat "$T/state/cache.json")" "111-2222222"
hasnt "no addresses" "$(cat "$T/state/cache.json")" "Teststr"
hasnt "no venue stored" "$(cat "$T/state/cache.json")" "Somestreet"
eq "cache is 0600" "$(stat -c %a "$T/state/cache.json")" "600"
has "reminder added 2 days before at 17:30 with the store run" "$(grep 'remind add' "$LOG")" 'remind add --owner --tag parcel:pickup 2026-03-15 17:30 📦 1 parcel waiting at the Stationdorf parcel station (Amazon): pickup by Tue 17.3. Same drive: 2 items from your list at Testmarkt (/shop).'
eq "only delivery mails asked (1 carrier + 1 station; newsletter/old skipped)" "$(grep -c 'vault ask' "$LOG")" "2"
eq "card checked for the carrier mail only (station mails are pickups already)" "$(grep -c 'vault gmail get' "$LOG")" "1"
has "the shop pickup query runs by default" "$(cat "$LOG")" "from:notice.aliexpress.com"
hasnt "station mail is the same parcel as the shop card" "$out" "station (Amazon/station"
: > "$LOG"
"$BIN" refresh >/dev/null
eq "second refresh: no vault asks (cached per ref)" "$(grep -c 'vault ask' "$LOG")" "0"
eq "second refresh: no card gets" "$(grep -c 'vault gmail get' "$LOG")" "0"

echo "# dedupe with an existing reminder"
echo ' 3. 2026-03-15 17:30  📦 Amazon package waiting at the Stationdorf parcel station: pickup by Tue 17.03.  (tag parcel) [id x1]' > "$REMIND_LIST"
: > "$LOG"
eq "kept when a 📦 reminder names the deadline" "$("$BIN" sync-reminder)" "kept (17.3)"
eq "no remind add" "$(grep -c 'remind add' "$LOG")" "0"

echo "# line / speak / board"
eq "shop line" "$("$BIN" line)" "📦 1 to pick up at the Stationdorf station by Tue 17.3 · 3 on the way (1 today) → /parcels"
eq "speak" "$("$BIN" speak)" "One parcel waits at the Stationdorf station, pick it up by Tuesday 17.3. Amazon: one item, arriving tomorrow. Galaxus: 3 items, arriving between Friday and Monday. The Post: a parcel arrives today."
eq "board" "$("$BIN" board)" '{"title": "📦 Pick up 1 parcel in Stationdorf by Tue 17.3", "pickup_by": "2026-03-17", "n": 1}'

echo "# picked"
: > "$LOG"
out=$("$BIN" picked); eq "picked message" "$out" "✅ Marked 1 parcel as picked up; pickup reminder cancelled."
has "cancels our tag" "$(cat "$LOG")" "remind cancel --tag parcel:pickup"
has "cancels legacy station reminders" "$(cat "$LOG")" "remind cancel --match parcel station"
hasnt "card: no pickup after picked" "$("$BIN")" "🏪"
eq "board empty" "$("$BIN" board)" "{}"
"$BIN" refresh >/dev/null
hasnt "picked stays hidden after a refresh" "$("$BIN")" "🏪"

echo "# pickup notices (shop pick_up notice, carrier pick_up card)"
mkdir -p "$T/state2"; : > "$LOG"; : > "$REMIND_LIST"
out=$(FAKE_MAIL=pickup PARCELS_STATE="$T/state2" "$BIN" refresh --only mail); rc=$?
eq "pickup refresh rc 0" "$rc" "0"
has "shop pickup notice → its pickup point's town, ready since" "$out" "🏪 Pickdorf am Testsee pickup point: 1 to pick up, ready since yesterday (AliExpress)"
has "carrier pick_up card → pickup; misspelled home town → its configured name" "$out" "🏪 Hometown am See pickup point: 1 to pick up, ready since Sun 8.3 (Post)"
hasnt "pickup notice is not a carrier ETA" "$out" "📬"
eq "pickup question asked for both notices" "$(grep -c 'vault ask gmail main:[23]0 Where and from when can this parcel be picked up' "$LOG")" "2"
eq "shop's other delivery mail: card only, no ask" "$(grep -c 'vault ask gmail main:21' "$LOG")/$(grep -c 'vault gmail get --json main:21' "$LOG")" "0/1"
eq "shop newsletter: no card, no ask" "$(grep -c 'main:22' "$LOG")" "0"
c2=$(cat "$T/state2/cache.json")
hasnt "no pickup address stored" "$c2" "Kioskweg"
hasnt "no postcode stored" "$c2" "8888"
hasnt "no pickup venue stored" "$c2" "Test Kiosk"
eq "no reminder for pickup points (no known deadline)" "$(grep -c 'remind add' "$LOG")" "0"
eq "pickup line" "$(PARCELS_STATE="$T/state2" "$BIN" line)" "📦 1 to pick up at the Hometown am See pickup point · 1 to pick up at the Pickdorf am Testsee pickup point → /parcels"
eq "pickup speak" "$(PARCELS_STATE="$T/state2" "$BIN" speak)" "One parcel waits at the Hometown am See pickup point. One parcel waits at the Pickdorf am Testsee pickup point."
eq "pickup board: two towns → none named" "$(PARCELS_STATE="$T/state2" "$BIN" board)" '{"title": "📦 Pick up 2 parcels", "pickup_by": null, "n": 2}'
: > "$LOG"
FAKE_MAIL=pickup PARCELS_STATE="$T/state2" "$BIN" refresh --only mail >/dev/null
eq "second pickup refresh: nothing asked or fetched again" "$(grep -c 'vault ask\|vault gmail get' "$LOG")" "0"
hasnt "shown for pickup_keep_days only" "$(FAKE_MAIL=pickup PARCELS_STATE="$T/state2" PARCELS_NOW=2026-03-25T10:00:00 "$BIN" refresh --only mail --no-remind)" "🏪"
FAKE_MAIL=pickup PARCELS_STATE="$T/state2" "$BIN" refresh --only mail >/dev/null
eq "picked (as /parcels picked does) clears pickup points" "$(PARCELS_STATE="$T/state2" "$BIN" picked station)" "✅ Marked 2 parcels as picked up; pickup reminder cancelled."
hasnt "card: no pickup point after picked" "$(PARCELS_STATE="$T/state2" "$BIN")" "🏪"

echo "# failures"
rm -f "$T/state/cache.json"
eq "speak without cache → exit 4" "$("$BIN" speak; echo $?)" "4"
out=$(FAKE_FETCH_FAIL=amazon "$BIN" refresh --cron --no-mail); rc=$?
eq "cron refresh quiet" "$out" ""
has "amazon failure shown" "$("$BIN")" "Amazon (browser gone)"
eq "sign-in needed → one inbox note at once" "$(ls "$T/inbox" | grep -c 'parcels-source-brack')" "1"
FAKE_FETCH_FAIL=amazon "$BIN" refresh --cron --no-mail >/dev/null
eq "no second note for the same streak" "$(ls "$T/inbox" | grep -c 'parcels-source-brack')" "1"
echo "{}" > "$T/config.json"; printf '#!/bin/sh\necho "Hometown am See"\n' > "$T/bin/profile-get"
eq "no station configured: card still works" "$("$BIN" line >/dev/null; echo $?)" "0"
eq "usage error" "$("$BIN" bogus 2>/dev/null; echo $?)" "2"

echo "# $pass passed, $fail failed"
[ "$fail" -eq 0 ]
