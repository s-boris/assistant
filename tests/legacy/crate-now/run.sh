#!/bin/bash
# Offline tests for ~/bin/crate-now (fake ha, real lists on a temp DB). Exit 0 = all pass.
# CRATE_NOW=path tests another copy.
BIN=${CRATE_NOW:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/crate-now}
D=$(dirname "$(readlink -f "$0")")
T=$(mktemp -d); trap 'rm -rf "${T:?}"' EXIT
export CRATE_NOW_HA="$D/fake/ha" CRATE_NOW_LISTS="$HOME"/bin/lists LISTS_DB="$T/lists.json" FAKE_HA_JSON="$T/ha.json"
pass=0; fail=0
eq() { if [ "$2" = "$3" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $1: got [$2] want [$3]"; fi; }
has() { case "$2" in *"$3"*) pass=$((pass+1));; *) fail=$((fail+1)); echo "FAIL $1: [$2] lacks [$3]";; esac; }
st() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"; }
ha() { cat > "$FAKE_HA_JSON"; }

# nothing playing: idle Spotify without attributes
ha <<'J'
[{"entity_id": "media_player.spotify_x", "state": "idle", "attributes": {}},
 {"entity_id": "media_player.tv", "state": "off", "attributes": {}}]
J
out=$("$BIN"); rc=$?
eq "idle rc" "$rc" 0; has "idle text" "$out" "Nothing is playing"
eq "idle json" "$(st "$("$BIN" --json)" status)" nothing

# Spotify playing beats a Sonos also playing; Spotify first even when listed last
ha <<'J'
[{"entity_id": "media_player.sonos_living", "state": "playing", "attributes": {"media_artist": "Other", "media_title": "Song"}},
 {"entity_id": "media_player.spotify_x", "state": "playing", "attributes": {"media_artist": "Nova Lumen, Ray Vex", "media_title": "Afterglow - Original Mix"}}]
J
out=$("$BIN" --dry-run); has "dry run text" "$out" "would add to the crate: Nova Lumen, Ray Vex – Afterglow - Original Mix"
eq "dry run wrote nothing" "$("$HOME"/bin/lists show crate --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("open") or []))' 2>/dev/null || echo 0)" 0
out=$("$BIN"); rc=$?
eq "add rc" "$rc" 0; has "add text" "$out" "➕ Crate: Nova Lumen, Ray Vex – Afterglow - Original Mix"
has "on the list" "$("$HOME"/bin/lists show crate)" "Nova Lumen, Ray Vex – Afterglow - Original Mix"

# same track again (other mix suffix / feat spelling) -> exists, no second item
ha <<'J'
[{"entity_id": "media_player.spotify_x", "state": "playing", "attributes": {"media_artist": "Nova Lumen", "media_title": "Afterglow (Original Mix)"}}]
J
out=$("$BIN" --json)
eq "dedupe status" "$(st "$out" status)" exists
eq "one item only" "$("$HOME"/bin/lists show crate --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["open"]))')" 1

# an item rave-radar wrote ("Artist – Title") also counts; a checked one too
"$HOME"/bin/lists add crate "Mira Kessel – Tidewalk" >/dev/null
"$HOME"/bin/lists done crate "Tidewalk" >/dev/null
ha <<'J'
[{"entity_id": "media_player.spotify_x", "state": "playing", "attributes": {"media_artist": "Mira Kessel", "media_title": "Tidewalk (feat. Someone)"}}]
J
eq "checked counts" "$(st "$("$BIN" --json)" status)" exists

# paused counts (he pauses to save it); --player picks one
ha <<'J'
[{"entity_id": "media_player.spotify_x", "state": "paused", "attributes": {"media_artist": "Ølander", "media_title": "Slate"}},
 {"entity_id": "media_player.sonos_living", "state": "playing", "attributes": {"media_artist": "Tilo Brück", "media_title": "Far Signals"}}]
J
eq "playing first" "$(st "$("$BIN" --json --dry-run)" artist)" "Tilo Brück"
out=$("$BIN" --player media_player.spotify_x); has "paused add" "$out" "Ølander – Slate (paused)"
out=$("$BIN" --player media_player.nope); rc=$?
eq "unknown player rc" "$rc" 3; has "unknown player text" "$out" "no player media_player.nope"

# ha down -> exit 3 with a fallback line
out=$(FAKE_HA_FAIL=1 "$BIN"); rc=$?
eq "ha fail rc" "$rc" 3; has "ha fail text" "$out" "Couldn't save the track (Home Assistant: ha: connection refused)"
out=$("$BIN" --help); rc=$?
eq "help rc" "$rc" 0; has "help text" "$out" "crate-now"

echo "crate-now tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
