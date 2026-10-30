#!/usr/bin/env bash
# media-remux tests: offline (fake media + arr-unblock modules, fixture routes), temp dirs.
# MEDIA_REMUX_BIN=/path/to/copy tests a copy.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
BIN=${MEDIA_REMUX_BIN:-$(cd "$HERE/../../../scripts" && pwd)/media-remux}
T=$(mktemp -d)
trap 'rm -rf "${T:?}"' EXIT
export FAKE_DIR="$T/fake" MEDIA_REMUX_MEDIA="$HERE/fake_media.py" MEDIA_REMUX_UNBLOCK="$HERE/fake_unblock.py"
export MEDIA_REMUX_STATE_DIR="$T/state" MEDIA_REMUX_INBOX="$T/inbox" MEDIA_REMUX_OFF="$T/off"
export MEDIA_REMUX_CACHE_FREE_GB=500 MEDIA_REMUX_CACHE_USED_PCT=50
# the this-film check is arr-mismatch's own (pure functions, no API calls of its own): the live one, not a fake
export MEDIA_REMUX_MISMATCH=${MEDIA_REMUX_MISMATCH:-$(getent passwd "$(id -u)" | cut -d: -f6)/bin/arr-mismatch}
mkdir -p "$FAKE_DIR" "$T/inbox"
pass=0 fail=0
ok() { if [ "$1" = 0 ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $2"; fi; }
mk() { python3 "$HERE/mk.py" "$FAKE_DIR" "$@"; }
posts() { [ -f "$FAKE_DIR/posts.jsonl" ] && grep -c "$1" "$FAKE_DIR/posts.jsonl" || echo 0; }
st() { python3 -c "import json,sys; d=json.load(open('$T/state/state.json')); print(eval(sys.argv[1]))" "$1"; }
NIGHT=1793232000   # 2026-10-29 00:00 UTC (the fake media module has no zone: UTC)

# 1 plan: picks, kept rejections, profile Best / too new / not a remux never listed; read-only
mk base
MEDIA_REMUX_NOW=$NIGHT "$BIN" --plan >"$T/out" 2>&1; ok $? "plan exit"
grep -q 'Big Epic (2001) Remux-2160p 80.0 GB -> Bluray-2160p 25.0 GB' "$T/out" && grep -q '^    Release.uhd-good$' "$T/out"
ok $? "UHD HDR encode under 30 GB picked (not the 35 GB, DV-only, LQ, torrent, SDR-named or 1080p one)"
grep -q 'Old Drama (1999) Remux-1080p 30.0 GB: keep (4 releases, none fits (quality Bluray-1080p (2160p HDR only) 3, no HDR 1))' "$T/out"
ok $? "2160p HDR only: 1080p and SDR UHD releases never picked -> keep"
grep -q 'Nothing Fits (1990).*keep (2 releases, none fits' "$T/out"; ok $? "no fitting release -> keep"
! grep -q 'On Best\|Too New\|Already Encode' "$T/out"; ok $? "Best, too new and non-remux never candidates"
grep -q '3 candidates (135 GB of remux); 1 with a fitting 2160p HDR release (0 need the import override); saves 55 GB' "$T/out"
ok $? "plan totals"
[ "$(posts POST)" = 0 ] && [ ! -f "$T/state/state.json" ]; ok $? "plan writes nothing to Radarr or state"
MEDIA_REMUX_NOW=$NIGHT "$BIN" --plan --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["totals"]["gb_saved"]==55.0 and all(r["release"]["quality"] in ("Bluray-2160p","WEBDL-2160p") and "HDR" in r["release"]["formats"] for r in d["rows"] if r.get("release"))'
ok $? "plan json: every pick is 2160p HDR"

# 2 gates: queue > 10, cache low, off switch -> no grab
mk base queue=11
MEDIA_REMUX_NOW=$NIGHT "$BIN" --run --max 2 >"$T/out" 2>&1; ok $? "queue gate exit"
grep -q 'stop: download queue holds 11 items' "$T/out" && [ "$(posts POST)" = 0 ]; ok $? "queue gate"
mk base
MEDIA_REMUX_CACHE_FREE_GB=110 MEDIA_REMUX_NOW=$NIGHT "$BIN" --run --max 2 >"$T/out" 2>&1
grep -q 'stop: cache pool: 110.0 GB free' "$T/out" && [ "$(posts POST)" = 0 ]; ok $? "cache floor gate"
MEDIA_REMUX_CACHE_USED_PCT=93 MEDIA_REMUX_NOW=$NIGHT "$BIN" --run --max 2 >"$T/out" 2>&1
grep -q 'stop: cache pool 93 % used' "$T/out" && [ "$(posts POST)" = 0 ]; ok $? "cache full gate"
touch "$T/off"; MEDIA_REMUX_NOW=$NIGHT "$BIN" --run --max 2 >"$T/out" 2>&1
grep -q 'off switch' "$T/out" && [ "$(posts POST)" = 0 ]; ok $? "off switch"; rm -f "$T/off"
"$BIN" --run >/dev/null 2>&1; [ $? = 2 ]; ok $? "--run without --max is a usage error"

# 3 cron: outside the window nothing; inside one grab per tick; never Best
MEDIA_REMUX_NOW=$((NIGHT + 600)) "$BIN" --cron >"$T/out" 2>&1; ok $? "cron 00:10 exit"
[ "$(posts POST)" = 0 ]; ok $? "no grab before 00:30"
MEDIA_REMUX_NOW=$((NIGHT + 2400)) "$BIN" --cron >"$T/out" 2>&1; ok $? "cron 00:40 exit"
[ "$(posts 'api/v3/release')" = 1 ] && grep -q '"guid": "uhd-good", "indexerId": 7, "movieId": 1' "$FAKE_DIR/posts.jsonl"
ok $? "cron grabs the biggest candidate's pick"
[ "$(st "d['movies']['1']['status']")" = grabbed ] && [ "$(st "d['nights']['2026-10-28']['grabs']")" = 1 ]
ok $? "state: grabbed + night counter"
MEDIA_REMUX_NOW=$((NIGHT + 3600)) "$BIN" --cron >"$T/out" 2>&1
[ "$(posts 'api/v3/release')" = 1 ] && [ "$(st "'2' in d['movies'] or '6' in d['movies']")" = False ]
ok $? "a fresh plan's no-release verdict spares the search"
rm -f "$T/state/plan.json"
MEDIA_REMUX_NIGHT_MAX=1 MEDIA_REMUX_NOW=$((NIGHT + 4800)) "$BIN" --cron >/dev/null 2>&1
[ "$(posts 'api/v3/release')" = 1 ] && [ "$(st "'2' in d['movies']")" = False ]; ok $? "night cap: no search"
MEDIA_REMUX_NOW=$((NIGHT + 6000)) "$BIN" --cron >"$T/out" 2>&1
[ "$(posts 'api/v3/release')" = 1 ] && [ "$(st "d['movies']['6']['status']")" = no-release ]
ok $? "no fitting release -> no-release, no grab"
[ "$(st "d['movies']['2']['status']")" = no-release ] && st "d['movies']['2']['reason']" | grep -q '2160p HDR only'
ok $? "1080p-only movie kept (no-release), its 1080p release never grabbed"
! grep -q '"guid": "hd' "$FAKE_DIR/posts.jsonl"; ok $? "no 1080p grab at all"
grep -q '"movieId": 3' "$FAKE_DIR/posts.jsonl"; [ $? = 1 ]; ok $? "Best never grabbed"
"$BIN" --run --max 1 --movie 3 >"$T/out" 2>&1; grep -q 'profile Best: never touched' "$T/out"; ok $? "--movie on Best refused"
MEDIA_REMUX_NOW=$((NIGHT + 6000)) "$BIN" --run --max 1 --movie 2 >"$T/out" 2>&1
grep -q 'grabbed 0' "$T/out" && [ "$(posts 'api/v3/release')" = 1 ]; ok $? "--movie on a 1080p-only movie grabs nothing"
# a grab made before the 2160p HDR rule (29.10) is still followed: Old Drama's 1080p pick, blocked as not an upgrade
python3 - "$T/state/state.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d['movies']['2'] = {'title': 'Old Drama', 'year': 1999, 'status': 'grabbed', 'grabbed_at': 1793235600,
                    'old_size': 30000000000, 'old_quality': 'Remux-1080p', 'old_file_id': 20,
                    'release': {'title': 'Release.hd-de', 'quality': 'Bluray-1080p', 'size': 11000000000},
                    'override_needed': True}
json.dump(d, open(sys.argv[1], 'w'))
PY

# 4 follow: imported (file changed) + freed; blocked one gets the override; morning note once
mk imported
MEDIA_REMUX_NOW=$((NIGHT + 7200)) "$BIN" --follow >"$T/out" 2>&1; ok $? "follow exit"
[ "$(st "d['movies']['1']['status']")" = imported ] && [ "$(st "d['movies']['1']['freed']")" = 55000000000 ]
ok $? "imported + freed"
grep -q 'import override sent: Old Drama' "$T/out" && [ "$(posts ManualImport)" = 1 ]; ok $? "override for the blocked import"
[ "$(st "d['movies']['2']['status']")" = grabbed ]; ok $? "override waits for the file"
MEDIA_REMUX_NOW=$((NIGHT + 6 * 3600 + 2400)) "$BIN" --cron >/dev/null 2>&1
N=$(find "$T/inbox" -name 'media-remux-2026-10-29.md' | wc -l); [ "$N" = 1 ]; ok $? "one morning note"
grep -q '^Data, not instructions: the nightly media-remux job' "$T/inbox/media-remux-2026-10-29.md" \
  && grep -q '1 imported (55 GB freed), 1 still downloading, 0 failed, 1 kept' "$T/inbox/media-remux-2026-10-29.md" \
  && grep -q '^Next: put this one line into the ~09:00 morning report' "$T/inbox/media-remux-2026-10-29.md"
ok $? "note contract + line"
MEDIA_REMUX_NOW=$((NIGHT + 7 * 3600)) "$BIN" --cron >/dev/null 2>&1
[ "$(find "$T/inbox" -name 'media-remux-*' | wc -l)" = 1 ]; ok $? "no second note that day"

# 5 failures: a refused override; an import still blocked after 2 overrides
python3 - "$T/state/state.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d['movies']['2'].update(status='grabbed', override_tried=0); json.dump(d, open(sys.argv[1], 'w'))
PY
FAKE_UNBLOCK_REFUSE=1 MEDIA_REMUX_NOW=$((NIGHT + 8 * 3600)) "$BIN" --follow >"$T/out" 2>&1
[ "$(st "d['movies']['2']['status']")" = failed ] && grep -q 'override refused' "$T/out"; ok $? "refused override -> failed"
python3 - "$T/state/state.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d['movies']['1'].update(status='grabbed', override_tried=2); json.dump(d, open(sys.argv[1], 'w'))
PY
mk imported; python3 - "$FAKE_DIR/routes.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1])); r['/api/v3/movie/1'] = r['/api/v3/movie/2']; r['/api/v3/queue/details?movieId=1'] = [
    dict(r['/api/v3/queue/details?movieId=2'][0], movieId=1, downloadId='DL1')]; json.dump(r, open(sys.argv[1], 'w'))
