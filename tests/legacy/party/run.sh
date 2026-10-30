#!/usr/bin/env bash
# Offline tests for ~/bin/party (WLED party lights following the music's BPM). Fake ha (fake_ha) + one fake
# server for the WLED JSON API and the Deezer BPM API (fake_server.py); fictional artists and tracks only.
# PARTY_BIN=/copy tests a copy. Exit 0 = all pass.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PARTY=${PARTY_BIN:-/home/node/bin/party}
T=$(mktemp -d); pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }

cat > "$T/init.json" <<'J'
{"w2": {"on": false, "bri": 76, "ps": -1, "pl": -1, "seg": [{"id": 0, "start": 0, "stop": 30, "on": true, "bri": 255,
        "fx": 0, "sx": 24, "ix": 128, "pal": 0, "frz": false, "c1": 128, "o1": false,
        "col": [[255, 65, 4, 0], [0, 0, 0, 0], [0, 0, 0, 0]]}]},
 "w4": {"on": true, "bri": 150, "ps": -1, "pl": -1, "seg": [{"id": 0, "start": 0, "stop": 20, "on": true, "bri": 255,
        "fx": 9, "sx": 128, "ix": 100, "pal": 4, "frz": false, "col": [[255, 145, 22, 160], [0, 0, 0, 0], [0, 0, 0, 0]]},
        {"id": 1, "start": 20, "stop": 40, "on": true, "bri": 200, "fx": 2, "sx": 60, "ix": 128, "pal": 0, "frz": false,
        "col": [[0, 0, 255, 0], [0, 0, 0, 0], [0, 0, 0, 0]]}]}}
J
cat > "$T/ctrl.json" <<'J'
{"tracks": [{"id": 1, "title": "Night Drive", "artist": "Test Artist", "bpm": 0},
            {"id": 2, "title": "Night Drive (Radio Edit)", "title_short": "Night Drive", "artist": "Test Artist", "bpm": 127.98},
            {"id": 3, "title": "Fast Lane", "artist": "Speed Crew", "bpm": 174},
            {"id": 4, "title": "Slow Burn", "artist": "Low Tide", "bpm": 0},
            {"id": 5, "title": "Half Step", "artist": "Dub Unit", "bpm": 64}]}
J
python3 "$HERE/fake_server.py" "$T" & SRV=$!
trap 'kill $SRV 2>/dev/null; find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
PORT=$(cat "$T/port"); B="http://127.0.0.1:$PORT"
mkha() {  # mkha ARTIST TITLE [STATE] [OTHER_STATE]: the fake HA with one Spotify player and one Sonos speaker
  python3 - "$T/ha.json" "$B" "$@" <<'PY'
import json, sys
path, b, artist, title = sys.argv[1:5]
state = sys.argv[5] if len(sys.argv) > 5 else 'playing'
other = sys.argv[6] if len(sys.argv) > 6 else 'playing'
attrs = {'media_artist': artist, 'media_title': title} if artist else {}
json.dump({'lights': [
    {'entity_id': 'light.wled_2', 'name': 'WLED Kitchen', 'state': 'off', 'area': 'Living Room', 'attributes': {}},
    {'entity_id': 'light.wled_4', 'name': 'WLED Shelf', 'state': 'on', 'area': 'Living Room', 'attributes': {}},
    {'entity_id': 'light.wled_5', 'name': 'WLED Bed', 'state': 'unavailable', 'area': 'Bedroom', 'attributes': {}},
    {'entity_id': 'light.lamp', 'name': 'Lamp', 'state': 'on', 'area': 'Living Room', 'attributes': {}}],
  'players': [{'entity_id': 'media_player.speaker_a', 'name': 'Speaker A', 'state': other,
               'attributes': {'media_artist': 'Wrong Player', 'media_title': 'Ignore Me'}},
              {'entity_id': 'media_player.spotify_test', 'name': 'Spotify', 'state': state, 'attributes': attrs}],
  'urls': {'light.wled_2': b + '/w2', 'light.wled_4': b + '/w4', 'light.lamp': b + '/nolamp'}}, open(path, 'w'))
PY
}
P() { PARTY_HA="$HERE/fake_ha" FAKE_HA_DIR="$T" PARTY_STATE_DIR="$T/state" PARTY_CACHE="$T/cache/bpm.json" \
      PARTY_CONFIG="$T/config.json" PARTY_INBOX="$T/inbox" PARTY_BPM_API="$B/dz" PARTY_POLL=0.2 "$PARTY" "$@"; }
