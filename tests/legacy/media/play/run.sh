#!/usr/bin/env bash
# Offline tests for `media play` (staged copy). Part 1, dry run: Emby GETs only (a logging fake Emby and a
# logging ha stub prove no POST and no ha call), env overrides, refusals, JSON, the tonight button gate.
# Part 2, the real path against localhost fakes only (fake_ha.py + fake_emby.py): wake over the wake entity or the
# TV's own turn_on, select_source, session pick, resume ticks, read-back, exit 3 when playback never starts.
# Part 3, the target by bitrate (devices.json fixtures): dry-run choices, --target, a fast LAN device, the hint.
# Usage: tests/run.sh   (MEDIA_BIN=... to test another copy). Exit 0 = all pass.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
MEDIA=${MEDIA_BIN:-$HERE/../media}
_pf=/home/node/opt/profile-tests
[ -f "$HERE/../../profile/fixtures/profile.json" ] && _pf=$HERE/../../profile
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
T=$(mktemp -d "${TMPDIR:-/tmp}/play-tests.XXXXXX")
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && printf '%s\n' "$2" | head -20 | sed 's/^/    /'; }
has()  { grep -qF -- "$2" <<<"$1"; }
check(){ if eval "$2"; then ok; else bad "$1" "${OUT:-}"; fi; }

echo ok > "$T/scenario"
cat > "$T/ha.json" <<'EOF'
{"_tv": "media_player.test_tv", "_wake": "media_player.test_box",
 "media_player.test_tv": {"state": "unavailable", "attributes": {"supported_features": 24377}},
 "media_player.test_box": {"state": "off", "attributes": {"supported_features": 153529}}}
EOF
cp "$T/ha.json" "$T/ha.orig.json"
python3 -I "$HERE/fake_emby.py" "$T/port" "$T/emby.log" "$T/ha.json" "$T/scenario" 2>"$T/emby.err" &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
printf '{"seerr": {"url": "%s", "key": "SEERR-KEY-Zq81"}, "emby": {"url": "%s", "key": "EMBY-KEY-Pt55"}}\n' "$B" "$B" > "$T/cfg.json"
printf '{"seerr": {"url": "%s", "key": "SEERR-KEY-Zq81"}}\n' "$B" > "$T/nokey.json"
cat > "$T/ha-stub" <<EOF
#!/bin/sh
echo "\$*" >> "$T/stub.log"
exit 1
EOF
chmod +x "$T/ha-stub"
printf '#!/bin/sh\nexec python3 -I %s "$@"\n' "$HERE/fake_ha.py" > "$T/ha-fake"; chmod +x "$T/ha-fake"

export MEDIA_SECRETS="$T/cfg.json" MEDIA_HA_BIN="$T/ha-stub" MEDIA_PLAY_CONFIG="$T/absent.json" \
       MEDIA_PLAY_LOCK="$T/play.lock" MEDIA_TIMEOUT=5 FAKE_HA_STATE="$T/ha.json" FAKE_HA_LOG="$T/ha.log" \
       MEDIA_PLAY_DEVICES="$T/nodevices.json"
unset MEDIA_PLAY_TV MEDIA_PLAY_WAKE MEDIA_PLAY_APP MEDIA_PLAY_DEVICE MEDIA_PLAY_CLIENT
mkplay() {  # mkplay FILE ENABLED [extra json]
  printf '{"enabled": %s, "tv": "media_player.test_tv", "wake": "media_player.test_box", "client": "Emby for LG", "device": "TESTTV"%s}\n' \
    "$2" "${3:-}" > "$1"
}
mkplay "$T/off.json" false
mkplay "$T/on.json" true
mkplay "$T/on-noresume.json" true ', "resume": false'
printf '{"enabled": "yes", "tv": "media_player.test_tv", "device": "TESTTV"}\n' > "$T/strenabled.json"
printf 'not json' > "$T/broken.json"
untouched() { [ ! -s "$T/emby.log" ] && [ ! -s "$T/stub.log" ] && [ ! -s "$T/ha.log" ]; }
nosend() { ! grep -q '^POST' "$T/emby.log" && ! grep -q 'token=bad' "$T/emby.log" && [ ! -s "$T/stub.log" ] && [ ! -s "$T/ha.log" ]; }
noleak() { ! grep -qE 'EMBY-KEY|SEERR-KEY' <<<"$OUT"; }

