#!/usr/bin/env bash
# Tests for remind's wake / departure calls (add --call, timed and --on), against a FAKE `voice-relay call`
# (REMIND_CALL_CMD). Never rings anyone: temp state only (REMIND_DB/INBOX), stub REMIND_TG / REMIND_TG_BUTTONS,
# REMIND_NOW. Exit 0 = all pass. REMIND_BIN: a test copy before installing. Run alone or from run.sh.
set -uo pipefail
R=${REMIND_BIN:-/home/node/bin/remind}
T=$(mktemp -d "${TMPDIR:-/tmp}/remind-call-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
export REMIND_DB=$T/rem.json REMIND_INBOX=$T/inbox REMIND_TG=$T/tg REMIND_TG_BUTTONS=$T/tgb REMIND_PLACE_CMD=$T/vault
export REMIND_CALL_CMD="$T/fake-voice-relay call" REMIND_BACK_OFFER=0
unset REMIND_BUTTONS REMIND_PLACE_DWELL REMIND_BACK_HOURS REMIND_TG_CMD REMIND_NOW
cat > "$T/tg" <<STUB
#!/bin/sh
[ -e "$T/tg-fail" ] && exit 1
{ printf '%s' "\$*" | tr '\n' '|'; printf '\n'; } >> "$T/tg.log"
STUB
cat > "$T/tgb" <<STUB
#!/bin/sh
[ -e "$T/tg-fail" ] && exit 1
{ printf '%s' "\$1" | tr '\n' '|'; printf '\n'; } >> "$T/tgb.log"
printf '%s\n' "\$2" >> "$T/tgb.markup"
STUB
# fake voice-relay: logs its argv, sleeps $T/call-sleep seconds, exits with $T/call-rc (default 0)
cat > "$T/fake-voice-relay" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$T/calls.log"
[ -e "$T/call-sleep" ] && sleep "\$(cat "$T/call-sleep")"
exit "\$(cat "$T/call-rc" 2>/dev/null || echo 0)"
STUB
cat > "$T/vault" <<STUB
#!/bin/sh
cat "$T/place.json"
STUB
chmod +x "$T/tg" "$T/tgb" "$T/fake-voice-relay" "$T/vault"
M=CALLTEST   # marker in every test text: must never reach the live store

pass=0 fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if grep -qF -- "$3" <<<"$2"; then ok "$1"; else no "$1" "$2"; fi; }
reset() { find "$T" -mindepth 1 -maxdepth 1 ! -name tg ! -name tgb ! -name vault ! -name fake-voice-relay -exec rm -rf {} +; }
lines() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }
py() { python3 -I -c "$@"; }
due() { REMIND_NOW="$1" timeout 60 "$R" due; }
calls() { lines "$T/calls.log"; }
last() { tail -n1 "$T/tgb.log" 2>/dev/null; }
store() { py 'import json,sys
try: items = json.load(open(sys.argv[1]))
except FileNotFoundError: items = []
print(json.dumps([{k: r.get(k) for k in sys.argv[2].split(",")} for r in items], ensure_ascii=False))' "$REMIND_DB" "$1"; }
here() { case $1 in
    home) printf '{"place": {"kind": "home", "zone": "Home", "band": null, "town": null, "flag": "home"}}' ;;
    away) printf '{"place": {"kind": "away", "zone": null, "band": "near", "town": "Testtown", "flag": "away"}}' ;;
  esac > "$T/place.json"; }
poll() { here "$2"; REMIND_NOW="2026-10-20 $1" timeout 30 "$R" place-due; }

