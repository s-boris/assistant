#!/usr/bin/env bash
# Offline tests for ~/bin/media's read-only extras (airing, missing, cutoff, health, trending, similar, hogs, unwatched),
# the ready-watch pinger (tg stubbed, fixture edited by ready_fx.py), tonight (tonight.sh), group (group.sh), continue (continue.sh), vibe (vibe.sh), series vibe + finish (series.sh), franchise (franchise.sh), wrapped (wrapped.sh), posters (posters.sh), digest (digest.sh), doctor (doctor.sh) plus a smoke test of
# search/queue. Exit 0 = all pass.
# A local fake Seerr/Radarr/Sonarr (fake_arr.py) stands in for the server; a temp config (MEDIA_SECRETS) points at
# it with fake keys, MEDIA_NOW fixes the clock at Sat 2026-10-10 12:00 Zurich, MEDIA_PAGE_SIZE=2 forces paging.
# Checks: output, Zurich times across the DST switch, partial failures, timeouts, errors, GET-only, no key leaks.
set -uo pipefail
# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}   # MEDIA_BIN: test another copy
export MEDIA_PLAY_CONFIG=/nonexistent/play.json   # tonight.sh asserts exact button rows: keep the live play.json out
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-tests.XXXXXX")
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT

echo ok > "$T/scenario"
python3 -I "$HERE/fake_arr.py" "$T/port" "$T/server.log" "$T/scenario" "$T/ready.json" 2>"$T/server.err" &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
DEAD=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')

mkcfg() {  # mkcfg FILE SEERR_URL RADARR_URL SONARR_URL
  printf '{"seerr": {"url": "%s", "key": "SEERR-KEY-Zq81"}, "radarr": {"url": "%s", "key": "RADARR-KEY-Xw42"}, "sonarr": {"url": "%s", "key": "SONARR-KEY-Vy73"}}\n' \
    "$2" "$3" "$4" > "$1"
}
mkcfg "$T/cfg.json" "$B/seerr" "$B/radarr" "$B/sonarr/"
mkcfg "$T/sonarr-down.json" "$B/seerr" "$B/radarr" "http://127.0.0.1:$DEAD"
mkcfg "$T/seerr-down.json" "http://127.0.0.1:$DEAD" "$B/radarr" "$B/sonarr"
mkcfg "$T/all-down.json" "http://127.0.0.1:$DEAD" "http://127.0.0.1:$DEAD" "http://127.0.0.1:$DEAD"
sed 's/SEERR-KEY-Zq81/WRONG/' "$T/cfg.json" > "$T/badkey.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d.pop("seerr"); json.dump(d, open(sys.argv[2], "w"))' "$T/cfg.json" "$T/noseerr.json"