# ---------- part 1: dry run (no network, no ha) ----------
OUT=$("$MEDIA" play 555 --dry-run 2>&1); RC=$?
check "dry run, config absent: exit 0" '[ $RC = 0 ]'
check "dry run, config absent: says real mode refuses" 'has "$OUT" "real mode refuses" && has "$OUT" "is absent"'
check "dry run, config absent: names missing tv/device" 'has "$OUT" "no tv set" && has "$OUT" "no device set"'
check "dry run no POST, no ha" nosend
check "dry run prints no key" noleak

OUT=$(MEDIA_PLAY_TV=media_player.env_tv MEDIA_PLAY_WAKE=remote.env_box MEDIA_PLAY_DEVICE=ENVTV MEDIA_PLAY_CLIENT="Emby for LG" \
      "$MEDIA" play 555 --dry-run 2>&1); RC=$?
check "env overrides: exit 0" '[ $RC = 0 ]'
check "env overrides: TV state read on env TV" 'has "$OUT" "ha state media_player.env_tv --json"'
check "env overrides: wake via remote.turn_on on env wake" 'has "$OUT" "ha call remote.turn_on remote.env_box --wait 3"'
check "env overrides: select_source with JSON quoted" "has \"\$OUT\" \"ha call media_player.select_source media_player.env_tv '{\\\"source\\\": \\\"Emby\\\"}' --wait 3\""
check "env overrides: session match by env device/client" "has \"\$OUT\" \"Client ~ 'Emby for LG' and DeviceName ~ 'ENVTV'\""
check "env overrides: still refuses real mode (env can't enable)" 'has "$OUT" "real mode refuses"'
check "env overrides: emby base url from config" 'has "$OUT" "GET  $B/emby/Items?Ids=555&Recursive=true&Fields=RunTimeTicks"'
check "env overrides: POST PlayNow with resume" 'has "$OUT" "POST $B/emby/Sessions/{session.Id}/Playing?ItemIds=555&PlayCommand=PlayNow[&StartPositionTicks={resume}]"'
check "env overrides: read-back step" 'has "$OUT" "GET  $B/emby/Sessions?Id={session.Id}"'
check "env overrides: no POST, no ha" nosend

OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" "$MEDIA" play 555 --dry-run 2>&1); RC=$?
check "enabled config: dry run says enabled" 'has "$OUT" "enabled, real mode runs"'
check "enabled config: 11 steps" '[ "$(grep -cE "^ ?[0-9]+\. " <<<"$OUT")" = 11 ]'
check "enabled config: dry run no POST, no ha" nosend
OUT=$(MEDIA_PLAY_CONFIG="$T/off.json" "$MEDIA" play 555 --dry-run 2>&1)
check "enabled false: refuses, asks for enabled true" 'has "$OUT" "is present; needs \"enabled\": true"'
OUT=$(MEDIA_PLAY_CONFIG="$T/strenabled.json" "$MEDIA" play 555 --dry-run 2>&1)
check "enabled as a string: problem named" 'has "$OUT" "enabled must be a bool"'
OUT=$(MEDIA_PLAY_CONFIG="$T/broken.json" "$MEDIA" play 555 --dry-run 2>&1)
check "broken config: unreadable" 'has "$OUT" "is unreadable" && has "$OUT" "JSONDecodeError"'

OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" "$MEDIA" play 555 --dry-run --from-start 2>&1)
check "--from-start: no StartPositionTicks" '! has "$OUT" "StartPositionTicks"'
OUT=$(MEDIA_PLAY_CONFIG="$T/on-noresume.json" "$MEDIA" play 555 --dry-run 2>&1)
check "resume false: no StartPositionTicks" '! has "$OUT" "StartPositionTicks"'
OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" "$MEDIA" play 555 --dry-run --no-wake 2>&1)
check "--no-wake: no turn_on steps" '! has "$OUT" "turn_on" && [ "$(grep -cE "^ ?[0-9]+\. " <<<"$OUT")" = 8 ]'
OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" MEDIA_PLAY_WAKE="" "$MEDIA" play 555 --dry-run 2>&1)
check "empty env leaves the config value" 'has "$OUT" "media_player.turn_on media_player.test_box"'

OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" "$MEDIA" play 555 --dry-run --json 2>&1); RC=$?
check "--json dry run: valid, 11 steps, flags" 'python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert d[\"dry_run\"] and d[\"enabled\"] and d[\"emby_key\"] and d[\"item\"]==\"555\" and len(d[\"steps\"])==11 and d[\"probe\"] is None and d[\"tv\"]==\"media_player.test_tv\"" "$OUT"'
check "--json dry run: no key" noleak
check "--json dry run: no POST, no ha" nosend

OUT=$(MEDIA_PLAY_TV="light.kitchen" "$MEDIA" play 555 --dry-run 2>&1)
check "TV must be a media_player" 'has "$OUT" "tv is not valid"'
OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" MEDIA_PLAY_WAKE='media_player.x;reboot' "$MEDIA" play 555 --dry-run 2>&1)
check "wake entity with shell chars refused" 'has "$OUT" "wake is not valid" && has "$OUT" "real mode refuses"'
check "dry runs so far: no POST, no ha" nosend
: > "$T/emby.log"
OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" MEDIA_PLAY_WAKE='light.x' "$MEDIA" play 555 2>&1); RC=$?
check "bad wake entity: real mode exit 2" '[ $RC = 2 ] && has "$OUT" "wake is not valid"'
for id in '12;id' 'abc' '' '1234567890123'; do
  OUT=$("$MEDIA" play "$id" --dry-run 2>&1); RC=$?
  check "bad item id [$id]: exit 2" '[ $RC = 2 ] && has "$OUT" "ITEM_ID must be"'
done
check "refusals touched nothing" untouched

# real mode refusals: nothing sent
OUT=$("$MEDIA" play 555 2>&1); RC=$?
check "real, config absent: exit 2" '[ $RC = 2 ] && has "$OUT" "play is staged"'
OUT=$(MEDIA_PLAY_CONFIG="$T/off.json" "$MEDIA" play 555 2>&1); RC=$?
check "real, enabled false: exit 2" '[ $RC = 2 ] && has "$OUT" "now: present"'
OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" MEDIA_SECRETS="$T/nokey.json" "$MEDIA" play 555 2>&1); RC=$?
check "real, no emby key: exit 2" '[ $RC = 2 ] && has "$OUT" "no emby url/key"'
OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" "$MEDIA" play 555 --probe 2>&1); RC=$?
check "--probe without --dry-run: exit 2" '[ $RC = 2 ]'
check "real-mode refusals touched nothing" untouched

# tonight card: the TV button only when play is enabled
TB='import importlib.machinery as m, importlib.util as u, json, sys
s = u.spec_from_loader("media", m.SourceFileLoader("media", sys.argv[1])); mod = u.module_from_spec(s); s.loader.exec_module(mod)
picks = [{"slot": "A", "title": "Test Movie", "link": "http://x/web/index.html#!/item?id=555", "emby_id": "555", "in_library": True},
         {"slot": "B", "title": "Show", "episode": "S01E02", "link": None, "emby_id": "556", "in_library": True},
         {"slot": "C", "title": "New", "in_library": False, "request": {"data": "mt:req:9"}}]
print(json.dumps(mod.tn_buttons(picks)))'
OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" python3 -c "$TB" "$MEDIA" 2>&1)
check "tonight, play on: button next to the link" 'python3 -c "import json,sys; r=json.loads(sys.argv[1]); assert r[0]==[{\"text\": \"\u25b6 Test Movie\", \"url\": \"http://x/web/index.html#!/item?id=555\"}, {\"text\": \"\U0001f4fa Play on TV\", \"data\": \"mt:play:555\"}]" "$OUT"'
check "tonight, play on: own row without a link" 'python3 -c "import json,sys; r=json.loads(sys.argv[1]); assert r[1]==[{\"text\": \"\U0001f4fa Play on TV\", \"data\": \"mt:play:556\"}]" "$OUT"'
check "tonight, play on: none for the request pick" '[ "$(grep -o "mt:play:" <<<"$OUT" | wc -l)" = 2 ]'
OUT=$(MEDIA_PLAY_CONFIG="$T/off.json" python3 -c "$TB" "$MEDIA" 2>&1)
check "tonight, play off: no TV button" '! has "$OUT" "mt:play:" && has "$OUT" "mt:more"'
OUT=$(python3 -c "$TB" "$MEDIA" 2>&1)
check "tonight, config absent: no TV button" '! has "$OUT" "mt:play:"'

