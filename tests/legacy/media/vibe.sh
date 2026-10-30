#!/usr/bin/env bash
# Offline tests for `media vibe` (4 library movies for a mood or "like <film>", local embedding index). Exit 0 = all pass.
# fake_vibe.py stands in for Emby (a fictional movie library, his played/partly watched ids) and for the embedding
# server (/v1/embeddings: keyword-count vectors, every embedded text logged); fake_tonight.py for Seerr (his user's
# linked Emby account, the Emby web link template). MEDIA_VIBE_EMBED_URL points at the fake, so no llama-server runs.
# Checks: hint, first build, mood ranking, seen penalty, rating floor, runtime caps (short / 90min / under 2h), like
# <film> and a bare title, unknown film, diversity, incremental sync (new, changed, removed), --refresh/--full/--quiet,
# JSON buttons and links, play on/off, Seerr/Emby/embedding failures, a missing model, GET-only, no key leaks.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-vibe-tests.XXXXXX")
trap 'kill $SRV $EMB 2>/dev/null; rm -rf "$T"' EXIT

python3 -I "$HERE/fake_tonight.py" "$T/port" "$T/server.log" 2>"$T/server.err" &
SRV=$!
echo ok > "$T/scenario"
python3 -I "$HERE/fake_vibe.py" "$T/eport" "$T/emby.log" "$T/emb.log" "$T/scenario" 2>"$T/emby.err" &
EMB=$!
for _ in $(seq 50); do [ -s "$T/port" ] && [ -s "$T/eport" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
E="http://127.0.0.1:$(cat "$T/eport")"
DEAD=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
mkcfg() {  # mkcfg FILE SEERR_URL [EMBY_URL]
  local emby=""
  [ -n "${3:-}" ] && emby=$(printf ', "emby": {"url": "%s", "key": "EMBY-KEY-Vb42"}' "$3")
  printf '{"seerr": {"url": "%s", "key": "SEERR-KEY-Zq81"}%s}\n' "$2" "$emby" > "$1"
}
mkcfg "$T/cfg.json" "$B/seerr" "$E"
mkcfg "$T/noemby.json" "$B/seerr"
mkcfg "$T/seerr-down.json" "http://127.0.0.1:$DEAD" "$E"
printf '{"enabled": true, "tv": "media_player.test_tv", "device": "TESTTV"}\n' > "$T/play-on.json"
export MEDIA_SECRETS=$T/cfg.json MEDIA_TIMEOUT=5 MEDIA_PAGE_SIZE=4 MEDIA_VIBE_DIR=$T/vibe MEDIA_VIBE_EMBED_URL=$E \
  MEDIA_PLAY_CONFIG=/nonexistent/play.json
unset MEDIA_TONIGHT_USER MEDIA_READY_USER MEDIA_CONTINUE_EMBY_USER

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
scen() { echo "$1" > "$T/scenario"; }

echo "# hint"
m vibe
eq "bare vibe: exit 0" "$rc" "0"
has "bare vibe: usage hint" "$out" "/vibe cozy rainy sunday"
mj vibe --json
eq "bare vibe --json: hint field" "$(printf '%s' "$out" | jq_ "d['hint'].splitlines()[0] + ' ' + str(d['picks'])")" "🎬 /vibe + a mood or a film, e.g. []"
[ -e "$T/vibe" ] && no "a hint builds nothing" "$(ls "$T/vibe")" || ok "a hint builds nothing"

echo "# first query builds the index"
: > "$T/emb.log"
mj vibe --json cozy rainy
eq "exit 0" "$rc" "0"
eq "all 15 titles embedded once (paged library)" "$(docs)" "15"
eq "one query embedding, with the movie suffix" "$(grep '^query ' "$T/emb.log")" "query cozy rainy movie"
[ -s "$T/vibe/index.json" ] && ok "index.json written" || no "index.json written"
eq "one vectors file" "$(find "$T/vibe" -name 'vectors-*.f32' | wc -l)" "1"
eq "index dir is private (0700)" "$(stat -c %a "$T/vibe")" "700"
eq "index: 15 items" "$(printf '%s' "$out" | jq_ "d['index']['items']")" "15"
eq "4 picks, mood first, cozy genres up" "$(titles)" "Cozy Little Cafe | Rainy Window | Autumn Tea House | Long Rain"
hasnt "rating floor: 5.2-rated Puddle Jump left out" "$(titles)" "Puddle Jump"
eq "Rainy Window = seen (played) and marked" "$(printf '%s' "$out" | jq_ "[(p['seen'], '👁 seen' in p['why']) for p in d['picks'] if p['title'] == 'Rainy Window'][0]")" "('played', True)"
eq "label" "$(printf '%s' "$out" | jq_ "d['label']")" "cozy rainy"
eq "why: genres, query words found, tagline (German genre → Comedy)" "$(printf '%s' "$out" | jq_ "d['picks'][0]['why']")" "Comedy · cozy · “Pull up a chair.”"
eq "link: Seerr's template with our item id" "$(printf '%s' "$out" | jq_ "d['picks'][0]['link']")" \
  "https://emby.example.test/web/index.html#!/item?id=115&context=home&serverId=srv1"
eq "play off: ▶ link buttons only" "$(printf '%s' "$out" | jq_ "' | '.join(','.join(b.get('url', b.get('data', '')) [:40] for b in r) for r in d['buttons'])")" \
  "https://emby.example.test/web/index.html | https://emby.example.test/web/index.html | https://emby.example.test/web/index.html | https://emby.example.test/web/index.html"
has "message: Markdown title" "$(printf '%s' "$out" | jq_ "d['message']")" "**Cozy Little Cafe** (2022) · 84 min · ★6.9"
txt=$(printf '%s' "$out" | jq_ "d['text']")
has "text: plain, with ▶ links" "$txt" "▶ https://emby.example.test/web/index.html#!/item?id=115"
hasnt "text: no Markdown" "$txt" "**"
has "text: numbered picks" "$txt" "1️⃣ Cozy Little Cafe (2022) · 84 min · ★6.9"

echo "# no rebuild on the next query"
: > "$T/emb.log"
MEDIA_PLAY_CONFIG=$T/play-on.json mj vibe --json cozy rainy
eq "nothing re-embedded" "$(docs)" "0"
eq "same picks" "$(titles)" "Cozy Little Cafe | Rainy Window | Autumn Tea House | Long Rain"
eq "play on: ▶ + 📺 mt:play per pick" "$(printf '%s' "$out" | jq_ "d['buttons'][0][1]['text'] + ' ' + d['buttons'][0][1]['data'] + ' ' + str(d['play_on_tv'])")" "📺 Play on TV mt:play:115 True"

echo "# runtime caps"
mj vibe --json short cozy
eq "short: ≤ 100 min only" "$(printf '%s' "$out" | jq_ "all(p['runtime_min'] <= 100 for p in d['picks']) and len(d['picks']) == 4")" "True"
eq "short: label" "$(printf '%s' "$out" | jq_ "d['label'] + ' ' + str(d['max_min'])")" "cozy · ≤ 100 min 100"
mj vibe --json 90min cozy
eq "90min: ≤ 90 only" "$(printf '%s' "$out" | jq_ "sorted(p['title'] for p in d['picks'])")" "['Cozy Little Cafe', 'Ghost Lake Returns', 'Puddle Jump', 'Tiny Signal']"
has "90min: low-rated fills in when too few fit" "$(titles)" "Puddle Jump"
mj vibe --json heist under 2h
eq "under 2h: cap 120" "$(printf '%s' "$out" | jq_ "d['max_min']")" "120"
eq "under 2h: heist first" "$(printf '%s' "$out" | jq_ "d['picks'][0]['title']")" "Glass Vault"
mj vibe --json Oceans 11 heist
eq "a bare number is no time cap" "$(printf '%s' "$out" | jq_ "d['max_min']")" "None"

echo "# like <film>"
mj vibe --json like First Signal
eq "ref = First Signal" "$(printf '%s' "$out" | jq_ "d['ref']['title'] + ' ' + str(d['ref']['year'])")" "First Signal 2017"
hasnt "the film itself isn't a pick" "$(titles)" "First Signal |"
eq "label" "$(printf '%s' "$out" | jq_ "d['label']")" "like First Signal (2017)"
eq "closest first; partly watched marked" "$(printf '%s' "$out" | jq_ "(d['picks'][0]['title'], d['picks'][0]['seen'])")" "('Signal From Beyond', 'partly')"
has "why: shared genres like the film" "$(printf '%s' "$out" | jq_ "d['picks'][0]['why']")" "Sci-Fi + Mystery like First Signal"
has "why: started marker" "$(printf '%s' "$out" | jq_ "d['picks'][0]['why']")" "👁 started"
eq "no query embedding for a bare like" "$(tail -1 "$T/emb.log" | cut -c1-6)" "query "
: > "$T/emb.log"
mj vibe --json first signal
eq "a bare title = like it (any case)" "$(printf '%s' "$out" | jq_ "d['ref']['title']")" "First Signal"
eq "bare title: no query embedding" "$(grep -c '^query' "$T/emb.log")" "0"
mj vibe --json laugh like Harbor Lights
eq "words + like: both in the label" "$(printf '%s' "$out" | jq_ "d['label']")" "like Harbor Lights (2018) · laugh"
mj vibe --json something like Nowhere Film
has "unknown film: note, matched on words" "$(printf '%s' "$out" | jq_ "d['message']")" "“Nowhere Film” isn't in the library"
eq "unknown film: still 4 picks" "$(printf '%s' "$out" | jq_ "len(d['picks'])")" "4"
mj vibe --json "I'd" like something cozy
hasnt "'like something cozy' is no film note" "$(printf '%s' "$out" | jq_ "d['message']")" "isn't in the library"

echo "# diversity"
mj vibe --json -n 2 ghost lake movie
eq "near-duplicate sequel gives way" "$(titles)" "Ghost Lake | Lake Phantom"

echo "# incremental sync (new, changed, removed)"
scen grown; : > "$T/emb.log"
mj vibe --json cozy ghost
eq "only the new and the changed title embedded" "$(grep '^doc ' "$T/emb.log" | sort | tr '\n' '|')" "doc Harbor Lights|doc The Lantern Keeper|"
eq "index: 15 items (1 new, 1 removed)" "$(printf '%s' "$out" | jq_ "d['index']['items']")" "15"
has "new title can be picked" "$(titles)" "The Lantern Keeper"
eq "still one vectors file" "$(find "$T/vibe" -name 'vectors-*.f32' | wc -l)" "1"
mj vibe --json like Dust Road
hasnt "removed title is gone" "$(printf '%s' "$out" | jq_ "str(d['ref']) + ' ' + ' '.join(p['title'] for p in d['picks'])")" "Dust Road"

echo "# refresh"
: > "$T/emb.log"
m vibe --refresh
eq "refresh: exit 0" "$rc" "0"
has "refresh: counts line" "$out" "vibe index: 15 movies (0 new, 0 changed, 0 removed; 0 embedded"
m vibe --refresh --quiet
eq "refresh --quiet: silent" "$out" ""
m vibe --refresh --full --quiet
eq "refresh --full: every title re-embedded" "$(docs)" "15"

echo "# failures"
MEDIA_SECRETS=$T/seerr-down.json mj vibe --json cozy rainy
eq "Seerr down: exit 0" "$rc" "0"
has "Seerr down: can't check seen, says so" "$(printf '%s' "$out" | jq_ "d['message']")" "couldn't check what you've seen"
eq "Seerr down: links from the stored template" "$(printf '%s' "$out" | jq_ "bool(d['picks'][0]['link'])")" "True"
scen emby-down
mj vibe --json cozy rainy
eq "Emby down + index: still 4 picks" "$(printf '%s' "$out" | jq_ "len(d['picks'])")" "4"
has "Emby down: warning" "$(printf '%s' "$out" | jq_ "' '.join(d['warnings'])")" "Emby library"
MEDIA_VIBE_DIR=$T/empty m vibe cozy
eq "Emby down + no index: exit 1" "$rc" "1"
has "Emby down + no index: says so" "$out" "no index yet and Emby is unreachable"
scen embed-down
m vibe cozy
eq "embedding down: exit 1" "$rc" "1"
has "embedding down: short error" "$out" "media: embedding: HTTPError"
scen ok
MEDIA_VIBE_EMBED_URL= MEDIA_VIBE_MODEL=$T/none.gguf m vibe cozy
eq "missing model: exit 1" "$rc" "1"
has "missing model: named" "$out" "embedding: missing"
MEDIA_SECRETS=$T/noemby.json m vibe cozy
eq "no emby key: exit 1" "$rc" "1"
m vibe -n 9 cozy
has "-n 9 refused" "$out" "-n must be 1..8"

echo "# safety"
eq "every Emby request was a GET ($(wc -l < "$T/emby.log") requests)" "$(grep -vc '^GET ' "$T/emby.log")" "0"
eq "every Seerr request was a GET" "$(grep -vc '^GET ' "$T/server.log")" "0"
hasnt "only his own user's history read" "$(grep '/Users/' "$T/emby.log" | grep -v '/Users/aaaa1111' || true)" "/Users/"
hasnt "no Emby key in any output" "$(cat "$T/all.out")" "EMBY-KEY-Vb42"
hasnt "no Seerr key in any output" "$(cat "$T/all.out")" "SEERR-KEY-Zq81"
hasnt "no tracebacks" "$(cat "$T/all.out")" "Traceback"
eq "fake servers clean" "$(cat "$T/server.err" "$T/emby.err")" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
