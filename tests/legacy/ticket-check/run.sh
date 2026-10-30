#!/bin/bash
# Offline tests for ~/bin/ticket-check: fake vault (fixtures/<word>.json by query word), fake pack, fictional
# calendar/trips/config fixtures; a temp inbox and state. Exit 0 = all pass. TICKET_CHECK=path tests another copy.
BIN=${TICKET_CHECK:-/home/node/bin/ticket-check}
D=$(dirname "$(readlink -f "$0")")
T=$(mktemp -d); trap 'rm -rf "${T:?}"' EXIT
export TICKET_CHECK_CALCACHE="$D/fixtures/events.json" TICKET_CHECK_TRIPS="$D/fixtures/trips.json" \
  TICKET_CHECK_CONFIG="$D/fixtures/config.json" TICKET_CHECK_STATE="$T/state/state.json" TICKET_CHECK_INBOX="$T/inbox" \
  TICKET_CHECK_VAULT="$D/fake/vault" TICKET_CHECK_PACK="$D/fake/pack" TICKET_CHECK_HOME_TOWNS="Hometown,Home Town" \
  FAKE_VAULT_DIR="$D/fixtures" FAKE_VAULT_LOG="$T/vault.log"
pass=0; fail=0
eq() { if [ "$2" = "$3" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $1: got [$2] want [$3]"; fi; }
has() { case "$2" in *"$3"*) pass=$((pass+1));; *) fail=$((fail+1)); echo "FAIL $1: [$2] lacks [$3]";; esac; }
hasnt() { case "$2" in *"$3"*) fail=$((fail+1)); echo "FAIL $1: [$2] has [$3]";; *) pass=$((pass+1));; esac; }
notes() { ls "$T/inbox" 2>/dev/null | sort | tr '\n' ' '; }
note() { cat "$T/inbox/$1"; }

# 20.10 evening: the dance class (out of town, no shop mail) is searched but gets no note
out=$("$BIN" run --now 2031-10-20T18:30); rc=$?
eq "20.10 rc" "$rc" 0; eq "20.10 no note" "$(notes)" ""
has "class searched" "$(cat "$T/vault.log")" "Pottery"
has "state keeps the no-note check" "$("$BIN" status)" "pottery@2031-10-21@L1  -> no note"

# 21.10 evening: Bandx tomorrow -> one note: ticket mail from the shop with PDF, Spotify dropped, no subject
: > "$T/vault.log"
"$BIN" run --now 2031-10-21T18:30 >/dev/null
eq "21.10 one note" "$(notes)" "ticket-check-2031-10-22-bandx.md "
n=$(note ticket-check-2031-10-22-bandx.md)
has "bandx head" "$n" "🎫 Bandx · tomorrow 19:00 (Bigcity)"
has "bandx ticket line" "$n" "🎟 Ticket: mail from ticketmaster.ch (12.04, PDF) + 1 more"
hasnt "no spotify" "$n" "spotify"; hasnt "never a subject" "$n" "SECRET SUBJECT"
has "relay instruction" "$n" "ONE short Telegram message"
hasnt "no ID line for a plain show" "$n" "🪪"
eq "note 0600" "$(stat -c %a "$T/inbox/ticket-check-2031-10-22-bandx.md")" 600
has "structured search only" "$(cat "$T/vault.log")" "--no-text --json"
hasnt "never --text" "$(cat "$T/vault.log")" "--text "
"$BIN" run --now 2031-10-21T19:30 >/dev/null
eq "second run: no second note" "$(notes)" "ticket-check-2031-10-22-bandx.md "

# 23.10: the wedding tomorrow is never checked (private gathering); 24.10: a person without a town, neither
: > "$T/vault.log"
"$BIN" run --now 2031-10-23T18:30 >/dev/null; "$BIN" run --now 2031-10-24T18:30 >/dev/null; "$BIN" run --now 2031-10-25T18:30 >/dev/null
eq "wedding/person/home-town: no notes" "$(notes)" "ticket-check-2031-10-22-bandx.md "
eq "wedding/person/home-town: no search" "$(cat "$T/vault.log")" ""

# 29.10: the rave (id_check, misspelled title) -> the leg's name is searched; promos are not tickets; photo ID; pack
"$BIN" run --now 2031-10-29T18:30 >/dev/null
n=$(note ticket-check-2031-10-30-dj-phoo.md)
has "rave leg name searched" "$(cat "$T/vault.log")" '"DJ Foo"'
has "rave no ticket" "$n" "🎟 Ticket: no ticket mail found (2 other mails mention it) — in an app or wallet?"
hasnt "openair is not an airline" "$n" "cityopenair"
has "rave photo ID" "$n" "🪪 Photo ID (age check at the door)"
has "rave pack (ticket item dropped)" "$n" "🎒 Earplugs · Powerbank + cable · Card + some cash"

