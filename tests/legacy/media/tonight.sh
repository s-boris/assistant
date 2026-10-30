#!/usr/bin/env bash
# Offline tests for `media tonight` (movie night picks). Exit 0 = all pass.
# fake_tonight.py stands in for Seerr; tonight_fx.py builds Emby's library.db + playback_reporting.db and plays the
# SSH lockdown verb `emby-sql library|playback` (enforcing its rules) or the root sqlite3 command, logging the SQL.
# Clock: Sat 2026-10-10 20:00 Zurich. Checks: the three picks and their reasons, taste, mood/time, args parsing,
# repeats and --more, Emby user mapping + cache, links, buttons, Markdown escaping, partial failures, dry runs,
# GET-only, read-only SQL that passes the verb's checks, no key leaks.
set -uo pipefail
# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
export MEDIA_PLAY_CONFIG=/nonexistent/play.json   # exact button rows: keep the live play.json out
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-tonight-tests.XXXXXX")
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT

python3 -I "$HERE/tonight_fx.py" build "$T/emby"
python3 -I "$HERE/fake_tonight.py" "$T/port" "$T/server.log" 2>"$T/server.err" &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
DEAD=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
mkcfg() {  # mkcfg FILE SEERR_URL
  printf '{"seerr": {"url": "%s", "key": "SEERR-KEY-Zq81"}, "radarr": {"url": "%s/radarr", "key": "RADARR-KEY-Xw42"}, "sonarr": {"url": "%s/sonarr", "key": "SONARR-KEY-Vy73"}}\n' \
    "$2" "$B" "$B" > "$1"
}
mkcfg "$T/cfg.json" "$B/seerr"
mkcfg "$T/seerr-down.json" "http://127.0.0.1:$DEAD"
SPLIT_SQL="python3 -I $HERE/tonight_fx.py sql $T/emby $T/sql.log {db}"
ROOT_SQL="python3 -I $HERE/tonight_fx.py sql $T/emby $T/sql.log"
export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-10T20:00:00+02:00 MEDIA_PAGE_SIZE=2 MEDIA_TIMEOUT=5 \
  MEDIA_EMBY_DIR=$T/emby MEDIA_EMBY_SQL=$SPLIT_SQL MEDIA_TONIGHT_STATE=$T/st/tonight.json