# ---------- part 2: real path against the localhost fakes ----------
export MEDIA_HA_BIN="$T/ha-fake" MEDIA_PLAY_CONFIG="$T/on.json" MEDIA_PLAY_POLL=0.1 MEDIA_PLAY_WAIT=3 MEDIA_PLAY_CONFIRM=2
reset() { cp "$T/ha.orig.json" "$T/ha.json"; : > "$T/ha.log"; : > "$T/emby.log"; echo "${1:-ok}" > "$T/scenario"
          : > "$T/scenario.fast"; }

reset
OUT=$("$MEDIA" play 555 2>&1); RC=$?
check "real: exit 0" '[ $RC = 0 ]'
check "real: playing with resume" 'has "$OUT" "▶ Playing Test Movie on the TV (from 60 min)"'
check "real: woke via the wake entity (TV has no turn_on)" 'grep -qx "call media_player.turn_on media_player.test_box --wait 3" "$T/ha.log" && ! grep -q "turn_on media_player.test_tv" "$T/ha.log"'
check "real: opened Emby" "grep -qxF 'call media_player.select_source media_player.test_tv {\"source\": \"Emby\"} --wait 3' \"\$T/ha.log\""
check "real: order wake < select_source" '[ "$(grep -n turn_on "$T/ha.log" | cut -d: -f1)" -lt "$(grep -n select_source "$T/ha.log" | cut -d: -f1)" ]'
check "real: PlayNow on the matching session with resume ticks" 'grep -qx "POST /emby/Sessions/s1/Playing?ItemIds=555&PlayCommand=PlayNow&StartPositionTicks=36000000000 token=ok" "$T/emby.log"'
check "real: exactly one POST" '[ "$(grep -c ^POST "$T/emby.log")" = 1 ]'
check "real: every Emby call had the token" '! grep -q "token=bad" "$T/emby.log"'
check "real: item checked before any HA call" 'grep -q "^GET /emby/Items?Ids=555" <<<"$(head -1 "$T/emby.log")"'
check "real: no key in output" noleak

reset
OUT=$("$MEDIA" play 555 --from-start --json 2>&1); RC=$?
check "real --from-start --json: ok, no resume" '[ $RC = 0 ] && python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert d[\"ok\"] and d[\"sent\"] and d[\"resume_min\"]==0 and d[\"title\"]==\"Test Movie\"" "$OUT"'
check "real --from-start: POST without StartPositionTicks" 'grep -qx "POST /emby/Sessions/s1/Playing?ItemIds=555&PlayCommand=PlayNow token=ok" "$T/emby.log"'

reset
python3 - "$T/ha.json" <<'EOF'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["media_player.test_tv"] = {"state": "on", "attributes": {"supported_features": 24377, "source": "Emby"}}
json.dump(d, open(p, "w"))
EOF
OUT=$("$MEDIA" play 556 2>&1); RC=$?
check "real, TV already in Emby: no HA writes" '[ $RC = 0 ] && ! grep -q "^call" "$T/ha.log"'
check "real, episode from the start (no resume point)" 'grep -qx "POST /emby/Sessions/s1/Playing?ItemIds=556&PlayCommand=PlayNow token=ok" "$T/emby.log"'

reset
python3 - "$T/ha.json" <<'EOF'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["media_player.test_tv"]["attributes"]["supported_features"] = 24377 | 128
json.dump(d, open(p, "w"))
EOF
OUT=$("$MEDIA" play 555 2>&1); RC=$?
check "real, TV supports turn_on: wakes the TV itself" '[ $RC = 0 ] && grep -qx "call media_player.turn_on media_player.test_tv --wait 3" "$T/ha.log" && ! grep -q "test_box" "$T/ha.log"'

