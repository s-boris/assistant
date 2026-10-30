#!/bin/bash
# drive-brief tests: fictional towns, fake whereabouts / Waze / relay / deck / call log; never rings, never
# reads his data. Usage: run.sh   (DRIVE_BRIEF_BIN=/path/to/copy tests a copy)
set -u
BIN=${DRIVE_BRIEF_BIN:-$HOME/bin/drive-brief}
T=$(mktemp -d /tmp/drive-brief-tests.XXXXXX)
trap 'find "${T:?}" -mindepth 1 -delete; rmdir "${T:?}"' EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "FAIL: $1"; }
check() { if eval "$2"; then ok; else bad "$1"; fi; }

export USAGE=off TRIPCLOCK_OFF=1 TRIPCLOCK_CONFIG=$T/none.json
export ASSISTANT_PROFILE=$T/profile.json DRIVE_BRIEF_CONFIG=$T/config.json DRIVE_BRIEF_STATE=$T/state
export DRIVE_BRIEF_ROUTES=$T/routes.json DRIVE_BRIEF_CALL_CMD="$T/fake-call" DRIVE_BRIEF_DECK_CMD="$T/fake-deck"
export DRIVE_BRIEF_WHERE="$T/fake-where" DRIVE_BRIEF_CALLS_CMD="$T/fake-hub" DRIVE_BRIEF_REMIND_DIR=$T/remind
export CALCACHE_DIR=$T/cal DRIVE_BRIEF_MAIL=$T/mail.json DRIVE_BRIEF_PHONE_CMD="$T/fake-phone"
mkdir -p "$T/remind" "$T/cal"
echo '{"home": {"town": "Hometown"}, "work": {"town": "Worktown"}}' > "$T/profile.json"
cat > "$T/routes.json" <<'EOF'
{"Hometown|Worktown": {"live": 60, "usual": 48}, "Worktown|Hometown": {"live": 47, "usual": 46},
 "Hometown|Eventville": {"live": 47, "usual": 45}, "Hometown|Trainville": {"live": 30, "usual": 30}}
EOF
echo '[]' > "$T/mail.json"
echo '[]' > "$T/remind/reminders.json"
printf '#!/bin/sh\nprintf "%%s\\n" "$FAKE_WHERE"\n' > "$T/fake-where"
printf '#!/bin/sh\necho "$@" >> %s/calls.txt\nexit "${FAKE_CALL_RC:-0}"\n' "$T" > "$T/fake-call"
cat > "$T/fake-deck" <<'EOF2'
#!/bin/sh
if [ -n "${FAKE_DECK:-}" ]; then printf '%s' "$FAKE_DECK"; else printf '{"items": []}'; fi
EOF2
printf '#!/bin/sh\n[ "$1 $2" = "open --help" ] && exit "${FAKE_PHONE_RC:-0}"\nexit 9\n' > "$T/fake-phone"
chmod +x "$T/fake-where" "$T/fake-call" "$T/fake-deck" "$T/fake-phone"
cfg() { echo "{\"mode\": \"${1:-live}\", \"max_per_day\": ${2:-4}, \"parcel\": {\"mail_query\": \"x\", \"town\": \"Parceldorf\", \"via\": [\"Worktown\"]}}" > "$T/config.json"; }
where() { # active since place-kind town [zone] [band]
  FAKE_WHERE="{\"place\": {\"kind\": \"$3\", \"zone\": ${5:-null}, \"band\": ${6:-null}, \"town\": ${4:-null}}, \"drive\": {\"active\": $1, \"since\": ${2:-null}}, \"age_min\": 1, \"source\": \"vault\"}"
  export FAKE_WHERE; }