req() {  # req METHOD PATH [BODY]: the fake server's control API (python, not curl: the CI runner has none)
  python3 -c 'import sys, http.client as h
c = h.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=10)
c.request(sys.argv[2], sys.argv[3], body=sys.argv[4].encode() if len(sys.argv) > 4 else None)
sys.stdout.write(c.getresponse().read().decode())' "$PORT" "$@"
}
ws() { req GET "/_get/$1" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(eval(sys.argv[1]))' "$2"; }
fresh() { req POST /_reset >/dev/null; find "$T/state" "$T/inbox" -mindepth 1 -delete 2>/dev/null; : > "$T/server.log"; : > "$T/calls.log"; }
writes() { grep -c "^$1 " "$T/server.log"; }
ctl() { python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); c.update(json.loads(sys.argv[2])); json.dump(c, open(sys.argv[1], "w"))' "$T/ctrl.json" "$1"; }

echo "# idle"
mkha "Test Artist" "Night Drive - Radio Edit"; fresh
eq "status: none" "$(P status)" "No party running."
eq "status --json: off" "$(P status --json)" '{"on": false}'
P tick --cron --loop 5; eq "tick without a party: rc 0" "$?" "0"
eq "tick without a party: no ha call" "$(cat "$T/calls.log")" ""
eq "off without a party" "$(P off)" "No party running."
P bogus >/dev/null 2>&1; eq "unknown command: rc 2" "$?" "2"

echo "# dry run"
out=$(P on -n); rc=$?
eq "dry on: rc 0" "$rc" "0"
has "dry on: shows the Kitchen write" "$out" '[dry-run] light.wled_2 POST /json/state'
has "dry on: 128 BPM from the radio edit" "$out" '128 BPM (Deezer), palette Party'
eq "dry on: no WLED write" "$(writes w2)$(writes w4)" "00"
eq "dry on: no state" "$(ls "$T/state/state.json" 2>/dev/null)" ""

echo "# on"
fresh; out=$(P on); rc=$?
eq "on: rc 0" "$rc" "0"
has "on: names both live WLEDs" "$out" "🎉 Party on · Kitchen + Shelf"
has "on: BPM and palette" "$out" "♪ 128 BPM (Deezer) · Party palette"
has "on: until + off hint" "$out" "/party off restores"
hasnt "on: no track name in the answer" "$out" "Night Drive"
eq "Kitchen: on, Bpm fx, speed = BPM, Party palette" "$(ws w2 's["on"], s["seg"][0]["fx"], s["seg"][0]["sx"], s["seg"][0]["pal"]')" "(True, 68, 128, 6)"
eq "Kitchen was off: config bri default 128" "$(ws w2 's["bri"]')" "128"
eq "Shelf was on: brightness kept" "$(ws w4 's["bri"]')" "150"
eq "Shelf: both segments follow" "$(ws w4 '[(g["fx"], g["sx"], g["pal"]) for g in s["seg"]]')" "[(68, 128, 6), (68, 128, 6)]"
eq "unavailable WLED Bed and the non-WLED lamp untouched" "$(grep -c 'wled_5\|light.lamp' "$T/calls.log")" "0"
has "Kitchen write: one-time transition, no default change" "$(grep '^w2 ' "$T/server.log")" '"tt": 7'
hasnt "Kitchen write: never 'transition'" "$(grep '^w2 ' "$T/server.log")" '"transition"'
eq "state file 0600" "$(stat -c %a "$T/state/state.json")" "600"
eq "Spotify player preferred over another playing one" "$(grep -c 'Wrong Player' "$T/server.log")" "0"
hasnt "no track names in state/log/cache" "$(cat "$T/state/state.json" "$T/state/party.log" "$T/cache/bpm.json")" "Night"
hasnt "no artist names in state/log/cache" "$(cat "$T/state/state.json" "$T/state/party.log" "$T/cache/bpm.json")" "Test Artist"
out=$(P on); has "on twice: already on" "$out" "🎉 Party already on · Kitchen + Shelf"
eq "on twice: no new write" "$(writes w2)" "1"
has "status: on" "$(P status)" "♪ 128 BPM (Deezer) · Party palette · until"
eq "status --json" "$(P status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["on"], d["bpm"], sorted(d["lights"]))')" "True 128 ['light.wled_2', 'light.wled_4']"

echo "# following the music"
: > "$T/server.log"; P tick --cron; eq "same track: no write" "$(cat "$T/server.log")" ""
mkha "Speed Crew feat. Guest" "Fast Lane"; P tick --cron
eq "new track 174 BPM: speed + Rainbow" "$(ws w2 's["seg"][0]["sx"], s["seg"][0]["pal"]')" "(174, 11)"
hasnt "track change: never sends on" "$(grep '^w2 ' "$T/server.log")" '"on"'
hasnt "track change: never sends bri" "$(grep '^w2 ' "$T/server.log")" '"bri"'
mkha "Dub Unit" "Half Step"; P tick --cron
eq "64 BPM doubled to 128" "$(ws w4 's["seg"][1]["sx"]')" "128"
mkha "Low Tide" "Slow Burn"; P tick --cron
eq "tempo unknown: default 125, Party" "$(ws w2 's["seg"][0]["sx"], s["seg"][0]["pal"]')" "(125, 6)"
has "status: unknown source" "$(P status)" "125 BPM (tempo unknown, default)"
mkha "Nobody Known" "Night Drive"; P tick --cron
eq "artist not found: default, not another band's BPM" "$(ws w2 's["seg"][0]["sx"]')" "125"
mkha "Dub Unit" "Half Step" paused idle; : > "$T/server.log"; P tick --cron
eq "paused: no change" "$(cat "$T/server.log")" ""
mkha "Dub Unit" "Half Step"; P tick --cron
eq "known BPM comes from the cache" "$(grep -c 'dz ' "$T/server.log")" "0"
req POST /_set/w4 '{"seg": [{"id": 0, "fx": 2}]}' >/dev/null
mkha "Speed Crew" "Fast Lane"; : > "$T/server.log"; P tick --cron
eq "he changed the Shelf effect: Shelf released, no write" "$(writes w4)" "0"
eq "Kitchen still follows" "$(ws w2 's["seg"][0]["sx"]')" "174"
has "status marks the released light" "$(P status)" "Shelf (yours again)"
mkha "Test Artist" "Night Drive - Radio Edit"; P tick --cron --loop 0.5 & LP=$!
sleep 0.15; P tick --cron; eq "overlapping tick returns at once" "$?" "0"; wait $LP

echo "# off"
out=$(P off); rc=$?
eq "off: rc 0" "$rc" "0"
eq "off: restored message" "$out" "💡 Party off · Kitchen + Shelf restored"
eq "Kitchen back to the snapshot (off, Solid, speed 24, palette 0, bri 76)" "$(ws w2 's["on"], s["bri"], s["seg"][0]["fx"], s["seg"][0]["sx"], s["seg"][0]["pal"], s["seg"][0]["col"][0]')" "(False, 76, 0, 24, 0, [255, 65, 4, 0])"
eq "explicit off restores the released Shelf too" "$(ws w4 's["on"], s["bri"], [(g["fx"], g["sx"], g["ix"], g["pal"]) for g in s["seg"]]')" "(True, 150, [(9, 128, 100, 4), (2, 60, 128, 0)])"
eq "off: state gone" "$(ls "$T/state/state.json" 2>/dev/null)" ""
eq "status after off" "$(P status)" "No party running."

echo "# end of the party (4 h)"
fresh; mkha "Test Artist" "Night Drive - Radio Edit"; P on --hours 4 >/dev/null
req POST /_set/w4 '{"on": false}' >/dev/null   # he switched the Shelf off at 01:00
out=$(PARTY_NOW=$(( $(date +%s) + 3 * 3600 )) P tick --cron); eq "before the end: silent, still on" "$out$(P status --json | cut -c1-11)" '{"on": true'
out=$(PARTY_NOW=$(( $(date +%s) + 4 * 3600 + 60 )) P tick --cron); rc=$?
eq "after 4 h: silent tick, rc 0" "$out$rc" "0"
eq "after 4 h: Kitchen restored (was off)" "$(ws w2 's["on"], s["seg"][0]["fx"]')" "(False, 0)"
eq "after 4 h: the Shelf he switched off stays off, effect restored" "$(ws w4 's["on"], s["seg"][0]["fx"], s["bri"]')" "(False, 9, 150)"
eq "after 4 h: state gone" "$(P status)" "No party running."
fresh; P on >/dev/null; req POST /_set/w4 '{"seg": [{"id": 0, "fx": 3}, {"id": 1, "fx": 3}]}' >/dev/null
mkha "Speed Crew" "Fast Lane"; P tick --cron; : > "$T/server.log"
PARTY_NOW=$(( $(date +%s) + 5 * 3600 )) P tick --cron
eq "auto end leaves a released light alone" "$(writes w4)" "0"
eq "--hours capped at 8" "$(fresh; P on --hours 99 >/dev/null; python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(round((s["until"] - s["started"]) / 3600))' "$T/state/state.json")" "8"
P off >/dev/null

echo "# failures"
fresh; ctl '{"down": ["w4"]}'; out=$(P on); rc=$?
eq "one WLED down: rc 0" "$rc" "0"
has "one WLED down: the other runs" "$out" "🎉 Party on · Kitchen"
has "one WLED down: named" "$out" "⚠️ Shelf:"
eq "the down one is not in the party" "$(P status --json | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)["lights"]))')" "['light.wled_2']"
ctl '{"down": ["w2"]}'; out=$(P off); rc=$?
eq "restore fails: rc 1" "$rc" "1"
has "restore fails: retry promised" "$out" "Kitchen: HTTPError; retrying every minute"
for i in 1 2 3; do PARTY_NOW=$(( $(date +%s) + 60 * i )) P tick --cron; done
eq "still retrying after 4 tries: no note yet" "$(ls "$T/inbox" 2>/dev/null)" ""
P tick --cron; eq "5th failure: one inbox note" "$(ls "$T/inbox" | grep -c '^party-restore-failed-')" "1"
eq "5th failure: light dropped" "$(P status)" "No party running."
ctl '{"down": []}'
fresh; ctl '{"down": ["w2", "w4"]}'; out=$(P on); rc=$?
eq "all WLEDs down: rc 1" "$rc" "1"
has "all WLEDs down: nothing changed" "$out" "No WLED answered: nothing changed"
eq "all WLEDs down: no state" "$(ls "$T/state/state.json" 2>/dev/null)" ""
ctl '{"down": [], "ignore": ["w2", "w4"]}'; out=$(P on); rc=$?
eq "writes not applied: rc 1" "$rc" "1"
has "writes not applied: says so" "$out" "Party lights failed"
eq "writes not applied: no state left" "$(ls "$T/state/state.json" 2>/dev/null)" ""
ctl '{"ignore": []}'
fresh; mkha "Test Artist" "Night Drive"; python3 -c 'import json,sys; h=json.load(open(sys.argv[1])); h["fail"]=True; json.dump(h, open(sys.argv[1],"w"))' "$T/ha.json"
out=$(P on); rc=$?; eq "HA down: rc 1" "$rc" "1"; has "HA down: message" "$out" "⚠️ Party: ha list: error: cannot reach"
mkha "" "" idle idle; fresh; out=$(P on); has "nothing playing: default" "$out" "♪ 125 BPM (nothing playing, default)"
P off >/dev/null
echo '{"lights": ["light.wled_4"], "bri": 40, "default_bpm": 140, "palettes": [[999, "Lava"]]}' > "$T/config.json"
fresh; out=$(P on); has "config lights only" "$out" "🎉 Party on · Shelf"
eq "config palette + default BPM" "$(ws w4 's["seg"][0]["sx"], s["seg"][0]["pal"]')" "(140, 8)"
eq "config lights: Kitchen untouched" "$(writes w2)" "0"
P off >/dev/null; rm -f "$T/config.json"

echo "# bpm lookup"
fresh; eq "bpm: exact version preferred, 0-BPM entries skipped" "$(P bpm "Test Artist" "Night Drive - Radio Edit" --json)" '{"bpm": 127.98, "speed": 128, "source": "cache"}'
rm -f "$T/cache/bpm.json"; eq "bpm: fresh lookup" "$(P bpm "Test Artist" "Night Drive - Radio Edit")" "127.98 BPM (deezer), effect speed 128"
eq "bpm: unknown cached for a day" "$(P bpm "Low Tide" "Slow Burn"; : > "$T/server.log"; P bpm "Low Tide" "Slow Burn"; grep -c 'dz ' "$T/server.log")" "BPM unknown (unknown)
BPM unknown (unknown)
0"
eq "bpm: unknown retried after a day" "$(PARTY_NOW=$(( $(date +%s) + 90000 )) P bpm "Low Tide" "Slow Burn" >/dev/null; grep -c 'dz search' "$T/server.log")" "1"
ctl '{"dz_down": true}'; out=$(P bpm "Fresh Artist" "New Song"); rc=$?; ctl '{"dz_down": false}'
eq "bpm: API down: rc 1, not cached" "$out|$rc|$(grep -c '' "$T/cache/bpm.json" >/dev/null; python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$T/cache/bpm.json")" "BPM unknown (error)|1|2"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
