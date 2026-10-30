#!/usr/bin/env bash
# Tests for ~/bin/remind (one-shot behaviour + recurring --every/skip + lossless `due`; snooze.sh: IDs,
# snooze/done/buttons/press, ~/bin/tg-buttons; place.sh: location reminders --on / place-due; back.sh: the welcome-back offer; app.sh: phone-app actions/act/route; smart.sh: Reminders 2.0). Exit 0 = all pass.
# Everything runs against temp state: REMIND_DB, REMIND_INBOX, a stub REMIND_TG that logs instead of texting,
# and REMIND_NOW to fake the clock. The real reminders.json and fired.log are checked for leaks only (hashes and
# counts, never printed): the live cron may fire real reminders meanwhile, so a change made by it is fine.
set -uo pipefail
R_BIN=${REMIND_BIN:-/home/node/bin/remind}   # REMIND_BIN: test a copy before installing
REAL=/home/node/.local/state/remind/reminders.json
REAL_FIRED=/home/node/.local/state/remind/fired.log
T=$(mktemp -d "${TMPDIR:-/tmp}/remind-tests.XXXXXX")
trap 'chmod -R u+w "$T" 2>/dev/null; rm -rf "$T"' EXIT
export REMIND_DB=$T/rem.json REMIND_INBOX=$T/inbox REMIND_TG=$T/tg
# Fixed clock for every call (add/list/skip/cancel too, not just `due`), set before all the test dates:
# otherwise recurring adds whose start is already past on the real clock jump forward and tests flake by date.
export REMIND_NOW="2026-10-10 12:00"
export TRIPCLOCK_CONFIG=$T/trips.json TRIPCLOCK_FLIGHTS=$T/no-flights   # trip clock: none unless a test writes it
# tg stub: $T/tg-fail = exit 1; $T/tg-crash = kill the calling `remind` (a crash mid-run) before sending.
cat > "$T/tg" <<STUB
#!/bin/sh
[ -e "$T/tg-crash" ] && { rm -f "$T/tg-crash"; kill -9 \$PPID; exit 1; }
[ -e "$T/tg-fail" ] && exit 1
printf '%s\n' "\$*" >> "$T/tg.log"
STUB
# R wraps the binary under test and records every text the tests add (leak markers for the live-store check).
R=$T/remind
cat > "$R" <<WRAP
#!/bin/sh
if [ "\$1" = add ]; then for a in "\$@"; do last=\$a; done; printf '%s\n' "\$last" >> "$T/markers"; fi
exec "$R_BIN" "\$@"
WRAP
chmod +x "$T/tg" "$R"
# Live-store leak check. snap: hashes of the real store's entries + fired.log size/hash, before the run.
# check: unchanged, or changed only the way the live cron changes it (due entries removed or moved to a later
# slot, fired.log lines appended for minutes inside the run); a future entry gone or changed, a test text in the
# store, or a foreign fired.log line = a leak. Entries others add meanwhile (no test text) are tolerated.
cat > "$T/realcheck.py" <<'PY2'
import hashlib, json, os, re, sys
from datetime import datetime, timedelta
mode, real, fired, state, markers = sys.argv[1:6]
sys.path.insert(0, '/home/node/bin/lib')
import assistant_profile
now = datetime.now(assistant_profile.tz()).replace(tzinfo=None, second=0, microsecond=0)
FMT = '%Y-%m-%d %H:%M'
def h(o): return hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
def raw(p):
    try:
        with open(p, 'rb') as f: return f.read()
    except FileNotFoundError: return None
def entries(b):
    try: items = json.loads(b) if b else []
    except ValueError: return None
    return [{'h': h(r), 'k': h({k: v for k, v in r.items() if k != 'at'}), 'at': r.get('at', ''), 't': r.get('text', '')}
            for r in items]
store, log = raw(real), raw(fired) or b''
if mode == 'snap':
    snap = {'md5': store and hashlib.md5(store).hexdigest(), 't0': now.strftime(FMT),
            'items': [{k: e[k] for k in ('h', 'k', 'at')} for e in entries(store) or []],
            'fired': [len(log), hashlib.md5(log).hexdigest()]}
    with open(state, 'w') as f: json.dump(snap, f)
    sys.exit(0)
snap = json.load(open(state))
t0 = datetime.strptime(snap['t0'], FMT)
try: marks = {l.rstrip('\n') for l in open(markers)}
except FileNotFoundError: marks = set()
bad, moved = [], 0
if (store and hashlib.md5(store).hexdigest()) != snap['md5']:
    after = entries(store)
    if after is None: bad.append('store unreadable')
    else:
        removed, added = list(snap['items']), []
        for e in after:
            m = next((r for r in removed if r['h'] == e['h']), None)
            if m: removed.remove(m)
            else: added.append(e)
        if any(r['at'] > now.strftime(FMT) for r in removed): bad.append('a future entry changed or vanished')
        for e in added:
            m = next((r for r in removed if r['k'] == e['k'] and r['at'] < e['at']), None)
            if m: removed.remove(m); moved += 1
            elif e['t'] in marks or 'Test AG' in e['t']: bad.append('a test reminder in the store')
n, m5 = snap['fired']
if len(log) < n or hashlib.md5(log[:n]).hexdigest() != m5: bad.append('fired.log rewritten')
else:
    for l in log[n:].decode(errors='replace').splitlines():
        ok = re.fullmatch(r'(\d{4}-\d\d-\d\d \d\d:\d\d) (owner|me)', l)
        if not ok or not t0 - timedelta(minutes=2) <= datetime.strptime(ok.group(1), FMT) <= now:
            bad.append('fired.log line from outside the run'); break