reset
OUT=$("$MEDIA" play 555 --no-wake 2>&1); RC=$?
check "real --no-wake, TV off: exit 1, nothing sent" '[ $RC = 1 ] && has "$OUT" "--no-wake" && ! grep -q "^call" "$T/ha.log" && ! grep -q ^POST "$T/emby.log"'

reset
OUT=$("$MEDIA" play 777 2>&1); RC=$?
check "real, a series id: exit 2 before any HA call" '[ $RC = 2 ] && has "$OUT" "is a Series" && [ ! -s "$T/ha.log" ]'
reset
OUT=$("$MEDIA" play 999 2>&1); RC=$?
check "real, unknown item: exit 2" '[ $RC = 2 ] && has "$OUT" "no Emby item 999" && [ ! -s "$T/ha.log" ]'

reset noplay
OUT=$("$MEDIA" play 555 2>&1); RC=$?
check "real, playback never starts: exit 3" '[ $RC = 3 ] && has "$OUT" "isn'"'"'t playing yet"'

reset
python3 - "$T/ha.json" <<'EOF'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["_wake_works"] = False; json.dump(d, open(p, "w"))
EOF
OUT=$("$MEDIA" play 555 2>&1); RC=$?
check "real, TV never wakes: exit 1 after the wait, no POST" '[ $RC = 1 ] && has "$OUT" "waiting for the TV to turn on" && ! grep -q ^POST "$T/emby.log"'

reset
OUT=$(MEDIA_PLAY_DEVICE=OTHERTV "$MEDIA" play 555 2>&1); RC=$?
check "real, no matching session: exit 1, no POST" '[ $RC = 1 ] && has "$OUT" "Emby session" && ! grep -q ^POST "$T/emby.log"'

reset
exec 9>"$T/play.lock"; flock -n 9
OUT=$("$MEDIA" play 555 2>&1); RC=$?
exec 9>&-
check "real, another play running: exit 2, nothing sent" '[ $RC = 2 ] && has "$OUT" "another play" && [ ! -s "$T/ha.log" ] && [ ! -s "$T/emby.log" ]'

# ---------- part 3: the target by bitrate (devices.json) ----------
cat > "$T/dev.json" <<'EOF'
{"TESTTV": {"lan_mbit": 100, "label": "the slow TV"},
 "FASTBOX": {"lan_mbit": 1000, "client": "Emby for Android", "label": "the fast box"}}
EOF
python3 - "$T" <<'EOF'
import json, sys
T = sys.argv[1]
d = json.load(open(f"{T}/dev.json")); d["FASTBOX"]["source"] = "HDMI2"; json.dump(d, open(f"{T}/dev-src.json", "w"))
d = json.load(open(f"{T}/dev.json")); d["TESTTV"]["prefer_over_mbit"] = 10; json.dump(d, open(f"{T}/dev-prefer.json", "w"))
json.dump({"TESTTV": {"lan_mbit": 100, "label": "the slow TV"}}, open(f"{T}/dev-one.json", "w"))
json.dump({"TESTTV": {"lan_mbit": "fast"}, "x;y": {}, "FASTBOX": {"lan_mbit": 1000, "source": "HDMI2;reboot"}},
          open(f"{T}/dev-bad.json", "w"))
EOF
printf 'not json' > "$T/dev-broken.json"
export MEDIA_PLAY_DEVICES="$T/dev.json"
fast() { echo "$1" > "$T/scenario.fast"; }
gets() { grep -c '^GET' "$T/emby.log"; }
I558="GET /emby/Items?Ids=558&Recursive=true&Fields=RunTimeTicks,MediaSources token=ok"
OVER="85 Mbit is above the slow TV's 80 Mbit limit"
HINT="4K remux 85 Mbit: may stutter on the slow TV — the fast box handles it"

reset
OUT=$("$MEDIA" play 558 --dry-run 2>&1); RC=$?
EXP="target: TESTTV ($OVER; no faster device is online)"
check "choice, fast device offline: the default device" '[ $RC = 0 ] && has "$OUT" "$EXP"'
check "choice, fast device offline: stutter hint" 'has "$OUT" "hint: $HINT"'
check "choice: item and sessions read, no POST, no ha" 'nosend && [ "$(gets)" = 2 ] && grep -qxF "$I558" "$T/emby.log" && grep -qx "GET /emby/Sessions token=ok" "$T/emby.log"'