PY
MEDIA_REMUX_NOW=$((NIGHT + 9 * 3600)) "$BIN" --follow >"$T/out" 2>&1
[ "$(st "d['movies']['1']['status']")" = failed ] && grep -q 'still blocked after 2 override' "$T/out"; ok $? "blocked after 2 overrides -> failed"
python3 - "$T/state/state.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d['movies']['1'].update(status='imported'); json.dump(d, open(sys.argv[1], 'w'))
PY
"$BIN" --status --json | python3 -c 'import json,sys; t=json.load(sys.stdin)["totals"]; assert (t["imported"],t["failed"],t["no-release"],t["gb_freed"])==(1,1,1,55.0), t'
ok $? "status json"
"$BIN" --status | grep -q 'imported 1 · failed 1 · no-release 1 · freed 55 GB'; ok $? "status text"
"$BIN" --help | grep -q -- '--plan'; ok $? "help"

# 6 the release must be this film: the interactive search returns other films "matched by Id" (29.10, real names)
rm -rf "$T/state" "$FAKE_DIR/posts.jsonl"; mk mismatch
MEDIA_REMUX_NOW=$NIGHT "$BIN" --plan >"$T/out" 2>&1; ok $? "plan exit (mismatch)"
grep -q 'Transformers: Age of Extinction (2014) Remux-2160p 87.8 GB -> Bluray-2160p 24.6 GB' "$T/out" \
  && grep -q '^    Transformers.4.Aera.des.Untergangs.2014.German.OPUS.DL.2160p.UHDBDRiP.HDR.AV1-Firecooler$' "$T/out"