unset MEDIA_TONIGHT_USER MEDIA_TONIGHT_EMBY_UID MEDIA_READY_USER

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
m() { out=$(timeout 30 "$MEDIA" "$@" 2>&1); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; }
jq_() { python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print($1)"; }
picks() { printf '%s' "$out" | jq_ "' | '.join(p['slot'] + ' ' + p['title'] + (' ' + p['episode'] if p.get('episode') else '') for p in d['picks'])"; }
why() { printf '%s' "$out" | jq_ "[p['why'] for p in d['picks'] if p['slot'] == '$1'][0]"; }
fresh() { rm -f "$T/st/tonight.json"; }   # literal target: a new state for the next case
sqlcount() { local n; n=$(grep -c -- "$1" "$T/sql.log" 2>/dev/null); echo "${n:-0}"; }
apick() { printf '%s' "$out" | jq_ "[p['title'] for p in d['picks'] if p['slot'] == 'A'][0]"; }
# bep: B's play id in the fixture DB = an episode of the pick's show with the episode number the card shows
bep() { printf '%s' "$out" | python3 -c "
import json, sqlite3, sys
p = [p for p in json.loads(sys.stdin.read())['picks'] if p['slot'] == 'B'][0]
r = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True).execute(
    'SELECT type, SeriesId, ParentIndexNumber, IndexNumber FROM MediaItems WHERE Id = ?', (int(p['emby_id']),)).fetchone()
print('ok' if r and r[0] == 8 and str(r[1]) == p['series_id'] and 'S%02dE%02d' % (r[2], r[3]) == p['episode'] else r)
" "$T/emby/library.db"; }

echo "# default picks (dry run)"
m tonight --json --dry-run
eq "rc 0" "$rc" "0"
eq "picks" "$(picks)" "A Req_Film [Cut] | B Star Voyage S01E04 | C Nova Drift"
eq "A: his own available request first" "$(why A)" "you requested it, on Emby since 07.10 · Science Fiction, Drama"
eq "B: next episode after his last (specials and missing files skipped)" "$(why B)" \
  "next up: you watched S01E03 on 07.10 · 2 more after this"
eq "C: genre discover, rated, because" "$(why C)" "popular Science Fiction from 2025 · because you finished Arrival"
eq "taste: liked genres relative to the library" "$(printf '%s' "$out" | jq_ "d['taste']")" \
  "{'top_genres': ['Science Fiction', 'Drama', 'Comedy'], 'movies_seen': 12, 'shows_seen': 3}"
eq "A fields" "$(printf '%s' "$out" | jq_ "[(p['year'], p['runtime_min'], p['rating'], p['tmdb'], p['in_library']) for p in d['picks'] if p['slot'] == 'A'][0]")" \
  "(2025, 95, 6.5, 5550, True)"
eq "B fields" "$(printf '%s' "$out" | jq_ "[(p['kind'], p['episode_title'], p['runtime_min'], p['rating'], p['emby_id']) for p in d['picks'] if p['slot'] == 'B'][0]")" \
  "('episode', 'The *Fourth*', 45, 8.2, '2014')"
eq "B next episode: play id = link id = the episode shown (fixture DB)" "$(bep) / $(printf '%s' "$out" | jq_ "[(p['series_id'], p['link'].split('id=')[1].split('&')[0]) for p in d['picks'] if p['slot'] == 'B'][0]")" \
  "ok / ('201', '2014')"
eq "C fields" "$(printf '%s' "$out" | jq_ "[(p['year'], p['runtime_min'], p['rating'], p['in_library'], p['request']) for p in d['picks'] if p['slot'] == 'C'][0]")" \
  "(2025, 118, 8.4, False, {'data': 'mt:req:7001', 'cmd': 'media request 7001 --type movie'})"
eq "links: Seerr's Emby URL with our item ids" "$(printf '%s' "$out" | jq_ "[p['link'] for p in d['picks']]")" \
  "['https://emby.example.test/web/index.html#!/item?id=106&context=home&serverId=srv1', 'https://emby.example.test/web/index.html#!/item?id=2014&context=home&serverId=srv1', None]"
eq "buttons" "$(printf '%s' "$out" | jq_ "[[(b['text'], b.get('data') or b['url'].split('?')[1]) for b in r] for r in d['buttons']]")" \
  "[[('▶ Req_Film [Cut]', 'id=106&context=home&serverId=srv1')], [('▶ Star Voyage S01E04', 'id=2014&context=home&serverId=srv1')], [('➕ Request Nova Drift', 'mt:req:7001')], [('🎲 3 more', 'mt:more')]]"
eq "message (rich Markdown, escaped)" "$(printf '%s' "$out" | jq_ "d['message']")" "🎬 **Movie night**

🍿 **Req\\_Film \\[Cut\\]** (2025) · 95 min · ★6.5
you requested it, on Emby since 07.10 · Science Fiction, Drama

📺 **Star Voyage** S01E04 “The \\*Fourth\\*” · 45 min · ★8.2
next up: you watched S01E03 on 07.10 · 2 more after this

✨ **Nova Drift** (2025) · 118 min · ★8.4 · not on Emby yet
popular Science Fiction from 2025 · because you finished Arrival"
eq "no warnings" "$(printf '%s' "$out" | jq_ "d['warnings']")" "[]"
eq "dry run writes no state" "$(ls "$T/st" 2>/dev/null)" ""
eq "Emby user mapped by play overlap (Playback Reporting read once)" "$(sqlcount 'FROM PlaybackActivity')" "1"
has "  his Emby id normalized (dashes, case)" "$(cat "$T/sql.log")" "= 'aaaa1111000000000000000000000000'"
eq "Seerr: links template fetched once" "$(grep -c '/api/v1/movie/5550\|/api/v1/tv/' "$T/server.log")" "1"
has "  discover asks for rated, recent, released" "$(cat "$T/server.log")" \
  "/seerr/api/v1/discover/movies?page=1&genre=878&sortBy=popularity.desc&voteAverageGte=6.8&voteCountGte=150&primaryReleaseDateGte=2022-10-11&primaryReleaseDateLte=2026-10-10"
hasnt "  no runtime limit without --time" "$(cat "$T/server.log")" "withRuntimeLte"

echo "# plain text"
m tonight --dry-run
eq "rc 0" "$rc" "0"
eq "text output" "$out" "🎬 Movie night

🍿 Req_Film [Cut] (2025) · 95 min · ★6.5
you requested it, on Emby since 07.10 · Science Fiction, Drama

📺 Star Voyage S01E04 “The *Fourth*” · 45 min · ★8.2
next up: you watched S01E03 on 07.10 · 2 more after this

✨ Nova Drift (2025) · 118 min · ★8.4 · not on Emby yet
popular Science Fiction from 2025 · because you finished Arrival
   A: https://emby.example.test/web/index.html#!/item?id=106&context=home&serverId=srv1
   B: https://emby.example.test/web/index.html#!/item?id=2014&context=home&serverId=srv1
   C: button mt:req:7001 = media request 7001 --type movie"

echo "# remembered picks, --more, no repeats"
fresh
: > "$T/sql.log"
m tonight --json --args "scifi"
eq "rc 0" "$rc" "0"
eq "first run" "$(picks)" "A Req_Film [Cut] | B Star Voyage S01E04 | C Nova Drift"
eq "state: shown picks, mood words, time, mapping" "$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(sorted(d['shown'].items()), d['last']['words'], d['last']['time'], d['last']['picks'], d['emby']['uid'], d['emby']['at'])" "$T/st/tonight.json")" \
  "[('e:2014', '2026-10-10'), ('m:106', '2026-10-10'), ('t:7001', '2026-10-10')] ['scifi'] None ['m:106', 'e:2014', 't:7001'] 7 2026-10-10"