reset; fast lan
OUT=$("$MEDIA" play 558 --dry-run 2>&1); RC=$?
EXP="target: FASTBOX ($OVER; the fast box is online)"
check "choice, fast device online on the LAN: chosen" '[ $RC = 0 ] && has "$OUT" "$EXP" && ! has "$OUT" "hint:"'
EXP="Client ~ 'Emby for Android' and DeviceName ~ 'FASTBOX' from the LAN shows up"
check "choice: plan waits for the fast device's LAN session" 'has "$OUT" "$EXP"'
check "choice: no select_source without a source" '! has "$OUT" "select_source"'
check "choice, fast device: no POST, no ha" nosend

reset; fast wan
OUT=$("$MEDIA" play 558 --dry-run 2>&1)
EXP="target: TESTTV ($OVER; no faster device is online)"
check "choice: a matching session from outside the LAN doesn't count" 'has "$OUT" "$EXP" && has "$OUT" "hint: $HINT"'

reset; fast lan
OUT=$("$MEDIA" play 555 --dry-run 2>&1)
check "choice, 20 Mbit item: fits the default device, sessions not read" 'has "$OUT" "target: TESTTV (20 Mbit fits the slow TV (limit 80))" && [ "$(gets)" = 1 ]'
OUT=$(MEDIA_PLAY_CONFIG="$T/on.json" "$MEDIA" play 555 --dry-run 2>&1)
check "with other devices: 12 steps (the choice's sessions read)" '[ "$(grep -cE "^ ?[0-9]+\. " <<<"$OUT")" = 12 ]'
OUT=$(MEDIA_PLAY_DEVICES="$T/dev-prefer.json" "$MEDIA" play 555 --dry-run 2>&1)
check "prefer_over_mbit 10: a 20 Mbit item goes to the fast device" 'has "$OUT" "target: FASTBOX (20 Mbit is above the slow TV'"'"'s 10 Mbit limit; the fast box is online)"'

reset; fast lan
OUT=$("$MEDIA" play 555 --dry-run --target "The Fast Box" 2>&1); RC=$?
check "--target by label: chosen, no sessions read" '[ $RC = 0 ] && has "$OUT" "target: FASTBOX (--target)" && [ "$(gets)" = 1 ]'
OUT=$("$MEDIA" play 558 --dry-run --target testtv 2>&1)
check "--target the slow device for a remux: hint" 'has "$OUT" "target: TESTTV (--target)" && has "$OUT" "hint: $HINT"'
: > "$T/emby.log"
OUT=$("$MEDIA" play 558 --dry-run --target nope 2>&1); RC=$?
check "--target unknown: exit 2, names the devices, nothing read" '[ $RC = 2 ] && has "$OUT" "unknown --target '"'"'nope'"'"' (known: TESTTV, FASTBOX)" && [ ! -s "$T/emby.log" ]'
OUT=$("$MEDIA" play 558 --target nope 2>&1); RC=$?
check "--target unknown, real mode: exit 2, nothing sent" '[ $RC = 2 ] && [ ! -s "$T/emby.log" ] && [ ! -s "$T/ha.log" ]'

reset; fast lan
OUT=$("$MEDIA" play 558 --dry-run --json 2>&1)
check "--json choice: target, label, mbit, reads" 'python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert d[\"target\"]==\"FASTBOX\" and d[\"target_label\"]==\"the fast box\" and d[\"primary\"] is False and d[\"mbit\"]==85.3 and d[\"hint\"] is None and len(d[\"emby_reads\"])==2 and d[\"devices\"]==[\"TESTTV\",\"FASTBOX\"]" "$OUT"'
check "--json choice: no key" noleak
OUT=$(MEDIA_PLAY_DEVICES="$T/dev-src.json" MEDIA_PLAY_CONFIG="$T/on.json" "$MEDIA" play 558 --dry-run 2>&1)
check "source set: plan switches the TV input" "has \"\$OUT\" \"ha call media_player.select_source media_player.test_tv '{\\\"source\\\": \\\"HDMI2\\\"}' --wait 3\""

