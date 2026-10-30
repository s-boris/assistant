#!/usr/bin/env bash
# Offline tests for `media doctor` (fake Seerr/Radarr/Sonarr/Emby: fake_doctor.py, stateful). Exit 0 = all pass.
# Checks every verdict, the dry run (GET-only), the exact fixes of a real run (and nothing else: no library deletes),
# throttling on a second run, title mode (+ forward lines, the requested match wins), --grab, --json, --cron (weekly:
# fixes + an inbox note only from 3 stuck on him), no key leaks.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/media-doctor.XXXXXX")
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT

start() {
  [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null
  rm -f "$T/port"; : > "$T/server.log"
  python3 -I "$HERE/fake_doctor.py" "$T/port" "$T/server.log" 2>>"$T/server.err" &
  SRV=$!
  for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
  B="http://127.0.0.1:$(cat "$T/port")"
  printf '{"seerr": {"url": "%s/seerr", "key": "SEERR-KEY-Zq81"}, "radarr": {"url": "%s/radarr", "key": "RADARR-KEY-Xw42"}, "sonarr": {"url": "%s/sonarr", "key": "SONARR-KEY-Vy73"}, "emby": {"url": "%s/embysvc", "key": "EMBY-KEY-Tt55"}}\n' \
    "$B" "$B" "$B" "$B" > "$T/cfg.json"
}
export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-20T12:00:00+02:00 MEDIA_PAGE_SIZE=2 MEDIA_TIMEOUT=5 \
       MEDIA_READY_USER=Owner MEDIA_DOCTOR_STATE=$T/state/doctor.json MEDIA_PLAY_CONFIG=/nonexistent/play.json

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
m() { out=$(timeout 60 "$MEDIA" "$@" 2>&1); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; }
writes() { grep -v '^GET ' "$T/server.log"; }

start
echo "# --all, dry run"
m doctor --dry-run
eq "rc 0" "$rc" "0"
has "header" "$out" "🩺 Request doctor · 13 open requests · dry run · 3 need you"
has "mismatch first, with requester + age" "$out" "🎬 Mismatch Movie (2022) · Ana · 7 w
   🔤 name mismatch: 5 releases, named 2021/2015, 2022 in the arr · 👉 \`media doctor \"Mismatch Movie\" --grab\` grabs the best match"
has "import blocked -> arr-unblock" "$out" "⛔ import blocked: Manual import required · 👉 arr-unblock imports it"
has "pending approval" "$out" "⏸ waiting for approval in Seerr"
has "failed download -> drop + blocklist (dry)" "$out" "🔁 download failed (No files found are eligible for import) → (dry run) dropped + blocklisted it"
has "stalled torrent -> drop (dry)" "$out" "🔁 stalled torrent → (dry run) dropped"
has "failed Seerr request -> retry (dry)" "$out" "📺 Failed Show S1 · Ana · 10 d
   🔁 Seerr failed to hand it to Sonarr → (dry run) retried the request"
has "unmonitored -> monitor + search (dry)" "$out" "🔎 1 of 1 releases usable → (dry run) monitored it again; (dry run) searched again"
has "missing episodes: only releases for them count" "$out" "🔎 2 of 4 aired eps missing (S1: 2), 1 of 1 releases usable → (dry run) monitored 1 missing episode again; (dry run) searched the 2 missing episodes again"
has "no release + grab history" "$out" "🔍 last grab 10.09 failed, no release on the indexers (searched 20.10)"
has "his own request = you, downloading with ETA" "$out" "Waiting normally:
  ⬇️ Downloading Movie: 40% · ETA 1h30m (~13:30)"
has "not out yet" "$out" "📅 Cinema Movie: in cinemas from 19.11.2026"
has "airing" "$out" "📅 Airing Show S1: up to date (3 eps), next S01E04 22.10"
has "already on Emby under another id" "$out" "Already there or gone: 1 (Emby Twin ✅)"
has "skipped counts" "$out" "Skipped: 1 old failed, 1 older than 180 d, 1 removed (--days 0 = all)"
hasnt "4K request ignored" "$out" "tmdb:101"
eq "dry run: only GETs" "$(writes)" ""
eq "dry run: no state" "$(ls "$T/state" 2>/dev/null)" ""

echo "# --all, real run: exactly the routine fixes"
m doctor
eq "rc 0" "$rc" "0"
has "header counts fixes" "$out" "13 open requests · 7 fixed · 3 need you"
has "fix shown" "$out" "→ ✅ dropped + blocklisted it; the arr searches for another"
w=$(writes)
eq "7 writes" "$(printf '%s\n' "$w" | grep -c .)" "7"
has "failed download dropped with blocklist" "$w" "DELETE /radarr/api/v3/queue/501?removeFromClient=true&blocklist=true&skipRedownload=false"
has "stalled torrent dropped with blocklist" "$w" "DELETE /radarr/api/v3/queue/502?removeFromClient=true&blocklist=true&skipRedownload=false"
has "Seerr retry" "$w" "POST /seerr/api/v1/request/9/retry"
has "monitor movie" "$w" 'PUT /radarr/api/v3/movie/editor {"movieIds": [16], "monitored": true}'
has "movie search" "$w" 'POST /radarr/api/v3/command {"name": "MoviesSearch", "movieIds": [16]}'
has "monitor episode" "$w" 'PUT /sonarr/api/v3/episode/monitor {"episodeIds": [2204], "monitored": true}'
has "episode search" "$w" 'POST /sonarr/api/v3/command {"name": "EpisodeSearch", "episodeIds": [2203, 2204]}'
hasnt "never deletes a movie" "$w" "DELETE /radarr/api/v3/movie"
hasnt "never deletes a file" "$w" "file"
hasnt "no grab without --grab" "$w" "/api/v3/release"
has "action log" "$(cat "$T/state/doctor.log")" "radarr DELETE /api/v3/queue/501 dropped + blocklisted it"

echo "# second run: throttled, nothing sent again"
: > "$T/server.log"
m doctor
eq "rc 0" "$rc" "0"
eq "no writes" "$(writes)" ""
has "retry remembered" "$out" "retried 20.10, still not there"
has "cached usable releases" "$out" "1 usable release(s) found (checked 20.10); the arr should grab one"
eq "no indexer search for cached probes" "$(grep -c 'GET /radarr/api/v3/release?movieId=16' "$T/server.log")" "0"

echo "# title mode"
start
rm -rf "$T/state"
m doctor "Mismatch Movie"
eq "rc 0" "$rc" "0"
has "the requested match wins" "$out" "🩺 🎬 Mismatch Movie (2022) · Ana · 7 w"
has "German line" "$out" '🇨🇭 "Mismatch Movie" hängt noch an einem Detail, ich schau'"'"'s mir an. 🔧'
has "English line" "$out" '🇬🇧 "Mismatch Movie" is stuck on a detail; I'"'"'m on it. 🔧'
m doctor "Waiting Approval"
has "pending: forward line" "$out" '🇨🇭 "Waiting Approval" ist angefragt und wartet noch auf Freigabe.'
m doctor "Nothing Like This"
eq "unknown title rc 1" "$rc" "1"
m doctor --grab
eq "--grab needs a title" "$rc" "1"
: > "$T/server.log"
m doctor "Mismatch Movie (2022)" --grab
eq "grab rc 0" "$rc" "0"
has "grabbed the best allowed match (Bluray-1080p = cutoff)" "$out" "✅ grabbed Mismatch.Movie.2021.1080p.BluRay-GRP2"
has "grab POST names the movie" "$(writes)" '"guid": "g-bluray", "indexerId": 5, "movieId": 11'
eq "grab = the only write" "$(writes | grep -c .)" "1"

echo "# --json"
start
rm -rf "$T/state"
m doctor --json --dry-run --no-probe
eq "json rc 0" "$rc" "0"
eq "13 items, text + message, skipped" "$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["items"]), "/doctor TITLE" in d["text"], d["message"].startswith("🩺"), d["skipped"]["removed"], d["probes"])')" "13 True True 1 0"
eq "no-probe: no indexer search" "$(grep -c '/release' "$T/server.log")" "0"
eq "first item = mismatch-free without probes" "$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["items"][0]["code"], d["items"][0]["de"][:16])')" "import_blocked \"Blocked Import\""

echo "# --cron (weekly): the routine fixes, an inbox note only from 3 requests stuck on him"
start
rm -rf "$T/state" "$T/inbox"
export MEDIA_DOCTOR_INBOX=$T/inbox
m doctor --cron --dry-run
eq "cron dry: rc 0" "$rc" "0"
has "cron dry: summary line" "$out" "doctor --cron: 13 open requests, 0 fixed, 3 need you"
has "cron dry: prints the note it would write" "$out" "(dry run) would write $T/inbox/media-doctor-2026-10-20.md"
eq "cron dry: no note, no writes" "$(ls "$T/inbox" 2>/dev/null)$(writes)" ""
m doctor --cron
eq "cron: rc 0" "$rc" "0"
eq "cron: one summary line + the note path, nothing else" "$out" "doctor --cron: 13 open requests, 7 fixed, 3 need you
note: $T/inbox/media-doctor-2026-10-20.md"
note=$(cat "$T/inbox/media-doctor-2026-10-20.md" 2>/dev/null)
has "note: header" "$note" "# media-doctor 2026-10-20: 3 requests stuck on the owner"
has "note: stuck one with its hint" "$note" "- 🎬 Mismatch Movie (2022) · Ana · 7 w
  🔤 name mismatch"
has "note: pending approval listed" "$note" "⏸ waiting for approval in Seerr"
hasnt "note: waiting ones left out" "$note" "Downloading Movie"
has "note: removed ones left alone" "$note" "Skipped (left alone on purpose): 1 old failed, 1 older than 180 d, 1 removed"
w=$(writes)
eq "cron: the same 7 routine fixes" "$(printf '%s\n' "$w" | grep -c .)" "7"
hasnt "cron: never deletes a movie" "$w" "DELETE /radarr/api/v3/movie"
hasnt "cron: never deletes a file" "$w" "file"
hasnt "cron: never re-adds (no POST to the arr's movie/series)" "$w" "POST /radarr/api/v3/movie "
hasnt "cron: no series add" "$w" "POST /sonarr/api/v3/series"
hasnt "cron: no grab" "$w" "/api/v3/release"
eq "cron: one note" "$(ls "$T/inbox" | grep -c .)" "1"
start
rm -rf "$T/state" "$T/inbox"
MEDIA_DOCTOR_NOTE_MIN=4 m doctor --cron
eq "below the threshold: rc 0" "$rc" "0"
eq "below the threshold: silent, no note" "$(ls "$T/inbox" 2>/dev/null)" ""
m doctor --cron "Mismatch Movie"
eq "--cron with a title refused" "$rc" "1"
m doctor --cron --json
eq "--cron with --json refused" "$rc" "1"
unset MEDIA_DOCTOR_INBOX

echo "# season pack = the release already on disk"
rm -f "$T/state/doctor.json"
export FAKE_SAME_PACK=same; start
m doctor --dry-run
has "same pack on disk: nothing new on the indexers" "$out" "🔍 2 of 4 aired eps missing (S1: 2), nothing new on the indexers: 2 releases, all the Q release already on disk (searched 20.10)"
has "same pack on disk: not counted as needing him" "$out" "13 open requests · dry run · 3 need you"
hasnt "same pack on disk: no 'would replace'" "$out" "would replace files"
has "same pack on disk: episode files read" "$(grep -c 'GET /sonarr/api/v3/episodefile' "$T/server.log")" "1"
eq "same pack on disk: dry run only GETs" "$(writes)" ""
export FAKE_SAME_PACK=bigger; start
m doctor --dry-run
has "same name but far bigger pack: still needs him" "$out" "🚫 2 of 4 aired eps missing (S1: 2), 2 releases, all packs that would replace files already on disk"
has "bigger pack: counted" "$out" "13 open requests · dry run · 4 need you"
export FAKE_SAME_PACK=other; start
m doctor --dry-run
has "another group's pack: still needs him" "$out" "2 releases, all packs that would replace files already on disk"
has "other group: counted" "$out" "13 open requests · dry run · 4 need you"
export FAKE_SAME_PACK=same; start
mkdir -p "$T/state"
printf '{"probe": {"sonarr:22:1": {"kind": "existing", "n": 2, "count": 2, "reason": "Existing file", "t": %s}}}\n' \
  "$(date -d 2026-10-19T12:00:00+02:00 +%s)" > "$T/state/doctor.json"
m doctor --dry-run --no-probe
has "old cached 'existing' with --no-probe: kept" "$out" "2 releases, all packs that would replace files already on disk"
m doctor
has "old cached 'existing': probed again and re-checked" "$out" "nothing new on the indexers: 2 releases, all the Q release already on disk"
eq "re-check cached" "$(python3 -c 'import json,sys; e=json.load(open(sys.argv[1]))["probe"]["sonarr:22:1"]; print(e["kind"], e.get("group"), e.get("disk_checked"))' "$T/state/doctor.json")" "same_on_disk Q True"
hasnt "nothing new: no search, no grab for it" "$(writes)" "EpisodeSearch"
m doctor --dry-run --no-probe
has "cached same_on_disk with --no-probe" "$out" "nothing new on the indexers: 2 releases, all the Q release already on disk (searched 20.10)"
rm -f "$T/state/doctor.json"
unset FAKE_SAME_PACK; start

echo "# safety"
hasnt "no Seerr key" "$(cat "$T/all.out")" "SEERR-KEY-Zq81"
hasnt "no Radarr key" "$(cat "$T/all.out")" "RADARR-KEY-Xw42"
hasnt "no Emby key" "$(cat "$T/all.out")" "EMBY-KEY-Tt55"
hasnt "no tracebacks" "$(cat "$T/all.out")" "Traceback"
eq "fake server clean" "$(cat "$T/server.err" 2>/dev/null)" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
