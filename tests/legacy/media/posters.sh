#!/usr/bin/env bash
# Offline tests for `media posters` (the weekly "Neu auf dem Server" collage). Exit 0 = all pass.
# posters_fx.py serves a fake Emby (views, items, series, poster images) and Radarr/Sonarr history; the clock is
# Fri 2026-10-23 17:30 Zurich (KW 43). Checks: what counts as new (window, upgrades/renames, specials, virtual
# episodes, extras, excluded library), series grouping, ranking + cap, PNG, cron note + send rule, failures,
# GET-only, the key only in a header, no key leaks. Also `media poster` (cached Primary images for tg-cmd's picture cards).
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-posters-tests.XXXXXX")
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT

echo ok > "$T/scenario"
python3 -I "$HERE/posters_fx.py" "$T/port" "$T/server.log" "$T/scenario" 2>"$T/server.err" &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
printf '{"seerr": {"url": "%s/seerr", "key": "SEERR-KEY-Zq81"}, "radarr": {"url": "%s/radarr", "key": "RADARR-KEY-Xw42"}, "sonarr": {"url": "%s/sonarr", "key": "SONARR-KEY-Vy73"}, "emby": {"url": "%s", "key": "EMBY-KEY-Po66"}}\n' \
  "$B" "$B" "$B" "$B" > "$T/cfg.json"
mkdir -p "$T/inbox" "$T/reports"
export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-23T17:30:00+02:00 MEDIA_TIMEOUT=5 \
  MEDIA_CONTINUE_EMBY_USER=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa MEDIA_POSTERS_INBOX=$T/inbox MEDIA_REPORT_DIR=$T/reports
unset MEDIA_POSTERS_MAX MEDIA_POSTERS_SEND_MIN MEDIA_POSTERS_FONTS
# no DejaVu or Noto font here (the CI runner's image has none): Bitstream Vera, DejaVu's base with its metrics, which
# the CI toolchain's reportlab carries, under DejaVu's names
_vera=${ASSISTANT_CI_TOOLCHAIN:-/nonexistent}/site/reportlab/fonts
if [ ! -e /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf ] && [ ! -e /usr/share/fonts/truetype/noto/NotoSans-Regular.ttf ] &&
  [ -f "$_vera/Vera.ttf" ]; then
  mkdir -p "$T/fonts/dejavu"
  ln -s "$_vera/Vera.ttf" "$T/fonts/dejavu/DejaVuSans.ttf" && ln -s "$_vera/VeraBd.ttf" "$T/fonts/dejavu/DejaVuSans-Bold.ttf"
  export MEDIA_POSTERS_FONTS=$T/fonts
fi

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2"; fi; }
# has/hasnt test the substring in the shell: `printf | grep -q` under pipefail reads as no match when grep exits
# on the match before printf (one write per line) is done, SIGPIPE (a false FAIL, or a false pass for hasnt)
has() { if [[ $2 == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ $2 == *"$3"* ]]; then no "$1" "$(printf '%s' "$2" | grep -F -- "$3" | head -3)"; else ok "$1"; fi; }
m() { "$MEDIA" "$@" >"$T/out" 2>"$T/err" </dev/null; rc=$?; cat "$T/out" "$T/err" >> "$T/all.out"; }
jq_() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$T/out" "$1"; }
pngsize() { python3 -c 'import sys; from PIL import Image; im=Image.open(sys.argv[1]); print(im.format, im.size[0], im.size[1] > 900)' "$1" 2>&1; }

echo "# what counts as new"
m posters --json --out "$T/a.png"
eq "exit 3 (one poster failed to load)" "$rc" "3"
eq "header + subtitle (KW of the clock, Zurich dates)" "$(jq_ '(d["header"], d["subtitle"])')" \
  "('Neu auf dem Server · KW 43', 'New on the server this week · 16–23 Oct')"
eq "movies: new in the window, best rated first; upgrade, rename, old, extra, excluded library out" \
  "$(jq_ '[x["title"] for x in d["items"] if x["kind"] == "movie"]')" "['Star Voyage', 'Broken Poster', 'New Then Better', 'No Poster']"
eq "shows grouped by series with episodes counted, seasons, new series flag" \
  "$(jq_ '[(x["title"], x["episodes"], x["seasons"], x["new_series"], x["rating"]) for x in d["items"] if x["kind"] == "show"]')" \
  "[('Nova Drift', 3, [1], True, 8.4), ('Night Shift', 2, [2, 3], False, 7.8), ('Laugh Track', 1, [4], False, 7.2)]"