reset
OUT=$(MEDIA_PLAY_DEVICES="$T/dev-one.json" "$MEDIA" play 558 --dry-run 2>&1)
check "no other device: no sessions read, hint without an alternative" 'has "$OUT" "hint: 4K remux 85 Mbit: may stutter on the slow TV" && ! has "$OUT" "handles it" && [ "$(gets)" = 1 ]'
OUT=$(MEDIA_SECRETS="$T/nokey.json" "$MEDIA" play 558 --dry-run 2>&1)
check "no Emby key: default device, no network" 'has "$OUT" "target: TESTTV (bitrate unknown: the default device)" && has "$OUT" "(no network)"'
OUT=$(MEDIA_PLAY_DEVICES="$T/dev-bad.json" "$MEDIA" play 558 --dry-run 2>&1); RC=$?
check "bad devices.json fields: named, skipped" '[ $RC = 0 ] && has "$OUT" "TESTTV: lan_mbit must be a positive number" && has "$OUT" "FASTBOX: source is not valid" && has "$OUT" "an entry is not valid" && has "$OUT" "target: TESTTV"'
OUT=$(MEDIA_PLAY_DEVICES="$T/dev-broken.json" "$MEDIA" play 558 --dry-run 2>&1); RC=$?
check "broken devices.json: named, default device" '[ $RC = 0 ] && has "$OUT" "dev-broken.json: JSONDecodeError" && has "$OUT" "target: TESTTV"'
check "choice dry runs: no POST, no ha" nosend

# real path with a fast device (fakes only)
reset; fast lan
OUT=$(MEDIA_PLAY_DEVICES="$T/dev-src.json" "$MEDIA" play 558 2>&1); RC=$?
check "real, remux + fast device online: plays there" '[ $RC = 0 ] && has "$OUT" "▶ Playing Big Remux on the fast box" && ! has "$OUT" "may stutter"'
check "real, fast device: woke the TV, switched to its input" 'grep -qx "call media_player.turn_on media_player.test_box --wait 3" "$T/ha.log" && grep -qxF "call media_player.select_source media_player.test_tv {\"source\": \"HDMI2\"} --wait 3" "$T/ha.log"'
check "real, fast device: one POST, to its session" '[ "$(grep -c ^POST "$T/emby.log")" = 1 ] && grep -qx "POST /emby/Sessions/f1/Playing?ItemIds=558&PlayCommand=PlayNow token=ok" "$T/emby.log"'

reset; fast lan
OUT=$("$MEDIA" play 558 --json 2>&1); RC=$?
check "real --json, fast device without a source: TV input left as is" '[ $RC = 0 ] && python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert d[\"ok\"] and d[\"target\"]==\"FASTBOX\" and d[\"primary\"] is False and d[\"hint\"] is None and any(\"left as is\" in x for x in d[\"log\"])" "$OUT" && ! grep -q select_source "$T/ha.log"'
check "real --json: no key" noleak

reset
OUT=$("$MEDIA" play 558 2>&1); RC=$?
check "real, remux, fast device offline: plays on the TV app with the hint" '[ $RC = 0 ] && has "$OUT" "▶ Playing Big Remux on the TV" && has "$OUT" "$HINT" && grep -qx "POST /emby/Sessions/s1/Playing?ItemIds=558&PlayCommand=PlayNow token=ok" "$T/emby.log"'
reset; fast wan
OUT=$("$MEDIA" play 558 --json 2>&1); RC=$?
check "real, only a WAN look-alike: TV app, hint in JSON, no POST to it" '[ $RC = 0 ] && python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert d[\"primary\"] and d[\"hint\"]==sys.argv[2]" "$OUT" "$HINT" && ! grep -q "Sessions/f1/Playing" "$T/emby.log"'

reset
OUT=$("$MEDIA" play 555 --target FASTBOX 2>&1); RC=$?
check "real --target, fast device offline: exit 1 after the wait, no POST" '[ $RC = 1 ] && has "$OUT" "the fast box'"'"'s Emby session" && ! grep -q ^POST "$T/emby.log"'

echo "play tests: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
