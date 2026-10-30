#!/usr/bin/env bash
# trackid tests: fake recogniser (no network), generated audio. TRACKID_BIN=/copy to test a copy;
# RAVE_RADAR_BIN=/copy for the crate hand-off.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
BIN=${TRACKID_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/trackid}
RR=${RAVE_RADAR_BIN:-$HOME/bin/rave-radar}
T=$(mktemp -d "${TMPDIR:-/tmp}/trackid-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
export TRACKID_STATE_DIR="$T/state" TRACKID_DEEZER_API=off RAVE_RADAR_CONFIG_DIR="$HERE/fixtures"
export TRACKID_RECOGNIZER="python3 $HERE/fixtures/fake_recognizer.py" FAKE_LOG="$T/calls.log"
pass=0; fail=0
ok() { pass=$((pass+1)); }
no() { fail=$((fail+1)); echo "FAIL: $*"; }
run() { rm -f "$FAKE_LOG"; FAKE_PLAN=$1; export FAKE_PLAN; shift; out=$("$BIN" "$@" 2>&1); code=$?; }
has() { grep -qF -- "$1" <<<"$out" && ok || no "$2: expected '$1' in: $out"; }
calls() { local n; n=$(wc -l <"$FAKE_LOG" 2>/dev/null || echo 0); [ "$n" -eq "$1" ] && ok || no "$2: $n calls, want $1"; }

# generated clips: 60 s video with audio, 10 s voice note (opus), 3 s clip, video without audio
ffmpeg -v error -f lavfi -i "color=c=black:s=160x120:r=10:d=60" -f lavfi -i "sine=f=330:d=60" \
  -filter_complex "[1:a]volume='if(between(t,30,42),1,0.2)':eval=frame[a]" -map 0:v -map "[a]" \
  -c:v libx264 -preset ultrafast -c:a aac -shortest "$T/club.mp4"
ffmpeg -v error -f lavfi -i "sine=f=440:d=10" -c:a libopus "$T/voice.oga"
ffmpeg -v error -f lavfi -i "sine=f=440:d=3" "$T/short.wav"
ffmpeg -v error -f lavfi -i "color=c=black:s=160x120:r=10:d=5" -c:v libx264 -preset ultrafast "$T/silent.mp4"

# 1. all three windows agree → high, crate + follow buttons, state 0600
run msm,msm,msm "$T/club.mp4"
[ $code -eq 0 ] && ok || no "match exit $code"
has "🎵 Kevin MacLeod – Monkeys Spinning Monkeys" match
has "Confidence high: 3 of 3 tries" match
has "Label Test Label" match
has "⭐ Kevin MacLeod is one of your artists" artist
has 'rr:crate:tid-' crate-button
grep -qF 'tid:follow' <<<"$out" && no "follow button for a followed artist" || ok
calls 3 match
[ "$(stat -c %a "$TRACKID_STATE_DIR/ids.json")" = 600 ] && ok || no "ids.json not 0600"
tid=$(grep -o 'tid-[0-9a-f]\{8\}' <<<"$out" | head -1)

# 2. loudest window is the louder 30-42 s part, picked first
run msm,none,none "$T/club.mp4" --json
python3 -c "import json,sys; d=json.load(sys.stdin); w=d['windows'][0]; assert w['label']=='loudest' and 28<=w['start']<=31, w" <<<"$out" && ok || no "loudest window: $out"

# 3. one match, re-check confirms → high; re-check fails → low + warning
run none,msm,none,msm "$T/club.mp4"
has "Confidence high: 2 of 4 tries" recheck-ok; calls 4 recheck-ok
run none,msm,none,none "$T/club.mp4"
has "🎵 Maybe: Kevin MacLeod" recheck-fail; has "Confidence low: 1 of 4 tries" recheck-fail; has "may be a false match" recheck-fail

# 4. windows disagree (DJ transition) → low + also heard
run msm,alt,msm "$T/club.mp4"
has "Confidence high: 2 of 3 tries" transition; has "Also heard: Somebody Else – Other Tune" transition
run alt,msm,none,none "$T/club.mp4"
has "Confidence low" disagree

# 5. no match → 3 + 2 extra windows, exit 1, honest card
run none,none,none,none,none "$T/club.mp4"
[ $code -eq 1 ] && ok || no "no-match exit $code"
has "No match (5 tries" no-match; calls 5 no-match
run none,none,none "$T/club.mp4" --windows 3
calls 3 windows-cap

# 6. lookup failures → exit 3; partial failure still matches
run err,err,err,err,err "$T/club.mp4"
[ $code -eq 3 ] && ok || no "error exit $code"; has "lookup failed" error
run err,msm,msm "$T/club.mp4"
has "Confidence high: 2 of 3 tries" partial-error

# 7. remix credit by a followed artist; not followed → follow button; short names match whole words only
run remix,remix,remix "$T/club.mp4"
has "remix by Kevin MacLeod" remix; has 'tid:follow:tid-' follow-button; has 'Follow Somebody Else' follow-name
run alt,alt,alt "$T/club.mp4"
has "Not one of your followed artists." not-followed

# 8. short voice note = one window; too short / no audio = exit 2
run msm "$T/voice.oga"
has "Confidence medium: 1 of 1 tries" voice-note; calls 1 voice-note
run msm "$T/short.wav"; [ $code -eq 2 ] && ok || no "short exit $code"; has "too short" short
run msm "$T/silent.mp4"; [ $code -eq 2 ] && ok || no "silent exit $code"; has "no audio" silent
run msm "$T/missing.mp4"; [ $code -eq 2 ] && ok || no "missing exit $code"

# 9. list / show; same track again counts as seen before
run msm,msm,msm "$T/club.mp4"; has "(identified before)" seen-before
out=$("$BIN" list); has "$tid" list; has "Kevin MacLeod – Monkeys Spinning Monkeys" list
out=$("$BIN" show "rr:crate:$tid" --json); has '"shazam_url"' show

# 10. rave-radar's ➕ Crate flow takes the trackid id
out=$(RAVE_RADAR_STATE_DIR="$T/rr" "$RR" releases crate "rr:crate:$tid" --dry-run 2>&1)
has "lists add crate 'Kevin MacLeod – Monkeys Spinning Monkeys'" rr-crate
out=$(RAVE_RADAR_STATE_DIR="$T/rr" "$RR" releases crate tid-00000000 --dry-run 2>&1); has "unknown release" rr-unknown

# 12. --source tags the stored id (the voice relay's "call"); --min-store medium keeps unsure matches out
run msm,msm,msm "$T/club.mp4" --json --source call
python3 -c "import json,sys; d=json.load(sys.stdin); assert d['stored'] and d['id'].startswith('tid-'), d" <<<"$out" && ok || no "source stored: $out"
sid=$(python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" <<<"$out")
out=$("$BIN" show "$sid" --json); has '"source": "call"' source-show
rm -f "$FAKE_LOG"; out=$(TRACKID_STATE_DIR="$T/state2" FAKE_PLAN=alt,msm,none,none "$BIN" "$T/club.mp4" --json --source call --min-store medium 2>&1); code=$?
python3 -c "import json,sys; d=json.load(sys.stdin); assert d['status']=='match' and d['confidence']['level']=='low' and not d['stored'] and d['id'] is None and d['buttons']==[], d" <<<"$out" && ok || no "min-store low kept out: $out"
[ ! -e "$T/state2/ids.json" ] && ok || no "min-store: ids.json written"
run msm,msm,msm "$T/club.mp4" --source 'Call!'
[ $code -eq 2 ] && ok || no "bad source exit $code"

# 11. set mode (own script: tone mix + fake recogniser)
TRACKID_BIN="$BIN" "$HERE/set.sh" || no "set.sh"

echo "trackid tests: $pass passed, $fail failed"
[ $fail -eq 0 ]
