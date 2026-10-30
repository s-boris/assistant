#!/usr/bin/env bash
# Offline tests for `media wrapped` (his Emby year in review). Exit 0 = all pass.
# wrapped_fx.py builds fictional Emby DBs (his plays, a friend's plays, a backup table, replaced item ids, a stuck
# session) and serves fake Seerr users + Emby covers; tonight_fx.py plays the SSH lockdown verb (or the root sqlite3
# command) and logs the SQL. Clock: Sat 2026-10-10 20:00 Zurich. Checks: numbers, card, JSON, viewing-day rule,
# rewatch vs partial, name fallback, caps, HTML (offline, covers, size), privacy (his rows only), GET-only, no key leaks.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-wrapped-tests.XXXXXX")
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT

python3 -I "$HERE/wrapped_fx.py" build "$T/emby"
python3 -I "$HERE/wrapped_fx.py" serve "$T/port" "$T/server.log" 2>"$T/server.err" &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
DEAD=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
printf '{"seerr": {"url": "%s/seerr", "key": "SEERR-KEY-Zq81"}, "emby": {"url": "%s", "key": "EMBY-KEY-Wr55"}}\n' "$B" "$B" > "$T/cfg.json"
printf '{"seerr": {"url": "http://127.0.0.1:%s", "key": "SEERR-KEY-Zq81"}, "emby": {"url": "%s", "key": "EMBY-KEY-Wr55"}}\n' "$DEAD" "$B" > "$T/seerr-down.json"
SPLIT_SQL="python3 -I $HERE/tonight_fx.py sql $T/emby $T/sql.log {db}"
ROOT_SQL="python3 -I $HERE/tonight_fx.py sql $T/emby $T/sql.log"
export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-10T20:00:00+02:00 MEDIA_TIMEOUT=5 MEDIA_EMBY_DIR=$T/emby \
  MEDIA_EMBY_SQL=$SPLIT_SQL
unset MEDIA_TONIGHT_USER MEDIA_WRAPPED_GUID

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2"; fi; }
# has/hasnt test the substring in the shell: `printf | grep -q` under pipefail reads as no match when grep exits
# on the match before printf (one write per line) is done, SIGPIPE (a false FAIL, or a false pass for hasnt)
has() { if [[ $2 == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ $2 == *"$3"* ]]; then no "$1" "$(printf '%s' "$2" | grep -F -- "$3" | head -3)"; else ok "$1"; fi; }
m() { "$MEDIA" "$@" >"$T/out" 2>"$T/err"; rc=$?; cat "$T/out" "$T/err" >> "$T/all.out"; }
jq_() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$T/out" "$1"; }

echo "# numbers (lockdown verb SQL)"
m wrapped --json
eq "exit 0" "$rc" "0"
eq "totals: hours, plays, days" "$(jq_ '(d["hours"], d["plays"], d["days"])')" "(17.5, 16, 10)"
eq "films, film plays, episodes, shows" "$(jq_ '(d["films"], d["film_plays"], d["episodes"], d["episode_plays"], d["shows"])')" "(5, 7, 9, 9, 2)"
eq "genres by time watched (show genres for episodes, Genres column fallback)" \
  "$(jq_ '[(g["name"], g["pct"]) for g in d["genres"]]')" "[('Science Fiction', 38), ('Drama', 34), ('Comedy', 16), ('Western', 13)]"
eq "top shows" "$(jq_ '[(s["name"], s["episodes"], s["hours"]) for s in d["top_shows"]]')" "[('Nova Drift', 6, 4.5), ('Laugh Track', 3, 1.1)]"
eq "binge: 7 episodes on Sat 07.03 (00:10 counts for the evening)" "$(jq_ '(d["binge"]["day"], d["binge"]["episodes"], d["binge"]["show"], d["binge"]["show_episodes"])')" "('2026-03-07', 7, 'Nova Drift', 6)"
eq "favourite weekday and hour" "$(jq_ '(d["fav_weekday"], d["fav_hour"])')" "('Sat', 20)"
eq "rewatched: 2 full watches (a 10-min replay is not one)" "$(jq_ '(d["rewatched"]["title"], d["rewatched"]["times"])')" "('Star Voyage', 2)"
eq "first and latest play" "$(jq_ '(d["first"]["title"], d["first"]["at"], d["latest"]["title"])')" "('Star Voyage', '2026-01-03T20:00', 'Vanished Film')"
eq "films by decade + top decade" "$(jq_ '([(x["decade"], x["films"]) for x in d["decades"]], d["top_decade"])')" "([('1960s', 1), ('2010s', 2), ('2020s', 1)], '2010s')"
eq "4K share of known time + 4K films" "$(jq_ '(d["uhd_pct"], d["uhd_films"])')" "(51, 2)"
eq "fun fact" "$(jq_ 'd["fun_fact"]')" "🏔 Longest film: Long Epic (3 h 20 min)."
eq "screens" "$(jq_ '[(x["name"], x["pct"]) for x in d["screens"]]')" "[('Emby for LG', 98), ('Emby for Android', 2)]"
eq "replaced ids matched by name (film + episode S01E03)" "$(jq_ 'sorted(x["title"] for x in d["film_wall"])')" "['Long Epic', 'Old Western', 'Renamed Copy', 'Star Voyage', 'Vanished Film']"
J=$(cat "$T/out")
hasnt "no friend's film" "$J" "Friend Only Film"
hasnt "no TV channel rows" "$J" "Some Channel"