m tonight --json --more
eq "--more: 3 new picks" "$(picks)" "A Dune: Part Two | B Fresh Hit S01E01 | C Tight Thriller"
eq "  keeps the last mood" "$(printf '%s' "$out" | jq_ "d['mood']")" "scifi"
eq "  A because" "$(why A)" "because you finished Arrival · Science Fiction"
eq "  B new show (nothing else in progress)" "$(why B)" "new to you · because you watched Star Voyage · Drama, Thriller · 2 episodes on Emby"
eq "  B links the show" "$(printf '%s' "$out" | jq_ "[p['link'].split('?')[1] for p in d['picks'] if p['slot'] == 'B'][0]")" "id=203&context=home&serverId=srv1"
eq "  B new show: plays its S01E01, links the show's page" "$(printf '%s' "$out" | jq_ "[(p['kind'], p['episode'], p['emby_id'], p['series_id']) for p in d['picks'] if p['slot'] == 'B'][0]")" \
  "('series', 'S01E01', '2031', '203')"
eq "  B play id is exactly the episode shown (fixture DB)" "$(bep)" "ok"
eq "  the show-page button doesn't name an episode" "$(printf '%s' "$out" | jq_ "[b['text'] for r in d['buttons'] for b in r if 'id=203&' in b.get('url', '')]")" "['▶ Fresh Hit']"
printf '{"enabled": true, "tv": "media_player.test_tv", "device": "Test TV"}\n' > "$T/play.json"
MEDIA_PLAY_CONFIG=$T/play.json m tonight --json --more --dry-run
eq "  play on (next 3): A and B rows pair the link with Play on TV of the item shown" "$(printf '%s' "$out" | jq_ "[[(b['text'], b.get('data') or b['url'].split('?')[1].split('&')[0]) for b in r] for r in d['buttons'][:2]]")" \
  "[[('▶ Other User Saw', 'id=114'), ('📺 Play on TV', 'mt:play:114')], [('▶ Quick Laughs', 'id=206'), ('📺 Play on TV', 'mt:play:2061')]]"
eq "  that new show's play id is its S01E01 (fixture DB)" "$(bep) / $(printf '%s' "$out" | jq_ "[p['episode'] for p in d['picks'] if p['slot'] == 'B'][0]")" "ok / S01E01"
eq "mapping cached: Playback Reporting not read again" "$(sqlcount 'FROM PlaybackActivity')" "1"
MEDIA_NOW=2026-10-12T20:00:00+02:00 m tonight --json
eq "2 days later: the next episode may come back, the rest stay out" "$(picks)" "A Other User Saw | B Star Voyage S01E04 | C Trend Wave"
MEDIA_NOW=2026-10-25T20:00:00+02:00 m tonight --json --dry-run
eq "15 days later: the first picks are back" "$(picks)" "A Req_Film [Cut] | B Star Voyage S01E04 | C Nova Drift"