ok $? "Transformers: Age of Extinction (2014) gets the 2014 encode (German title, mapped by the parser)"
grep -q 'not this film: Extinction.2018.German.DL.EAC3.2160p.DV.HDR.NF.WEB.H265-ZeroTwo (year 2018 vs 2014' "$T/out"
ok $? "Extinction (2018) skipped although it scores best"
grep -q 'Catch Me If You Can (2002) Remux-2160p 92.3 GB: keep (1 releases, none fits (not this film 1))' "$T/out" \
  && grep -q 'not this film: Catch.Me.2018.GERMAN.DUBBED.DL.HDR.2160p.WEB.h265-TMSF (year 2018 vs 2002' "$T/out"
ok $? "Catch Me (2018) never picked for Catch Me If You Can (2002): keep"
rm -f "$T/state/plan.json"
MEDIA_REMUX_NOW=$((NIGHT + 2400)) "$BIN" --run --max 2 >"$T/out" 2>&1; ok $? "run exit (mismatch)"
[ "$(posts 'api/v3/release')" = 1 ] && grep -q '"guid": "tf-firecooler", "indexerId": 7, "movieId": 7' "$FAKE_DIR/posts.jsonl" \
  && ! grep -q 'tf-extinction\|cm-2018' "$FAKE_DIR/posts.jsonl"; ok $? "run grabs the right film only"