# ---------------------------------------------------------------- add, list
reset
export REMIND_NOW="2026-10-20 10:00"
out=$("$R" add --call "2026-10-21 06:30" "$M wake up")
eq "add --call: implies --owner" "$out" "added owner 📞 call reminder for Wed 2026-10-21 06:30"
eq "store: owner + call" "$(store owner,call)" '[{"owner": true, "call": true}]'
eq "list marks it" "$("$R" list)" " 1. 2026-10-21 06:30  [owner] $M wake up (📞 call)"
"$R" add --owner "2026-10-21 06:30" "$M plain" >/dev/null
eq "plain owner reminder: no call key" "$(store call)" '[{"call": true}, {"call": null}]'
unset REMIND_NOW

# ---------------------------------------------------------------- answered → plain text, one ring
reset
REMIND_NOW="2026-10-20 10:00" "$R" add --owner --call "2026-10-21 06:30" "$M wake up" >/dev/null
due "2026-10-21 06:29"; eq "before the time: no ring" "$(calls)" "0"
due "2026-10-21 06:30"; eq "due: rang once" "$(calls)" "1"
eq "fake got call --reason TEXT" "$(cat "$T/calls.log")" "call --reason $M wake up"
eq "answered: text without 📞" "$(last)" "⏰ $M wake up"
has "snooze/done buttons" "$(tail -n1 "$T/tgb.markup")" "rm:s1h:"
eq "one-off left the store" "$(store id)" "[]"
due "2026-10-21 06:31"; eq "next run: no second ring" "$(calls)" "1"

# ---------------------------------------------------------------- not answered (3) and the other exits → 📞 tried calling
for rc in 3 2 4 5; do
  reset; echo "$rc" > "$T/call-rc"
  REMIND_NOW="2026-10-20 10:00" "$R" add --call "2026-10-21 06:30" "$M rc$rc" >/dev/null
  due "2026-10-21 06:30"
  eq "exit $rc: one ring, text starts with 📞 tried calling" "$(calls) $(last)" "1 📞 tried calling|⏰ $M rc$rc"
done

# ---------------------------------------------------------------- any hour: quiet hours don't stop a --call he set
reset; echo 3 > "$T/call-rc"
REMIND_NOW="2026-10-20 10:00" "$R" add --call "2026-10-21 03:15" "$M night" >/dev/null
due "2026-10-21 03:15"; eq "03:15: rings" "$(calls)" "1"

# ---------------------------------------------------------------- series: one ring per occurrence, mark not carried over
reset
REMIND_NOW="2026-10-20 10:00" "$R" add --call --every daily "2026-10-21 06:30" "$M daily" >/dev/null
due "2026-10-21 06:30"; due "2026-10-21 06:31"
eq "series day 1: one ring" "$(calls)" "1"
eq "next slot without the mark" "$(store at,call,called)" '[{"at": "2026-10-22 06:30", "call": true, "called": null}]'
due "2026-10-22 06:30"; eq "series day 2: rings again" "$(calls)" "2"

# ---------------------------------------------------------------- crash / overlap safety: never a second ring
reset
py 'import json,sys,time
json.dump([{"id": "aaaaaaaa", "at": "2026-10-21 06:30", "text": sys.argv[2] + " fresh", "owner": True, "call": True,
            "called": {"rc": None, "ts": int(time.time())}}], open(sys.argv[1], "w"))' "$REMIND_DB" "$M"
due "2026-10-21 06:31"
eq "fresh ringing mark (another run): no ring, no text, kept" "$(calls) $(lines "$T/tgb.log") $(store id)" '0 0 [{"id": "aaaaaaaa"}]'
py 'import json,sys
d = json.load(open(sys.argv[1])); d[0]["called"]["ts"] = 0; json.dump(d, open(sys.argv[1], "w"))' "$REMIND_DB"
due "2026-10-21 06:42"
eq "stale mark (crashed run): no ring, text with 📞" "$(calls) $(last)" "0 📞 tried calling|⏰ $M fresh"