tick() { DRIVE_BRIEF_NOW=$1 "$BIN" tick --cron; }
ncalls() { [ -f "$T/calls.txt" ] && wc -l < "$T/calls.txt" | tr -d ' ' || echo 0; }
last() { tail -n 1 "$T/state/log.jsonl"; }
# the hub's call log: `voice-hub calls --last 20 --out` (voice.calls digests); FAKE_HUB unset = the hub does not answer
cat > "$T/fake-hub" <<'EOF2'
#!/bin/sh
[ -n "${FAKE_HUB_RC:-}" ] && exit "$FAKE_HUB_RC"
[ -z "${FAKE_HUB+x}" ] && exit 2
printf '%s' "$FAKE_HUB"
EOF2
chmod +x "$T/fake-hub"
hubcall() { # start-epoch ring-outcome (answered|declined|no_answer|busy|failed; - = never rang) [him] [tools-json] [duration]
  local ring="[\"$2\", 12.0]" oc="\"$2\"" ans=false reason=${2//_/ }
  [ "$2" = answered ] && ans=true reason=hangup
  [ "$2" = - ] && ring=null oc='"not_rung"' reason="not rung"
  printf '{"id": "h%s", "kind": "tg-out", "start": %s.5, "end": {"reason": "%s", "by": "him", "why": null, "duration_s": %s}, "ring": %s, "outcome": %s, "answered": %s, "talked": true, "events": {"start": 1, "ring": 1, "him": %s, "end": 1}, "tools": %s, "idle": [], "results": [], "reply_ms": [], "closing": []}' \
    "$1" "$1" "$reason" "${5:-0}" "$ring" "$oc" "$ans" "${3:-0}" "${4:-[]}"; }
hub() { FAKE_HUB="{\"calls\": [$1], \"outgoing\": {}, \"via\": \"hub\"}"; export FAKE_HUB; }
reset() { find "$T/state" -mindepth 1 -delete 2>/dev/null; : > "$T/calls.txt"; unset FAKE_HUB; }

# 1. office morning with traffic: one live call, say text <= 200 chars, marked before ringing
cfg live; reset
where true '"2026-10-12T07:50:00+02:00"' home null
tick 2026-10-12T07:51:00+02:00
check "office call placed" '[ "$(ncalls)" = 1 ]'
check "traffic text" 'grep -q "Traffic to Worktown: about 60 minutes, 12 more than usual" "$T/calls.txt"'
check "reason marker" 'grep -q -- "--reason drive briefing --say" "$T/calls.txt"'
check "say <= 200" '[ "$(last | jq ".text|length")" -le 200 ]'
check "outcome answered" '[ "$(last | jq -r .outcome)" = answered ]'
check "pending follow-up" '[ "$(jq ".pending|length" "$T/state/state.json")" = 1 ]'
# 1b. a drive flag in an answer older than 60 min (core.drive, `driving`'s rule): no drive, no decision, no call
cfg live; reset
where true '"2026-10-12T07:50:00+02:00"' home null
FAKE_WHERE=$(printf '%s' "$FAKE_WHERE" | jq -c '.age_min = 120'); export FAKE_WHERE
tick 2026-10-12T07:51:00+02:00
check "stale drive flag: no call, no decision" '[ "$(ncalls)" = 0 ] && ! grep -q decision "$T/state/log.jsonl" 2>/dev/null'
cfg live; reset
where true '"2026-10-12T07:50:00+02:00"' home null
tick 2026-10-12T07:51:00+02:00
# 2. same drive again, then a new leg after a 4-min stop (same chain): no second call
tick 2026-10-12T07:52:00+02:00
where false null away '"Midtown"' null '"near"'; tick 2026-10-12T07:55:00+02:00
where true '"2026-10-12T07:57:00+02:00"' away '"Midtown"' null '"near"'; tick 2026-10-12T07:58:00+02:00
check "one call per chain" '[ "$(ncalls)" = 1 ]'
check "no decision for same chain" '[ "$(grep -c "\"kind\": \"decision\"" "$T/state/log.jsonl")" = 1 ]'
# 3. a later drive with nothing to say: skip
where true '"2026-10-12T12:00:00+02:00"' away '"Worktown"' '"Work"' '"region"'; tick 2026-10-12T12:01:00+02:00
check "nothing to say" '[ "$(last | jq -r .why)" = "nothing to say" ] && [ "$(ncalls)" = 1 ]'
# 4. evening from work, decisions waiting, small traffic: decisions only on a long drive (once a day)
export FAKE_DECK='{"items": [{"options": ["a", "b"]}, {"options": ["x", "y"]}]}'
where true '"2026-10-12T17:00:00+02:00"' zone null '"Work"' '"region"'; tick 2026-10-12T17:01:00+02:00
check "decisions alone on a long drive" '[ "$(last | jq -r .decisions_alone)" = true ] && grep -q "2 decisions are waiting" "$T/calls.txt"'
where false null home null; tick 2026-10-12T18:00:00+02:00
where true '"2026-10-12T18:30:00+02:00"' zone null '"Work"' '"region"'; tick 2026-10-12T18:31:00+02:00
check "decisions alone once a day" '[ "$(last | jq -r .why)" = "nothing to say" ]'
unset FAKE_DECK
# 5. night, stale start, recent decline, day cap
reset; where true '"2026-10-13T01:30:00+02:00"' home null; tick 2026-10-13T01:31:00+02:00
check "night skip" '[ "$(last | jq -r .why)" = night ] && [ "$(ncalls)" = 0 ]'
reset; where true '"2026-10-13T07:30:00+02:00"' home null; tick 2026-10-13T07:50:00+02:00
check "late skip" '[ "$(last | jq -r .why)" = "noticed late" ]'
reset; now=$(date -d 2026-10-13T07:40:00+02:00 +%s)
hub "$(hubcall "$now" declined)"
where true '"2026-10-13T07:50:00+02:00"' home null; tick 2026-10-13T07:51:00+02:00
check "declined skip" '[ "$(last | jq -r .why)" = "declined a call recently" ] && [ "$(ncalls)" = 0 ]'
reset; cfg live 1
where true '"2026-10-13T07:50:00+02:00"' home null; tick 2026-10-13T07:51:00+02:00
where false null away '"Worktown"'; tick 2026-10-13T09:00:00+02:00
where true '"2026-10-13T16:00:00+02:00"' away '"Worktown"' null '"region"'
echo '{"Worktown|Hometown": {"live": 70, "usual": 46}}' > "$T/routes2.json"
DRIVE_BRIEF_ROUTES=$T/routes2.json tick 2026-10-13T16:01:00+02:00
check "day cap" '[ "$(last | jq -r .why)" = "day cap" ] && [ "$(ncalls)" = 1 ]'
# 6. calendar: event ahead (arrive / leave by), event behind a train leg skipped, parcel entry
cfg dry-run; reset
cat > "$T/ev.json" <<'EOF'
{"events": [
 {"id": "e1", "status": "confirmed", "summary": "Dance class", "location": "Eventville", "start": {"dateTime": "2026-10-14T21:00:00+02:00"}, "end": {"dateTime": "2026-10-14T22:00:00+02:00"}},
 {"id": "e2", "status": "confirmed", "summary": "🚆 Hometown → Trainville", "location": "Hometown", "start": {"dateTime": "2026-10-14T14:00:00+02:00"}, "end": {"dateTime": "2026-10-14T14:30:00+02:00"}},
 {"id": "e3", "status": "confirmed", "summary": "Concert", "location": "Trainville", "start": {"dateTime": "2026-10-14T15:00:00+02:00"}, "end": {"dateTime": "2026-10-14T17:00:00+02:00"}},
 {"id": "e4", "status": "confirmed", "transparency": "transparent", "summary": "📦 Pick up parcel (post)", "start": {"date": "2026-10-14"}, "end": {"date": "2026-10-15"}}
]}
EOF
CALCACHE_NOW=2026-10-14T00:00:00+02:00 "$HOME/bin/calcache" import "$T/ev.json" --days 3 >/dev/null 2>&1
where true '"2026-10-14T20:05:00+02:00"' home null; tick 2026-10-14T20:06:00+02:00
check "event eta" 'last | jq -r .text | grep -q "Dance class at 21:00 in Eventville: Waze says 47 minutes, so you.d arrive around 20:53"'
check "dry run, no ring" '[ "$(last | jq -r .outcome)" = dry_run ] && [ "$(ncalls)" = 0 ]'
check "parcel entry" 'last | jq -r .text | grep -q "On today.s list: Pick up parcel"'
out=$(DRIVE_BRIEF_NOW=2026-10-14T18:30:00+02:00 FAKE_WHERE="$FAKE_WHERE" "$BIN" now --json)
check "leave by" 'echo "$out" | jq -r .text | grep -q "leave by 20:03"'
out=$(DRIVE_BRIEF_NOW=2026-10-14T13:20:00+02:00 "$BIN" now --json)
check "train-leg event not a drive" '[ "$(echo "$out" | jq -r .dest_why)" != event ]'
# 7. reminders due during the drive (his own only), parcel mail on the way unless he stopped there
echo '[{"at": "2026-10-15 08:20", "text": "Call the garage about tyres", "owner": true, "id": "a1"},
       {"at": "2026-10-15 08:10", "text": "internal follow-up", "owner": false, "id": "a2"}]' > "$T/remind/reminders.json"