grep -q 'NOT-THIS-FILM 7 Transformers: Age of Extinction (2014): skipped Extinction.2018' "$T/state/actions.log" \
  && [ "$(st "d['movies']['8']['status']")" = no-release ]; ok $? "skips logged; Catch Me stays (no-release)"
for how in nocheck noparse; do        # fails closed: no arr-mismatch, or names Radarr can't parse -> no grab
  rm -rf "$T/state" "$FAKE_DIR/posts.jsonl"; mk mismatch "noparse=$([ $how = noparse ] && echo 1 || echo 0)"
  M=$MEDIA_REMUX_MISMATCH; [ $how = nocheck ] && M=$T/no-arr-mismatch
  MEDIA_REMUX_MISMATCH=$M MEDIA_REMUX_NOW=$((NIGHT + 2400)) "$BIN" --run --max 2 >"$T/out" 2>&1
  [ "$(posts 'api/v3/release')" = 0 ] && grep -q 'not this film 1' "$T/out"; ok $? "$how: nothing grabbed"
done
grep -q 'title check unavailable (arr-mismatch)' "$T/state/actions.log" 2>/dev/null; [ $? = 1 ]
ok $? "noparse run loaded the check (only nocheck logs it unavailable)"
# follow: Radarr itself imported the wrong film (a CF upgrade imports without a check) -> wrong, not imported
mk mismatch-imported
python3 - "$T/state/state.json" <<'PY'
import json, os, sys
os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
json.dump({'movies': {'7': {'title': 'Transformers: Age of Extinction', 'year': 2014, 'status': 'grabbed',
                            'grabbed_at': 1793232000, 'old_size': 87800000000, 'old_quality': 'Remux-2160p',
                            'old_file_id': 70, 'download_id': 'DL7',
                            'release': {'title': 'Extinction.2018.German.DL.EAC3.2160p.DV.HDR.NF.WEB.H265-ZeroTwo'}}},
           'nights': {}}, open(sys.argv[1], 'w'))
PY
MEDIA_REMUX_NOW=$((NIGHT + 3600)) "$BIN" --follow >"$T/out" 2>&1; ok $? "follow exit (wrong import)"
[ "$(st "d['movies']['7']['status']")" = wrong ] && [ "$(st "d['movies']['7'].get('freed')")" = None ] \
  && grep -q 'WRONG FILM imported for Transformers: Age of Extinction (2014): year 2018 vs 2014' "$T/out"
ok $? "a wrong film imported over the remux -> state wrong, not counted as freed"
"$BIN" --status | grep -q 'imported 0 .*freed 0 GB .*WRONG FILM 1'; ok $? "status shows the wrong film"
MEDIA_REMUX_NOW=$((NIGHT + 6 * 3600 + 2400)) "$BIN" --cron >/dev/null 2>&1
grep -q '1 WRONG film imported over the remux (Transformers: Age of Extinction (2014))' "$T/inbox/media-remux-2026-10-29.md" \
  2>/dev/null; ok $? "the morning note names the wrong film"
rm -f "$T/inbox"/media-remux-*.md
mk mismatch-imported; python3 - "$FAKE_DIR/routes.json" "$T/state/state.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1])); n = 'Transformers.4.Aera.des.Untergangs.2014.German.OPUS.DL.2160p.UHDBDRiP.HDR.AV1-Firecooler'
r['/api/v3/movie/7']['movieFile'].update(sceneName=n, quality={'quality': {'name': 'Bluray-2160p'}}, size=24600000000)
json.dump(r, open(sys.argv[1], 'w'))
d = json.load(open(sys.argv[2])); d['movies']['7']['status'] = 'grabbed'; json.dump(d, open(sys.argv[2], 'w'))
PY
MEDIA_REMUX_NOW=$((NIGHT + 3600)) "$BIN" --follow >"$T/out" 2>&1
[ "$(st "d['movies']['7']['status']")" = imported ] && [ "$(st "d['movies']['7']['freed']")" = 63200000000 ]
ok $? "the right film imported -> imported + freed"

