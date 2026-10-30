#!/usr/bin/env bash
# Offline tests for `media continue` (Emby Continue watching -> one Play-on-TV button each) and tonight's
# "▶ Continue" button. Exit 0 = all pass.
# fake_continue.py stands in for Emby (Resume, Next Up, a series' last played episode), fake_tonight.py for Seerr
# (his user's linked Emby account), tonight_fx.py for Emby's DB (tonight's picks). Clock: Sat 2026-10-10 20:00 Zurich.
# Checks: Emby 4.9+ merged Resume and the legacy Next Up order, empty, series/season/folder/played entries never
# become buttons, one entry per series, long titles, Markdown escaping, play off, partial and total failures, the
# user override, tonight's button (recent only, never tonight's own pick, rows kept), GET-only, no key leaks.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-continue-tests.XXXXXX")
trap 'kill $SRV $EMB 2>/dev/null; rm -rf "$T"' EXIT

python3 -I "$HERE/tonight_fx.py" build "$T/emby"
python3 -I "$HERE/fake_tonight.py" "$T/port" "$T/server.log" 2>"$T/server.err" &
SRV=$!
echo merged > "$T/scenario"
python3 -I "$HERE/fake_continue.py" "$T/eport" "$T/emby.log" "$T/scenario" 2>"$T/emby.err" &
EMB=$!
for _ in $(seq 50); do [ -s "$T/port" ] && [ -s "$T/eport" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
E="http://127.0.0.1:$(cat "$T/eport")"
DEAD=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
mkcfg() {  # mkcfg FILE SEERR_URL [EMBY_URL]
  local emby=""
  [ -n "${3:-}" ] && emby=$(printf ', "emby": {"url": "%s", "key": "EMBY-KEY-Ct42"}' "$3")
  printf '{"seerr": {"url": "%s", "key": "SEERR-KEY-Zq81"}, "radarr": {"url": "%s/radarr", "key": "RADARR-KEY-Xw42"}, "sonarr": {"url": "%s/sonarr", "key": "SONARR-KEY-Vy73"}%s}\n' \
    "$2" "$B" "$B" "$emby" > "$1"
}
mkcfg "$T/cfg.json" "$B/seerr" "$E"
mkcfg "$T/noemby.json" "$B/seerr"
mkcfg "$T/seerr-down.json" "http://127.0.0.1:$DEAD" "$E"
mkcfg "$T/emby-down.json" "$B/seerr" "http://127.0.0.1:$DEAD"
printf '{"enabled": true, "tv": "media_player.test_tv", "device": "TESTTV"}\n' > "$T/play-on.json"
printf '{"enabled": false, "tv": "media_player.test_tv", "device": "TESTTV"}\n' > "$T/play-off.json"
export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-10T20:00:00+02:00 MEDIA_PAGE_SIZE=2 MEDIA_TIMEOUT=5 \
  MEDIA_EMBY_DIR=$T/emby MEDIA_EMBY_SQL="python3 -I $HERE/tonight_fx.py sql $T/emby $T/sql.log {db}" \
  MEDIA_TONIGHT_STATE=$T/st/tonight.json MEDIA_PLAY_CONFIG=$T/play-on.json
unset MEDIA_TONIGHT_USER MEDIA_TONIGHT_EMBY_UID MEDIA_READY_USER MEDIA_CONTINUE_EMBY_USER

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
m() { out=$(timeout 30 "$MEDIA" "$@" 2>&1); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; }
mj() { out=$(timeout 30 "$MEDIA" "$@" 2>"$T/err"); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; cat "$T/err" >> "$T/all.out"; }
jq_() { python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print($1)"; }
scen() { echo "$1" > "$T/scenario"; }
ids() { printf '%s' "$out" | jq_ "' '.join(i['id'] for i in d['items'])"; }
btns() { printf '%s' "$out" | jq_ "' | '.join(r[0]['text'] + ' ' + r[0]['data'] for r in d['buttons'])"; }
clearlog() { : > "$T/emby.log"; }

echo "# merged Resume (Emby 4.9+: next episodes inside Resume)"
scen merged; clearlog
mj continue --json
eq "rc 0" "$rc" "0"
eq "items in Emby's order, first 4" "$(ids)" "9001 9201 9301 9003"
eq "total after filtering (series/season/folder/played/duplicate series dropped, Next Up extra kept)" \
  "$(printf '%s' "$out" | jq_ "d['total']")" "5"
eq "states" "$(printf '%s' "$out" | jq_ "[(i['state'], i['kind'], i['source']) for i in d['items']]")" \
  "[('resume', 'movie', 'resume'), ('next', 'episode', 'resume'), ('resume', 'episode', 'resume'), ('resume', 'movie', 'resume')]"
eq "movie fields" "$(printf '%s' "$out" | jq_ "[(i['title'], i['year'], i['pos_min'], i['runtime_min'], i['last_played'], i['last_from']) for i in d['items']][0]")" \
  "('Test Movie', 2024, 45, 110, '2026-10-10T17:00:00Z', 'item')"
eq "next episode: SxxEyy, series id kept apart, date from the series' last played episode" \
  "$(printf '%s' "$out" | jq_ "[(i['episode'], i['episode_title'], i['series_id'], i['pos_min'], i['last_played'], i['last_from']) for i in d['items']][1]")" \
  "('S02E03', 'Third', '9200', 0, '2026-10-08T19:00:00Z', 'series')"
eq "message" "$(printf '%s' "$out" | jq_ "d['message']")" "▶ **Continue watching**

🍿 **Test Movie** (2024) · 45 of 110 min · today
📺 **The Extraordinarily Long Chronicles of Somew…** S02E03 · next up · 50 min · 2 days ago
📺 **Dark \\*Waters\\*** S01E05 · 10 of 55 min · 20.09.
🍿 **Old Movie** (2019) · 5 of 95 min · 01.12.2025"
eq "buttons: one per row, exact movie/episode ids" "$(btns)" \
  "📺 Test Movie mt:play:9001 | 📺 The Extraordinarily L… S02E03 mt:play:9201 | 📺 Dark *Waters* S01E05 mt:play:9301 | 📺 Old Movie mt:play:9003"
eq "no series, season or folder id in any button" \
  "$(printf '%s' "$out" | jq_ "sorted({r[0]['data'].split(':')[2] for r in d['buttons']} & {'9100', '9200', '9300', '9400', '9500', 'abc-def', '9002'})")" "[]"
eq "button texts stay short" "$(printf '%s' "$out" | jq_ "max(len(r[0]['text']) for r in d['buttons']) <= 32")" "True"
eq "play enabled, no warnings" "$(printf '%s' "$out" | jq_ "(d['play_enabled'], d['warnings'])")" "(True, [])"
eq "series lookup only for the shown undated next episode" "$(grep -c 'ParentId=' "$T/emby.log")" "1"
has "Resume asked with his Emby id (dashes stripped, lower case)" "$(cat "$T/emby.log")" "/emby/Users/aaaa1111000000000000000000000000/Items/Resume?"
has "Next Up asked the legacy way" "$(cat "$T/emby.log")" "LegacyNextUp=true"
mj continue --json -n 8
eq "-n 8: the Next Up extra comes last" "$(ids)" "9001 9201 9301 9003 9501"
eq "a never-played show's next episode has no date" "$(printf '%s' "$out" | jq_ "[i['last_played'] for i in d['items']][-1]")" "None"
mj continue --json -n 2
eq "-n 2" "$(ids)" "9001 9201"
m continue -n 9
eq "-n 9 refused" "$rc" "1"; has "-n cap message" "$out" "-n is at most 8"
m continue -n 0
eq "-n 0 refused" "$rc" "1"

echo "# text output"
m continue
eq "rc 0" "$rc" "0"
has "plain title, no Markdown" "$out" "📺 Dark *Waters* S01E05 · 10 of 55 min · 20.09."
has "button lines" "$out" "   button: mt:play:9201 = media play 9201"
hasnt "no bold markers" "$out" "**"

echo "# legacy Emby (next episodes only under Next Up)"
scen legacy
mj continue --json
eq "rc 0" "$rc" "0"
eq "ordered by last watched (series date for next episodes), undated last" "$(ids)" "9001 9201 9301 9003"
mj continue --json -n 5
eq "-n 5 adds the undated next episode" "$(ids)" "9001 9201 9301 9003 9501"

echo "# empty"
scen empty
mj continue --json
eq "rc 0" "$rc" "0"
eq "no items, no buttons" "$(printf '%s' "$out" | jq_ "(d['items'], d['buttons'], d['total'])")" "([], [], 0)"
has "empty message" "$(printf '%s' "$out" | jq_ "d['message']")" "Nothing to continue: no partly watched movies or next episodes."

echo "# play switched off"
scen merged
MEDIA_PLAY_CONFIG=$T/play-off.json mj continue --json
eq "rc 0" "$rc" "0"
eq "no buttons when play is off" "$(printf '%s' "$out" | jq_ "(d['play_enabled'], d['buttons'], len(d['items']))")" "(False, [], 4)"
has "says why" "$(printf '%s' "$out" | jq_ "d['message']")" "Play on TV is switched off, so no buttons."

echo "# failures"
scen nextup-down
mj continue --json
eq "Next Up down: rc 3" "$rc" "3"
eq "Next Up down: Resume items still there" "$(ids)" "9001 9201 9301 9003"
has "Next Up down: warning" "$(printf '%s' "$out" | jq_ "d['warnings']")" "Next Up: "
has "Next Up down: ⚠️ line" "$(printf '%s' "$out" | jq_ "d['message']")" "⚠️ Next Up didn't load"
scen resume-down
m continue --json
eq "Resume down: rc 1" "$rc" "1"
has "Resume down: says so" "$out" "media: "
scen merged
MEDIA_SECRETS=$T/emby-down.json m continue
eq "Emby unreachable: rc 1" "$rc" "1"
MEDIA_SECRETS=$T/noemby.json m continue
eq "no emby key: rc 1" "$rc" "1"; has "no emby key: message" "$out" "no emby url/key"
MEDIA_SECRETS=$T/seerr-down.json m continue
eq "Seerr down, no override: rc 1" "$rc" "1"
MEDIA_SECRETS=$T/seerr-down.json MEDIA_CONTINUE_EMBY_USER=AAAA1111-0000-0000-0000-000000000000 mj continue --json
eq "MEDIA_CONTINUE_EMBY_USER skips Seerr" "$rc $(ids)" "0 9001 9201 9301 9003"
MEDIA_CONTINUE_EMBY_USER=nope m continue
eq "bad override: rc 1" "$rc" "1"; has "bad override: message" "$out" "MEDIA_CONTINUE_EMBY_USER must be an Emby user id"
MEDIA_TONIGHT_USER=Friend mj continue --json
eq "another Seerr user's Emby id gets nothing from Emby" "$rc" "1"

echo "# tonight's ▶ Continue button"
rows() { printf '%s' "$out" | jq_ "' | '.join(' + '.join(b['text'] for b in r) for r in d['buttons'])"; }
scen tonight
mj tonight --json --dry-run
eq "rc 0" "$rc" "0"
eq "tonight's B pick is the fixture's episode 2014" "$(printf '%s' "$out" | jq_ "[p['emby_id'] for p in d['picks'] if p['slot'] == 'B'][0]")" "2014"
eq "continue skips tonight's own pick, takes the movie watched today" "$(printf '%s' "$out" | jq_ "d.get('continue')")" "mt:play:9001"
eq "one ▶ Continue row, just above 🎲 3 more" "$(printf '%s' "$out" | jq_ "[[b.get('data') for b in r] for r in d['buttons']][-2:]")" \
  "[['mt:play:9001'], ['mt:more']]"
eq "button text" "$(printf '%s' "$out" | jq_ "d['buttons'][-2][0]['text']")" "▶ Continue Test Movie"
with=$(rows | sed 's/ | ▶ Continue Test Movie//')
scen old
mj tonight --json --dry-run
eq "nothing recent (> 14 days): rc 0, no button" "$rc $(printf '%s' "$out" | jq_ "d.get('continue')")" "0 None"
eq "the card's other rows are unchanged" "$(rows)" "$with"
scen merged
mj tonight --json --dry-run
eq "merged: newest recent item" "$(printf '%s' "$out" | jq_ "d.get('continue')")" "mt:play:9001"
scen resume-down
mj tonight --json --dry-run
eq "Emby Resume down: tonight still rc 0, no button" "$rc $(printf '%s' "$out" | jq_ "d.get('continue')")" "0 None"
has "Emby Resume down: a warning, not a card line" "$(printf '%s' "$out" | jq_ "d['warnings']")" "continue: "
hasnt "Emby Resume down: card unchanged" "$(printf '%s' "$out" | jq_ "d['message']")" "continue"
scen tonight; clearlog
MEDIA_PLAY_CONFIG=$T/play-off.json mj tonight --json --dry-run
eq "play off: no button and no Emby call" "$(printf '%s' "$out" | jq_ "d.get('continue')") $(wc -l < "$T/emby.log")" "None 0"
MEDIA_SECRETS=$T/noemby.json mj tonight --json --dry-run
eq "no emby key: no button, no warning" "$rc $(printf '%s' "$out" | jq_ "(d.get('continue'), [w for w in d['warnings'] if 'continue' in w])")" "0 (None, [])"
m tonight --dry-run
has "tonight text mode names the button" "$out" "   continue: button mt:play:9001"
eq "dry runs left no tonight state" "$([ -e "$T/st/tonight.json" ] && echo saved || echo none)" "none"

echo "# help"
m -h
has "help lists continue" "$out" "media continue [--json] [-n 4]"

echo "# safety"
eq "Emby: every request was a GET with the key ($(wc -l < "$T/emby.log") since the last reset)" \
  "$(grep -vc '^GET .* token=ok$' "$T/emby.log")" "0"
eq "Seerr: every request was a GET" "$(grep -vc '^GET ' "$T/server.log")" "0"
hasnt "no Emby key in output" "$(cat "$T/all.out")" "EMBY-KEY-Ct42"
hasnt "no Seerr key in output" "$(cat "$T/all.out")" "SEERR-KEY-Zq81"
hasnt "no tracebacks" "$(cat "$T/all.out")" "Traceback"
eq "fake Emby clean" "$(cat "$T/emby.err")" ""
eq "fake Seerr clean" "$(cat "$T/server.err")" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