echo '["2026-10-14T16:00:00+02:00"]' > "$T/mail.json"
reset; where true '"2026-10-15T07:50:00+02:00"' home null
out=$(DRIVE_BRIEF_NOW=2026-10-15T07:51:00+02:00 "$BIN" now --json)
check "own reminder" 'echo "$out" | jq -r .text | grep -q "Reminder at 08:20: Call the garage about tyres"'
check "not the assistant one" '! echo "$out" | jq -r .text | grep -q internal'
check "parcel mail on the way" 'echo "$out" | jq -r .text | grep -q "A parcel may be waiting in Parceldorf"'
check "priority cap" '[ "$(echo "$out" | jq .chars)" -le 200 ]'
cat > "$T/ha.jsonl" <<'EOF'
{"cursor": 1, "ts": "2026-10-14T17:00:00+02:00", "label": "drive", "ha": {"state": "start"}}
{"cursor": 2, "ts": "2026-10-14T17:20:00+02:00", "label": "drive", "ha": {"state": "end", "minutes": 20}}
{"cursor": 3, "ts": "2026-10-14T17:21:00+02:00", "label": "zone", "ha": {"place": "away", "band": "near", "town": "Parceldorf"}}
{"cursor": 4, "ts": "2026-10-14T17:40:00+02:00", "label": "zone", "ha": {"place": "home", "zone": "Home"}}
EOF
out=$(DRIVE_BRIEF_HA_EVENTS=$T/ha.jsonl DRIVE_BRIEF_NOW=2026-10-15T07:51:00+02:00 "$BIN" now --json)
check "picked up: no parcel" '! echo "$out" | jq -r .text | grep -q Parceldorf'
# 8. replay: drive chains from HA events, at most one briefing per chain, no calls
cat > "$T/ha2.jsonl" <<'EOF'
{"cursor": 1, "ts": "2026-10-15T07:40:00+02:00", "label": "zone", "ha": {"place": "home", "zone": "Home"}}
{"cursor": 2, "ts": "2026-10-15T07:50:00+02:00", "label": "drive", "ha": {"state": "start"}}
{"cursor": 3, "ts": "2026-10-15T07:53:00+02:00", "label": "drive", "ha": {"state": "end", "minutes": 3}}
{"cursor": 4, "ts": "2026-10-15T07:56:00+02:00", "label": "drive", "ha": {"state": "start"}}
{"cursor": 5, "ts": "2026-10-15T08:56:00+02:00", "label": "drive", "ha": {"state": "end", "minutes": 60}}
{"cursor": 6, "ts": "2026-10-15T08:58:00+02:00", "label": "zone", "ha": {"place": "zone", "zone": "Work", "band": "region"}}
EOF
: > "$T/calls.txt"
out=$(DRIVE_BRIEF_HA_EVENTS=$T/ha2.jsonl DRIVE_BRIEF_TODO_AT=/bin/false DRIVE_BRIEF_NOW=2026-10-16T09:00:00+02:00 "$BIN" replay --days 7 --json)
check "replay one chain" '[ "$(echo "$out" | jq .drives)" = 1 ] && [ "$(echo "$out" | jq ".rows[0].legs")" = 2 ]'
check "replay traffic proxy" 'echo "$out" | jq -r ".rows[0].text" | grep -q "Traffic to Worktown: about 60 minutes, 12 more"'
check "replay never rings" '[ "$(ncalls)" = 0 ]'
# 9. follow-ups from the call log (counts only), stats
reset; cfg live
where true '"2026-10-16T07:50:00+02:00"' home null; tick 2026-10-16T07:51:00+02:00
s=$(date -d 2026-10-16T07:51:10+02:00 +%s)
hub "$(hubcall "$s" answered 2 '[["weather", "fast", null, false], ["end_call", "local", null, false]]' 31)"
where false null away '"Worktown"'; tick 2026-10-16T08:10:00+02:00
check "follow-up logged" 'grep "\"kind\": \"followup\"" "$T/state/log.jsonl" | jq -e ".turns == 2 and .tools == 1 and .tool_names == [\"weather\"]" >/dev/null'
check "no text in log" '! grep -q "weather\"" "$T/state/log.jsonl" || ! grep -q "what is" "$T/state/log.jsonl"'
st=$(DRIVE_BRIEF_NOW=2026-10-16T09:00:00+02:00 "$BIN" stats --json --days 3)
check "stats answered" '[ "$(echo "$st" | jq .answered_rate)" = 1 ] && [ "$(echo "$st" | jq .followup_calls)" = 1 ]'
# 10. kill switches: DRIVE_BRIEF=off and mode off
reset; where true '"2026-10-19T07:50:00+02:00"' home null
DRIVE_BRIEF=off tick 2026-10-19T07:51:00+02:00
check "env kill switch" '[ "$(ncalls)" = 0 ] && [ ! -f "$T/state/log.jsonl" ]'
cfg off; tick 2026-10-19T07:51:00+02:00
check "mode off" '[ "$(ncalls)" = 0 ] && [ ! -f "$T/state/log.jsonl" ]'
"$BIN" mode dry-run >/dev/null
check "mode verb" '[ "$(jq -r .mode "$T/config.json")" = dry-run ]'

