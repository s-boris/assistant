#!/usr/bin/env bash
# Offline tests for `media vibe` on series (second index, series words, like <series>) and `media finish` (shows he
# started and paused). fake_vibe.py (scenario "series") stands in for Emby: a fictional series library, his series
# user data and episodes; fake_tonight.py for Seerr. MEDIA_VIBE_EMBED_URL points at the fake (keyword vectors).
# Checks: series detection (words, --series, tv show, like <series>, moods stay movies), TMDB TV / German / Serbian
# genres, seen states (seen / partly / barely), rating floor, no play buttons for series, both indexes on --refresh,
# finish: idle filter, specials and finished shows left out, ranking, resume point, next = after the furthest episode
# (skipped ones mentioned, gaps-only shows last), -n/--days, play buttons, a show
# failing (exit 3), GET-only, only his user, no key leaks.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-series-tests.XXXXXX")
trap 'kill $SRV $EMB 2>/dev/null; rm -rf "$T"' EXIT

python3 -I "$HERE/fake_tonight.py" "$T/port" "$T/server.log" 2>"$T/server.err" &
SRV=$!
echo series > "$T/scenario"
python3 -I "$HERE/fake_vibe.py" "$T/eport" "$T/emby.log" "$T/emb.log" "$T/scenario" 2>"$T/emby.err" &
EMB=$!
for _ in $(seq 50); do [ -s "$T/port" ] && [ -s "$T/eport" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
E="http://127.0.0.1:$(cat "$T/eport")"
printf '{"seerr": {"url": "%s", "key": "SEERR-KEY-Zq81"}, "emby": {"url": "%s", "key": "EMBY-KEY-Vb42"}}\n' "$B/seerr" "$E" > "$T/cfg.json"
printf '{"seerr": {"url": "%s", "key": "SEERR-KEY-Zq81"}}\n' "$B/seerr" > "$T/noemby.json"
printf '{"enabled": true, "tv": "media_player.test_tv", "device": "TESTTV"}\n' > "$T/play-on.json"
export MEDIA_SECRETS=$T/cfg.json MEDIA_TIMEOUT=5 MEDIA_PAGE_SIZE=4 MEDIA_VIBE_DIR=$T/vibe MEDIA_VIBE_EMBED_URL=$E \
  MEDIA_PLAY_CONFIG=/nonexistent/play.json
unset MEDIA_TONIGHT_USER MEDIA_READY_USER MEDIA_CONTINUE_EMBY_USER MEDIA_NOW

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
m() { out=$(timeout 30 "$MEDIA" "$@" 2>&1); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; }
mj() { out=$(timeout 30 "$MEDIA" "$@" 2>"$T/err"); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; cat "$T/err" >> "$T/all.out"; }
jq_() { python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print($1)"; }
titles() { printf '%s' "$out" | jq_ "' | '.join(p['title'] for p in d['picks'])"; }
docs() { grep -c '^doc ' "$T/emb.log" 2>/dev/null || true; }

echo "# movies first (series index not built by a movie query)"
mj vibe --json cozy rainy
eq "movie query: exit 0" "$rc" "0"
eq "movie query: kind movie" "$(printf '%s' "$out" | jq_ "d['kind']")" "movie"
[ -e "$T/vibe/series-index.json" ] && no "no series index yet" || ok "no series index yet"

echo "# a series word switches to series"
: > "$T/emb.log"
mj vibe --json a dark crime series
eq "series: exit 0" "$rc" "0"
eq "series: kind" "$(printf '%s' "$out" | jq_ "d['kind']")" "series"
eq "series: all 8 shows embedded" "$(docs)" "8"
eq "series: query without the series word, TV series suffix" "$(grep '^query ' "$T/emb.log")" "query a dark crime TV series"
eq "series: picks" "$(titles)" "Night Precinct | Cold Case Lake | Star Frontier | Office Laughs"
hasnt "series: rating floor keeps Weak Show (5.0) out" "$(titles)" "Weak Show"
hasnt "series: the finished Harbor Detective ranks below" "$(titles)" "Harbor Detective"
eq "series: barely started = no penalty, marked" "$(printf '%s' "$out" | jq_ "[(p['seen'], p['why'].split(' · ')[-1], p['episodes'], p['watched']) for p in d['picks'] if p['title'] == 'Cold Case Lake'][0]")" "('barely', '👁 1 of 10 eps watched', 10, 1)"
eq "series: half watched = partly (penalised: below a comedy here)" "$(printf '%s' "$out" | jq_ "[p['seen'] for p in d['picks'] if p['title'] == 'Office Laughs'][0]")" "partly"
eq "series: TMDB TV genres split" "$(printf '%s' "$out" | jq_ "[p['genres'] for p in d['picks'] if p['title'] == 'Star Frontier'][0]")" "['Fantasy', 'Science Fiction', 'Action', 'Adventure']"
has "series: header + eps in the message" "$(printf '%s' "$out" | jq_ "d['message']")" "📺 **Vibe** · series · a dark crime"
has "series: N eps on the line" "$(printf '%s' "$out" | jq_ "d['message']")" "**Night Precinct** (2019) · 6 eps · ★8.1"
has "series: text has ▶ links (template from the movie index)" "$(printf '%s' "$out" | jq_ "d['text']")" "▶ https://emby.example.test/web/index.html#!/item?id=201&"
eq "series: German TV genre (Krieg & Politik) mapped" "$(python3 -c "import json; d=json.load(open('$T/vibe/series-index.json')); print([x['genres'] for x in d['items'] if x['title']=='Weak Show'][0])")" "['War', 'Politics']"
eq "series: doc says TV series" "$(python3 -c "import json; d=json.load(open('$T/vibe/series-index.json')); print(all(x.get('kind')=='series' for x in d['items']))")" "True"
eq "series + movie vector files side by side" "$(ls "$T/vibe" | sed 's/-[0-9a-f]*\.f32$//' | sort | tr '\n' ' ')" "index.json series-index.json series-vectors vectors "
MEDIA_PLAY_CONFIG=$T/play-on.json mj vibe --json detective series
eq "series + play on: no mt:play buttons (a series is no play target)" "$(printf '%s' "$out" | jq_ "sum(1 for r in d['buttons'] for b in r if 'data' in b)")" "0"

echo "# other ways in"
: > "$T/emb.log"
mj vibe --json --series detective murder
eq "--series: kind" "$(printf '%s' "$out" | jq_ "d['kind']")" "series"
eq "--series: nothing re-embedded, query" "$(cat "$T/emb.log")" "query detective murder TV series"
eq "--series: detective shows first" "$(printf '%s' "$out" | jq_ "d['picks'][0]['title']")" "Night Precinct"
eq "series: half watched = partly" "$(printf '%s' "$out" | jq_ "[p['seen'] for p in d['picks'] if p['title'] == 'Murder Valley'][0]")" "partly"
eq "series: Serbian genres mapped" "$(printf '%s' "$out" | jq_ "[p['genres'] for p in d['picks'] if p['title'] == 'Murder Valley'][0]")" "['Crime', 'Drama']"
: > "$T/emb.log"
mj vibe --json funny tv show
eq "tv show: series, both words dropped" "$(printf '%s' "$out" | jq_ "d['kind']") $(cat "$T/emb.log")" "series query funny TV series"
mj vibe --json eine lustige Serie
eq "German Serie: series" "$(printf '%s' "$out" | jq_ "d['kind']")" "series"
mj vibe --json like Night Precinct
eq "like <series> without a series word: series" "$(printf '%s' "$out" | jq_ "d['kind'] + ' ' + d['ref']['title']")" "series Night Precinct"
hasnt "like <series>: the show itself not picked" "$(titles)" "Night Precinct"
mj vibe --json Cold Case Lake
eq "bare series title: series, like it" "$(printf '%s' "$out" | jq_ "d['kind'] + ' ' + d['ref']['title']")" "series Cold Case Lake"
mj vibe --json like First Signal
eq "like <movie>: movie" "$(printf '%s' "$out" | jq_ "d['kind']")" "movie"
mj vibe --json a show like First Signal
eq "series word before like <movie>: series wins (words count)" "$(printf '%s' "$out" | jq_ "d['kind']")" "series"
mj vibe --json heist with a laugh
eq "plain mood: movie" "$(printf '%s' "$out" | jq_ "d['kind']")" "movie"
m vibe a dark crime series
has "plain text: series header" "$out" "📺 Vibe · series · a dark crime"

echo "# refresh: both indexes"
m vibe --refresh
eq "refresh: exit 0" "$rc" "0"
has "refresh: movie line" "$out" "vibe index: 15 movies (0 new, 0 changed, 0 removed; 0 embedded"
has "refresh: series line" "$out" "vibe index: 8 series (0 new, 0 changed, 0 removed; 0 embedded"
: > "$T/emb.log"
m vibe --refresh --full --quiet
eq "refresh --full: both re-embedded" "$(docs)" "23"
eq "refresh --quiet: silent" "$out" ""

echo "# finish"
m finish
eq "finish: exit 0" "$rc" "0"
eq "finish: text" "$(printf '%s\n' "$out" | sed -n '1,3p;4p;6p;7p')" "🏁 Finish next
Started, paused 14+ days, episodes left

📺 Star Frontier · 3 of 8 left · ★8.4
   ▶ https://emby.example.test/web/index.html#!/item?id=2050106&context=home&serverId=srv1
📺 Murder Valley · 6 of 10 left · ★6.9"
has "finish: next episode + when" "$out" "   next S01E06 · last watched "
has "finish: in-progress episode resumes" "$out" "   next S01E05 at 20 min · last watched "
has "finish: barely started show listed too" "$out" "📺 Cold Case Lake · 9 of 10 left · ★7.6"
hasnt "finish: watched 5 days ago = not paused" "$out" "Office Laughs"
hasnt "finish: only specials left = finished" "$out" "Specials Left"
hasnt "finish: finished show left out" "$out" "Harbor Detective"
hasnt "finish: never started left out" "$out" "Night Precinct"
mj finish --json
eq "finish --json: order" "$(printf '%s' "$out" | jq_ "' | '.join(f\"{i['title']}:{i['left']}/{i['total']}:{i['id']}:{i['state']}\" for i in d['items'])")" \
  "Star Frontier:3/8:2050106:next | Murder Valley:6/10:2060105:resume | Cold Case Lake:9/10:2020102:next"
eq "finish --json: play off = no buttons, says so" "$(printf '%s' "$out" | jq_ "str(d['buttons']) + ' ' + str('switched off' in d['message'])")" "[] True"
has "finish --json: Markdown line" "$(printf '%s' "$out" | jq_ "d['message']")" "📺 **Star Frontier** · 3 of 8 left · ★8.4"
eq "finish --json: counts" "$(printf '%s' "$out" | jq_ "(d['total'], d['started'], d['idle_days'])")" "(3, 5, 14)"
MEDIA_PLAY_CONFIG=$T/play-on.json mj finish --json -n 2
eq "finish play on: continue's buttons, -n 2" "$(printf '%s' "$out" | jq_ "[b['text'] + '=' + b['data'] for r in d['buttons'] for b in r]")" \
  "['📺 Star Frontier S01E06=mt:play:2050106', '📺 Murder Valley S01E05=mt:play:2060105']"
mj finish --json --days 3
eq "--days 3: Office Laughs joins" "$(printf '%s' "$out" | jq_ "' | '.join(i['title'] for i in d['items'])")" "Star Frontier | Office Laughs | Murder Valley | Cold Case Lake"
mj finish --json --days 1000
eq "--days 1000: nothing" "$(printf '%s' "$out" | jq_ "d['text'].splitlines()[-1]")" "Nothing to finish: no paused shows with episodes left."
echo gaps > "$T/scenario"
m finish
eq "gaps: exit 0" "$rc" "0"
has "gaps: continue after the furthest episode, not after the last played one" "$out" "📺 Weak Show · 3 of 6 left · ★5.0
   next S01E05 · last watched "
has "gaps: earlier skipped episode mentioned" "$out" " · skipped S01E03"
has "gaps only: watched to the end, the skipped ones named" "$out" "📺 Night Precinct · 2 of 10 left · ★8.1
   watched to S03E02 · skipped S02E01–E02 · last watched "
hasnt "gaps only: no 'next' for a show watched to the end" "$out" "next S02E01"
mj finish --json
eq "gaps --json: forward shows first, gaps-only last" "$(printf '%s' "$out" | jq_ "' | '.join(f\"{i['title']}:{i['id']}:{i['state']}:{i['gaps_only']}:{i['gaps']}\" for i in d['items'])")" \
  "Star Frontier:2050106:next:False:None | Murder Valley:2060105:resume:False:None | Cold Case Lake:2020102:next:False:None | Weak Show:2070105:next:False:S01E03 | Night Precinct:2010201:next:True:S02E01–E02"
eq "gaps --json: furthest" "$(printf '%s' "$out" | jq_ "' '.join(i['furthest'] for i in d['items'])")" "S01E05 S01E05 S01E01 S01E04 S03E02"
echo ep-down > "$T/scenario"
mj finish --json
eq "a show failing: exit 3" "$rc" "3"
has "a show failing: named" "$(printf '%s' "$out" | jq_ "' '.join(d['warnings'])")" "1 show(s) didn't load: Murder Valley"
eq "a show failing: the rest still listed" "$(printf '%s' "$out" | jq_ "len(d['items'])")" "2"
echo series > "$T/scenario"
m finish -n 11
has "-n 11 refused" "$out" "-n must be 1..10"
MEDIA_SECRETS=$T/noemby.json m finish
eq "no emby key: exit 1" "$rc" "1"

echo "# safety"
eq "every Emby request was a GET ($(wc -l < "$T/emby.log") requests)" "$(grep -vc '^GET ' "$T/emby.log")" "0"
eq "every Seerr request was a GET" "$(grep -vc '^GET ' "$T/server.log")" "0"
hasnt "only his own user's data read" "$(grep '/Users/' "$T/emby.log" | grep -v '/Users/aaaa1111' || true)" "/Users/"
hasnt "no Emby key in any output" "$(cat "$T/all.out")" "EMBY-KEY-Vb42"
hasnt "no Seerr key in any output" "$(cat "$T/all.out")" "SEERR-KEY-Zq81"
hasnt "no tracebacks" "$(cat "$T/all.out")" "Traceback"
eq "fake servers clean" "$(cat "$T/server.err" "$T/emby.err")" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