# 04.11: the business trip abroad (Schengen): ID card or passport, health card, business pack; no early check
"$BIN" run --now 2031-10-29T18:31 >/dev/null
eq "no early note for Schengen" "$(ls "$T/inbox" | grep -c acme)" 0
"$BIN" run --now 2031-11-04T18:30 >/dev/null
n=$(note ticket-check-2031-11-05-acme-widgets-visit-lowtown.md)
has "acme ID" "$n" "🪪 ID card or passport"; hasnt "acme no passport" "$n" "🛂"
has "acme health card" "$n" "💳 Health insurance card"
has "acme pack" "$n" "🎒 Shirt · Charger"
# 05.11: its flight home: booking mail found
"$BIN" run --now 2031-11-05T18:30 >/dev/null
n=$(note ticket-check-2031-11-06-zz-202-lowtown-southcity.md)
has "zz202 found" "$n" "🎟 Ticket: mail from example-airways.com (25.10, PDF)"
has "zz202 head" "$n" "✈️ ✈️ ZZ 202 Lowtown → Southcity · tomorrow 20:15"

# 13.11: the long trip outside Schengen -> an early check 7 days ahead (passport validity + entry paper)
"$BIN" run --now 2031-11-13T18:30 >/dev/null
n=$(note ticket-check-2031-11-20-xy-101-qq-9-to-islandia-early.md)
has "early head" "$n" "(in 7 days)"
has "passport validity" "$n" "🛂 Passport: valid ≥ 6 months after the return (06.12.2031)"
has "entry paper" "$n" "📝 Islandia travel authorisation approved"
has "dive papers" "$n" "🤿 Dive cert card + dive insurance card"
has "flight no booking mail" "$n" "⚠️ Ticket: no booking or boarding-pass mail found (1 other mail mention it) — airline app, or booked by someone else?"
hasnt "early: no pack yet" "$n" "🎒"
has "flight numbers searched" "$(tail -1 "$T/vault.log")" "XY101"
has "trip pack path" "$n" "offline pack ~/reports/trips/islandia-pack.html"
# 19.11: the day before -> a second note; the all-day trip event is folded into the flight (no own note)
"$BIN" run --now 2031-11-19T18:30 >/dev/null
eq "flight day-before note" "$(ls "$T/inbox" | grep -c 'islandia')" 2
eq "all-day trip folded" "$(ls "$T/inbox" | grep -c '2031-11-20-islandia')" 0
has "day-before dive pack (passport item dropped)" "$(note ticket-check-2031-11-20-xy-101-qq-9-to-islandia.md)" "🎒 Mask + computer · Reef-safe sunscreen · Plug adapter"

# vault down -> the note still comes, with a failed-check line; an out-of-town event without a shop mail stays silent
rm -f "$TICKET_CHECK_STATE"
out=$(FAKE_VAULT_FAIL=1 "$BIN" run --now 2031-10-29T18:30 --dry-run); rc=$?
eq "dry-run rc" "$rc" 0
has "vault failure line" "$out" "🎟 Ticket: mail check failed (vault: unreachable) — check the app"
eq "dry-run writes no state" "$(ls "$T/state" 2>/dev/null | grep -c state.json)" 0
out=$(FAKE_VAULT_FAIL=1 "$BIN" run --now 2031-10-21T18:30 --dry-run)
eq "vault down + plain event: silent" "$out" ""

# config "vendors": an airline the built-in list doesn't know counts as a shop
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["vendors"]=["flyislandia"]; json.dump(d, open(sys.argv[2], "w"))' "$D/fixtures/config.json" "$T/cfg.json"
out=$(TICKET_CHECK_CONFIG="$T/cfg.json" "$BIN" show 2031-11-20 --now 2031-11-19T18:30)
has "config vendor counts" "$out" "🎟 Ticket: mail from flyislandia.example (01.09, PDF)"

# show: a preview for any date, no note, no state
out=$("$BIN" show 2031-10-30 --now 2031-10-20T09:00)
has "show rave" "$out" "🎫 Dj Phoo · in 10 days 20:00 (Farcity)"
out=$("$BIN" show tomorrow --now 2031-10-25T12:00)
eq "show nothing" "$out" "nothing to check on 26.10"
out=$("$BIN" show nonsense --now 2031-10-25T12:00 2>&1); rc=$?
eq "show bad date rc" "$rc" 2
# no calendar cache -> exit 3
TICKET_CHECK_CALCACHE="$T/missing.json" "$BIN" run >/dev/null 2>&1; eq "no cache rc" "$?" 3
out=$("$BIN" --help); has "help" "$out" "day-before ticket"

echo "ticket-check tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