# 11. navigation offer: right after a located event ahead (not here, not home, within nav_offer_h); switches
cfg live; reset
cat > "$T/ev2.json" <<'EOF'
{"events": [
 {"id": "n1", "status": "confirmed", "summary": "Concert night", "location": "Eventville", "start": {"dateTime": "2026-10-21T21:00:00+02:00"}, "end": {"dateTime": "2026-10-21T23:00:00+02:00"}},
 {"id": "n2", "status": "confirmed", "summary": "Team dinner", "location": "Hometown", "start": {"dateTime": "2026-10-22T19:00:00+02:00"}, "end": {"dateTime": "2026-10-22T21:00:00+02:00"}}
]}
EOF
CALCACHE_NOW=2026-10-21T00:00:00+02:00 "$HOME/bin/calcache" import "$T/ev2.json" --days 3 >/dev/null 2>&1
where true '"2026-10-21T20:00:00+02:00"' home null; tick 2026-10-21T20:01:00+02:00
check "nav offer after the event" 'last | jq -r .text | grep -q "Concert night at 21:00 in Eventville: .*Want navigation to Concert night?"'
check "nav item kept" '[ "$(last | jq -c .items)" = "[\"event\",\"nav\"]" ] && [ "$(last | jq .chars)" -le 200 ]'
check "nav offer spoken" 'grep -q "Want navigation to Concert night?" "$T/calls.txt"'
s=$(date -d 2026-10-21T20:01:10+02:00 +%s)
hub "$(hubcall "$s" answered 1 '[["navigate", "phone", null, false]]' 20)"
where false null away '"Eventville"'; tick 2026-10-21T20:20:00+02:00
st=$(DRIVE_BRIEF_NOW=2026-10-21T21:00:00+02:00 "$BIN" stats --json --days 1)
check "stats nav offered + taken" '[ "$(echo "$st" | jq .nav_offers)" = 1 ] && [ "$(echo "$st" | jq .nav_taken)" = 1 ]'
where true '"2026-10-21T20:00:00+02:00"' home null
out=$(DRIVE_BRIEF_NAV_OFFER=off DRIVE_BRIEF_NOW=2026-10-21T20:01:00+02:00 "$BIN" now --json)
check "env switch off" 'echo "$out" | jq -r .text | grep -q "Concert night" && ! echo "$out" | jq -r .text | grep -q navigation'
echo '{"mode": "dry-run", "nav_offer": false}' > "$T/config.json"
out=$(DRIVE_BRIEF_NOW=2026-10-21T20:01:00+02:00 "$BIN" now --json)
check "config switch off" '! echo "$out" | jq -r .text | grep -q navigation'
echo '{"mode": "dry-run", "nav_offer_h": 1}' > "$T/config.json"
out=$(DRIVE_BRIEF_NOW=2026-10-21T18:30:00+02:00 "$BIN" now --json)
check "too far ahead: no offer" 'echo "$out" | jq -r .text | grep -q "leave by" && ! echo "$out" | jq -r .text | grep -q navigation'
cfg dry-run
out=$(DRIVE_BRIEF_NOW=2026-10-21T18:30:00+02:00 "$BIN" now --json)
check "within 3 h: offer with leave by" 'echo "$out" | jq -r .text | grep -q "leave by.*Want navigation to Concert night?"'
out=$(FAKE_PHONE_RC=2 DRIVE_BRIEF_NOW=2026-10-21T18:30:00+02:00 "$BIN" now --json)
check "phone open missing: no offer" 'echo "$out" | jq -r .text | grep -q "leave by" && ! echo "$out" | jq -r .text | grep -q navigation'
where true '"2026-10-21T20:00:00+02:00"' away '"Eventville"' null '"region"'
out=$(DRIVE_BRIEF_NOW=2026-10-21T20:01:00+02:00 "$BIN" now --json)
check "already there: no offer" '! echo "$out" | jq -r .text | grep -q navigation'
where true '"2026-10-22T17:30:00+02:00"' zone null '"Work"' '"region"'
out=$(DRIVE_BRIEF_NOW=2026-10-22T17:31:00+02:00 "$BIN" now --json)
check "event in the home town: no offer" '[ "$(echo "$out" | jq -r .dest_why)" = event ] && ! echo "$out" | jq -r .text | grep -q navigation'

