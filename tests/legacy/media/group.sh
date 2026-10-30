#!/usr/bin/env bash
# Offline tests for `media group` (movie night with friends: library movies nobody in the group has seen + a poll).
# Exit 0 = all pass. tonight_fx.py builds Emby's DBs and plays the SQL verb, group_fx.py adds the friends' Playback
# Reporting plays and fakes Emby's /Users and per-user played/resumable movies, fake_tonight.py stands in for Seerr.
# Clock: Sat 2026-10-10 20:00 Zurich. Checks: who is in the group (default and named, accents, prefixes, unknown and
# ambiguous names, disabled users), what counts as seen (played, resumable, other versions by TMDB id, Playback
# Reporting plays of 20+ min), ★7+ first and mixed genres, mood/time/args, the poll spec and buttons, partial failures,
# errors, GET-only, read-only SQL, no key leaks, nothing remembered.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
export MEDIA_PLAY_CONFIG=/nonexistent/play.json
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-group-tests.XXXXXX")
trap 'kill $SRV $EMB 2>/dev/null; rm -rf "$T"' EXIT

python3 -I "$HERE/tonight_fx.py" build "$T/emby" && python3 -I "$HERE/group_fx.py" plays "$T/emby"
python3 -I "$HERE/fake_tonight.py" "$T/port" "$T/seerr.log" 2>"$T/seerr.err" &
SRV=$!
python3 -I "$HERE/group_fx.py" serve "$T/eport" "$T/emby.log" 2>"$T/emby.err" &
EMB=$!
for _ in $(seq 50); do [ -s "$T/port" ] && [ -s "$T/eport" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
E="http://127.0.0.1:$(cat "$T/eport")"
DEAD=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
mkcfg() {  # mkcfg FILE SEERR_URL EMBY_URL [EMBY_KEY]
  printf '{"seerr": {"url": "%s", "key": "SEERR-KEY-Zq81"}, "emby": {"url": "%s", "key": "%s"}}\n' "$2" "$3" "${4:-EMBY-KEY-Pq55}" > "$1"
}
mkcfg "$T/cfg.json" "$B/seerr" "$E"
mkcfg "$T/seerr-down.json" "http://127.0.0.1:$DEAD" "$E"
mkcfg "$T/emby-down.json" "$B/seerr" "http://127.0.0.1:$DEAD"
printf '{"seerr": {"url": "%s/seerr", "key": "SEERR-KEY-Zq81"}}\n' "$B" > "$T/noemby.json"
printf '{"enabled": true, "tv": "media_player.living_room_tv", "device": "Living Room"}\n' > "$T/play.json"
SPLIT_SQL="python3 -I $HERE/tonight_fx.py sql $T/emby $T/sql.log {db}"
ROOT_SQL="python3 -I $HERE/tonight_fx.py sql $T/emby $T/sql.log"
export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-10T20:00:00+02:00 MEDIA_PAGE_SIZE=2 MEDIA_TIMEOUT=5 \
  MEDIA_EMBY_DIR=$T/emby MEDIA_EMBY_SQL=$SPLIT_SQL MEDIA_TONIGHT_STATE=$T/st/tonight.json
unset MEDIA_TONIGHT_USER MEDIA_TONIGHT_EMBY_UID MEDIA_READY_USER MEDIA_CONTINUE_EMBY_USER

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
m() { out=$(timeout 30 "$MEDIA" "$@" 2>&1); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; }
jq_() { printf '%s' "$out" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print($1)"; }
titles() { jq_ "' | '.join(sorted(p['title'] for p in d['picks']))"; }
logn() { local n; n=$(grep -c -- "$1" "$T/emby.log" 2>/dev/null); echo "${n:-0}"; }

echo "# default group: him + enabled users with a play in 180 days"
m group --json
eq "default rc 0" "$rc" "0"
eq "members" "$(jq_ "', '.join(d['group']['names'])")" "Friend, Ana, Ben, Zoë"
eq "size counts him" "$(jq_ "d['group']['size'], d['group']['default'], d['group']['active_days']")" "5 True 180"
eq "header names them" "$(jq_ "d['message'].splitlines()[0]")" "🎬 **Movie night** · you + Friend, Ana, Ben, Zoë"
eq "second line" "$(jq_ "d['message'].splitlines()[1]")" "Nobody in the group has seen these:"
eq "4 picks" "$(jq_ "len(d['picks'])")" "4"
has "the two unseen ★7+ films lead" "$(jq_ "' | '.join(p['title'] for p in d['picks'][:2])")" "Kids Toon"
has "  ... Krimi Film too" "$(jq_ "' | '.join(p['title'] for p in d['picks'][:2])")" "Krimi Film"
for t in "Arrival" "Half Seen" "Other User Saw" "Dune: Part Two" "Solid Western" "Funny One" "Scary Night" "Bulk Movie"; do
  hasnt "seen by someone: $t" "$(titles)" "$t"
done
has "note: not enough ★7+" "$(jq_ "d['message']")" "⚠️ 2 of the 4 picks rated under ★7: not enough unseen ★7+ movies fit"
eq "Kids Toon's why: genres" "$(jq_ "[p['why'] for p in d['picks'] if p['title'] == 'Kids Toon'][0]")" "Animation, Family"
eq "numbered lines" "$(jq_ "d['message'].count('1️⃣ '), d['message'].count('4️⃣ ')")" "1 1"
eq "poll spec" "$(jq_ "d['poll']['question'], len(d['poll']['options']), d['poll']['anonymous'], d['poll']['multiple']")" "Movie night: which one? 4 False False"
eq "poll options = titles (year), in pick order" "$(jq_ "d['poll']['options'] == [p['title'] + ' (' + str(p['year']) + ')' for p in d['picks']]")" "True"
has "poll hint" "$(jq_ "d['message']")" "🗳 Poll below: forward it to the group."
eq "play off: link rows only" "$(jq_ "[len(r) for r in d['buttons']], d['play_on_tv'], all('url' in r[0] for r in d['buttons'])")" "[1, 1, 1, 1] False True"
eq "two Emby calls per person" "$(logn 'Filters=IsPlayed'),$(logn 'Filters=IsResumable')" "5,5"
eq "nobody else's history read" "$(logn "/Users/efef9999"),$(logn "/Users/34343434"),$(logn "/Users/ffff6666")" "0,0,0"
eq "remembers nothing" "$([ -e "$T/st/tonight.json" ] && echo yes || echo no)" "no"

echo "# named groups"
m group --with Ana --json
eq "with Ana rc 0" "$rc" "0"
eq "with Ana: four ★7+ films, one per genre" "$(titles)" "Funny One | Kids Toon | Krimi Film | Scary Night"
eq "  all ★7+" "$(jq_ "all(p['rating'] >= 7 for p in d['picks'])")" "True"
eq "  four lead genres" "$(jq_ "len({p['lead'] for p in d['picks']})")" "4"
eq "  no genre twice" "$(jq_ "len(sum((p['genres'] for p in d['picks']), [])) == len(set(sum((p['genres'] for p in d['picks']), [])))")" "True"
eq "  header" "$(jq_ "d['message'].splitlines()[0]")" "🎬 **Movie night** · you + Ana"
eq "  not default" "$(jq_ "d['group']['default'], d['group']['active_days']")" "False None"
eq "  only his and Ana's history" "$(logn 'Filters=IsPlayed')" "7"
m group --with Ana -n 2 --json
eq "-n 2: two picks, two options" "$(jq_ "len(d['picks']), len(d['poll']['options'])")" "2 2"
m group --with ben --json
eq "Ben's 40 min of Funny One count" "$(jq_ "len(d['picks'])")" "4"
hasnt "  Funny One out" "$(titles)" "Funny One"
has "  5 min of Krimi Film don't" "$(titles)" "Krimi Film"
m group --with Oldie --json
hasnt "named inactive user: his old play counts" "$(titles)" "Krimi Film"
m group --with zoe --json
eq "accents ignored" "$(jq_ "d['group']['names']")" "['Zoë']"
hasnt "  resumable counts as seen" "$(titles)" "Scary Night"
m group --with "ki, Nobody, Robin" --json
eq "ambiguous + unknown + himself: just him" "$(jq_ "d['group']['names'], d['group']['not_found']")" "[] ['ki', 'Nobody']"
has "  ambiguous note" "$(jq_ "d['message']")" "'ki' could be Kim, Kim2, Kimi: left out"
has "  unknown note" "$(jq_ "d['message']")" "'Nobody': no such Emby user"
eq "  alone" "$(jq_ "d['message'].splitlines()[:2]")" "['🎬 **Movie night** · just you', \"You haven't seen these:\"]"
m group --with Sleeper --json
eq "disabled user by name: included" "$(jq_ "d['group']['names']")" "['Sleeper']"
m group --with Broken --json
eq "a member's history fails: rc 3" "$rc" "3"
has "  says so" "$(jq_ "d['message']")" "couldn't check what Broken saw"
eq "  still picks" "$(jq_ "len(d['picks']), d['group']['unchecked']")" "4 ['Broken']"

echo "# args, mood, time"
m group --args "ana comedy" --json
eq "args: name + mood" "$(jq_ "d['group']['names'], d['mood'], [p['title'] for p in d['picks']]")" "['Ana'] comedy ['Funny One']"
eq "  one pick: no poll" "$(jq_ "d['poll']")" "None"
hasnt "  no poll hint" "$(jq_ "d['message']")" "Poll below"
m group --args "with Ana and Ben 95" --json
eq "args: two names + time" "$(jq_ "d['group']['names'], d['time']")" "['Ana', 'Ben'] 95"
eq "  within 95 min" "$(titles)" "Kids Toon | Req_Film [Cut]"
has "  Markdown escaped" "$(jq_ "d['message']")" "**Req\\_Film \\[Cut\\]** (2025) · 95 min · ★6.5"
has "  his request" "$(jq_ "[p['why'] for p in d['picks'] if p['title'].startswith('Req')][0]")" "you requested it"
has "  poll unescaped" "$(jq_ "d['poll']['options']")" "Req_Film [Cut] (2025)"
has "  header" "$(jq_ "d['message'].splitlines()[0]")" "you + Ana, Ben · ≤ 95 min"
m group --with Ana --mood "funny" --time 100 --json
eq "--mood/--time" "$(jq_ "d['mood'], d['time'], [p['title'] for p in d['picks']]")" "funny 100 ['Funny One']"

echo "# play buttons, plain text, root SQL"
MEDIA_PLAY_CONFIG=$T/play.json m group --with Ana --json
eq "play on: ▶ link + 📺 Play on TV per pick" "$(jq_ "[[b['text'][:2] for b in r] for r in d['buttons']][0], d['play_on_tv']")" "['▶ ', '📺 '] True"
eq "  play data = the pick's Emby id" "$(jq_ "all(r[1]['data'] == 'mt:play:' + p['emby_id'] for r, p in zip(d['buttons'], d['picks']))")" "True"
has "  link carries the item id" "$(jq_ "d['buttons'][0][0]['url']")" "item?id=$(jq_ "d['picks'][0]['emby_id']")&"
m group --with Ana
eq "plain rc 0" "$rc" "0"
eq "plain header" "$(printf '%s\n' "$out" | head -1)" "🎬 Movie night · you + Ana"
has "plain poll line" "$out" "   poll: Movie night: which one? "
MEDIA_EMBY_SQL=$ROOT_SQL m group --with Ana --json
eq "root sqlite3 command: same picks" "$(titles)" "Funny One | Kids Toon | Krimi Film | Scary Night"

echo "# errors and partial results"
MEDIA_SECRETS=$T/noemby.json m group
eq "no emby entry: rc 1" "$rc" "1"
has "  says why" "$out" "can't tell who has seen what"
MEDIA_SECRETS=$T/emby-down.json m group
eq "Emby down: rc 1" "$rc" "1"
has "  says Emby users" "$out" "Emby users:"
MEDIA_SECRETS=$T/seerr-down.json MEDIA_TONIGHT_EMBY_UID=7 MEDIA_CONTINUE_EMBY_USER=aaaa1111000000000000000000000000 m group --with Ana --json
eq "Seerr down (ids from env): rc 0" "$rc" "0"
eq "  4 picks, no links (Seerr has the link template)" "$(jq_ "len(d['picks']), d['buttons']")" "4 []"
m group -n 9
eq "-n 9: rc 1" "$rc" "1"
m group --time 0
eq "--time 0: rc 1" "$rc" "1"

echo "# safety"
eq "GET-only (Emby, Seerr)" "$(grep -vc '^GET ' "$T/emby.log"),$(grep -vc '^GET ' "$T/seerr.log")" "0,0"
hasnt "no Emby key in output" "$(cat "$T/all.out")" "EMBY-KEY-Pq55"
hasnt "no Seerr key in output" "$(cat "$T/all.out")" "SEERR-KEY-Zq81"
eq "SQL: SELECTs only, none rejected" "$(grep -c 'claude-bot-cmd' "$T/all.out")" "0"
eq "no writes in the SQL log" "$(grep -ciE '\b(insert|update|delete|create|drop)\b' "$T/sql.log")" "0"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