echo "# mood and time"
fresh
m tonight --json --dry-run --time 100
eq "--time 100: runtimes fit (details checked)" "$(picks)" "A Req_Film [Cut] | B Star Voyage S01E04 | C Tight Thriller"
has "  discover gets the runtime limit" "$(cat "$T/server.log")" "&withRuntimeLte=100"
m tonight --json --dry-run --time 30
eq "--time 30: short film, short show, short new one" "$(picks)" "A Short Film | B Quick Laughs S01E01 | C Tiny Short"
eq "  header" "$(printf '%s' "$out" | jq_ "d['message'].splitlines()[0]")" "🎬 **Movie night** · ≤ 30 min"
m tonight --json --dry-run --mood funny
eq "--mood funny" "$(picks)" "A Funny One | B Quick Laughs S01E01 | C Laugh Riot"
eq "  B: a new show that fits beats one in progress that doesn't" "$(why B)" "new to you · Comedy · 2 episodes on Emby"
m tonight --json --dry-run --mood dark
eq "--mood dark (German genre alias Krimi = Crime)" "$(picks)" "A Krimi Film | B Fresh Hit S01E01 | C Tight Thriller"
m tonight --json --dry-run --mood horror
eq "--mood horror (a genre)" "$(printf '%s' "$out" | jq_ "[p['title'] for p in d['picks'] if p['slot'] == 'A'][0]")" "Scary Night"
m tonight --json --dry-run --mood kids
eq "--mood kids lets Family titles through" "$(printf '%s' "$out" | jq_ "[p['title'] for p in d['picks'] if p['slot'] == 'A'][0]")" "Kids Toon"
m tonight --json --dry-run --args "blah 1h30"
eq "--args: time parsed, unknown mood noted" "$(printf '%s' "$out" | jq_ "(d['time'], d['mood'], d['message'].splitlines()[-1])")" \
  "(90, None, \"⚠️ unknown mood 'blah' (try: action, chill, cozy, dark, date, deep, doc, docu, epic, funny, kids, learn, light, mind, romantic, sad, scary, smart, tense or a genre)\")"
for c in "light 90:light:90" "2:None:120" "45m doc:doc:45" "1.5h:None:90" "Sci-Fi 2h:sci-fi:120"; do
  IFS=: read -r args wmood wtime <<< "$c"
  m tonight --json --dry-run --args "$args"
  eq "  --args '$args'" "$(printf '%s' "$out" | jq_ "(str(d['mood']), d['time'])")" "('$wmood', $wtime)"
done
m tonight --json --dry-run --args "light" --time 75
eq "--time wins over a time in --args" "$(printf '%s' "$out" | jq_ "(d['mood'], d['time'])")" "('light', 75)"
m tonight --time 0
eq "--time 0 refused" "$rc" "1"

echo "# quality words and ratings"
fresh
m tonight --json --dry-run --args "western"
eq "western: his fresh ★5.8 request doesn't beat a ★8.0 (under ★6 only when nothing better fits)" "$(apick)" "Solid Western"
eq "  no quality word: quality off" "$(printf '%s' "$out" | jq_ "(d['quality'], d['mood'])")" "(False, 'western')"
: > "$T/server.log"
m tonight --json --dry-run --args "good western"
eq "good western: quality on, in the label" "$(printf '%s' "$out" | jq_ "(d['quality'], d['mood'], d['message'].splitlines()[0])")" \
  "(True, 'good western', '🎬 **Movie night** · good western')"