# 12. the hub's call log (claude-hub-voice; voice.calls through voice-hub)
reset; cfg live; unset FAKE_HUB_RC
s=$(date -d 2026-10-13T07:40:00+02:00 +%s)
hub "$(hubcall "$s" declined)"; where true '"2026-10-13T07:50:00+02:00"' home null; tick 2026-10-13T07:51:00+02:00
check "hub: declined skip" '[ "$(last | jq -r .why)" = "declined a call recently" ] && [ "$(ncalls)" = 0 ]'
reset; hub "$(hubcall "$s" no_answer)"; tick 2026-10-13T07:51:00+02:00
check "hub: missed skip" '[ "$(last | jq -r .why)" = "declined a call recently" ]'
reset; hub "$(hubcall "$s" answered 3)"; tick 2026-10-13T07:51:00+02:00
check "hub: answered call is no skip" '[ "$(last | jq -r .why)" != "declined a call recently" ]'
reset; hub "$(hubcall "$(date -d 2026-10-13T07:10:00+02:00 +%s)" declined)"; tick 2026-10-13T07:51:00+02:00
check "hub: older than 30 min is no skip" '[ "$(last | jq -r .why)" != "declined a call recently" ]'
reset; hub "$(hubcall "$s" -)"; tick 2026-10-13T07:51:00+02:00
check "hub: a call that never rang is no skip" '[ "$(last | jq -r .why)" != "declined a call recently" ]'
reset; FAKE_HUB_RC=1 tick 2026-10-13T07:51:00+02:00
check "hub down: no skip" '[ "$(last | jq -r .why)" != "declined a call recently" ]'
reset; hub "$(hubcall "$s" declined)"; DRIVE_BRIEF_CALLS_CMD=off DRIVE_BRIEF_NOW=2026-10-13T07:51:00+02:00 "$BIN" tick --cron
check "hub off: not asked" '[ "$(last | jq -r .why)" != "declined a call recently" ]'
# follow-ups from the hub: the first outbound call right after the decision; hub down -> retried, not lost
reset; cfg live; hub ""
where true '"2026-10-16T07:50:00+02:00"' home null; tick 2026-10-16T07:51:00+02:00
check "hub: briefing placed" '[ "$(ncalls)" = 1 ]'
where false null away '"Worktown"'
FAKE_HUB_RC=1 tick 2026-10-16T08:05:00+02:00
check "hub down: follow-up kept pending" '! grep -q "\"kind\": \"followup\"" "$T/state/log.jsonl" && [ "$(jq ".pending | length" "$T/state/state.json")" = 1 ]'
s=$(date -d 2026-10-16T07:51:10+02:00 +%s)
hub "$(hubcall "$s" answered 2 '[["weather", "fast", null, false], ["end_call", "local", null, false]]' 31)"
tick 2026-10-16T08:10:00+02:00
check "hub: follow-up logged" 'grep "\"kind\": \"followup\"" "$T/state/log.jsonl" | jq -e ".found and .turns == 2 and .tools == 1 and .tool_names == [\"weather\"] and .duration_s == 31" >/dev/null'
check "hub: pending cleared" '[ "$(jq ".pending | length" "$T/state/state.json")" = 0 ]'
reset; cfg live; hub ""
where true '"2026-10-16T07:50:00+02:00"' home null; tick 2026-10-16T07:51:00+02:00
where false null away '"Worktown"'; tick 2026-10-16T08:10:00+02:00
check "hub: no call found -> found false" 'grep "\"kind\": \"followup\"" "$T/state/log.jsonl" | jq -e ".found == false" >/dev/null'
unset FAKE_HUB

echo "drive-brief tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
