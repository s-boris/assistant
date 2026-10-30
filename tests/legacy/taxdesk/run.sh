#!/usr/bin/env bash
# Tests for ~/bin/taxdesk. Exit 0 = all pass. Offline: a fake paperless (fixture_docs.json, fake documents only),
# a fixture deadlines data file + profile, temp state and a temp reminders file; the real state is checked untouched.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$HOME/opt/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
X=${TAXDESK_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/taxdesk}   # TAXDESK_BIN: test a copy
HERE=$(cd "$(dirname "$0")" && pwd)
R=$HOME/bin/remind
T=$(mktemp -d "${TMPDIR:-/tmp}/taxdesk-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
export TAXDESK_DEADLINES=$HOME/bin/deadlines TAXDESK_PAPERLESS=$HERE/fake_paperless.py FAKE_PAPERLESS_LOG=$T/pl.log
export TAXDESK_STATE=$T/state DEADLINES_DATA=$HERE/data.json DEADLINES_PROFILE=$T/dlprof.json
export TAXDESK_REMIND_DB=$T/rem.json REMIND_DB=$T/rem2.json REMIND_INBOX=$T/inbox REMIND_TG=$T/tg TAXDESK_TODAY=2026-10-18
unset FAKE_PAPERLESS_FAIL
cp "$HERE/dl_profile.json" "$T/dlprof.json"
printf '#!/bin/sh\nexit 0\n' > "$T/tg"; chmod +x "$T/tg"
REAL=/home/node/.local/state/taxdesk/3a.json
fp() { [ -f "$1" ] && md5sum < "$1" || echo absent; }
real_before=$(fp "$REAL")
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
# has/hasnt test the substring in the shell: `printf | grep -q` under pipefail reads as no match when grep exits
# on the match before printf (one write per line) is done, SIGPIPE (a false FAIL, or a false pass for hasnt)
has() { if [[ $2 == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ $2 == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
jget() { python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(eval(sys.argv[1]))' "$1"; }

# --- 3a card, nothing logged
out=$("$X" 3a </dev/null 2>&1); rc=$?
eq "3a: exit 0" "$rc" 0
has "3a: title" "$out" "Pillar 3a 2026"
has "3a: max from the data, cited" "$out" "Max: CHF 7’258 (employee with pension fund; BSV)"
has "3a: next year's max from the fallback table" "$out" "2027: CHF 7’373"
has "3a: unknown paid -> tell me" "$out" "Paid: unknown — tell me what you paid and I log it"
has "3a: last banking day 30.12" "$out" "Credited by: Wed 30.12"
has "3a: safe date ~20.12 (weekday before)" "$out" "Pay by ~20.12 to be safe (Fri 18.12) · 73 days left"
has "3a: paperless 2 docs this year, latest + amount" "$out" "Paperless 2026: 2 docs, latest 01.08 · #3 Säule 3a Kontoauszug, CHF 3000.00"
has "3a: older 3a doc of the year listed" "$out" "#5 10.01"
hasnt "3a: second pillar doc vetoed" "$out" "#4"
hasnt "3a: last year's doc not counted" "$out" "#9"
has "3a: log hint" "$out" "→ log: taxdesk 3a log AMOUNT"
[ -e "$T/state/3a.json" ] && no "3a: card writes no state" || ok "3a: card writes no state"

# --- log / undo
out=$("$X" 3a log "1'000" --date 2026-10-10 </dev/null 2>&1); rc=$?
eq "log: exit 0" "$rc" 0
has "log: confirms" "$out" "Logged CHF 1’000 for 2026 (10.10.2026)."
has "log: card shows paid + left" "$out" "Paid: CHF 1’000 (1×, last 10.10) · left CHF 6’258"
eq "log: state file is 0600" "$(stat -c %a "$T/state/3a.json")" 600
eq "log: state dir is 0700" "$(stat -c %a "$T/state")" 700
out=$("$X" 3a log "CHF 6,258" </dev/null 2>&1)
has "log: max reached" "$out" "Paid: CHF 7’258 (2×, last 18.10) · max reached ✓"
hasnt "log: no log hint at the max" "$out" "→ log:"
out=$("$X" 3a log 500 </dev/null 2>&1)
has "log: over the max flagged" "$out" "CHF 500 OVER the max"
out=$("$X" 3a undo </dev/null 2>&1)
has "undo: removes the last" "$out" "Removed CHF 500 (2026-10-18) from 2026."
js=$("$X" 3a --json </dev/null)
eq "json: paid" "$(printf '%s' "$js" | jget 'd["paid_chf"]')" 7258.0
eq "json: max" "$(printf '%s' "$js" | jget 'd["max_chf"]')" 7258.0
eq "json: credit_by" "$(printf '%s' "$js" | jget 'd["credit_by"]')" 2026-12-30
eq "json: paperless count" "$(printf '%s' "$js" | jget 'd["paperless"]["count"]')" 2
"$X" 3a log abc </dev/null >/dev/null 2>&1; eq "log: bad amount -> exit 2" "$?" 2
"$X" 3a log </dev/null >/dev/null 2>&1; rc=$?; [ "$rc" -ne 0 ] && ok "log: missing amount fails" || no "log: missing amount fails" "$rc"
"$X" 3a log 100 --date 2026-13-01 </dev/null >/dev/null 2>&1; eq "log: bad date -> exit 2" "$?" 2
out=$("$X" 3a --year 2025 --offline </dev/null 2>&1)
has "3a 2025: fallback max (not in data)" "$out" "Max: CHF 7’258"
has "3a 2025: deadline passed" "$out" "Deadline passed (30.12.2025)"
n0=$(grep -c '' "$T/pl.log"); "$X" 3a --offline </dev/null >/dev/null; eq "offline: paperless untouched" "$(grep -c '' "$T/pl.log")" "$n0"

# --- return card
out=$("$X" return 2026 </dev/null 2>&1); rc=$?
eq "return: exit 0" "$rc" 0
has "return: canton from the profile" "$out" "ZZ tax return 2026"
has "return: deadline (partly verified)" "$out" "Due: Wed 31.03.2027 (standard date; your form shows the exact one)"
has "return: hint from the data" "$out" "File it, or extend online before the deadline."
has "return: Lohnausweis present by title year" "$out" "✓ Lohnausweis 2026 · #1"
hasnt "return: payslip never counts" "$out" "#2"
has "return: 3a mid-year statement is unsure" "$out" "? 3a certificate 2026 · #3 (check)"
has "return: bank statements missing" "$out" "✗ Bank/broker year-end statements 2026"
hasnt "return: not-applicable item hidden" "$out" "Pension fund buy-in"
has "return: unknown flag = if applicable" "$out" "Crypto balances 31.12.2026 (if applicable)"
has "return: kk unsure" "$out" "? Health insurance premium certificate 2026 · #6 #8 (check)"
has "return: donation present" "$out" "✓ Donation receipts 2026 · #7"
has "return: paper-only item" "$out" "? Tax return form 2026 + eFiling code (by mail, not in paperless)"
has "return: summary + arrival note" "$out" "2 ✓ · 3 ? · 2 ✗ — most arrive Jan–Feb 2027"
out=$("$X" return 2025 </dev/null 2>&1)
has "return 2025: deadline passed" "$out" "Due: Tue 31.03.2026 (passed)"
has "return 2025: 3a certificate by title year" "$out" "✓ 3a certificate 2025 · #5 #9"
eq "return: default year in Oct = this year" "$("$X" return --json </dev/null | jget 'd["tax_year"]')" 2026
eq "return: default year in Feb = last year" "$(TAXDESK_TODAY=2027-02-10 "$X" return --json </dev/null | jget 'd["tax_year"]')" 2026
eq "return: default year in May = this year" "$(TAXDESK_TODAY=2027-05-10 "$X" return --json --offline </dev/null | jget 'd["tax_year"]')" 2027
python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["tax_extension_until"]="2026-11-30"; json.dump(d,open(p,"w"))' "$T/dlprof.json"
eq "return: open extension keeps last year" "$("$X" return --json --offline </dev/null | jget 'd["tax_year"]')" 2025
has "return: extended deadline shown" "$("$X" return --offline </dev/null)" "Due: extended to Mon 30.11.2026"
cp "$HERE/dl_profile.json" "$T/dlprof.json"
out=$(FAKE_PAPERLESS_FAIL=1 "$X" return 2026 </dev/null 2>&1); rc=$?
eq "paperless down: return still exit 0" "$rc" 0
has "paperless down: marked" "$out" "! Lohnausweis 2026 (paperless not reachable)"
out=$(FAKE_PAPERLESS_FAIL=1 "$X" 3a </dev/null 2>&1); rc=$?
eq "paperless down: 3a exit 0" "$rc" 0
has "paperless down: 3a says so" "$out" "Paperless: not reachable"

# --- read-only paperless: only search/get with --json
bad=$(grep -cvE '^(search .* -n [0-9]+ --json|get [0-9]+ --json)$' "$T/pl.log")
eq "paperless: only search/get --json (no fields/content/writes)" "$bad" 0

# --- reminders: proposal only
printf '%s' '[{"at":"2026-12-01 09:00","owner":true,"tag":"deadline:pillar3a:2026-12-31:30d","text":"x"},{"at":"2026-12-15 09:00","owner":true,"tag":"taxdesk:3a:2026-12-15","text":"y"},{"at":"2026-12-02 09:00","owner":false,"tag":"other","text":"z"}]' > "$T/rem.json"
before=$(fp "$T/rem.json")
out=$("$X" reminders </dev/null 2>&1); rc=$?
eq "reminders: exit 0" "$rc" 0
has "reminders: overlap flagged" "$out" "# overlaps deadline:pillar3a:2026-12-31:30d at 2026-12-01 09:00"
has "reminders: already added skipped" "$out" "# already added: taxdesk:3a:2026-12-15"
eq "reminders: 3 add lines" "$(printf '%s\n' "$out" | grep -c '^remind add --owner --tag taxdesk:')" 3
has "reminders: 3a 01.12 09:00" "$out" "--tag taxdesk:3a:2026-12-01 '2026-12-01 09:00'"
has "reminders: checklist 15.02" "$out" "--tag taxdesk:return:2027-02-15 '2027-02-15 18:00'"
has "reminders: checklist 15.03 names the due date" "$out" "Tax return 2026 is due 31.03.2027"
eq "reminders: reminders file untouched" "$(fp "$T/rem.json")" "$before"
[ -e "$T/rem2.json" ] && no "reminders: nothing added" || ok "reminders: nothing added"
line=$(printf '%s\n' "$out" | grep '^remind add' | head -1)
eval "$R ${line#remind }" >/dev/null 2>&1; rc=$?
eq "reminders: a proposed line runs with remind (temp db)" "$rc" 0
has "reminders: it landed in the temp db" "$(cat "$T/rem2.json")" "taxdesk:3a:2026-12-01"
out=$(TAXDESK_TODAY=2026-12-20 "$X" reminders </dev/null 2>&1)
has "reminders: after 15.12 -> next year" "$out" "taxdesk:3a:2027-12-01"

eq "real taxdesk state untouched" "$(fp "$REAL")" "$real_before"
echo "pass=$pass fail=$fail"
[ "$fail" = 0 ]