export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-10T12:00:00+02:00 MEDIA_PAGE_SIZE=2 MEDIA_TIMEOUT=5

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }       # exact substring, newlines too
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
line() { printf '%s\n' "$1" | sed -n "${2}p"; }
m() { out=$(timeout 30 "$MEDIA" "$@" 2>&1); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; }
logn() { local n; n=$(grep -c -- "$1" "$T/server.log" 2>/dev/null); echo "${n:-0}"; }
jq_() { python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print($1)"; }
scen() { echo "$1" > "$T/scenario"; }
reset_log() { cat "$T/server.log" >> "$T/all.log"; : > "$T/server.log"; }

echo "# airing"
m airing
eq "airing rc 0" "$rc" "0"
eq "header" "$(line "$out" 1)" "Airing Sat 10.10 – Fri 16.10 (Zurich time, ✓ = on disk): 6 episodes, 2 movie releases"
eq "today first, marked" "$(line "$out" 2)" "Sat 10.10 (today)"
eq "aired-today episode on disk ✓" "$(line "$out" 3)" '  06:00 Slow Horses S06E05 "Sayonara" ✓'
has "UTC 03:30 -> 05:30 CEST next day" "$out" '  05:30 Night Show S52E03 "Host / Band"'
has "TBA title hidden" "$out" "  15:00 Daily Shop S2026E2911"
hasnt "  no TBA" "$out" "TBA"
has "3 eps collapsed, earliest time" "$out" "  09:00 Avatar: Seven Havens S01E04-E06 (3 eps)"
has "digital release" "$out" "  🎬 Hope (2026) digital"
has "physical release on disk" "$out" "  🎬 Tuner (2026) physical ✓"
hasnt "cinema hidden by default" "$out" "Clayface"
hasnt "outside window filtered" "$out" "Late Show"
hasnt "out-of-window movie filtered" "$out" "Old News"
eq "Tue block: day then movie" "$(printf '%s\n' "$out" | grep -A1 '^Tue 13.10')" "$(printf 'Tue 13.10\n  🎬 Hope (2026) digital')"
has "window start = Zurich midnight in UTC" "$(cat "$T/server.log")" "/sonarr/api/v3/calendar?start=2026-10-09T22%3A00%3A00Z&end=2026-10-16T22%3A00%3A00Z&unmonitored=false&includeSeries=true"
has "radarr calendar same window" "$(cat "$T/server.log")" "/radarr/api/v3/calendar?start=2026-10-09T22%3A00%3A00Z&end=2026-10-16T22%3A00%3A00Z&unmonitored=false"
m airing --cinema
has "--cinema adds cinema releases" "$out" "  🎬 Clayface (2026) cinema"
m airing --days 17
has "end across DST = 23:00Z" "$(tail -2 "$T/server.log")" "end=2026-10-26T23%3A00%3A00Z"
has "before DST: 19:00Z = 21:00 CEST" "$out" "$(printf 'Sat 24.10\n  21:00 Late Show S01E01 "Before DST"')"
has "after DST: 19:00Z = 20:00 CET" "$out" "$(printf 'Mon 26.10\n  20:00 Late Show S01E02 "After DST"')"
m airing --days 1
eq "1 day: single-day header" "$(line "$out" 1)" "Airing Sat 10.10 (Zurich time, ✓ = on disk): 1 episode, 0 movie releases"
MEDIA_NOW=2026-12-20T10:00:00+01:00 m airing
eq "nothing airing" "$out" "Nothing airing Sun 20.12 – Sat 26.12."
m airing --json
eq "json episodes" "$(printf '%s' "$out" | jq_ 'len(d["episodes"])')" "6"
eq "json movies" "$(printf '%s' "$out" | jq_ '[(x["title"], x["kind"], x["date"]) for x in d["movies"]]')" "[('Hope', 'digital', '2026-10-13'), ('Tuner', 'physical', '2026-10-16')]"
eq "json local time has offset" "$(printf '%s' "$out" | jq_ '[x["air_local"] for x in d["episodes"] if x["series"]=="Night Show"][0]')" "2026-10-11T05:30:00+02:00"
m airing --days 0
eq "--days 0 refused" "$rc" "1"

echo "# missing"
reset_log
m missing
eq "missing rc 0" "$rc" "0"
eq "header" "$(line "$out" 1)" "Missing (monitored, no file): 7 episodes in 2 shows, 2 released movies; 3 more not out yet"
has "show with most eps first" "$(line "$out" 3)" "  Daily Shop: 5 eps (S2026), last aired 25.09.2026"
has "seasons listed" "$out" "  Top Gear: 2 eps (S3,S4), last aired 01.01.1981"
has "longest-wanted released movie first" "$(printf '%s\n' "$out" | grep -A1 'released but not')" "  Stuck Doc (2023), wanted since 02.2024"
has "digital date preferred over cinema" "$out" "  Verity (2026): digital 27.10.2026"
has "cinema date when no digital" "$out" "  Wildwood (2026): cinema 22.10.2026"
has "no date" "$out" "  Ghost Film (2027): no date yet"
eq "sonarr paged (7 records, page size 2)" "$(logn 'sonarr/api/v3/wanted/missing')" "4"
eq "radarr paged (5 records)" "$(logn 'radarr/api/v3/wanted/missing')" "3"
has "monitored only" "$(cat "$T/server.log")" "wanted/missing?monitored=true&includeSeries=true&page=1&pageSize=2"
m missing -n 1
has "-n 1 truncates" "$out" "  … +1 more"
m missing --json
eq "json shows" "$(printf '%s' "$out" | jq_ '[(s["series"], s["episodes"], s["seasons"]) for s in d["shows"]]')" "[('Daily Shop', 5, [2026]), ('Top Gear', 2, [3, 4])]"
eq "json not-out order" "$(printf '%s' "$out" | jq_ '[m["title"] for m in d["movies_not_out"]]')" "['Wildwood', 'Verity', 'Ghost Film']"

echo "# cutoff"
m cutoff
eq "cutoff rc 0" "$rc" "0"
eq "header" "$(line "$out" 1)" "Below quality cutoff (upgrade candidates; upgrades usually need more space): 4 episodes in 2 shows, 2 movies"
has "group cutoff name via /series profile" "$out" "  Big Old Show: 3 eps below WEB 1080p (Up to 1080p)"
has "quality nested in a group" "$out" "  Fresh Show: 1 ep below Bluray-2160p (Up to 4K)"
has "movie quality from statistics" "$out" "  Startup.com (2001): has DVD, cutoff Bluray-1080p"
has "movie quality from movieFile" "$out" "  Another Film (2004): has DVD-R, cutoff Bluray-1080p"
m cutoff --json
eq "json" "$(printf '%s' "$out" | jq_ '(d["episodes_total"], [m["quality"] for m in d["movies"]])')" "(4, ['DVD-R', 'DVD'])"

echo "# health"
m health
eq "all fine one-liner" "$out" "all fine: Sonarr, Radarr, Seerr"
eq "rc 0" "$rc" "0"
m health --quiet
eq "--quiet silent when fine" "$out|$rc" "|0"
scen notes; m health
eq "notices don't count as problems" "$out|$rc" "all fine: Sonarr, Radarr, Seerr · notes: Radarr: New update available; Seerr: update available (3 commits behind)|0"
scen unhealthy; m health
eq "problems rc 2" "$rc" "2"
eq "problem count" "$(line "$out" 1)" "media health: 4 problems"
has "arr warning" "$out" "  Sonarr warning: Indexers unavailable due to failures: NZBgeek"
has "arr error" "$out" "  Radarr error: Missing root folder: /data/Movies"
has "Seerr restart" "$out" "  Seerr warning: restart required"
has "Seerr serviceErrors" "$out" "  Seerr error: cannot reach radarr: Radarr 4K"
has "note kept" "$out" "  (Seerr note: update available)"
m health --quiet
eq "--quiet still reports problems" "$rc" "2"
m health --json
eq "json" "$(printf '%s' "$out" | jq_ '(d["ok"], len(d["problems"]), d["problems"][0]["wiki"])')" "(False, 4, 'https://wiki.servarr.com/sonarr/system#indexers')"
scen ok
MEDIA_SECRETS=$T/sonarr-down.json m health
eq "unreachable service rc 2" "$rc" "2"
has "unreachable reported, others checked" "$out" "  Sonarr error: unreachable (cannot reach sonarr:"

echo "# trending"
reset_log
m trending -n 2
eq "trending -n 2" "$out" "$(printf 'Trending titles this week (TMDB via Seerr):\n 1. Spider-Man: Brand New Day (2026) [movie] ★8.4 tmdb:101 -- available\n 2. Carrie (2026) [tv] ★8.4 tmdb:102 -- not requested')"
eq "one page is enough" "$(logn 'discover/trending')" "1"
m trending
eq "all 6 titles over 2 pages, person dropped" "$(printf '%s\n' "$out" | grep -c '^ *[0-9]*\. ')" "6"
hasnt "no people" "$out" "Some Actor"
has "pending request status" "$out" "Pending Show (2026) [tv] ★7.0 tmdb:105 -- requested (pending approval)"
has "no star without rating" "$out" " 6. Matchbox the Movie (2026) [movie] tmdb:107 -- not requested"
m trending --new
eq "--new: only not on server / not requested" "$(printf '%s\n' "$out" | sed 1d | sed -E 's/ \(.*//')" "$(printf ' 1. Carrie\n 2. Matchbox the Movie')"
m trending --type tv -n 2
eq "--type tv" "$(printf '%s\n' "$out" | sed 1d | sed -E 's/ \(.*//')" "$(printf ' 1. Carrie\n 2. Pending Show')"
has "type in header" "$out" "Trending shows this week"
m trending --json -n 1
eq "json" "$(printf '%s' "$out" | jq_ 'd')" "[{'title': 'Spider-Man: Brand New Day', 'year': '2026', 'type': 'movie', 'tmdb': 101, 'rating': 8.4, 'status': 'available'}]"

echo "# similar"
m similar Inception
eq "similar header (exact match picked)" "$(line "$out" 1)" "Like Inception (2010) [movie]:"
has "recommendation with status" "$out" " 2. The Dark Tower (2017) [movie] ★5.8 tmdb:353491 -- available"
m similar Inception --new
eq "--new" "$(printf '%s\n' "$out" | sed 1d | sed -E 's/ \(.*//')" "$(printf ' 1. Paycheck\n 2. Interstellar')"
m similar "breaking bad"
eq "falls back to /similar when no recommendations" "$out" "$(printf 'Like Breaking Bad (2008) [tv]:\n 1. Narcos: Mexico (2018) [tv] ★7.9 tmdb:80968 -- not requested\n 2. The Wire (2002) [tv] ★8.6 tmdb:1438 -- available')"
m similar --tmdb 27205 --type movie -n 1
eq "--tmdb" "$(line "$out" 1)|$(printf '%s\n' "$out" | wc -l | tr -d ' ')" "Like Inception (2010) [movie]:|2"
m similar --tmdb 27205
eq "--tmdb needs --type" "$rc" "1"
m similar zzzunknown
eq "no match" "$out|$rc" 'no match for "zzzunknown"|0'
m similar Inception --json -n 1
eq "json" "$(printf '%s' "$out" | jq_ '(d["like"]["tmdb"], d["results"][0]["title"])')" "(27205, 'Paycheck')"

echo "# hogs"
reset_log
m hogs
eq "hogs rc 0" "$rc" "0"
eq "disk line" "$(line "$out" 1)" "/data: 113 TB used of 123 TB (92%), 9.7 TB free"
has "library totals skip empty titles" "$(line "$out" 2)" "Library: movies 3 = 112 GB, shows 2 ="
eq "quality breakdown" "$(line "$out" 3)" "Movies by quality: Remux-2160p 1 = 80.0 GB, Bluray-2160p 1 = 30.0 GB, DVD 1 = 2.0 GB"
eq "top header" "$(line "$out" 4)" "Top 5 by size = 1.4 TB, 100% of the library:"
eq "1st: big show, requesters oldest first" "$(line "$out" 5)" " 1. Big Old Show [tv] 1.2 TB · 8 seasons/160 eps (7.5 GB/ep) · ended · last aired 05.2012 · added 01.2019 · req. PersonG+PersonH"
eq "2nd: movie with quality and file date" "$(line "$out" 6)" " 2. Huge Remux (2002) 80.0 GB · Remux-2160p · file from 06.2019 · req. Timon"
eq "3rd: requester matched by tmdb fallback" "$(line "$out" 7)" " 3. Fresh Show [tv] 50.0 GB · 2 seasons/20 eps (2.5 GB/ep) · continuing · last aired 10.2026 · added 03.2024 · req. Noah"
eq "5th: statistics-only size" "$(line "$out" 9)" " 5. Small Old (1999) 2.0 GB · DVD · file from 01.2015"
hasnt "no-file titles skipped" "$out" "No File"
eq "Seerr requests paged (5, page size 2)" "$(logn 'seerr/api/v1/request')" "3"
m hogs --sort stale
eq "stale order (size x idle years)" "$(printf '%s\n' "$out" | sed -n '5,9p' | sed -E 's/^ *[0-9]+\. //; s/ [\[(0-9].*//')" "$(printf 'Big Old Show\nHuge Remux\nSmall Old\nNew Big\nFresh Show')"
has "idle years shown" "$out" "nothing new for 7.7 y"
m hogs --top 2 --type movie
eq "--type movie --top 2" "$(printf '%s\n' "$out" | sed -n '2p;4,6p')" "$(printf 'Library: movies 3 = 112 GB\nTop 2 by size = 110 GB, 98%% of the library:\n 1. Huge Remux (2002) 80.0 GB · Remux-2160p · file from 06.2019 · req. Timon\n 2. New Big (2026) 30.0 GB · Bluray-2160p · file from 09.2026')"
m hogs --type tv --top 1
eq "--type tv uses Sonarr disk space, no movie quality line" "$(printf '%s\n' "$out" | sed -n '1,3p')" "$(printf '/data: 113 TB used of 123 TB (92%%), 9.7 TB free\nLibrary: shows 2 = 1.2 TB\nTop 1 by size = 1.2 TB, 96%% of the library:')"
m hogs --json --top 1
eq "json" "$(printf '%s' "$out" | jq_ '(d["top"][0]["title"], d["top"][0]["requested_by"], d["top"][0]["score"], d["library"]["movie"]["titles"], d["top"][0]["added"])')" "('Big Old Show', ['PersonG', 'PersonH'], 9240, 3, '2019-01-15T10:00:00+00:00')"
MEDIA_SECRETS=$T/seerr-down.json m hogs --top 1
eq "Seerr down: still works, rc 0" "$rc" "0"
has "  and says names are missing" "$out" "(no requester names: Seerr requesters: cannot reach seerr:"
scen badjson; m hogs --top 1
eq "Radarr broken: partial rc 3" "$rc" "3"
has "  shows still listed" "$out" " 1. Big Old Show [tv]"
has "  clear non-JSON message" "$out" "!! skipped Radarr: radarr sent non-JSON on GET /api/v3/movie (wrong URL, or a login page?)"
scen ok

echo "# errors and partial results"
scen http500; m missing
eq "Sonarr 500: partial rc 3" "$rc" "3"
has "  movies still shown" "$out" "  Stuck Doc (2023)"
has "  HTTP error explained" "$out" '!! skipped Sonarr: sonarr HTTP 500 on GET /api/v3/wanted/missing: {"message": "database is locked"}'
m airing --json
eq "json partial: rc 3 + warnings" "$rc|$(printf '%s' "$out" | jq_ 'len(d["warnings"])')" "3|1"
m queue
eq "existing command: HTTP error -> rc 1" "$rc" "1"
has "  same message style" "$out" "media: sonarr HTTP 500 on GET /api/v3/queue"
scen ok
MEDIA_SECRETS=$T/all-down.json m missing
eq "all down: rc 1" "$rc" "1"
has "  both causes named" "$out" "media: Sonarr: cannot reach sonarr:"
has "  ... and Radarr" "$out" "; Radarr: cannot reach radarr:"
scen slow
s0=$(date +%s.%N); MEDIA_TIMEOUT=1 m trending; s1=$(date +%s.%N)
eq "timeout rc 1" "$rc" "1"
eq "timeout message" "$out" "media: seerr timed out after 1s on GET /api/v1/discover/trending"
eq "gave up after ~1 s (server sleeps 3 s)" "$(python3 -c "print($s1 - $s0 < 2.5)")" "True"
scen ok
MEDIA_SECRETS=$T/badkey.json m trending
eq "wrong key: 401 explained" "$rc|$out" '1|media: seerr HTTP 401 on GET /api/v1/discover/trending: {"error": "Unauthorized"}'
MEDIA_SECRETS=$T/noseerr.json m trending
has "missing service config" "$out" "media: no seerr url/key in $T/noseerr.json"
MEDIA_SECRETS=$T/nope.json m health
eq "missing config file" "$rc|$out" "1|media: cannot read $T/nope.json: FileNotFoundError"
m missing -n 0
eq "-n 0 refused" "$rc" "1"

echo "# ready-watch"
reset_log
python3 -I "$HERE/ready_fx.py" "$T/ready.json" init
rfx() { python3 -I "$HERE/ready_fx.py" "$T/ready.json" "$@"; }
cat > "$T/tg" <<'STUB'
#!/bin/sh
[ -e "$TG_FAIL" ] && exit 1
printf '%s\n--\n' "$1" >> "$TG_LOG"
STUB
chmod +x "$T/tg"
export MEDIA_READY_STATE=$T/rw MEDIA_READY_TG=$T/tg MEDIA_READY_INBOX=$T/inbox TG_LOG=$T/tg.log TG_FAIL=$T/tg.fail
export MEDIA_READY_QUIET=0-8 MEDIA_READY_FAIL_NOTE=2
: > "$T/tg.log"
tgn() { grep -c '^--$' "$T/tg.log" 2>/dev/null || true; }
sent() { cat "$T/tg.log"; : > "$T/tg.log"; }
rw() { local at=$1; shift; MEDIA_NOW=$at m ready-watch "$@"; }
st() { python3 -c "import json,sys; d=json.load(open('$T/rw/state.json')); print($1)"; }

rw 2026-10-10T10:00:00Z
eq "baseline rc 0" "$rc" "0"
has "baseline says so" "$out" "baseline: 1 of robin's requested movies already available; no pings"
has "Sonarr baseline too" "$out" "baseline: Sonarr history up to 2026-10-10T10:00:00Z"
eq "baseline sends nothing" "$(tgn)" "0"
eq "user resolved exactly (not 'Robin'; robin = the fixture profile's services.seerr_user)" "$(st 'd["user"]')" "{'query': 'robin', 'id': 1, 'name': 'robin'}"
eq "available movies recorded (4K request needs status4k)" "$(st 'd["movies_done"]')" "['501']"
eq "pre-baseline import marked seen" "$(st 'd["sonarr"]["seen"]')" "[1]"
has "Seerr requests paged" "$(cat "$T/server.log")" "/seerr/api/v1/user/1/requests?take=2&skip=2"
has "history since cursor - 1 h" "$(cat "$T/server.log")" "/sonarr/api/v3/history/since?date=2026-10-10T09%3A00%3A00Z&includeSeries=true&includeEpisode=true"

rw 2026-10-10T10:10:00Z
eq "nothing new: silent" "$out|$rc|$(tgn)" "|0|0"
has "pre-baseline import not pinged later" "$(cat "$T/server.log")" "date=2026-10-10T09%3A00%3A00Z"
eq "user cached (no user list per run)" "$(grep -c 'seerr/api/v1/user?' "$T/server.log")" "2"

rfx movie 502 5; rfx movie 601 5; rfx movie 503 5; rfx movie 901 5
rfx imp 2 2026-10-10T10:15:00Z pl 1 3; rfx imp 3 2026-10-10T10:15:00Z pl 1 4; rfx imp 4 2026-10-10T10:15:01Z pl 1 5
rfx imp 5 2026-10-10T10:15:00Z ls 1 1
rfx upg 6 2026-10-10T10:16:00Z sh 6 4
rfx grab 7 2026-10-10T10:17:00Z sh 6 9
rw 2026-10-10T10:20:00Z
eq "ready run rc 0" "$rc" "0"
eq "movie + show pinged, one message each" "$(sent)" "$(printf '🎬 Dune: Part Three (2026) is ready on Emby\n--\n📺 Pluribus S01E03–E05 ready\n--')"
has "log line for cron" "$out" "2026-10-10 12:20 sent: 🎬 Dune: Part Three (2026) is ready on Emby"
rw 2026-10-10T10:30:00Z
eq "no repeat pings" "$out|$(tgn)" "|0"

rfx imp 8 2026-10-10T10:35:00Z sh 6 8; rfx imp 9 2026-10-10T10:35:00Z sh 6 10; rfx imp 10 2026-10-10T10:35:00Z sh 6 11
rfx imp 11 2026-10-10T10:35:00Z sh 5 2
rw 2026-10-10T10:40:00Z
eq "non-contiguous + two seasons in one message" "$(sent)" "$(printf '📺 Slow Horses S05E02, S06E08, E10–E11 ready\n--')"

echo "# ready-watch: quiet hours, retries, batching"
rfx addmovie 1 505 "Night Movie" 2025-11-11 5
rfx imp 12 2026-10-11T00:50:00Z pl 1 6
rw 2026-10-11T01:00:00Z
eq "03:00 Zurich: held, not sent" "$rc|$(tgn)" "0|0"
has "held logged" "$out" "quiet hours: holding 2 ping(s): Night Movie (2025); Pluribus S01E06"
rfx imp 13 2026-10-11T02:50:00Z pl 1 7
rw 2026-10-11T03:00:00Z
eq "05:00: still held" "$(tgn)" "0"
eq "same show merged in the queue" "$(st '[p.get("eps") for p in d["pending"]]')" "[None, {'1': [6, 7]}]"
rw 2026-10-11T06:10:00Z
eq "08:10: delivered" "$(sent)" "$(printf '🎬 Night Movie (2025) is ready on Emby\n--\n📺 Pluribus S01E06–E07 ready\n--')"
eq "queue empty" "$(st 'd["pending"]')" "[]"

touch "$T/tg.fail"; rfx addmovie 1 506 "Retry Film" 2026-01-01 5
rw 2026-10-11T06:20:00Z
eq "tg fails: rc 3, kept" "$rc|$(st 'len(d["pending"])')" "3|1"
has "tg failure logged" "$out" "tg failed; 1 ping(s) kept for the next run"
MEDIA_NOW=2026-10-11T06:25:00Z m ready-watch --status
has "--status shows the pending ping" "$out" "pending: 1
  🎬 Retry Film (2026) is ready on Emby"
has "--status shows failures" "$out" "failing: 1 run(s) in a row; last error: tg: delivery failed"
rm -f "$T/tg.fail"
rw 2026-10-11T06:30:00Z
eq "retried once tg works" "$rc|$(sent)" "0|$(printf '🎬 Retry Film (2026) is ready on Emby\n--')"

for i in 1 2 3 4 5; do rfx addmovie 1 $((510 + i)) "Batch $i" 2026-0$i-01 5; done
rw 2026-10-11T06:40:00Z
eq "5 at once: one combined message" "$(sent)" "$(printf '🍿 Ready on Emby (5):\n🎬 Batch 1 (2026)\n🎬 Batch 2 (2026)\n🎬 Batch 3 (2026)\n🎬 Batch 4 (2026)\n🎬 Batch 5 (2026)\n--')"

echo "# ready-watch: failures"
scen http500; rfx addmovie 1 520 "During Outage" 2026-02-02 5; rfx imp 14 2026-10-11T06:45:00Z sh 6 12
rw 2026-10-11T06:50:00Z
eq "Sonarr down: movie still pinged, rc 3" "$rc|$(sent)" "3|$(printf '🎬 During Outage (2026) is ready on Emby\n--')"
has "Sonarr error logged" "$out" "!! Sonarr: sonarr HTTP 500 on GET /api/v3/history/since"
scen ok
rw 2026-10-11T07:00:00Z
eq "Sonarr back: missed episode caught up" "$rc|$(sent)" "0|$(printf '📺 Slow Horses S06E12 ready\n--')"
eq "failure counter reset" "$(st 'd["failures"]')" "0"
MEDIA_SECRETS=$T/seerr-down.json rw 2026-10-11T07:10:00Z
eq "Seerr down: rc 1, nothing sent" "$rc|$(tgn)" "1|0"
has "Seerr error" "$out" "media: ready-watch: cannot reach seerr:"
eq "no inbox note after 1 failure" "$(ls "$T/inbox" 2>/dev/null | wc -l | tr -d ' ')" "0"
MEDIA_SECRETS=$T/seerr-down.json rw 2026-10-11T07:20:00Z
eq "inbox note at the threshold" "$(ls "$T/inbox" | wc -l | tr -d ' ')" "1"
has "note explains" "$(cat "$T"/inbox/*)" "media ready-watch has failed 2 runs in a row"
MEDIA_SECRETS=$T/seerr-down.json rw 2026-10-11T07:30:00Z
eq "only one note per streak" "$(ls "$T/inbox" | wc -l | tr -d ' ')" "1"

rfx addmovie 1 530 "Dry Film" 2026-03-03 5
cp "$T/rw/state.json" "$T/state.before"
rw 2026-10-11T07:40:00Z --dry-run
eq "dry run prints, sends nothing" "$rc|$(tgn)" "0|0"
has "dry run says what it would send" "$out" "(dry run) would send: 🎬 Dry Film (2026) is ready on Emby"
eq "dry run saves nothing" "$(cmp -s "$T/rw/state.json" "$T/state.before" && echo same)" "same"
rw 2026-10-11T07:50:00Z
eq "then sent for real" "$(sent)" "$(printf '🎬 Dry Film (2026) is ready on Emby\n--')"

echo '{broken' > "$T/rw/state.json"; rm -f "$T"/inbox/*
rfx addmovie 1 540 "After Corruption" 2026-04-04 5
rw 2026-10-11T08:00:00Z
eq "corrupt state: fresh baseline, no flood" "$rc|$(tgn)" "0|0"
has "  says so" "$out" "state unreadable (JSONDecodeError), moved to"
eq "  inbox note" "$(ls "$T/inbox" | wc -l | tr -d ' ')" "1"

rm -rf "$T/rw"; scen http500
rw 2026-10-11T08:10:00Z
eq "first run with Sonarr down: rc 3, no pings" "$rc|$(tgn)" "3|0"
eq "  movie baseline kept, Sonarr baseline pending" "$(st 'd.get("sonarr") is None and len(d["movies_done"]) > 5')" "True"
scen ok
rw 2026-10-11T08:20:00Z
has "  Sonarr baseline on the next good run" "$out" "baseline: Sonarr history up to"
eq "  still no pings" "$(tgn)" "0"
hasnt "never pinged: declined / PersonG's / capitalized Robin / 4K" "$(cat "$T/all.out")" "Declined Film is ready"
hasnt "  PersonG's movie" "$(cat "$T/all.out")" "PersonG's Movie"
hasnt "  PersonG's show" "$(cat "$T/all.out")" "PersonG Show"
hasnt "  capitalized Robin" "$(cat "$T/all.out")" "Capitalized Robin"
hasnt "  4K-only request" "$(cat "$T/all.out")" "Four K Film"
hasnt "  upgrade" "$(cat "$T/all.out")" "S06E04"
unset MEDIA_READY_STATE MEDIA_READY_TG MEDIA_READY_INBOX MEDIA_READY_QUIET MEDIA_READY_FAIL_NOTE

echo "# existing commands still work"
m search inception
eq "search" "$(line "$out" 1)" "Inception (2010)  [movie]  tmdb:27205  -- available"
m queue
eq "queue" "$out" "nothing downloading"
echo "# --json on search/status/queue/recent/request (the typed tools' shape)"
m search inception --json
eq "search --json" "$(printf '%s' "$out" | jq_ '[(x["title"], x["year"], x["type"], x["tmdb"], x["status"]) for x in d][0]')" \
  "('Inception', '2010', 'movie', 27205, 'available')"
m status inception --json
eq "status --json" "$(printf '%s' "$out" | jq_ '(d[0]["title"], d[0]["status"], d[0]["requests"], d[0]["downloading"])')" \
  "('Inception', 'available', [], [])"
m queue --json
eq "queue --json" "$out" '{
 "movies": [],
 "tv": []
}'
m request 27205 --type movie --json
eq "request --json: already there" "$(printf '%s' "$out" | jq_ '(d["result"], d["reason"])')" \
  "('not_requested', 'already available')"
m -h
has "help lists the extras" "$out" "media hogs [--top 20]"

echo "# unwatched (own suite: unwatched.sh, fake_unwatched.py, unwatched_fx.py)"
if uw=$(MEDIA_BIN=$MEDIA bash "$HERE/unwatched.sh" 2>&1); then ok "unwatched suite: $(printf '%s\n' "$uw" | tail -1)"
else no "unwatched suite" "$(printf '%s\n' "$uw" | grep -A3 '^FAIL' | head -12)"; fi
m -h
has "help lists unwatched" "$out" "media unwatched [--top 15]"

echo "# tonight (own suite: tonight.sh, fake_tonight.py, tonight_fx.py)"
if tn=$(MEDIA_BIN=$MEDIA bash "$HERE/tonight.sh" 2>&1); then ok "tonight suite: $(printf '%s\n' "$tn" | tail -1)"
else no "tonight suite" "$(printf '%s\n' "$tn" | grep -A3 '^FAIL' | head -12)"; fi

echo "# group (own suite: group.sh, group_fx.py; reuses tonight_fx.py and fake_tonight.py)"
if gr=$(MEDIA_BIN=$MEDIA bash "$HERE/group.sh" 2>&1); then ok "group suite: $(printf '%s\n' "$gr" | tail -1)"
else no "group suite" "$(printf '%s\n' "$gr" | grep -A2 '^FAIL' | head -20)"; fi

echo "# continue (own suite: continue.sh, fake_continue.py)"
if ct=$(MEDIA_BIN=$MEDIA bash "$HERE/continue.sh" 2>&1); then ok "continue suite: $(printf '%s\n' "$ct" | tail -1)"
else no "continue suite" "$(printf '%s\n' "$ct" | grep -A3 '^FAIL' | head -12)"; fi

echo "# vibe (own suite: vibe.sh, fake_vibe.py; reuses fake_tonight.py)"
if vb=$(MEDIA_BIN=$MEDIA bash "$HERE/vibe.sh" 2>&1); then ok "vibe suite: $(printf '%s\n' "$vb" | tail -1)"
else no "vibe suite" "$(printf '%s\n' "$vb" | grep -A3 '^FAIL' | head -12)"; fi
echo "# series vibe + finish (own suite: series.sh, fake_vibe.py)"
if sv=$(MEDIA_BIN=$MEDIA bash "$HERE/series.sh" 2>&1); then ok "series suite: $(printf '%s\n' "$sv" | tail -1)"
else no "series suite" "$(printf '%s\n' "$sv" | grep -A3 '^FAIL' | head -12)"; fi

echo "# play (own suite: play/run.sh, play/fake_emby.py, play/fake_ha.py)"
if pl=$(MEDIA_BIN=$MEDIA bash "$HERE/play/run.sh" 2>&1); then ok "play suite: $(printf '%s\n' "$pl" | tail -1)"
else no "play suite" "$(printf '%s\n' "$pl" | grep -A3 '^FAIL' | head -12)"; fi

echo "# franchise (own suite: franchise.sh, fake_franchise.py)"
if fr=$(MEDIA_BIN=$MEDIA bash "$HERE/franchise.sh" 2>&1); then ok "franchise suite: $(printf '%s\n' "$fr" | tail -1)"
else no "franchise suite" "$(printf '%s\n' "$fr" | grep -A3 '^FAIL' | head -12)"; fi
echo "# wrapped (own suite: wrapped.sh, wrapped_fx.py; reuses tonight_fx.py's SQL verb)"
if wr=$(MEDIA_BIN=$MEDIA bash "$HERE/wrapped.sh" 2>&1); then ok "wrapped suite: $(printf '%s\n' "$wr" | tail -1)"
else no "wrapped suite" "$(printf '%s\n' "$wr" | grep -A3 '^FAIL' | head -12)"; fi

echo "# posters (own suite: posters.sh, posters_fx.py)"
if po=$(MEDIA_BIN=$MEDIA bash "$HERE/posters.sh" 2>&1); then ok "posters suite: $(printf '%s\n' "$po" | tail -1)"
else no "posters suite" "$(printf '%s\n' "$po" | grep -A2 '^FAIL' | head -20)"; fi

echo "# digest (own suite: digest.sh, digest_fx.py)"
if dg=$(MEDIA_BIN=$MEDIA bash "$HERE/digest.sh" 2>&1); then ok "digest suite: $(printf '%s\n' "$dg" | tail -1)"
else no "digest suite" "$(printf '%s\n' "$dg" | grep '^FAIL' | head -8)"; fi
echo "# doctor (own suite: doctor.sh, fake_doctor.py; its fixes are checked there, so the GET-only check below stays)"
if dr=$(MEDIA_BIN=$MEDIA bash "$HERE/doctor.sh" 2>&1); then ok "doctor suite: $(printf '%s\n' "$dr" | tail -1)"
else no "doctor suite" "$(printf '%s\n' "$dr" | grep -A3 '^FAIL' | head -12)"; fi

echo "# safety"
reset_log
eq "every request was a GET ($(wc -l < "$T/all.log") requests)" "$(grep -vc '^GET ' "$T/all.log")" "0"
hasnt "no Seerr key in any output" "$(cat "$T/all.out")" "SEERR-KEY-Zq81"
hasnt "no Radarr key in any output" "$(cat "$T/all.out")" "RADARR-KEY-Xw42"
hasnt "no Sonarr key in any output" "$(cat "$T/all.out")" "SONARR-KEY-Vy73"
hasnt "no tracebacks" "$(cat "$T/all.out")" "Traceback"
eq "fake server clean" "$(cat "$T/server.err")" ""

echo "# URL source: profile first, media.json fallback, own config, missing live file (test_url_source.py)"
if pu=$(MEDIA_BIN=$MEDIA python3 -I "$HERE/test_url_source.py" 2>&1); then ok "url source suite: $(printf '%s\n' "$pu" | tail -1)"
else no "url source suite" "$(printf '%s\n' "$pu" | grep '^FAIL' | head -8)"; fi
echo "# Tracearr key by handle: hub mode without the key file, clear local/shadow error (test_tracearr_key.py)"
if pt=$(MEDIA_BIN=$MEDIA python3 -I "$HERE/test_tracearr_key.py" 2>&1); then ok "tracearr key suite: $(printf '%s\n' "$pt" | tail -1)"
else no "tracearr key suite" "$(printf '%s\n' "$pt" | grep '^FAIL' | head -8)"; fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
