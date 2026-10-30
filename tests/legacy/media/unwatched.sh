#!/usr/bin/env bash
# Offline tests for `media unwatched` (cleanup candidates nobody watches). Exit 0 = all pass.
# fake_unwatched.py stands in for Sonarr/Radarr/Seerr/Tracearr; unwatched_fx.py builds Emby's playback_reporting.db +
# library.db and plays sqlite3 (MEDIA_EMBY_SQL), logging the SQL it gets. Clock: Sat 2026-10-10 12:00 Zurich.
# Checks: ranking, thresholds, Tracearr/Emby cutoff, id/folder/localized-title/ambiguous matching, viewers across
# sources, bogus 1970 dates, short plays, stale seasons, report, partial failures, GET-only, read-only SQL, no key leaks.
set -uo pipefail
# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-uw-tests.XXXXXX")
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT

python3 -I "$HERE/unwatched_fx.py" build "$T/emby"
python3 -I "$HERE/fake_unwatched.py" "$T/port" "$T/server.log" 2>"$T/server.err" &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
DEAD=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
mkcfg() {  # mkcfg FILE TRACEARR_URL   (Tracearr key comes from MEDIA_TRACEARR_KEYFILE)
  printf '{"seerr": {"url": "%s/seerr", "key": "SEERR-KEY-Zq81"}, "radarr": {"url": "%s/radarr", "key": "RADARR-KEY-Xw42"}, "sonarr": {"url": "%s/sonarr", "key": "SONARR-KEY-Vy73"}, "tracearr": {"url": "%s"}}\n' \
    "$B" "$B" "$B" "$2" > "$1"
}
mkcfg "$T/cfg.json" "$B/tracearr"
mkcfg "$T/ta-down.json" "http://127.0.0.1:$DEAD"
echo TRACEARR-KEY-Tk55 > "$T/ta-key"
echo WRONG > "$T/bad-key"
FAKE_SQL="python3 -I $HERE/unwatched_fx.py sql $T/emby $T/sql.log"
export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-10T12:00:00+02:00 MEDIA_PAGE_SIZE=2 MEDIA_TIMEOUT=5 \
  MEDIA_TRACEARR_KEYFILE=$T/ta-key MEDIA_EMBY_DIR=$T/emby MEDIA_EMBY_SQL=$FAKE_SQL MEDIA_REPORT_DIR=$T/reports

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
m() { out=$(timeout 30 "$MEDIA" "$@" 2>&1); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; }
jq_() { python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print($1)"; }
cand() { printf '%s' "$out" | jq_ "[(c['plays'], c['viewers'], c['last_played']) for c in d['candidates'] if c['title'] == '$1' and str(c['year']) == '$2'][0]"; }

echo "# summary"
m unwatched
eq "rc 0" "$rc" "0"
eq "summary" "$out" "Unwatched: play history since 25.02.2020 (Emby Playback Reporting, Tracearr from 25.08.2026)
Reclaimable, on disk >90 d:
  no plays 180 d: 2.1 TB (3 shows 2.0 TB, 3 movies 95.0 GB)
  no plays 365 d: 1.8 TB (2 shows 1.7 TB, 3 movies 95.0 GB)
  never played: 585 GB (1 show 500 GB, 2 movies 85.0 GB)
Shows, top 3 of 3 (size × idle time):
 1. Big Old Show · 1.2 TB · last 03.2024 · req. PersonG
 2. Never Show · 500 GB · never played
 3. Ubice mog oca · 300 GB · last 03.2026
Movies, top 3 of 3 (size × idle time):
 1. Huge Remux (2002) · 80.0 GB · never played · req. Timon
 2. Id Movie (2010) · 10.0 GB · last 06.2023
 3. Short One (2018) · 5.0 GB · never played
Stale seasons of watched big shows (365 d): 150 GB in 1 season
88% of last year's plays map to Sonarr/Radarr titles (the rest: 1 title deleted or added by hand)"
m unwatched --top 2
eq "--top 2 splits 1 show + 1 movie" "$(printf '%s\n' "$out" | grep -c '^ [0-9]\. ')" "2"
m unwatched --type tv --top 2
has "--type tv: shows only" "$out" "Shows, top 2 of 3"
hasnt "  no movies" "$out" "Movies"
has "  totals for shows only" "$out" "  no plays 180 d: 2.0 TB (3 shows 2.0 TB)"
has "  stale seasons still listed" "$out" "150 GB in 1 season"

echo "# matching and counting (json, --days 1 makes every played title a candidate)"
m unwatched --days 1 --json
eq "json rc 0" "$rc" "0"
eq "candidates" "$(printf '%s' "$out" | jq_ 'len(d["candidates"])')" "12"
eq "Big Old Show: 2 plays (same-day segments merged), 2 viewers (backup table too)" "$(cand 'Big Old Show' 2004)" "(2, 2, '2024-03-01')"
eq "localized Emby title -> Sonarr item" "$(cand 'Ubice mog oca' 2017)" "(1, 1, '2026-03-01')"
eq "viewer seen in Emby + Tracearr counts once" "$(cand 'Watched Big Show' 2021)" "(2, 1, '2026-09-20')"
eq "show via Tracearr show rollup (not the episode's tvdb id)" "$(cand 'Fresh Show' 2024)" "(2, 1, '2026-10-01')"
eq "Emby row after Tracearr's start ignored (no double count)" "$(cand 'Small Old' 1999)" "(1, 1, '2026-09-15')"
eq "Tracearr rating key -> Emby id -> folder" "$(cand 'Emby Id Film' 2015)" "(1, 1, '2026-09-10')"
eq "Emby id with another name -> tmdb" "$(cand 'Id Movie' 2010)" "(1, 1, '2023-06-01')"
eq "ambiguous title counts for both (conservative)" "$(cand 'Dune' 1984)|$(cand 'Dune' 2021)" "(1, 1, '2026-08-01')|(1, 1, '2026-08-01')"
eq "1970 date ignored" "$(cand 'Huge Remux' 2002)" "(0, 0, None)"
eq "play under 2 min ignored" "$(cand 'Short One' 2018)" "(0, 0, None)"
hasnt "too new to judge (on disk < 90 d)" "$out" '"New Big"'
hasnt "no file, no row" "$out" '"No File"'
eq "matching stats" "$(printf '%s' "$out" | jq_ '[d["matching"][k] for k in ("emby_plays", "ta_plays", "by_id", "by_name", "ambiguous", "unmatched", "unmatched_titles", "cutoff")]')" \
  "[8, 5, 7, 4, 1, 2, 1, '2026-08-25']"
eq "history start = oldest Emby row" "$(printf '%s' "$out" | jq_ 'd["history_start"]')" "2020-02-25"
eq "requesters: oldest first" "$(printf '%s' "$out" | jq_ '[c["requested_by"] for c in d["candidates"] if c["title"] == "Big Old Show"][0]')" "['PersonG', 'PersonH']"
m unwatched --json
eq "score = GB x idle years, sorted" "$(printf '%s' "$out" | jq_ '[(c["title"], c["score"]) for c in d["candidates"]]')" \
  "[('Big Old Show', 3131), ('Huge Remux', 530), ('Never Show', 381), ('Ubice mog oca', 183), ('Id Movie', 34), ('Short One', 33)]"
eq "stale seasons: S1 only (S0 specials, S2 watched, S3 just aired)" "$(printf '%s' "$out" | jq_ '[(s["title"], s["season"], s["last"]) for s in d["stale_seasons"]]')" \
  "[('Watched Big Show', 1, '2025-01-10')]"
eq "totals in json" "$(printf '%s' "$out" | jq_ 'd["totals"]["no plays 365 d"]')" \
  "{'tv': {'titles': 2, 'bytes': 1700000000000}, 'movie': {'titles': 3, 'bytes': 95000000000}}"
m unwatched --min-age 400 --json
eq "--min-age 400 drops Never Show (added 278 d ago)" "$(printf '%s' "$out" | jq_ '[c["title"] for c in d["candidates"] if c["type"] == "tv"]')" "['Big Old Show', 'Ubice mog oca']"
m unwatched --days 0
eq "--days 0 refused" "$rc" "1"

echo "# report"
m unwatched --report
R=$T/reports/unwatched-2026-10-10.md
has "default report path" "$out" "Full table: $R"
has "report: header" "$(cat "$R")" "Play history since **25.02.2020** (Emby Playback Reporting, Tracearr from 25.08.2026)"
has "report: totals row" "$(cat "$R")" "| no plays 180 d | 3 · 2.0 TB | 3 · 95.0 GB | 6 · 2.1 TB |"
has "report: show row" "$(cat "$R")" "| 1 | Big Old Show (2004) | 1.2 TB | 8 / 80 | 15.01.2019 | 01.03.2024 | 0/0 (2) | 2 | PersonG, PersonH | ended |"
has "report: movie row" "$(cat "$R")" "| 1 | Huge Remux (2002) | 80.0 GB | 01.06.2019 | never | 0/0 (0) | 0 | Timon Muster |"
has "report: stale season" "$(cat "$R")" "| Watched Big Show | 1 | 150 GB | 10 | 10.01.2025 | 20.09.2026 |"
has "report: unmatched list" "$(cat "$R")" "  - Gone Show [tv]: 2 plays, last 02.02.2026"
m unwatched --report "$T/custom.md" --type movie
eq "custom report path" "$(test -s "$T/custom.md" && echo yes)" "yes"
hasnt "--type movie report has no show section" "$(cat "$T/custom.md")" "## Shows"

echo "# sources and failures"
sqln() { grep -c -- '-- end' "$T/sql.log"; }
before=$(sqln)
m unwatched --no-emby --json
eq "--no-emby: Tracearr only" "$(printf '%s' "$out" | jq_ '(d["sources"], d["history_start"])')" "('Tracearr from 25.08.2026', '2026-08-25')"
eq "--no-emby: no SQL run" "$(sqln)" "$before"
MEDIA_EMBY_SQL="echo 'ssh: connect to host unraid port 22: Connection refused' >&2; exit 255" m unwatched
eq "Emby down: rc 3" "$rc" "3"
has "  says why" "$out" "!! skipped Emby playback DB: ssh: connect to host unraid port 22: Connection refused"
has "  Tracearr-only history" "$out" "play history since 25.08.2026 (Tracearr from 25.08.2026)"
MEDIA_TRACEARR_KEYFILE=$T/bad-key m unwatched --json
eq "Tracearr 401: rc 3" "$rc" "3"
has "  warning" "$out" "Tracearr: tracearr HTTP 401"
eq "  Emby only: its 15.09 row counts, Tracearr-only play gone" "$(printf '%s' "$out" | jq_ '(d["sources"], sorted(c["title"] for c in d["candidates"] if c["type"] == "movie"))')" \
  "('Emby Playback Reporting', ['Emby Id Film', 'Huge Remux', 'Id Movie', 'Short One'])"
MEDIA_SECRETS=$T/ta-down.json MEDIA_TRACEARR_KEYFILE=$T/ta-key m unwatched
eq "Tracearr unreachable: rc 3" "$rc" "3"
MEDIA_TRACEARR_KEYFILE=$T/nope MEDIA_EMBY_SQL="exit 1" m unwatched
eq "no history at all: rc 1" "$rc" "1"
has "  explains" "$out" "media: no play history: Tracearr: no Tracearr key"

echo "# safety"
eq "every request was a GET ($(wc -l < "$T/server.log") requests)" "$(grep -vc '^GET ' "$T/server.log")" "0"
eq "Tracearr pages walked (2 rows per page)" "$(grep -c 'history?pageSize=100&cursor=' "$T/server.log" | awk '{print ($1 > 0) ? "yes" : "no"}')" "yes"
eq "SQL is read-only (no writes)" "$(grep -ciE '\b(insert|update|delete|drop|create|alter|replace into|pragma|vacuum)\b' "$T/sql.log")" "0"
has "library attached immutable" "$(cat "$T/sql.log")" "ATTACH 'file:$T/emby/library.db?immutable=1' AS lib;"
has "default Emby command is read-only + immutable" "$(grep -F 'sqlite3 -readonly -batch -ascii' "$MEDIA")" "'file:{dir}/playback_reporting.db?immutable=1'"
hasnt "no Tracearr key in output" "$(cat "$T/all.out")" "TRACEARR-KEY-Tk55"
hasnt "no Sonarr key in output" "$(cat "$T/all.out")" "SONARR-KEY-Vy73"
hasnt "no tracebacks" "$(cat "$T/all.out")" "Traceback"
eq "fake server clean" "$(cat "$T/server.err")" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