eq "  A: the ★8.9 from 4 votes drops under ★7 once checked; the well-voted ★8.0 wins" "$(apick)" "Solid Western"
eq "  A fields: Emby's rating, TMDB's vote count" "$(printf '%s' "$out" | jq_ "[(p['rating'], p['votes']) for p in d['picks'] if p['slot'] == 'A'][0]")" "(8.0, 800)"
has "  the leader's vote count was checked" "$(cat "$T/server.log")" "GET /seerr/api/v1/movie/121"
hasnt "  the suspect ★10.0 never led, so never checked" "$(cat "$T/server.log")" "/api/v1/movie/120"
m tonight --json --dry-run --args "good scifi"
eq "good scifi: the ★8.6 beats his ★6.5 request" "$(picks)" "A Dune: Part Two | B Star Voyage S01E04 | C Nova Drift"
has "  C: discover asks for ★7.2+" "$(cat "$T/server.log")" "&genre=878&sortBy=popularity.desc&voteAverageGte=7.2&"
m tonight --json --dry-run --args "scifi"
eq "scifi without a quality word: his request still first" "$(apick)" "Req_Film [Cut]"
m tonight --json --args "gute western"
eq "German quality word (gute)" "$(apick) / $(printf '%s' "$out" | jq_ "d['quality']")" "Solid Western / True"
m tonight --json --more --dry-run
eq "--more keeps quality; nothing else ★7+ fits: the best rated" "$(apick)" "Gem Western"
has "  card shows the few votes" "$(printf '%s' "$out" | jq_ "d['message']")" "★8.9 (4 votes)"
has "  and says why" "$(printf '%s' "$out" | jq_ "d['message']")" "⚠️ no unwatched movie rated ★7+ fits; picked the best rated"
fresh
m tonight --json --dry-run --mood doc
eq "C: discover's next page when page 1 is all on the server" "$(printf '%s' "$out" | jq_ "[p['title'] for p in d['picks'] if p['slot'] == 'C']")" "['Deep Docs']"
has "  page 2 asked" "$(cat "$T/server.log")" "/discover/movies?page=2&genre=99&"
hasnt "  no page 3 (page 2 had something new)" "$(cat "$T/server.log")" "page=3&genre=99"

echo "# rating checks (unit)"
out=$(python3 - "$MEDIA" <<'PY' 2>&1
import importlib.machinery, importlib.util, sys, types
ld = importlib.machinery.SourceFileLoader("media_under_test", sys.argv[1])
m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
ld.exec_module(m)
t = m.Tonight(types.SimpleNamespace(), {}, [])
tmdb = {"1": (10.0, 2), "2": (8.833, 108), "3": (8.7, 31613), "5": (7.0, 0)}
t.seerr_item = lambda kind, i: ({"voteAverage": tmdb[str(i)][0], "voteCount": tmdb[str(i)][1]} if str(i) in tmdb else {})
def it(r, i=None):
    return {"rating": r, "tmdb": i, "kind": "tv"}
def show(x):
    return (round(t.rating(x), 2) if t.rating(x) is not None else None, x.get("shown"), x.get("votes"))
a, b, c, d, e, f = it(10.0, "1"), it(10.0, "2"), it(9.3, "3"), it(10.0), it(7.4, "4"), it(None, "5")
print("provisional", [t.rating(x) for x in (a, b, c, d, e, f)])
for x in (a, b, c, d, e, f):
    t.verify(x)
print("verified", [show(x) for x in (a, b, c, d, e, f)])
print("again", t.verify(a))
print("mood", m.tn_mood(["Good", "scifi"])[::4], m.tn_mood(["gute", "Komödie"])[1], m.tn_mood(["scifi"])[4])
print("line", m.tn_line({"slot": "A", "title": "X", "rating": 10.0, "votes": 2, "in_library": True}, False),
      "|", m.tn_line({"slot": "A", "title": "Y", "rating": 8.0, "votes": 800, "in_library": True}, False))
PY
)
eq "provisional: Emby 10.0 unknown until checked" "$(printf '%s\n' "$out" | grep '^provisional')" \
  "provisional [None, None, 9.3, None, 7.4, None]"
eq "checked: damped by TMDB votes; TMDB's average for a suspect 10.0; no TMDB id = unknown" "$(printf '%s\n' "$out" | grep '^verified')" \
  "verified [(6.47, 10.0, 2), (8.49, 8.833, 108), (9.3, 9.3, 31613), (None, None, None), (7.4, 7.4, None), (None, None, 0)]"
eq "checked once" "$(printf '%s\n' "$out" | grep '^again')" "again True"
eq "quality words" "$(printf '%s\n' "$out" | grep '^mood')" "mood ('good scifi', True) {'Comedy'} False"
eq "card: few votes named" "$(printf '%s\n' "$out" | grep '^line')" "line 🍿 X · ★10.0 (2 votes) | 🍿 Y · ★8.0"