reset; echo 2 > "$T/call-sleep"
REMIND_NOW="2026-10-20 10:00" "$R" add --call "2026-10-21 06:30" "$M overlap" >/dev/null
REMIND_NOW="2026-10-21 06:30" timeout 60 "$R" due & bg=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ "$(calls)" = 1 ] && break; sleep 0.2; done
t0=$(date +%s)
REMIND_NOW="2026-10-21 06:30" timeout 10 "$R" add --owner "2026-10-21 09:00" "$M during" >/dev/null; arc=$?
due "2026-10-21 06:31"
t1=$(date +%s)
eq "while it rings: add + a 2nd due don't wait on the call" "$arc $((t1 - t0 < 2))" "0 1"
eq "2nd due: no ring, no text yet" "$(calls) $(lines "$T/tgb.log")" "1 0"
wait "$bg"
eq "the ringing run sends the text after the call" "$(calls) $(last)" "1 ⏰ $M overlap"
eq "only the 09:00 one is left" "$(store text)" "[{\"text\": \"$M during\"}]"
rm -f "$T/call-sleep"

# ---------------------------------------------------------------- fallbacks
reset; echo 3 > "$T/call-rc"; touch "$T/tg-fail"
REMIND_NOW="2026-10-20 10:00" "$R" add --call "2026-10-21 06:30" "$M note" >/dev/null
due "2026-10-21 06:30"
has "tg down: the inbox note says the call wasn't answered" "$(cat "$T"/inbox/reminder-*.md)" "wake call was not answered"
rm -f "$T/tg-fail"

reset
REMIND_NOW="2026-10-20 10:00" "$R" add --call "2026-10-21 06:30" "$M nostub" >/dev/null
REMIND_CALL_CMD= REMIND_NOW="2026-10-21 06:30" timeout 60 "$R" due
eq "test state without a stub: never rings, text with 📞" "$(calls) $(last)" "0 📞 tried calling|⏰ $M nostub"

# ---------------------------------------------------------------- snooze: back as text only
reset
REMIND_NOW="2026-10-20 10:00" "$R" add --call "2026-10-21 06:30" "$M snooze" >/dev/null
id=$(store id | sed -n 's/.*"id": "\([0-9a-f]*\)".*/\1/p')
due "2026-10-21 06:30"
REMIND_NOW="2026-10-21 06:31" "$R" snooze "$id" +1h >/dev/null
eq "snoozed: back without the call" "$(store at,call)" '[{"at": "2026-10-21 07:31", "call": null}]'

# ---------------------------------------------------------------- location: --on home --call
reset; echo 3 > "$T/call-rc"
out=$(REMIND_NOW="2026-10-20 10:00" "$R" add --on home --call "$M home")
has "add --on --call" "$out" "📞 calls him when it fires live"
has "list marks it" "$(REMIND_NOW="2026-10-20 10:00" "$R" list)" "📍 when home: $M home (📞 call)"
poll 10:02 away; poll 10:20 home
eq "arrival: no text from place-due, handed to the timed store" "$(lines "$T/tgb.log") $(store at,call,on)" \
   '0 [{"at": "2026-10-20 10:20", "call": true, "on": "home"}]'
eq "once: left places.json" "$(cat "$T/places.json")" "[]"
due "2026-10-20 10:21"
eq "next due: rings, 📍 text with 📞" "$(calls) $(last)" "1 📞 tried calling|📍 Home: $M home"
has "place buttons (next time home)" "$(tail -n1 "$T/tgb.markup")" "rm:snxt:"

reset
REMIND_NOW="2026-10-20 01:00" "$R" add --on home --call "$M held" >/dev/null
REMIND_NOW="2026-10-20 01:00" "$R" add --on home --call --urgent "$M urgent" >/dev/null
poll 01:02 away; poll 02:10 home
due "2026-10-20 02:11"
eq "02:10 arrival: only --urgent rings" "$(calls) $(last)" "1 📍 Home: $M urgent"
poll 08:00 home
eq "held one at 08:00: text only, no ring" "$(calls) $(last)" "1 📍 Home (since 02:10): $M held"

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