eq "skipped: 2 movie + 1 episode upgrades, 1 special, 1 virtual" "$(jq_ 'sorted(d["skipped"].items())')" \
  "[('missing', 1), ('specials', 1), ('upgrades', 3)]"
eq "counts line + totals" "$(jq_ '(d["counts"], d["total"], d["more"], d["send"])')" \
  "('4 movies · 3 shows (6 new episodes)', 7, 0, True)"
has "German + English caption" "$(jq_ 'd["caption"]')" "Neu auf dem Server (KW 43): 4 Filme, 3 Serien (6 neue Folgen)."
has "broken poster -> warning, placeholder tile" "$(jq_ 'd["warnings"]')" "poster HTTP 500"
eq "PNG, 1080 wide" "$(pngsize "$T/a.png")" "PNG 1080 True"
hasnt "excluded library never queried" "$(cat "$T/server.log")" "ParentId=20"
hasnt "boxsets/live TV views never queried" "$(grep -E 'ParentId=(30|4)&' "$T/server.log")" "GET"
eq "every request is a GET" "$(grep -vc '^GET ' "$T/server.log")" "0"
eq "the key only in a header (never in a URL)" "$(grep -c 'KEY' "$T/server.log")" "0"
eq "every request carried the key header" "$(grep -c 'token=no' "$T/server.log")" "0"

echo "# text output, cap"
m posters --out "$T/b.png" --days 7
has "text: collage path + size" "$(cat "$T/out")" "Collage: $T/b.png (1080x"
has "text: left out line" "$(cat "$T/out")" "left out: 1 missing, 1 specials, 3 upgrades"
echo many > "$T/scenario"
m posters --json --max 10 --out "$T/c.png"
eq "cap: movies keep at least half, shows the rest, +N more" "$(jq_ '(d["total"], d["shown"], d["more"], sum(x["kind"] == "movie" for x in d["items"]))')" "(37, 10, 27, 7)"
eq "cap: best rated movies first" "$(jq_ '[x["title"] for x in d["items"]][:3]')" "['Star Voyage', 'Bulk Film 29', 'Bulk Film 28']"
eq "capped PNG rendered" "$(pngsize "$T/c.png")" "PNG 1080 True"
m posters --json --max 30 --out "$T/c2.png"
eq "few shows: movies take their share (27 movies + 3 shows)" "$(jq_ '(d["shown"], sum(x["kind"] == "movie" for x in d["items"]))')" "(30, 27)"
echo ok > "$T/scenario"

echo "# failures"
echo radarr-down > "$T/scenario"
m posters --json --out "$T/d.png"
eq "Radarr down: exit 3" "$rc" "3"
has "Radarr down: warning says upgrades not filtered" "$(jq_ 'd["warnings"]')" "radarr history failed, upgrades not filtered"
has "Radarr down: the upgrade then counts as new" "$(jq_ '[x["title"] for x in d["items"]]')" "Old Upgrade"
echo ok > "$T/scenario"
m posters --days 0
eq "--days 0 refused" "$rc" "1"
MEDIA_POSTERS_FONTS=$T/nofonts m posters --out "$T/e.png"
eq "font dir missing: Pillow finds the system fonts by name, still renders" "$(pngsize "$T/e.png")" "PNG 1080 True"
MEDIA_SECRETS=$T/nocfg.json m posters
eq "missing config: exit 1" "$rc" "1"