echo "# Emby modes and user mapping"
: > "$T/sql.log"
MEDIA_EMBY_SQL=$ROOT_SQL m tonight --json --dry-run
eq "root sqlite3 command (ATTACH library): same picks" "$(picks)" "A Req_Film [Cut] | B Star Voyage S01E04 | C Nova Drift"
has "  library attached read-only" "$(cat "$T/sql.log")" "ATTACH 'file:$T/emby/library.db?immutable=1' AS lib;"
: > "$T/sql.log"
MEDIA_TONIGHT_EMBY_UID=7 m tonight --json --dry-run
eq "MEDIA_TONIGHT_EMBY_UID skips the mapping" "$(picks) / $(sqlcount 'FROM PlaybackActivity')" \
  "A Req_Film [Cut] | B Star Voyage S01E04 | C Nova Drift / 0"
MEDIA_TONIGHT_EMBY_UID=3 m tonight --json --dry-run
eq "  another user's history: his own progress in the show" "$(picks)" "A Req_Film [Cut] | B Star Voyage S01E02 | C Nova Drift"
eq "  B why" "$(why B)" "next up: you watched S01E01 on 01.10 · 4 more after this"
MEDIA_TONIGHT_USER=Friend m tonight --json --dry-run
eq "a user without enough overlap: refused, C still there (rc 3)" "$rc / $(printf '%s' "$out" | jq_ "(' / '.join(d['warnings']), [p['slot'] for p in d['picks']])")" \
  "3 / (\"Emby: can't tell your Emby user from the others (set MEDIA_TONIGHT_EMBY_UID)\", ['C'])"
MEDIA_TONIGHT_USER=Nobody m tonight --json --dry-run
eq "unknown Seerr user: rc 3" "$rc / $(printf '%s' "$out" | jq_ "d['warnings'][0]")" \
  "3 / Seerr: Seerr user 'Nobody': 0 matches (set MEDIA_TONIGHT_USER)"

echo "# partial failures"
fresh
MEDIA_EMBY_SQL="exit 1" m tonight --json
eq "Emby down: rc 3, only C (trending)" "$rc / $(picks)" "3 / C Trend Wave"
has "  owner sees why" "$(printf '%s' "$out" | jq_ "d['message']")" "⚠️ Emby's data didn't load: no library picks"
MEDIA_SECRETS=$T/seerr-down.json m tonight --json --dry-run
eq "Seerr down, no cached mapping: rc 1" "$rc" "1"
has "  says why" "$out" "media: no picks: Seerr: cannot reach seerr"
fresh
m tonight --json --args "scifi"
MEDIA_SECRETS=$T/seerr-down.json m tonight --json --dry-run
eq "Seerr down, cached mapping: A and B without links, rc 3" "$rc / $(printf '%s' "$out" | jq_ "[(p['slot'], p['link']) for p in d['picks']]")" \
  "3 / [('A', None), ('B', None)]"
has "  owner sees the failed pick" "$(printf '%s' "$out" | jq_ "d['message']")" "⚠️ the new pick failed"
fresh
MEDIA_SECRETS=$T/seerr-down.json MEDIA_EMBY_SQL="exit 1" m tonight --json
eq "everything down: rc 1, short reason" "$rc / $(printf '%s' "$out" | head -c 60)" "1 / media: no picks: Seerr: cannot reach seerr: [Errno 111] Conn"

echo "# help"
m -h
has "help lists tonight" "$out" "media tonight [--json] [--mood X] [--time MIN]"

echo "# safety"
eq "every request was a GET ($(wc -l < "$T/server.log") requests)" "$(grep -vc '^GET ' "$T/server.log")" "0"
eq "SQL passed the emby-sql verb's checks (no rejections)" "$(grep -c 'claude-bot-cmd' "$T/all.out")" "0"
eq "SQL is read-only" "$(grep -ciE '\b(insert|update|delete|drop|create|alter|pragma|vacuum)\b' "$T/sql.log")" "0"
hasnt "no Seerr key in output" "$(cat "$T/all.out")" "SEERR-KEY-Zq81"
hasnt "no tracebacks" "$(cat "$T/all.out")" "Traceback"
eq "fake server clean" "$(cat "$T/server.err")" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
