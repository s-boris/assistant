#!/usr/bin/env bash
# trackid set tests: a generated 15 min "mix" of tones, a fake recogniser that hears the tone (no network),
# a fake trackinfo. TRACKID_BIN=/copy to test a copy.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
BIN=${TRACKID_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/trackid}
T=$(mktemp -d "${TMPDIR:-/tmp}/trackid-set-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
export TRACKID_STATE_DIR="$T/state" TRACKID_DEEZER_API=off RAVE_RADAR_CONFIG_DIR="$HERE/fixtures"
export TRACKID_RECOGNIZER="python3 $HERE/fixtures/fake_set_recognizer.py" FAKE_LOG="$T/calls.log"
export TRACKID_TRACKINFO="python3 $HERE/fixtures/fake_trackinfo.py" TRACKINFO_STATE="$T/ti"
pass=0; fail=0
ok() { pass=$((pass+1)); }
no() { fail=$((fail+1)); echo "FAIL: $*"; }
run() { rm -f "$FAKE_LOG"; out=$("$BIN" set "$@" --pause 0 2>"$T/err"); code=$?; }
has() { grep -qF -- "$1" <<<"$out" && ok || no "$2: expected '$1' in: $out"; }
calls() { local n=0; [ -f "$FAKE_LOG" ] && n=$(wc -l <"$FAKE_LOG"); [ "$n" -eq "$1" ] && ok || no "$2: $n calls, want $1"; }
MAP='770=Fake|False Hit@0,330=Alpha DJ|First Light@-30,660=Alpha DJ|First Light@-30,440=Beta|Second Wave@170,880=none,550=Gamma|Third Eye@600,600=Delta|Short One@720,1000=err'

# mix (900 s, samples at 24+60i): A 0-180 (DJ started it 30 s in), B 180-420 with a 16 s A blip at 260,
# unknown 420-600 with a 12 s false-hit tone at 504 (its re-check hears mostly the unknown tone), C 600-720, D 720-780 (one sample), lookups fail 780-900
ffmpeg -v error -f lavfi -i "sine=f=330:d=180" -f lavfi -i "sine=f=440:d=80" -f lavfi -i "sine=f=660:d=16" \
  -f lavfi -i "sine=f=440:d=144" -f lavfi -i "sine=f=880:d=84" -f lavfi -i "sine=f=770:d=12" \
  -f lavfi -i "sine=f=880:d=84" -f lavfi -i "sine=f=550:d=120" -f lavfi -i "sine=f=600:d=60" \
  -f lavfi -i "sine=f=1000:d=120" -filter_complex "concat=n=10:v=0:a=1" -c:a libmp3lame -b:a 64k "$T/mix.mp3"
ffmpeg -v error -f lavfi -i "sine=f=330:d=3700" -c:a libopus -b:a 12k "$T/long.opus"
ffmpeg -v error -f lavfi -i "color=c=black:s=160x120:r=10:d=30" -c:v libx264 -preset ultrafast "$T/silent.mp4"

# 1. card: start times from the offset (clamped at 0 and after the previous track), blip dropped, ID? gap,
#    lone hits re-checked (Delta confirmed, the false hit dropped), failed tail = ID? (not checked) and exit 4 (incomplete)
export FAKE_SET="$MAP"
run "$T/mix.mp3"
[ $code -eq 4 ] && ok || no "partial exit $code"
has "🎧 Tracklist · mix.mp3 · 15:00 · 4 tracks, 2 ID?" header
has "0:00 Alpha DJ – First Light" t1; has "2:50 Beta – Second Wave" t2-offset
has "7:00 ID?" gap; has "10:00 Gamma – Third Eye" t3; has "12:00 Delta – Short One" single; grep -qF "Short One (?)" <<<"$out" && no "confirmed single marked (?)" || ok
grep -qF "False Hit" <<<"$out" && no "false hit kept" || ok
has "7:00 ID? (3 min)" gap-length; has "13:00 ID? (2 min, not checked)" unchecked; has "samples not checked" warn
grep -qF "Alpha DJ – First Light" <<<"$(grep -n Alpha <<<"$out" | sed -n 2p)" && no "blip kept" || ok
calls 17 first-run
grep -q 'Beta – Second Wave' "$T/state/sets/"*.log && ok || no "progress log missing"
grep -qF 'progress:' "$T/err" && ok || no "stderr progress pointer missing"
grep -qF '/15' <<<"$out" && no "progress on stdout" || ok
[ "$(stat -c %a "$T/state/sets")" = 700 ] && ok || no "sets dir not 0700"
ls "$T/state/sets/"*.json >/dev/null && [ "$(stat -c %a "$T/state/sets/"*.json)" = 600 ] && ok || no "cache not 0600"

# 2. rerun: hits/misses cached, only the 2 failed samples are retried; now they answer → complete, exit 0
export FAKE_SET="${MAP/1000=err/1000=Echo|Last Call@780}"
run "$T/mix.mp3"
calls 2 resume; [ $code -eq 0 ] && ok || no "resume exit $code"
has "13:00 Echo – Last Call" resumed
run "$T/mix.mp3"; calls 0 cached

# 3. --json, --text (1001tracklists), --info (+ trackinfo's last list kept), --store
run "$T/mix.mp3" --json
python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d['count']=={'tracks':5,'ids':1}, d['count']
assert {(n['match'], n.get('why')) for n in d['noise']} == {('Alpha DJ – First Light', None), ('Fake – False Hit', 'unconfirmed')}, d['noise']
assert d['tracks'][4]['confidence']=='medium' and d['tracks'][0]['confidence']=='high', d['tracks']
t=d['tracks']; assert t[1]['start']==170.0 and t[1]['start_from']=='offset' and t[1]['hits']==3, t[1]
assert t[0]['start']==0 and t[2]['kind']=='id' and d['complete'], t
" <<<"$out" && ok || no "json: $out"
run "$T/mix.mp3" --no-recheck
has "12:00 Delta – Short One (?)" no-recheck; has "False Hit (?)" no-recheck-false; calls 0 no-recheck
run "$T/mix.mp3" --text
has "01. [00:00] Alpha DJ - First Light [Test Label]" text; has "03. [07:00] ID - ID" text-id
mkdir -p "$T/ti"; echo '["kept"]' >"$T/ti/last-list.json"
run "$T/mix.mp3" --info
has "2:50 Beta – Second Wave · 121 BPM · 2A" info
[ "$(cat "$T/ti/last-list.json")" = '["kept"]' ] && ok || no "trackinfo last list not restored"
run "$T/mix.mp3" --store --json
grep -q '"tid": "tid-' <<<"$out" && ok || no "store: no tid"
grep -qF 'Third Eye' "$T/state/ids.json" && ok || no "store: not in ids.json"

# 4. step/window options; long recording → h:mm:ss; bad input
run "$T/mix.mp3" --step 30 --window 30 --json
python3 -c "import json,sys; d=json.load(sys.stdin); assert d['samples']==30 and d['window']==12.0, (d['samples'], d['window'])" <<<"$out" && ok || no "step 30: $out"
export FAKE_SET='330=Alpha DJ|First Light@0'
run "$T/long.opus" --step 600
has "1:01:40 · 1 tracks" long-header; has "0:00:00 Alpha DJ – First Light" long-time; calls 6 long
run "$T/long.opus" --step 600 --text; has "01. [00:00:00] Alpha DJ - First Light" long-text
run "$T/silent.mp4"; [ $code -eq 2 ] && ok || no "silent exit $code"
run "$T/nope.mp3"; [ $code -eq 2 ] && ok || no "missing exit $code"

# 5. every lookup fails → stops after --max-errors, exit 3
export FAKE_SET='330=err,660=err,440=err,880=err,550=err,600=err,1000=err'
run "$T/mix.mp3" --fresh --max-errors 3
[ $code -eq 3 ] && ok || no "all-fail exit $code"; calls 3 max-errors

# 6. --deep: a 46 s track hidden between the 60 s samples of an 8 min unknown stretch; deep samples every 20 s
#    find it (two agreeing hits), the stretch splits; short ID? stretches (< 6 min) are left alone
ffmpeg -v error -f lavfi -i "sine=f=330:d=120" -f lavfi -i "sine=f=880:d=157" -f lavfi -i "sine=f=500:d=46" \
  -f lavfi -i "sine=f=880:d=277" -f lavfi -i "sine=f=440:d=120" -filter_complex "concat=n=5:v=0:a=1" \
  -c:a libmp3lame -b:a 64k "$T/deep.mp3"
export FAKE_SET='330=Alpha DJ|First Light@0,880=none,500=Hidden|Gem@277,440=Beta|Second Wave@600'
run "$T/deep.mp3"
has "2:00 ID? (8 min)" deep-before; grep -qF "Hidden" <<<"$out" && no "coarse pass found the hidden track" || ok
run "$T/deep.mp3" --json
python3 -c "
import json,sys; d=json.load(sys.stdin); r=d['tracks'][1]
assert r['kind']=='id' and r['length_s']==480 and r['length']=='8 min' and r['until']=='10:00' and r['end']==600, r
assert d['deep'] is None, d['deep']
" <<<"$out" && ok || no "json length: $out"
run "$T/deep.mp3" --deep
has "1:51 ID? (3 min)" deep-gap1; has "4:37 Hidden – Gem" deep-found; grep -qF "Gem (?)" <<<"$out" && no "deep hit unconfirmed" || ok
has "5:20 ID? (5 min)" deep-gap2; has "10:00 Beta – Second Wave" deep-next; has "Deep: 16 extra samples (every 20 s) in 1 long ID? stretch" deep-footer
[ $code -eq 0 ] && ok || no "deep exit $code"; calls 16 deep-lookups
run "$T/deep.mp3" --deep --json; calls 0 deep-cached
python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d['deep']=={'step':20.0,'min':360.0,'stretches':1,'samples':16}, d['deep']
g=[t for t in d['tracks'] if t['kind']=='track' and t['title']=='Gem'][0]
assert g['confidence']=='high' and g['start']==277.0 and g['start_from']=='offset', g
" <<<"$out" && ok || no "deep json: $out"
run "$T/deep.mp3" --deep --text
has "03. [04:37] Hidden - Gem [Test Label]" deep-text; has "02. [01:51] ID - ID" deep-text-id
grep -qF "min" <<<"$out" && no "text has a length comment" || ok
export FAKE_SET="${MAP/1000=err/1000=Echo|Last Call@780}"
run "$T/mix.mp3" --deep; calls 0 deep-short; grep -qF "Deep:" <<<"$out" && no "short stretch deep-sampled" || ok

echo "trackid set tests: $pass passed, $fail failed"
[ $fail -eq 0 ]