echo "# cron"
m posters --cron
eq "cron: exit 3 (poster warning)" "$rc" "3"
N="$T/inbox/media-posters-2026-10-23.md"
eq "cron: PNG in the report dir" "$(pngsize "$T/reports/new-on-server-2026-10-23.png")" "PNG 1080 True"
has "cron: note names the image" "$(cat "$N" 2>&1)" "- Image: $T/reports/new-on-server-2026-10-23.png (1080x"
has "cron: send yes with 7 items" "$(cat "$N" 2>&1)" "- Send: yes"
has "cron: note carries the caption" "$(cat "$N" 2>&1)" "- Caption: 🎬 Neu auf dem Server (KW 43)"
rm -f "$T/inbox/media-posters-2026-10-23.md"
m posters --cron --days 4
has "cron, 3 items: send no" "$(cat "$N" 2>&1)" "- Send: no"
has "cron, 3 items: counts" "$(cat "$N" 2>&1)" "1 movie · 2 shows (4 new episodes) = 3 items"
rm -f "$T/inbox/media-posters-2026-10-23.md" "$T/reports/new-on-server-2026-10-23.png"
m posters --cron --days 1
eq "nothing new: exit 0" "$rc" "0"
has "nothing new: says so" "$(cat "$T/out")" "Nothing new in the last 1 days"
eq "nothing new: no note, no image" "$(find "$T/inbox" "$T/reports" -type f | grep -c .)" "0"
echo empty > "$T/scenario"
m posters --json
eq "empty library window: no path, total 0" "$(jq_ '(d["path"], d["total"])')" "(None, 0)"
echo ok > "$T/scenario"

echo "# poster (cached Primary images for picture cards)"
PC=$T/pcache; : > "$T/server.log"
pm() { HOME="$T/home" MEDIA_POSTER_CACHE="$PC" m poster "$@"; }
pm --json 101 102 101
eq "poster: rc 0" "$rc" "0"
eq "poster: one path per id (duplicates once), JPEG in the cache" "$(jq_ 'sorted(d["posters"].items())')" "[('101', '$PC/101-w400.jpg'), ('102', '$PC/102-w400.jpg')]"
eq "poster: 2 fetched, 0 cached, no warnings" "$(jq_ '[d["fetched"], d["cached"], d["warnings"]]')" "[2, 0, []]"
eq "poster: a real JPEG" "$(python3 -c 'import sys; from PIL import Image; print(Image.open(sys.argv[1]).format)' "$PC/101-w400.jpg")" "JPEG"
eq "poster: cache dir 0700, files 0600" "$(stat -c %a "$PC") $(stat -c %a "$PC/101-w400.jpg")" "700 600"
eq "poster: two GETs with maxWidth" "$(grep -c 'Images/Primary?maxWidth=400' "$T/server.log")" "2"
: > "$T/server.log"
pm --json 101 102
eq "poster: second run from the cache, no request" "$(jq_ '[d["fetched"], d["cached"]]') $(grep -c . "$T/server.log")" "[0, 2] 0"
touch -d '40 days ago' "$PC/101-w400.jpg"
pm --json 101
eq "poster: older than 30 days → fetched again" "$(jq_ 'd["fetched"]')" "1"
pm --json --width 200 103 107
eq "poster: Emby 500 → null + warning, exit 3" "$rc $(jq_ '[d["posters"]["107"], d["warnings"]]')" "3 [None, ['poster HTTP 500']]"
eq "poster: own cache entry per width" "$(jq_ 'd["posters"]["103"]')" "$PC/103-w200.jpg"
pm 104
eq "poster: plain output = ID PATH" "$(cat "$T/out")" "104 $PC/104-w400.jpg"
pm --json '../x'
eq "poster: an id with other characters → usage" "$rc" "2"
pm --json 1 2 3 4 5 6 7 8 9 10 11 12 13
eq "poster: more than 12 ids → usage" "$rc" "2"
pm --json --width 5000 101
eq "poster: width out of range → usage" "$rc" "2"
MEDIA_SECRETS=$T/nocfg.json pm --json 101
eq "poster: no config → exit 1" "$rc" "1"
eq "poster: every request is a GET with the key header, never in the URL" "$(grep -vc '^GET .* token=yes$' "$T/server.log") $(grep -c 'KEY' "$T/server.log")" "0 0"
touch -d '70 days ago' "$PC/102-w400.jpg"; touch -d '2 days ago' "$PC/.pruned"
pm --json 101
eq "poster: once a day, entries not refreshed in 60 days are dropped" "$(test -e "$PC/102-w400.jpg" && echo kept || echo gone)" "gone"
eq "poster: never writes under HOME" "$(find "$T/home" -type f 2>/dev/null | wc -l)" "0"

echo "# no key leaks"
for k in EMBY-KEY-Po66 RADARR-KEY-Xw42 SONARR-KEY-Vy73 SEERR-KEY-Zq81; do
  hasnt "no $k in any output or note" "$(cat "$T/all.out" "$T"/inbox/* 2>/dev/null)" "$k"
done

echo "# $pass passed, $fail failed"
[ "$fail" -eq 0 ]