# 7 a failed download (incomplete upload) left the remux: searched again after 30 days; a refused import never
seed() {  # movie 1's state: status reason failed_at-days-ago; 2 + 6 already checked (no-release)
  rm -rf "$T/state" "$FAKE_DIR/posts.jsonl"; mkdir -p "$T/state"
  python3 - "$T/state/state.json" "$NIGHT" "$@" <<'PY'
import json, sys
f, night, status, reason, days = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], int(sys.argv[5])
kept = {'status': 'no-release', 'checked': night, 'reason': 'x'}
json.dump({'movies': {'1': {'title': 'Big Epic', 'year': 2001, 'status': status, 'reason': reason,
                            'failed_at': night - days * 86400},
                      '2': dict(kept, title='Old Drama'), '6': dict(kept, title='Nothing Fits')},
           'nights': {}}, open(f, 'w'))
PY
}
mk base
seed failed 'download failed' 31
MEDIA_REMUX_NOW=$((NIGHT + 2400)) "$BIN" --run --max 1 >"$T/out" 2>&1
grep -q '"guid": "uhd-good", "indexerId": 7, "movieId": 1' "$FAKE_DIR/posts.jsonl" 2>/dev/null \
  && [ "$(st "d['movies']['1']['status']")" = grabbed ]; ok $? "failed download 31 days ago -> searched + grabbed again"
seed failed 'gone from the queue without an import' 40
MEDIA_REMUX_NOW=$((NIGHT + 2400)) "$BIN" --run --max 1 >"$T/out" 2>&1
[ "$(posts 'api/v3/release')" = 1 ]; ok $? "vanished grab 40 days ago -> grabbed again"
seed failed 'download failed' 5
MEDIA_REMUX_NOW=$((NIGHT + 2400)) "$BIN" --run --max 1 >"$T/out" 2>&1
[ "$(posts 'api/v3/release')" = 0 ] && [ "$(st "d['movies']['1']['status']")" = failed ]; ok $? "failed 5 days ago -> waits"
seed failed 'import blocked, override refused: x' 60
MEDIA_REMUX_NOW=$((NIGHT + 2400)) "$BIN" --run --max 1 >"$T/out" 2>&1
[ "$(posts 'api/v3/release')" = 0 ] && [ "$(st "d['movies']['1']['status']")" = failed ]
ok $? "refused import never re-grabbed (the download waits in the queue)"

# 8 the night cap counts grabs inside the window only (30.10: a daytime --run of 30 on 29.10 12:00-12:37 put 17
#   grabs under the night 29 -> 30, which read 17/15 and grabbed nothing)
seed failed 'download failed' 31
MEDIA_REMUX_NOW=$((NIGHT + 2400)) "$BIN" --run --max 1 >/dev/null 2>&1
[ "$(st "d['nights'].get('2026-10-28', {}).get('grabs')")" = 1 ] && [ "$(st "d['outside']")" = '{}' ]
ok $? "--run inside the window counts toward that night"
seed failed 'download failed' 31
MEDIA_REMUX_NOW=$((NIGHT + 45000)) "$BIN" --run --max 1 >/dev/null 2>&1        # 29.10 12:30
[ "$(posts 'api/v3/release')" = 1 ] && [ "$(st "d['nights']")" = '{}' ] \
  && [ "$(st "d['outside'].get('2026-10-29')")" = 1 ]; ok $? "daytime --run: counted outside, not in a night"
MEDIA_REMUX_NOW=$((NIGHT + 45000)) "$BIN" --status | grep -q 'this night 0/15 · outside the window today 1 (--run)'
ok $? "status: this night 0/15 + outside today"
python3 - "$T/state/state.json" "$NIGHT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))     # movie 1's download failed again: due for another search
d['movies']['1'].update(status='failed', reason='download failed', failed_at=int(sys.argv[2]) - 31 * 86400)
json.dump(d, open(sys.argv[1], 'w'))
PY
MEDIA_REMUX_NIGHT_MAX=1 MEDIA_REMUX_NOW=$((NIGHT + 86400 + 2400)) "$BIN" --cron >/dev/null 2>&1   # 30.10 00:40
[ "$(posts 'api/v3/release')" = 2 ] && [ "$(st "d['nights'].get('2026-10-29', {}).get('grabs')")" = 1 ]
ok $? "next night's cron still has its full budget after a daytime run"

echo "media-remux tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