echo "# card"
m wrapped
eq "card exit 0" "$rc" "0"
eq "card <= 8 lines" "$(wc -l < "$T/out" | awk '{print ($1 <= 8)}')" "1"
has "card head" "$(cat "$T/out")" "🎬 Emby Wrapped 2026 so far (to 10.10)"
has "card totals" "$(cat "$T/out")" "⏱ 17.5 h · 5 films · 9 episodes of 2 shows"
has "card binge" "$(cat "$T/out")" "🍿 Binge record: 7 episodes on Sat 07.03 (Nova Drift)"
has "card when" "$(cat "$T/out")" "📅 Saturdays around 20:00 · 4K 51 % · films: the 2010s"
has "card rewatch + first" "$(cat "$T/out")" "🔁 Star Voyage 2× · first Star Voyage 03.01"
CARD=$(cat "$T/out")
MEDIA_EMBY_SQL=$ROOT_SQL m wrapped
eq "root sqlite3 command (ATTACH library): same card" "$(cat "$T/out")" "$CARD"

echo "# years"
m wrapped --year 2025 --json
eq "2025: the 02:00 play on 1 Jan counts for New Year's Eve" "$(jq_ '(d["plays"], d["films"], d["partial"], d["latest"]["title"])')" "(1, 1, False, 'Old Western')"
m wrapped --year 2024
eq "empty year" "$(sed -n 2p "$T/out")" "No plays of yours in 2024."
m wrapped --year 2031
eq "future year refused" "$rc" "1"

echo "# html"
: > "$T/server.log"
m wrapped --html "$T/html/w.html" --json
eq "html exit 0" "$rc" "0"
H="$T/html/w.html"
eq "html written, covers embedded (404 and non-image skipped)" "$(jq_ '(d["html"]["path"].endswith("w.html"), d["html"]["covers"])')" "(True, 5)"
eq "html under 3 MB" "$(stat -c %s "$H" | awk '{print ($1 < 3000000)}')" "1"
has "html: story sections" "$(cat "$H")" "Binge record"
has "html: data: covers" "$(cat "$H")" 'src="data:image/png;base64,'
eq "html: offline (no http/https refs)" "$(grep -cE '(src|href)="https?:|url\(https?:|@import' "$H")" "0"
hasnt "html: no friend's film" "$(cat "$H")" "Friend Only Film"
has "html: title escaped/plain" "$(cat "$H")" "<title>Emby Wrapped 2026</title>"
eq "covers sent the key as a header, never in the URL" "$(grep -c 'Images/Primary.*token=yes' "$T/server.log") $(grep -c 'api_key' "$T/server.log")" "7 0"
m wrapped --html "$T/nc.html" --no-covers --json
eq "--no-covers: initials, no image requests" "$(jq_ 'd["html"]["covers"]') $(grep -c 'Images' "$T/server.log")" "0 7"
HOME=$T/home m wrapped --html --no-covers
eq "default --html path (under HOME)" "$(tail -1 "$T/out" | grep -c 'reports/media/emby-wrapped-2026.html') $(ls "$T/home/reports/media")" "1 emby-wrapped-2026.html"
m wrapped --html /proc/nope/w.html
eq "unwritable --html: exit 1, no traceback" "$rc $(grep -c "can't write" "$T/err")" "1 1"

echo "# user mapping and failures"
: > "$T/server.log"
MEDIA_WRAPPED_GUID=aaaa1111-0000-0000-0000-000000000000 m wrapped
eq "MEDIA_WRAPPED_GUID skips Seerr, same card" "$(cat "$T/out") / $(grep -c seerr "$T/server.log")" "$CARD / 0"
MEDIA_SECRETS=$T/seerr-down.json m wrapped
eq "Seerr down: exit 1" "$rc" "1"
has "Seerr down: says why" "$(cat "$T/err")" "can't tell which Emby user is yours"
MEDIA_EMBY_SQL="exit 1" m wrapped
eq "Emby DB unreachable: exit 1" "$rc" "1"

echo "# privacy + safety"
eq "every PlaybackActivity read is filtered to his user id" \
  "$(grep -c 'FROM (SELECT DateCreated' "$T/sql.log") $(grep 'FROM (SELECT DateCreated' "$T/sql.log" | grep -c "= 'aaaa1111000000000000000000000000'")" \
  "$(n=$(grep -c 'FROM (SELECT DateCreated' "$T/sql.log"); echo "$n $n")"
hasnt "the friend's id never in SQL" "$(cat "$T/sql.log")" "bbbb2222"
hasnt "no verb rejections" "$(cat "$T/all.out")" "claude-bot-cmd:"
eq "only GETs" "$(grep -vc '^GET ' "$T/server.log")" "0"
hasnt "no Emby key in outputs" "$(cat "$T/all.out" "$H")" "EMBY-KEY-Wr55"
hasnt "no Seerr key in outputs" "$(cat "$T/all.out")" "SEERR-KEY-Zq81"
hasnt "no tracebacks" "$(cat "$T/all.out")" "Traceback"
eq "fake server clean" "$(cat "$T/server.err")" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
