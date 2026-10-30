#!/usr/bin/env bash
# Offline tests for `media franchise` (missing parts of film series he owns, a request poll, ignore list). Exit 0 = all pass.
# fake_franchise.py stands in for Radarr (/api/v3/collection, /api/v3/movie) and Seerr (collections, movie details,
# POST /api/v1/request) with fictional series; MEDIA_FRANCHISE_STATE points at a temp dir.
# Checks: gap list and counts, skip reasons (requested, wanted, unmonitored, excluded, blocklisted, unreleased by Radarr
# and by Seerr's date, Emby-only copies), Seerr-only parts, --min-owned, ranking, JSON, the poll (<= 10 options, <= 3 a
# series, prefix trimming, 100-char cap, saved for --picked), ignore/unignore/ignored, request (dry run, --picked,
# already requested, blocklisted, partial), Seerr/Radarr down, argument errors, GET-only list, no key leaks.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-franchise-tests.XXXXXX")
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT

echo ok > "$T/scenario"
python3 -I "$HERE/fake_franchise.py" "$T/port" "$T/server.log" "$T/scenario" 2>"$T/server.err" &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
printf '{"seerr": {"url": "%s/seerr", "key": "SEERR-KEY-Zq81"}, "radarr": {"url": "%s/radarr", "key": "RADARR-KEY-Xw42"}}\n' "$B" "$B" > "$T/cfg.json"
export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-10T12:00:00+02:00 MEDIA_TIMEOUT=5 MEDIA_FRANCHISE_STATE=$T/state

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
m() { out=$(timeout 30 "$MEDIA" "$@" 2>&1); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; }
jq_() { python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print($1)"; }
logn() { local n; n=$(grep -c -- "$1" "$T/server.log" 2>/dev/null); echo "${n:-0}"; }
reset_log() { cat "$T/server.log" >> "$T/all.log" 2>/dev/null; : > "$T/server.log"; }

echo "# list"
m franchise
eq "rc 0" "$rc" "0"
eq "header" "$(printf '%s\n' "$out" | head -1)" "Film series with missing parts (owned ≥ 2): 6 series, 15 titles"
eq "best series first" "$(printf '%s\n' "$out" | sed -n 2p)" "Starfall (2 owned, 2 missing):"
eq "best title first" "$(printf '%s\n' "$out" | sed -n 3p)" "  Starfall III (2008) ★7.2 (4000 votes)  tmdb:103"
has "Seerr-only part found" "$out" "Starfall: Echoes (2011)"
hasnt "unreleased (Radarr) skipped" "$out" "Origins"
hasnt "unreleased (Seerr date) skipped" "$out" "Tin Lantern 6"
hasnt "requested skipped" "$out" "Copper Kettle"
hasnt "wanted + unmonitored skipped" "$out" "Night Ferry"
hasnt "Emby-only copy = owned, excluded skipped" "$out" "Harbor Lights"
hasnt "blocklisted skipped" "$out" "Iron Choir"
hasnt "one owned part < --min-owned" "$out" "Lone Signal"
hasnt "complete series absent" "$out" "Complete Tide"
eq "skip line" "$(printf '%s\n' "$out" | tail -1)" "Skipped: 2 not released yet; 1 on Seerr's blocklist; 1 on Radarr's exclusion list; 1 requested in Seerr; 1 unmonitored in Radarr; 1 in Radarr (wanted or downloading)"
has "Glass Orchard: owned 3" "$out" "Glass Orchard (3 owned, 2 missing):"
has "the text list shows every missing part (Saga: 5)" "$out" "Saga of Ash (2 owned, 5 missing):"
m franchise -n 2
has "-n caps series" "$out" "… +4 more"
m franchise --min-owned 1
has "--min-owned 1 adds Lone Signal" "$out" "Lone Signal Returns (2018)"
m franchise --min-owned 0
eq "--min-owned 0 refused" "$rc" "1"

echo "# json"
m franchise --json
eq "json rc 0" "$rc" "0"
eq "json counts" "$(printf '%s' "$out" | jq_ 'd["series"], d["titles"], d["ignored_series"]')" "6 15 0"
eq "json sorted by score" "$(printf '%s' "$out" | jq_ 'all(a["score"] >= b["score"] for a, b in zip(d["gaps"], d["gaps"][1:]))')" "True"
eq "json first gap" "$(printf '%s' "$out" | jq_ '[d["gaps"][0][k] for k in ("collection", "collection_id", "owned", "missing", "title", "year", "tmdb", "rating", "votes")]')" "['Starfall', 9001, 2, 2, 'Starfall III', 2008, 103, 7.2, 4000]"
eq "json skipped" "$(printf '%s' "$out" | jq_ 'sorted(d["skipped"].items())')" "[('blocklisted', 1), ('excluded', 1), ('requested', 1), ('unmonitored', 1), ('unreleased', 2), ('wanted', 1)]"
reset_log
m franchise
eq "list is GET-only" "$(grep -vc '^GET ' "$T/server.log")" "0"
eq "Seerr asked only for series with candidates" "$(logn 'GET /seerr/api/v1/collection/')" "9"

echo "# poll"
m franchise --poll-json
eq "poll rc 0" "$rc" "0"
eq "poll flags" "$(printf '%s' "$out" | jq_ 'd["poll"]["multiple"], d["poll"]["anonymous"], len(d["poll"]["options"])')" "True False 10"
eq "≤ 3 per series" "$(printf '%s' "$out" | jq_ 'max(__import__("collections").Counter(p["collection_id"] for p in d["picks"]).values())')" "3"
eq "ids match picks" "$(printf '%s' "$out" | jq_ 'd["ids"] == [p["tmdb"] for p in d["picks"]] and len(d["ids"]) == 10')" "True"
eq "first options: series kept together" "$(printf '%s' "$out" | jq_ 'd["poll"]["options"][:2]')" "['Starfall III (2008)', 'Starfall: Echoes (2011)']"
has "series prefix when the title lacks it" "$(printf '%s' "$out" | jq_ 'd["poll"]["options"]')" "'Saga of Ash: Part 3 (2002)'"
eq "options ≤ 100 chars" "$(printf '%s' "$out" | jq_ 'max(len(o) for o in d["poll"]["options"]) <= 100')" "True"
has "message" "$(printf '%s' "$out" | jq_ 'd["message"]')" "6 film series you own have gaps; the best 10 missing parts are in the poll."
eq "poll saved for --picked" "$(jq_ 'len(d["options"]), len(d["ids"])' < "$T/state/last-poll.json")" "10 10"
eq "saved options = sent options" "$({ cat "$T/state/last-poll.json"; printf '\n\x1e'; printf '%s' "$out"; } | python3 -c 'import json,sys; a,b=sys.stdin.read().split("\x1e"); print(json.loads(a)["options"] == json.loads(b)["poll"]["options"])')" "True"
eq "state file private" "$(stat -c %a "$T/state/last-poll.json")" "600"
POLL=$out

echo "# ignore"
m franchise ignore "Paper Moons"
has "ignore by name" "$out" "Ignoring Paper Moons Collection (9006)"
m franchise
hasnt "ignored series gone" "$out" "Paper Moons"
has "ignored counted" "$(printf '%s\n' "$out" | tail -1)" "; 1 ignored series"
m franchise ignored
has "ignored lists it" "$out" "Paper Moons Collection (9006), since 2026-10-10"
m franchise ignore 9001
has "ignore by id" "$out" "Ignoring Starfall Collection (9001)"
m franchise ignore "Collection"
eq "ambiguous name refused" "$rc" "1"
has "ambiguous message" "$out" "matches 12 collections"
m franchise ignore "Nope Saga"
eq "unknown name refused" "$rc" "1"
m franchise unignore 9001
has "unignore by id" "$out" "No longer ignoring Starfall Collection (9001)"
m franchise unignore paper
has "unignore by name" "$out" "No longer ignoring Paper Moons Collection (9006)"
m franchise ignored
eq "ignore list empty" "$out" "No ignored collections."

echo "# request"
reset_log
m franchise request --dry-run 103 405
eq "dry run rc 0" "$rc" "0"
has "dry run line" "$out" 'DRY RUN, would POST /api/v1/request {"mediaType": "movie", "mediaId": 103, "is4k": false}  for Starfall III (2008)'
has "dry run second" "$out" "mediaId\": 405"
eq "dry run sends nothing" "$(logn '^POST')" "0"
OPT1=$(printf '%s' "$POLL" | jq_ 'd["poll"]["options"][0]'); OPT3=$(printf '%s' "$POLL" | jq_ 'd["poll"]["options"][2]')
m franchise request --dry-run --picked "$OPT1 | $OPT3" 103
eq "picked + duplicate id: 2 lines" "$(printf '%s\n' "$out" | grep -c 'DRY RUN')" "2"
m franchise request --dry-run --picked "Nope: X (1999)"
eq "unknown option refused" "$rc" "1"
has "unknown option named" "$out" "not in the last franchise poll: 'Nope: X (1999)'"
m franchise request --dry-run 203
has "already requested" "$out" "Not requested: Copper Kettle 3 (2005) is already requested (pending)."
m franchise request --dry-run 803
eq "blocklisted rc 1" "$rc" "1"
has "blocklisted said" "$out" "on Seerr's blocklist"
m franchise request abc
eq "bad id refused" "$rc" "1"
m franchise request
eq "no ids refused" "$rc" "1"
eq "still no POST" "$(logn '^POST')" "0"
m franchise request 103 803
eq "partial rc 3" "$rc" "3"
has "fake request made" "$out" "Requested Starfall III (2008) (request #77, approved), Radarr will grab it"
eq "one POST" "$(logn '^POST /seerr/api/v1/request')" "1"
eq "POST body" "$(grep '^BODY' "$T/server.log")" 'BODY {"is4k": false, "mediaId": 103, "mediaType": "movie"}'

echo "# errors"
m franchise --json request 103
eq "--json with request refused" "$rc" "1"
m franchise --dry-run
eq "--dry-run without request refused" "$rc" "1"
m franchise list 5
eq "list takes no items" "$rc" "1"
m franchise --bogus
eq "unknown flag rc 2" "$rc" "2"
echo seerr-down > "$T/scenario"; reset_log
m franchise
eq "Seerr down rc 3" "$rc" "3"
has "Seerr down warns" "$out" "!! skipped Seerr: seerr HTTP 500"
has "Radarr-only gaps still listed" "$out" "Starfall III (2008)"
eq "one Seerr try only" "$(logn 'GET /seerr/')" "1"
m franchise --poll-json
eq "poll Seerr down rc 3" "$rc" "3"
has "poll warnings" "$(printf '%s' "$out" | jq_ 'd["warnings"]')" "Seerr: seerr HTTP 500"
echo radarr-down > "$T/scenario"
m franchise
eq "Radarr down rc 1" "$rc" "1"
has "Radarr down said" "$out" "media: radarr HTTP 500"
echo ok > "$T/scenario"

echo "# safety"
hasnt "no Seerr key in output" "$(cat "$T/all.out")" "SEERR-KEY-Zq81"
hasnt "no Radarr key in output" "$(cat "$T/all.out")" "RADARR-KEY-Xw42"
hasnt "no tracebacks" "$(cat "$T/all.out")" "Traceback"
eq "fake server clean" "$(cat "$T/server.err")" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