print('LEAK: ' + '; '.join(bad) if bad else 'ok')
PY2
realcheck() { env -u REMIND_NOW -u ASSISTANT_PROFILE HOME=/home/node python3 "$T/realcheck.py" "$1" "$REAL" "$REAL_FIRED" "$T/real.snap" "$T/markers"; }
realcheck snap
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if grep -qF -- "$3" <<<"$2"; then ok "$1"; else no "$1" "$2"; fi; }
reset() { chmod -R u+w "$T/inbox" "$T/rem.json.tmp" "$T/rem.json.journal" 2>/dev/null
          rm -rf "$T/rem.json" "$T/rem.json.tmp" "$T/rem.json.journal" "$T/inbox" "$T/probe" "$T/tg.log" "$T/tg-fail" \
                 "$T/tg-crash" "$T/fired.log"; }
ats() { python3 -c 'import json,sys; print(" ".join(r["at"] for r in json.load(open(sys.argv[1]))))' "$REMIND_DB"; }
tgn() { [ -f "$T/tg.log" ] && wc -l < "$T/tg.log" | tr -d ' ' || echo 0; }
noid() { cat "$T"/inbox/* | sed -E 's/, ID [0-9a-f]{8}\)/)/'; }   # notes without their ID (snooze.sh tests IDs)
inboxn() { ls "$T/inbox" 2>/dev/null | wc -l | tr -d ' '; }
due() { REMIND_NOW="$1" timeout 20 "$R" due; }

echo "# A. one-shot behaviour (unchanged)"
reset
out=$("$R" list); eq "empty list" "$out" "(none)"
out=$("$R" add --owner --tag bill:7 "2026-10-12 10:00" "Pay rent"); eq "add owner+tag output" "$out" "added owner reminder for Mon 2026-10-12 10:00"
out=$("$R" add "2026-10-11 08:00" "Check backups"); eq "add self output" "$out" "added reminder for Sun 2026-10-11 08:00"
"$R" add --owner "2026-10-20 09:00" "Dentist" >/dev/null
out=$("$R" list)
eq "list sorted + format" "$out" " 1. 2026-10-11 08:00  Check backups
 2. 2026-10-12 10:00  [owner] Pay rent  (tag bill:7)
 3. 2026-10-20 09:00  [owner] Dentist"
"$R" add "bad" "x" >/dev/null 2>&1; eq "bad time rejected (rc 2)" "$?" "2"
due "2026-10-11 07:59"; eq "nothing due yet" "$(tgn)/$(inboxn)" "0/0"
due "2026-10-12 10:00"
eq "due fired both due ones, left the future one" "$(ats)" "2026-10-20 09:00"
eq "owner reminder texted as ⏰ text" "$(cat "$T/tg.log")" "⏰ Pay rent"
has "self reminder became an inbox note" "$(noid)" "Reminder (set for 2026-10-11 08:00): Check backups"
due "2026-10-12 10:00"; eq "never fires twice" "$(tgn)/$(inboxn)" "1/1"
touch "$T/tg-fail"; due "2026-10-20 09:00"
has "tg failure falls back to inbox" "$(noid)" "OWNER reminder (tg failed, send it yourself) (set for 2026-10-20 09:00): Dentist"
reset
"$R" add --owner --tag bill:3 "2026-10-12 09:00" "Bill A soon" >/dev/null
"$R" add --owner --tag bill:3 "2026-10-15 09:00" "Bill A today" >/dev/null
"$R" add --owner --tag bill:4 "2026-10-15 09:00" "Bill B today" >/dev/null
out=$("$R" cancel --tag bill:3); eq "cancel --tag prints cancelled: lines (bills counts them)" "$(printf '%s\n' "$out" | grep -c '^cancelled:')" "2"
out=$("$R" cancel --tag bill:9); eq "cancel --tag no match" "$out" "no matching reminder"
out=$("$R" cancel --match "BILL b"); eq "cancel --match (case-insensitive)" "$out" "cancelled: 2026-10-15 09:00  Bill B today"
"$R" add "2026-10-13 09:00" "One" >/dev/null; "$R" add "2026-10-14 09:00" "Two" >/dev/null
out=$("$R" cancel 2); eq "cancel N" "$out" "cancelled: 2026-10-14 09:00  Two"
"$R" cancel 5 >/dev/null; eq "cancel bad N rc 1" "$?" "1"

echo "# B. recurring"
reset
out=$("$R" add --owner --every daily "2026-10-24 09:00" "Vitamins")
eq "add daily output" "$out" "added owner reminder for Sat 2026-10-24 09:00, every day"
eq "list shows (every day)" "$("$R" list)" " 1. 2026-10-24 09:00  [owner] Vitamins  (every day)"
due "2026-10-24 09:00"; eq "daily: fired, next is 10-25 09:00" "$(tgn) $(ats)" "1 2026-10-25 09:00"
due "2026-10-25 09:00"; eq "daily across DST: next 10-26 09:00 local" "$(tgn) $(ats)" "2 2026-10-26 09:00"
off=$(python3 -c 'from datetime import datetime; from zoneinfo import ZoneInfo as Z
z=Z("Europe/Zurich"); f=lambda s: datetime.strptime(s,"%Y-%m-%d %H:%M").replace(tzinfo=z)
print(f("2026-10-24 09:00").utcoffset(), f("2026-10-26 09:00").utcoffset(), (f("2026-10-25 09:00").timestamp()-f("2026-10-24 09:00").timestamp())/3600)')
eq "  (wall time kept: +2h → +1h, the DST day is 25 h long)" "$off" "2:00:00 1:00:00 25.0"
reset
"$R" add --every monthly:31 "2026-10-31 10:00" "Month end" >/dev/null
due "2026-10-31 10:00"; eq "monthly:31 → Nov 30" "$(ats)" "2026-11-30 10:00"
due "2026-11-30 10:00"; eq "monthly:31 → then Dec 31" "$(ats)" "2026-12-31 10:00"
reset
"$R" add --every monthly "2027-01-31 10:00" "Anchor" >/dev/null
due "2027-01-31 10:00"; due "2027-02-28 10:00"; eq "plain monthly from Jan 31 keeps the 31st (Feb 28 → Mar 31)" "$(ats)" "2027-03-31 10:00"
reset
"$R" add --every monthly:last "2027-01-15 10:00" "Last day" >/dev/null
eq "monthly:last snaps to Jan 31" "$(ats)" "2027-01-31 10:00"
due "2027-01-31 10:00"; eq "monthly:last → Feb 28" "$(ats)" "2027-02-28 10:00"
due "2027-02-28 10:00"; eq "monthly:last → Mar 31" "$(ats)" "2027-03-31 10:00"
reset
"$R" add --every weekdays "2026-10-16 07:30" "Standup" >/dev/null
due "2026-10-16 07:30"; eq "weekdays: Fri → Mon (skips Sat/Sun)" "$(ats)" "2026-10-19 07:30"
reset
out=$("$R" add --every weekdays "2026-10-17 07:30" "Standup")
eq "weekdays added on a Saturday snaps to Monday" "$(ats)" "2026-10-19 07:30"
has "  and says so" "$out" "(first matching day)"
reset
"$R" add --owner --every daily "2026-10-12 08:00" "Daily thing" >/dev/null
due "2026-10-15 12:00"
eq "3 days downtime: fired once, next is the next future slot" "$(tgn) $(ats)" "1 2026-10-16 08:00"
reset
"$R" add --every "mon,thu" "2026-10-12 09:00" "Gym" >/dev/null
"$R" add --every weekly:di "2026-10-13 18:00" "Dienstag" >/dev/null
"$R" add --every yearly "2026-10-28 09:00" "PersonB birthday" >/dev/null
"$R" add --every 3d "2026-10-12 21:00" "Water plants" >/dev/null
"$R" add --every 2w "2026-10-12 06:00" "Bins" >/dev/null
out=$("$R" list)
has "list (every Mon, Thu)" "$out" "Gym  (every Mon, Thu)"
has "list German day name (every Tue)" "$out" "Dienstag  (every Tue)"
has "list (every year on 28 Oct)" "$out" "(every year on 28 Oct)"
has "list (every 3 days)" "$out" "(every 3 days)"
has "list (every 2 weeks)" "$out" "(every 2 weeks)"
n=$(printf '%s\n' "$out" | grep -n 'Gym' | cut -d: -f1)
out=$("$R" skip "$n"); has "skip N: next occurrence skipped" "$out" "skipped 2026-10-12 09:00; next: Thu 2026-10-15 09:00"
eq "  series kept, one Gym entry at Thu" "$(python3 -c 'import json,sys; print([r["at"] for r in json.load(open(sys.argv[1])) if r["text"]=="Gym"])' "$REMIND_DB")" "['2026-10-15 09:00']"
n=$("$R" list | grep -n 'Water plants' | cut -d: -f1)
out=$("$R" cancel "$n"); has "cancel N cancels the series" "$out" "cancelled: 2026-10-12 21:00  Water plants  (every 3 days)"
eq "  gone for good" "$("$R" list | grep -c 'Water plants')" "0"
due "2026-10-12 21:00"; eq "3d series really gone after due" "$(grep -c 'Water' "$REMIND_DB")" "0"
"$R" add "2026-10-30 10:00" "One-shot" >/dev/null
n=$("$R" list | grep -n 'One-shot' | cut -d: -f1)
"$R" skip "$n" >/dev/null; eq "skip on a one-shot refuses (rc 1)" "$?" "1"
reset
"$R" add --every daily --until 2026-10-13 "2026-10-12 09:00" "Short series" >/dev/null
has "list shows until" "$("$R" list)" "(every day, until 2026-10-13)"
due "2026-10-12 09:00"; due "2026-10-13 09:00"; eq "--until ends the series after its last day" "$(inboxn) $(ats)" "2 "
reset
REMIND_NOW="2026-10-14 10:00" "$R" add --every daily "2026-10-10 09:00" "Past start" >/dev/null
eq "recurring with a past start moves to the next future slot" "$(ats)" "2026-10-15 09:00"
reset
"$R" add --every fortnightly "2026-10-12 09:00" "x" >/dev/null 2>&1; eq "unknown rule rejected (rc 2)" "$?" "2"
"$R" add --until 2026-10-20 "2026-10-12 09:00" "x" >/dev/null 2>&1; eq "--until without --every rejected" "$?" "2"
"$R" add --every monthly:32 "2026-10-12 09:00" "x" >/dev/null 2>&1; eq "monthly:32 rejected" "$?" "2"
"$R" add --every weekends "2026-10-12 10:00" "Weekend" >/dev/null; eq "weekends snaps to Sat" "$(ats)" "2026-10-17 10:00"
due "2026-10-17 10:00"; eq "weekends: Sat → Sun" "$(ats)" "2026-10-18 10:00"
reset
"$R" add --owner --every daily "2026-10-12 09:00" "Owner series" >/dev/null
due "2026-10-12 09:00"; eq "recurring owner reminder texted as ⏰ text" "$(cat "$T/tg.log")" "⏰ Owner series"
touch "$T/tg-fail"; due "2026-10-13 09:00"
has "recurring tg failure → inbox note with the rule" "$(noid)" "(set for 2026-10-13 09:00, every day): Owner series"
eq "  and the series continues" "$(ats)" "2026-10-14 09:00"

echo "# C. fired.log (counts only, next to the state file)"
reset
"$R" add --owner "2026-10-12 10:00" "Secret owner text" >/dev/null
"$R" add "2026-10-12 10:00" "Secret note for me" >/dev/null
"$R" add --every daily "2026-10-12 11:00" "Series" >/dev/null
due "2026-10-12 09:59"; eq "nothing fired, no log yet" "$([ -e "$T/fired.log" ] && echo yes || echo no)" "no"
due "2026-10-12 10:00"; eq "one line per fired reminder, time + who" "$(sort "$T/fired.log" | tr '\n' '|')" "2026-10-12 10:00 me|2026-10-12 10:00 owner|"
due "2026-10-12 11:00"; due "2026-10-13 11:00"
eq "recurring: one line per firing" "$(grep -c ' me$' "$T/fired.log")" "3"
eq "never the reminder text" "$(grep -c -i 'secret\|series' "$T/fired.log")" "0"
touch "$T/tg-fail"; "$R" add --owner "2026-10-14 08:00" "x" >/dev/null; due "2026-10-14 08:00"
eq "tg failure still logged (as owner)" "$(tail -1 "$T/fired.log")" "2026-10-14 08:00 owner"
rm -f "$T/tg-fail"; "$R" cancel --match Series >/dev/null; "$R" add "2026-10-15 08:00" "y" >/dev/null; n0=$(inboxn)
REMIND_FIRED_LOG=$T/nodir/fired.log due "2026-10-15 08:00"; eq "unwritable log: still fires" "$(inboxn)" "$((n0 + 1))"
eq "REMIND_FIRED_LOG honoured (no write to the default log)" "$(wc -l < "$T/fired.log" | tr -d ' ')" "5"

echo "# D. bills compatibility (temp bills state + temp remind db)"
reset
export BILLS_STATE_DIR=$T/bills BILLS_NOW="2026-10-10 12:00"
out=$(timeout 30 /home/node/bin/bills add --payee "Test AG" --amount 12.50 --due 2026-10-20 --source manual 2>&1)
has "bills add schedules reminders" "$out" "reminders"
eq "  two tagged owner reminders in the temp db" "$(python3 -c 'import json,sys; print(sum(1 for r in json.load(open(sys.argv[1])) if r.get("tag","").startswith("bill:") and r["owner"]))' "$REMIND_DB")" "2"
id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[0]["tag"].split(":")[1])' "$REMIND_DB")
out=$(timeout 30 /home/node/bin/bills paid "$id" 2>&1); has "bills paid cancels them" "$out" "cancelled 2 reminders"
eq "  temp db empty" "$("$R" list)" "(none)"

echo "# E. clock in the owner zone (container on UTC, REMIND_NOW=@EPOCH = real instants, cron's every-minute due)"
reset
# Pin the owner zone (the dates below are its DST switch nights), whatever the live profile says.
printf '{"locale": {"tz": "Europe/Zurich"}}\n' > "$T/profile.json"
export ASSISTANT_PROFILE=$T/profile.json
ep() { date -u -d "$1" +%s; }                          # "YYYY-MM-DD HH:MM" UTC → epoch seconds
utc_due() { REMIND_NOW="@$(ep "$1")" TZ=UTC timeout 20 "$R" due; }
sweep() {                                              # cron: `due` once per minute, UTC instants FROM..TO
    local t end; t=$(ep "$1"); end=$(ep "$2")
    while [ "$t" -le "$end" ]; do REMIND_NOW="@$t" TZ=UTC timeout 20 "$R" due; t=$((t + 60)); done
}
firedn() { local n; n=$(grep -c -- "$1" "$T/fired.log" 2>/dev/null); echo "${n:-0}"; }
"$R" add --owner "2027-01-01 00:00" "probe" >/dev/null
utc_due "2026-12-31 22:59"; eq "hook: @EPOCH 22:59 UTC on Dec 31 = 23:59 local, not due yet" "$(tgn)" "0"
utc_due "2026-12-31 23:00"; eq "  23:00 UTC = 00:00 local on Jan 1: fires" "$(tgn)" "1"

reset
# Spring forward (local 02:00 → 03:00 at 01:00 UTC): 02:30 does not exist on that day.
"$R" add --owner "2027-03-28 02:30" "spring once" >/dev/null
"$R" add --owner --every daily "2027-03-28 02:30" "spring daily" >/dev/null
sweep "2027-03-28 00:50" "2027-03-28 01:40"
eq "spring-forward 02:30 (nonexistent): one-off + daily fire once each, not never" "$(tgn)" "2"
eq "  both at the first minute after the gap (03:00 local)" "$(firedn '^2027-03-28 03:00 owner$')" "2"
eq "  daily moves to the next day's 02:30" "$(ats)" "2027-03-29 02:30"
utc_due "2027-03-29 00:29"; eq "  next day: not before 02:30 local (00:29 UTC = 02:29)" "$(tgn)" "2"
utc_due "2027-03-29 00:30"; eq "  next day: fires at 02:30 local" "$(tgn) $(ats)" "3 2027-03-30 02:30"

reset
# Fall back (local 03:00 → 02:00 at 01:00 UTC): 02:00–02:59 happens twice.
"$R" add --owner "2026-10-25 02:30" "fall once" >/dev/null
"$R" add --owner --every daily "2026-10-25 02:30" "fall daily" >/dev/null
sweep "2026-10-24 23:50" "2026-10-25 00:59"
eq "fall-back 02:30 (ambiguous): fired in the first pass (summer time)" "$(tgn) $(firedn '^2026-10-25 02:30 owner$')" "2 2"
sweep "2026-10-25 01:00" "2026-10-25 01:45"
eq "  the repeated hour does not fire them again (once, not twice)" "$(tgn)" "2"
eq "  daily moves to the next day's 02:30" "$(ats)" "2026-10-26 02:30"
utc_due "2026-10-26 01:30"; eq "  next day: fires at 02:30 local (01:30 UTC, winter time)" "$(tgn) $(ats)" "3 2026-10-27 02:30"

reset
# Month turn and year turn: the container (UTC) is still on the previous day/month/year.
"$R" add --owner "2026-11-01 00:30" "month turn" >/dev/null
sweep "2026-10-31 23:25" "2026-10-31 23:35"
eq "month turn: 00:30 local Nov 1 fires at 23:30 UTC Oct 31, once" "$(tgn) $(firedn '^2026-11-01 00:30 owner$')" "1 1"
reset
"$R" add --owner "2027-01-01 00:00" "new year" >/dev/null
"$R" add --owner --every daily "2026-12-31 23:30" "daily late" >/dev/null
"$R" add --owner --every monthly:last "2026-12-31 23:45" "month end" >/dev/null
"$R" add --owner --every yearly "2026-12-31 23:50" "year end" >/dev/null
sweep "2026-12-31 22:25" "2026-12-31 23:05"
eq "year turn: daily 23:30, monthly:last 23:45, yearly 23:50, one-off 00:00 Jan 1: each once" "$(tgn)" "4"
eq "  fired at local times (23:30, 23:45, 23:50, 00:00 next year)" \
   "$(firedn '^2026-12-31 23:30 ')$(firedn '^2026-12-31 23:45 ')$(firedn '^2026-12-31 23:50 ')$(firedn '^2027-01-01 00:00 ')" "1111"
eq "  next slots roll into the new year" "$(ats)" "2027-01-01 23:30 2027-01-31 23:45 2027-12-31 23:50"

reset
# Set while the container is on UTC: the typed time is owner-local, stored as typed, compared with owner-local now.
NOW_AT="@$(ep '2026-11-15 09:30')"                     # = 10:30 local (winter time)
out=$(REMIND_NOW="$NOW_AT" TZ=UTC "$R" add --owner "2026-11-15 12:00" "lunch")
eq "UTC container: add keeps the owner-local time as typed" "$out $(ats)" \
   "added owner reminder for Sun 2026-11-15 12:00 2026-11-15 12:00"
out=$(REMIND_NOW="$NOW_AT" TZ=UTC "$R" add --owner --every daily "2026-11-15 10:00" "past locally")
has "  recurring 10:00 is already past at 10:30 local (09:30 UTC): moved to tomorrow" "$out" "Mon 2026-11-16 10:00"
utc_due "2026-11-15 10:59"; eq "  12:00 local not due at 10:59 UTC (11:59 local)" "$(tgn)" "0"
utc_due "2026-11-15 11:00"; eq "  fires at 11:00 UTC = 12:00 local" "$(tgn)" "1"

reset
# Real clock (no REMIND_NOW), container on UTC: one owner-local minute ago is due, ten minutes ahead is not.
read -r past fut < <(python3 -c 'from datetime import datetime, timedelta; from zoneinfo import ZoneInfo
n = datetime.now(ZoneInfo("Europe/Zurich")); f = "%Y-%m-%d_%H:%M"
print((n - timedelta(minutes=1)).strftime(f), (n + timedelta(minutes=10)).strftime(f))')
env -u REMIND_NOW TZ=UTC "$R" add --owner "${past/_/ }" "real past" >/dev/null
env -u REMIND_NOW TZ=UTC "$R" add --owner "${fut/_/ }" "real future" >/dev/null
env -u REMIND_NOW TZ=UTC timeout 20 "$R" due
eq "real clock on a UTC container: owner-local past one fires, future one waits" "$(tgn) $(ats)" "1 ${fut/_/ }"

reset
# Downtime across the fall-back night: everything overdue fires once on the first `due`, then nothing repeats.
"$R" add --owner "2026-10-25 02:30" "ambiguous, missed" >/dev/null
"$R" add --owner "2026-10-25 12:00" "noon, missed" >/dev/null
"$R" add --owner --every daily "2026-10-24 21:00" "daily, missed twice" >/dev/null
"$R" add --owner "2026-10-26 09:00" "still ahead" >/dev/null
utc_due "2026-10-26 06:00"                              # back up at 07:00 local
eq "downtime: 2 one-offs + daily (missed twice) fire once each" "$(tgn)" "3"
eq "  daily jumps to its next future slot, the future one-off stays" "$(ats)" "2026-10-26 09:00 2026-10-26 21:00"
utc_due "2026-10-26 06:00"; utc_due "2026-10-26 06:01"
eq "  the next minutes fire nothing again" "$(tgn)" "3"
unset ASSISTANT_PROFILE

echo "# F. lossless due: failed note writes, crashes, retries (fixed note name per reminder + slot)"
dbmd5() { md5sum < "$REMIND_DB"; }
notes() { find "$T/inbox" -maxdepth 1 -type f -name 'reminder-*.md' 2>/dev/null | wc -l | tr -d ' '; }
hidden() { find "$T/inbox" -maxdepth 1 -name '.*' 2>/dev/null | wc -l | tr -d ' '; }
logn() { [ -f "$T/fired.log" ] && wc -l < "$T/fired.log" | tr -d ' ' || echo 0; }
texts() { python3 -c 'import json,sys; print("|".join(r["text"] + "@" + r["at"] for r in json.load(open(sys.argv[1]))))' "$REMIND_DB"; }
jn() { [ -f "$REMIND_DB.journal" ] && wc -l < "$REMIND_DB.journal" | tr -d ' ' || echo none; }
probe() {  # the notes a clean run would write (same store copy, own inbox): names are fixed per reminder + slot
    mkdir -p "$T/probe"; cp "$REMIND_DB" "$T/probe/rem.json"
    REMIND_DB=$T/probe/rem.json REMIND_INBOX=$T/probe/inbox REMIND_FIRED_LOG=$T/probe/fired.log due "$1"
}
reset
"$R" add "2026-10-12 10:00" "Lose A" >/dev/null
"$R" add "2026-10-12 10:00" "Lose B" >/dev/null
"$R" add --every daily "2026-10-12 10:00" "Lose series" >/dev/null
"$R" add "2026-10-20 10:00" "Later" >/dev/null
mkdir -p "$T/inbox"; chmod 555 "$T/inbox"
if touch "$T/inbox/w" 2>/dev/null; then no "read-only inbox (running as root?)"; fi
m0=$(dbmd5)
due "2026-10-12 10:00" 2>"$T/err"; rc=$?
eq "unwritable inbox: due exits 1" "$rc" "1"
eq "  store untouched (nothing removed or advanced)" "$(dbmd5)" "$m0"
eq "  no note, no temp file, no fired.log line, no journal" "$(notes)/$(hidden)/$(logn)/$(jn)" "0/0/0/none"
has "  stderr: what failed and how many were kept" "$(cat "$T/err")" "3 of 3 due reminder(s) kept for the next run"
eq "  stderr never has the reminder text" "$(grep -c 'Lose' "$T/err")" "0"
chmod 755 "$T/inbox"
due "2026-10-12 10:01"; rc=$?
eq "next run: rc 0, each fired exactly once" "$rc $(notes) $(logn)" "0 3 3"
eq "  one-offs gone, series advanced, future one kept" "$(ats)" "2026-10-13 10:00 2026-10-20 10:00"
eq "  note names: reminder-<slot>-<hash>.md" "$(ls "$T/inbox" | grep -cE '^reminder-202610121000-[0-9a-f]{12}\.md$')" "3"
eq "  no temp files left, journal cleared" "$(hidden)/$(jn)" "0/none"
has "  note text unchanged" "$(noid)" "Reminder (set for 2026-10-12 10:00, every day): Lose series"
due "2026-10-12 10:02"; eq "  a later minute fires nothing again" "$(notes) $(logn)" "3 3"

reset
"$R" add "2026-10-12 10:00" "Part A" >/dev/null
"$R" add "2026-10-12 10:00" "Part B" >/dev/null
"$R" add --every daily "2026-10-12 10:00" "Part C series" >/dev/null
probe "2026-10-12 10:00"
nb=$(basename "$(grep -l 'Part B' "$T"/probe/inbox/*)")
mkdir -p "$T/inbox/$nb/x"                              # B's note can't be written (a directory holds its name)
due "2026-10-12 10:00" 2>"$T/err"; rc=$?
eq "fails midway: rc 1, A delivered, B and C not" "$rc $(notes) $(logn)" "1 1 1"
eq "  store: A removed, B and C unchanged" "$(texts)" "Part B@2026-10-12 10:00|Part C series@2026-10-12 10:00"
has "  stderr counts the kept ones" "$(cat "$T/err")" "2 of 3 due reminder(s) kept"
rm -rf "${T:?}/inbox/${nb:?}"
due "2026-10-12 10:01"; rc=$?
eq "retry: rc 0, B and C fire once, A not again" "$rc $(notes) $(logn) $(ats)" "0 3 3 2026-10-13 10:00"
eq "  same note names as one clean run" "$(ls "$T/inbox" | sort | tr '\n' ' ')" "$(ls "$T/probe/inbox" | sort | tr '\n' ' ')"

reset
"$R" add --owner "2026-10-12 10:00" "Owner first" >/dev/null
"$R" add "2026-10-12 10:00" "Mine second" >/dev/null
"$R" add --owner "2026-10-12 10:00" "Owner third" >/dev/null
mkdir -p "$T/inbox"; chmod 555 "$T/inbox"
due "2026-10-12 10:00" 2>/dev/null; rc=$?
eq "owner text sent, then a note fails: rc 1, owner one advanced, the rest kept" "$rc $(tgn) $(texts)" \
   "1 1 Mine second@2026-10-12 10:00|Owner third@2026-10-12 10:00"
chmod 755 "$T/inbox"
due "2026-10-12 10:01"
eq "  retry: each texted / noted exactly once" "$(tgn) $(notes) $(logn) [$(ats)]" "2 1 3 []"
reset
"$R" add --owner "2026-10-12 10:00" "Owner, tg down" >/dev/null
mkdir -p "$T/inbox"; chmod 555 "$T/inbox"; touch "$T/tg-fail"
due "2026-10-12 10:00" 2>/dev/null; rc=$?
eq "tg down and the fallback note fails: kept, rc 1" "$rc $(tgn) $(ats)" "1 0 2026-10-12 10:00"
chmod 755 "$T/inbox"; rm -f "$T/tg-fail"
due "2026-10-12 10:01"; eq "  next run: texted once, no note" "$(tgn) $(notes) [$(ats)]" "1 0 []"

reset
"$R" add "2026-10-12 10:00" "Crash me" >/dev/null
"$R" add --owner "2026-10-12 10:00" "Crash owner" >/dev/null
"$R" add --every daily "2026-10-12 10:00" "Crash series" >/dev/null
mkdir -p "$REMIND_DB.tmp/x"; m0=$(dbmd5)                # the store can't be saved after delivering
due "2026-10-12 10:00" 2>"$T/err"; rc=$?
eq "save fails after delivery: rc 1, all delivered, store unchanged" "$rc $(tgn) $(notes) $([ "$(dbmd5)" = "$m0" ] && echo same)" "1 1 2 same"
eq "  journal: one key per delivered slot, no text" "$(jn) $(grep -c 'Crash' "$REMIND_DB.journal")" "3 0"
has "  stderr says the journal covers the retry" "$(cat "$T/err")" "couldn't save the store"
find "$T/inbox" -mindepth 1 -delete                    # the heartbeat handled and deleted the notes meanwhile
rm -rf "${T:?}/rem.json.tmp"
due "2026-10-12 10:01"; rc=$?
eq "  retry: rc 0, nothing delivered twice (even with the notes gone)" "$rc $(tgn) $(notes) $(logn)" "0 1 0 3"
eq "  store advanced, journal cleared" "$(ats) $(jn)" "2026-10-13 10:00 none"

reset
"$R" add "2026-10-12 10:00" "Kill A" >/dev/null
"$R" add --owner "2026-10-12 10:00" "Kill B" >/dev/null
"$R" add "2026-10-12 10:00" "Kill C" >/dev/null
m0=$(dbmd5); touch "$T/tg-crash"                       # the process dies (kill -9) while texting B
due "2026-10-12 10:00" 2>/dev/null; rc=$?
eq "crash mid-run: killed, A's note written + journaled, store unchanged" \
   "$([ "$rc" -ne 0 ] && echo failed) $(notes) $(jn) $([ "$(dbmd5)" = "$m0" ] && echo same)" "failed 1 1 same"
due "2026-10-12 10:01"; rc=$?
eq "  retry: rc 0, A not again, B texted once, C noted once" "$rc $(notes) $(tgn) [$(ats)] $(jn)" "0 2 1 [] none"

reset
"$R" add "2026-10-12 10:00" "Hard A" >/dev/null
"$R" add "2026-10-12 10:00" "Hard B" >/dev/null
mkdir -p "$REMIND_DB.journal/x" "$REMIND_DB.tmp/x"      # neither the journal nor the store can be written
due "2026-10-12 10:00" 2>/dev/null; rc=$?
eq "journal and store unwritable: rc 1, stops after the first delivery" "$rc $(notes)" "1 1"
rm -rf "${T:?}/rem.json.journal" "${T:?}/rem.json.tmp"
due "2026-10-12 10:01"; rc=$?
eq "  retry rewrites A's note under its fixed name: no duplicate" "$rc $(notes) [$(ats)]" "0 2 []"

reset
"$R" add "2026-10-12 10:00" "Same" >/dev/null
"$R" add "2026-10-12 10:00" "Same" >/dev/null
due "2026-10-12 10:00"; eq "two identical reminders: two notes" "$(notes)" "2"
reset
mkdir -p "$T"; printf 'stale-key\n' > "$REMIND_DB.journal"; "$R" add "2026-10-20 10:00" "Later" >/dev/null
due "2026-10-12 10:00"; eq "nothing due: a stale journal is cleared" "$(jn)" "none"

# ---------------------------------------------------------------- trip clock: owner reminders added while away
reset
printf '{"trips": [{"name": "t", "tz": "Asia/Dubai", "from": "2026-10-14", "to": "2026-10-18"}]}' > "$T/trips.json"
out=$(REMIND_NOW="2026-10-15 09:00" "$R" add --owner "2026-10-16 10:00" "Trip pill")
has "away: owner time read as his local time" "$out" "reminder for Fri 2026-10-16 08:00 home time = Fri 2026-10-16 10:00 his time (Asia/Dubai)"
eq "  stored as home wall time" "$(ats)" "2026-10-16 08:00"
has "  list shows his time" "$("$R" list)" "2026-10-16 08:00 (= 2026-10-16 10:00 his time)  [owner] Trip pill"
REMIND_NOW="2026-10-15 09:00" "$R" add "2026-10-16 10:00" "My own" >/dev/null
eq "away: my own (non-owner) reminder stays home wall time" "$(ats)" "2026-10-16 08:00 2026-10-16 10:00"
reset
REMIND_NOW="2026-10-15 09:00" "$R" add --owner --every daily "2026-10-16 07:00" "Daily pill" >/dev/null
eq "away: daily owner reminder converted" "$(ats)" "2026-10-16 05:00"
out=$(REMIND_NOW="2026-10-15 09:00" "$R" add --owner --every weekly "2026-10-17 01:00" "Weekly" 2>&1); rc=$?
eq "away: weekly rule crossing midnight at home refused" "$rc" "2"
has "  says why" "$out" "would shift a day"
reset
REMIND_NOW="2026-10-12 09:00" "$R" add --owner "2026-10-16 10:00" "Set at home" >/dev/null
eq "set at home before the trip: keeps its home wall time" "$(ats)" "2026-10-16 10:00"
due "2026-10-16 09:59"; eq "  not due at 09:59 home" "$(tgn)" "0"
due "2026-10-16 10:00"; eq "  due at 10:00 home (12:00 his time)" "$(tgn)" "1"
rm -f "$T/trips.json"
reset

eq "real reminders.json + fired.log: no test data (live cron changes allowed)" "$(realcheck check)" "ok"
# IDs, snooze/done/buttons/press and tg-buttons: their own file, counted here
sn=$(REMIND_BIN="$R_BIN" timeout 200 bash "$(dirname "$0")/snooze.sh" 2>&1)
printf '%s\n' "$sn" | grep -E '^(FAIL|       got)' || true
sp=$(printf '%s\n' "$sn" | sed -n 's/^pass=\([0-9]*\) fail=\([0-9]*\)$/\1 \2/p')
if [ -n "$sp" ]; then pass=$((pass + ${sp% *})); fail=$((fail + ${sp#* })); else no "snooze.sh ran to the end" "$(printf '%s' "$sn" | tail -n3)"; fi
# Location reminders (--on, place-due): their own file, counted here
pl=$(REMIND_BIN="$R_BIN" timeout 200 bash "$(dirname "$0")/place.sh" 2>&1)
printf '%s\n' "$pl" | grep -E '^(FAIL|       got)' || true
pp=$(printf '%s\n' "$pl" | sed -n 's/^pass=\([0-9]*\) fail=\([0-9]*\)$/\1 \2/p')
if [ -n "$pp" ]; then pass=$((pass + ${pp% *})); fail=$((fail + ${pp#* })); else no "place.sh ran to the end" "$(printf '%s' "$pl" | tail -n3)"; fi
bk=$(REMIND_BIN="$R_BIN" timeout 200 bash "$(dirname "$0")/back.sh" 2>&1)
printf '%s\n' "$bk" | grep -E '^(FAIL|       got)' || true
bp=$(printf '%s\n' "$bk" | sed -n 's/^pass=\([0-9]*\) fail=\([0-9]*\)$/\1 \2/p')
if [ -n "$bp" ]; then pass=$((pass + ${bp% *})); fail=$((fail + ${bp#* })); else no "back.sh ran to the end" "$(printf '%s' "$bk" | tail -n3)"; fi
# Wake / departure calls (--call, fake voice-relay): their own file, counted here
cl=$(REMIND_BIN="$R_BIN" timeout 200 bash "$(dirname "$0")/call.sh" 2>&1)
printf '%s\n' "$cl" | grep -E '^(FAIL|       got)' || true
cp=$(printf '%s\n' "$cl" | sed -n 's/^pass=\([0-9]*\) fail=\([0-9]*\)$/\1 \2/p')
if [ -n "$cp" ]; then pass=$((pass + ${cp% *})); fail=$((fail + ${cp#* })); else no "call.sh ran to the end" "$(printf '%s' "$cl" | tail -n3)"; fi
# Phone-app side (actions, act, route app/both in due; stub phone CLI): its own file, counted here
ap=$(REMIND_BIN="$R_BIN" timeout 200 bash "$(dirname "$0")/app.sh" 2>&1)
printf '%s\n' "$ap" | grep -E '^(FAIL|       got)' || true
aq=$(printf '%s\n' "$ap" | sed -n 's/^pass=\([0-9]*\) fail=\([0-9]*\)$/\1 \2/p')
if [ -n "$aq" ]; then pass=$((pass + ${aq% *})); fail=$((fail + ${aq#* })); else no "app.sh ran to the end" "$(printf '%s' "$ap" | tail -n3)"; fi
# Reminders 2.0 (when / phrases / move / aliases / list --json / instant press / leave:zone): its own file
sm=$(REMIND_BIN="$R_BIN" timeout 200 bash "$(dirname "$0")/smart.sh" 2>&1)
printf '%s\n' "$sm" | grep -E '^(FAIL|       got)' || true
sq=$(printf '%s\n' "$sm" | sed -n 's/^pass=\([0-9]*\) fail=\([0-9]*\)$/\1 \2/p')
if [ -n "$sq" ]; then pass=$((pass + ${sq% *})); fail=$((fail + ${sq#* })); else no "smart.sh ran to the end" "$(printf '%s' "$sm" | tail -n3)"; fi
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
