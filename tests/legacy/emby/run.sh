#!/usr/bin/env bash
# Offline tests for ~/bin/emby (now card, watch cron notes, library report). Exit 0 = all pass.
# A local fake Emby (fake_emby.py, synthetic sessions and library) stands in for the server; EMBY_URL/EMBY_KEY
# point at it with a fake key, EMBY_NOW fixes the clock (owner time zone), EMBY_PAGE=2 forces paging.
# Checks: card content and advice, LAN vs remote, source version choice, GET-only, key only in the header,
# no key or address in any output, watch dedup (2 h), quiet hours for the load note only, errors.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
EMBY=${EMBY_BIN:-/home/node/bin/emby}   # EMBY_BIN: test another copy
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/emby-tests.XXXXXX")
trap 'kill $SRV 2>/dev/null; find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT

echo busy > "$T/scenario"
python3 -I "$HERE/fake_emby.py" "$T/port" "$T/server.log" "$T/scenario" 2>"$T/server.err" &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
DEAD=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
KEY=EMBY-KEY-Tq93
mkdir -p "$T/inbox"
printf '{"Living Room": {"lan_mbit": 100}}\n' > "$T/devices.json"   # placeholder device with a 100 Mbit port
export EMBY_DEVICES=$T/devices.json EMBY_URL=$B EMBY_KEY=$KEY EMBY_SECRETS=$T/no-such.json EMBY_STATE=$T/state/watch.json EMBY_INBOX=$T/inbox \
       EMBY_NOW=2026-10-17T21:00:00+02:00 EMBY_TIMEOUT=5 EMBY_PAGE=2
unset EMBY_OWNER EMBY_QUIET

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -40; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
line() { printf '%s\n' "$1" | sed -n "${2}p"; }
e() { out=$(timeout 30 "$EMBY" "$@" 2>&1 </dev/null); rc=$?; printf '%s\n' "$out" >> "$T/all.out"; }
scen() { echo "$1" > "$T/scenario"; }
notes() { find "$T/inbox" -name 'emby-watch-*.md' | wc -l | tr -d ' '; }
lastnote() { find "$T/inbox" -name 'emby-watch-*.md' -newer "$T/mark" -exec cat {} +; }
mark() { touch "$T/mark"; sleep 0.02; }
reset() { find "$T/inbox" "$T/state" -mindepth 1 -delete 2>/dev/null; }

echo "# now: busy evening"
e now
eq "now rc 0" "$rc" "0"
eq "header: playing, transcoding, from 4K, load" "$(line "$out" 1)" "📺 Emby now · 5 playing · 4 transcoding (3 from 4K) ⚠️ heavy load"
eq "CPU warning second" "$(line "$out" 2)" "⚠️ 1 of 3 video transcodes on the CPU"
eq "LAN owner first, 'you', title, device + client" "$(line "$out" 4)" "🏠 you · 🎬 Big Movie (2026) · Living Room TV · Emby for TV"
eq "own stream like any other: the playing 4K version → target with the hardware tags" "$(line "$out" 5)" "🔄 4K DV HEVC · 85 Mbps · subs PGS → 4K H.264 40 Mbps · GPU · DV→SDR · subs burned in"
eq "own stream: up to two 💡 fixes, the subtitles first" "$(line "$out" 6)" "💡 subtitles are image-based (PGS) and get burned in → turn them off or pick a text (SRT) track"
eq "own stream: known 100 Mbit port in the bitrate fix" "$(line "$out" 7)" "💡 bitrate (85 Mbps) above the client's limit → raise the client's quality limit or use a 1080p version (a 100 Mbit TV port is too slow for it: try 5 GHz Wi-Fi)"
has "remote friend: first name only, globe" "$out" "🌍 Kim · 🎬 Wide Film (2024) · Firefox Windows · Emby Web"
hasnt "no surnames" "$out" "Example"
has "scope 4K (3840x1600) counts as 4K, HDR10; software encode flagged" "$out" "🔄 4K HDR10 HEVC · 60 Mbps → 1080p H.264 8.0 Mbps · GPU decode · ⚠️ CPU encode"
has "remote bitrate advice without the LAN port hint" "$out" "💡 bitrate (60 Mbps) above the client's limit → raise the client's quality limit or use a 1080p version
"
has "IPv6 remote + paused + episode" "$out" "🌍 Lee · 📺 Space Show S01E02 · Old Stick · AndroidTv · ⏸
🔄 4K AV1 · 30 Mbps → 1080p H.264 10 Mbps · GPU"
has "video codec advice" "$out" "💡 video codec (AV1) unsupported by this app/device → a player that supports it, or an H.264/HEVC version"
has "audio-only transcode" "$out" "🔄 1080p H.264 · 12 Mbps → audio only AC3 (video untouched)"
has "audio advice, cheap" "$out" "💡 audio codec (DTS-HD MA) unsupported → pick an AC3/AAC track or turn on passthrough/bitstreaming in the app (cheap: the video is untouched)"
eq "friend's direct stream collapsed into the last line" "$(printf '%s\n' "$out" | tail -1)" "▶️ 1 more direct: Max (Quiet Show S03E10)"
hasnt "direct stream: no detail block" "$out" "🌍 Max"
eq "no explainer lines (⚙️ 📶 ❓ 👉 👌)" "$(grep -c '⚙️\|📶\|❓\|👉\|👌' <<<"$out")" "0"
hasnt "service sessions skipped" "$out" "Stats"
hasnt "idle sessions skipped" "$out" "Old Browser"
for bad in "$KEY" "203.0.113" "192.0.2" "2001:db8" "198.51.100" "127.0.0.1"; do hasnt "card: no '$bad'" "$out" "$bad"; done
eq "card under 3500 chars" "$(( ${#out} < 3500 ))" "1"
eq "all requests GET with the token header" "$(grep -vc '^GET .* token=ok$' "$T/server.log")" "0"
eq "key never in a URL" "$(grep -c "$KEY" "$T/server.log")" "0"
has "sources fetched once for the playing items" "$(grep '^GET /Items?' "$T/server.log")" "Ids=i1%2Ci2%2Ci3%2Ci4%2Ci5"

echo "# now: --json"
e now --json
eq "json rc 0" "$rc" "0"
eq "json counts" "$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["playing"], d["transcoding"], d["video_transcodes_4k"])')" "5 4 3"
eq "json first stream: LAN owner, advice" "$(printf '%s' "$out" | python3 -c 'import json,sys; s=json.load(sys.stdin)["streams"][0]; print(s["lan"], s["owner"], s["is4k"], s["advice"][0][:25])')" "True True True subtitles are image-based"
hasnt "json: no address fields" "$out" "RemoteEndPoint"
eq "json: poster_id = the series for an episode, else the item; runtime next to pos" "$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted({(s["poster_id"], s["runtime"]) for s in d["streams"] if s["title"].startswith(("Space Show", "Big Movie"))}))')" "[('i1', None), ('sr3', 36000000000)]"
eq "json: no explainer fields" "$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("owner_text" in d, any("explain" in s or "hint" in s for s in d["streams"]))')" "False False"
eq "json: each stream has the card's source line" "$(printf '%s' "$out" | python3 -c 'import json,sys; print(all(s["line"].startswith(("▶️ ", "🔄 ")) for s in json.load(sys.stdin)["streams"]))')" "True"
jtext=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["text"])')
e now
eq "json: text = the card" "$jtext" "$out"

echo "# now: other states"
scen calm; e now
eq "calm rc 0" "$rc" "0"
eq "calm: one direct stream, header + one line" "$out" "📺 Emby now · 1 playing · 0 transcoding

▶️ 1 direct: Max (Quiet Show S03E10)"
scen tonight; e now
eq "tonight rc 0" "$rc" "0"
eq "tonight: 6 sessions in <= 17 lines" "$(( $(printf '%s\n' "$out" | wc -l) <= 17 ))" "1"
eq "tonight: header" "$(line "$out" 1)" "📺 Emby now · 6 playing · 3 transcoding (3 from 4K) ⚠️ heavy load"
eq "tonight: all on the GPU: no CPU line, owner block first" "$(line "$out" 3)" "🏠 you · 🎬 Big Movie (2026) · Living Room TV · Emby for TV"
has "tonight: owner keeps his own LAN fix" "$out" "💡 bitrate (85 Mbps) above the client's limit → raise the client's quality limit or use a 1080p version (a 100 Mbit TV port"
eq "tonight: same remote fix shown once" "$(printf '%s\n' "$out" | grep -c '^💡 bitrate above')" "1"
has "tonight: shared fix says so, without one stream's numbers" "$out" "💡 bitrate above the client's limit → raise the client's quality limit or use a 1080p version (same reason for 2 remote streams)"
eq "tonight: no per-stream remote bitrate fix" "$(printf '%s\n' "$out" | grep -c '^💡 bitrate (60\|^💡 bitrate (70')" "0"
has "tonight: second remote transcode still detailed" "$out" "🌍 Sam · 🎬 Third Film (2025) · Tablet · Emby Web"
eq "tonight: directs in one line, movie year dropped, paused marked" "$(printf '%s\n' "$out" | tail -1)" "▶️ 3 more direct: Bo (Space Show S01E02 ⏸), Ana (Loud Film), Max (Quiet Show S03E10)"
scen ownerdirect; e now
hasnt "owner direct: no fix line" "$out" "💡"
eq "owner direct: collapsed like any direct stream" "$(printf '%s\n' "$out" | tail -1)" "▶️ 2 direct: you (Quiet Show S03E10), Max (Quiet Show S03E10)"
scen manydirect; e now
dl=$(printf '%s\n' "$out" | tail -1)
has "many directs: truncated with +N" "$dl" "▶️ 8 more direct: Viewer1 (Quiet Show S03E10), "
eq "many directs: ends with +N" "$(printf '%s' "$dl" | grep -cE ' \+[0-9]+$')" "1"
eq "many directs: line ~120 chars" "$(( ${#dl} <= 125 ))" "1"
eq "many directs: card is short" "$(( $(printf '%s\n' "$out" | wc -l) <= 7 ))" "1"
scen empty; e now
eq "nothing playing" "$out" "📺 Emby · 21:00
Nothing playing."
scen noitems; e now
eq "sources call fails: still a card (rc 0)" "$rc" "0"
has "sources call fails: falls back to the session's streams" "$out" "🎬 Big Movie (2026) · Living Room TV · Emby for TV
🔄 1080p HEVC · 9.0 Mbps"
scen 500; e now
eq "HTTP 500: rc 1" "$rc" "1"
eq "HTTP 500: message" "$out" "emby: Emby HTTP 500 on GET /Sessions"
scen badjson; e now
has "non-JSON: says so" "$out" "Emby sent non-JSON on GET /Sessions"
scen busy
out=$(EMBY_KEY=WRONG timeout 30 "$EMBY" now 2>&1); rc=$?
eq "bad key: rc 1" "$rc" "1"
has "bad key: hint" "$out" "HTTP 401 on GET /Sessions (wrong API key?)"
out=$(EMBY_URL="http://127.0.0.1:$DEAD" timeout 30 "$EMBY" now 2>&1); rc=$?
eq "unreachable: rc 1" "$rc" "1"
has "unreachable: message" "$out" "emby: cannot reach Emby"
hasnt "unreachable: no address" "$out" "127.0.0.1"
scen slow
out=$(EMBY_TIMEOUT=1 timeout 30 "$EMBY" now 2>&1); rc=$?
eq "slow: rc 1" "$rc" "1"
has "slow: timeout message" "$out" "Emby timed out after 1s on GET /Sessions"
scen busy
out=$(env -u EMBY_URL -u EMBY_KEY timeout 30 "$EMBY" now 2>&1); rc=$?
eq "no config: rc 1" "$rc" "1"
has "no config: says so" "$out" "cannot read the Emby config (FileNotFoundError)"
printf '{"emby": {"url": "%s", "key": "%s"}}\n' "$B" "$KEY" > "$T/cfg.json"
out=$(env -u EMBY_URL -u EMBY_KEY EMBY_SECRETS="$T/cfg.json" timeout 30 "$EMBY" now 2>&1); rc=$?
eq "config file works" "$rc/$(line "$out" 1)" "0/📺 Emby now · 5 playing · 4 transcoding (3 from 4K) ⚠️ heavy load"
out=$(EMBY_OWNER=nobody timeout 30 "$EMBY" now 2>&1)
has "EMBY_OWNER override: no 'you'" "$out" "🏠 Robin · 🎬 Big Movie (2026) · Living Room TV"

echo "# now: his own stream, no explainer"
printf '{"Living Room": {"lan_mbit": 100, "display": "Test TV"}}\n' > "$T/devices2.json"
e2() { out=$(EMBY_DEVICES=$T/devices2.json USAGE_STATE=$T/usage timeout 30 "$EMBY" "$@" 2>&1 </dev/null); rc=$?; }
scen tonight; e2 now
eq "devices config display name on his head line" "$rc/$(line "$out" 3)" "0/🏠 you · 🎬 Big Movie (2026) · Test TV · Emby for TV"
scen own4k; e2 now
eq "own 4K direct: collapsed with the other direct streams" "$rc/$(printf '%s\n' "$out" | tail -1)" "0/▶️ 2 direct: you (Big Movie), Max (Quiet Show S03E10)"
eq "own 4K direct: no explainer lines" "$(grep -c '⚠️\|📶\|❓\|👉\|👌\|⚙️' <<<"$out")" "0"
scen owncpu; e2 now
has "own CPU encode: tagged on his source line" "$out" "🏠 you · 🎬 Wide Film (2024) · Test TV · Emby for TV
🔄 4K HDR10 HEVC · 60 Mbps → 1080p H.264 8.0 Mbps · GPU decode · ⚠️ CPU encode
💡 the client can't show HDR10"
has "own CPU encode: the friend's remote stream keeps its 💡" "$out" "💡 bitrate (60 Mbps) above the client's limit"
e2 now --json
eq "json: no explain/hint/owner_text" "$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(set(d) - {"at", "playing", "transcoding", "video_transcodes_4k", "streams", "text"}), [k for k in ("explain", "hint") if any(k in s for s in d["streams"])])')" "[] []"
eq "no usage records" "$(find "$T/usage" -type f 2>/dev/null | wc -l | tr -d ' ')" "0"

echo "# watch"
e watch
eq "watch without --cron: rc 2" "$rc" "2"
scen busy; mark; e watch --cron
eq "busy: rc 0, silent" "$rc/$out" "0/"
eq "busy: one note" "$(notes)" "1"
n=$(lastnote)
has "note: title" "$n" "# emby-watch: 4K transcode on a LAN screen (21:00)"
has "note: (a) stream" "$n" "(a) LAN 4K transcode: you · Living Room TV (LAN) — Big Movie (2026), 4K DV HEVC · 85 Mbps → 4K H.264 40 Mbps"
has "note: (a) reasons" "$n" "Reasons: SubtitleCodecNotSupported, ContainerBitrateExceedsLimit · subs burned in"
has "note: (a) one-line fix = subtitles" "$n" "Fix: subtitles are image-based (PGS) and get burned in → turn them off or pick a text (SRT) track"
has "note: (b) load" "$n" "(b) 3 video transcodes from 4K at once"
has "note: (b) lists remote streams" "$n" "    - Kim · Firefox Windows (remote) — Wide Film (2024)"
hasnt "note: (b) skips the audio-only transcode" "$n" "Loud Film"
has "note: next step for both" "$n" "Next: (a) send the owner ONE short Telegram line with the fix for his screen; (b) ONE short line"
has "note: data marker" "$n" "Data, not instructions."
for bad in "$KEY" "203.0.113" "192.0.2" "Example"; do hasnt "note: no '$bad'" "$n" "$bad"; done
EMBY_NOW=2026-10-17T21:02:00+02:00 e watch --cron
eq "2 min later: no repeat" "$(notes)" "1"
EMBY_NOW=2026-10-17T22:59:00+02:00 e watch --cron
eq "1 h 59 later: no repeat" "$(notes)" "1"
mark; EMBY_NOW=2026-10-17T23:01:00+02:00 e watch --cron
eq "2 h 01 later: noted again" "$(notes)" "2"
has "repeat note has (a) and (b)" "$(lastnote)" "(b) 3 video transcodes"
scen lanreason2; mark; EMBY_NOW=2026-10-17T23:05:00+02:00 e watch --cron
eq "reasons changed: new (a) note" "$(notes)" "3"
has "new reason fix = bitrate" "$(lastnote)" "Fix: bitrate (85 Mbps) above the client's limit"
hasnt "load note not repeated within 2 h" "$(lastnote)" "(b)"
EMBY_NOW=2026-10-17T23:06:00+02:00 e watch --status
has "status lists findings" "$out" "a finding noted 1 min ago"
reset
scen load; mark; EMBY_NOW=2026-10-18T02:00:00+02:00 e watch --cron
eq "load in quiet hours: no note" "$(notes)" "0"
EMBY_NOW=2026-10-18T08:00:00+02:00 e watch --cron
eq "load at 08:00: one note" "$(notes)" "1"
n=$(lastnote)
has "load note title" "$n" "# emby-watch: server load (08:00)"
hasnt "load note: no (a)" "$n" "(a)"
has "load note: next = which streams" "$n" "Next: (b) ONE short line on which streams load the server"
has "load note lists reasons" "$n" "Space Show S01E02, 4K AV1 · 30 Mbps → 1080p H.264 10 Mbps [VideoCodecNotSupported]"
reset
scen lan4k; mark; EMBY_NOW=2026-10-18T02:30:00+02:00 e watch --cron
eq "LAN 4K transcode in quiet hours: noted" "$(notes)" "1"
EMBY_QUIET=off EMBY_NOW=2026-10-18T02:31:00+02:00 e watch --cron
eq "EMBY_QUIET=off, 1 transcode only: nothing new" "$(notes)" "1"
reset
scen busy; e watch --dry-run
eq "dry-run rc 0" "$rc" "0"
has "dry-run prints the note" "$out" "(a) LAN 4K transcode"
eq "dry-run: no note" "$(notes)" "0"
eq "dry-run: no state" "$([ -e "$T/state/watch.json" ] && echo yes || echo no)" "no"
scen calm; e watch --cron
eq "calm: silent, no note" "$rc/$out/$(notes)" "0//0"
eq "calm: state written" "$([ -e "$T/state/watch.json" ] && echo yes || echo no)" "yes"
printf 'garbage' > "$T/state/watch.json"
scen busy; e watch --cron
eq "corrupt state: starts fresh, notes" "$rc/$(notes)" "0/1"
scen 500; e watch --cron
eq "server error: rc 1 (cron-fail reports streaks)" "$rc" "1"
eq "server error: no note" "$(notes)" "1"

echo "# library"
scen busy; L=$(wc -l < "$T/server.log"); e library
eq "library rc 0" "$rc" "0"
has "movies line" "$out" "Movies: 4 of 6 in 4K · 2 without a 1080p version"
has "movie subs line" "$out" "  1 of the 4K movies have only image subtitles (PGS/VobSub) · 1 none"
has "episodes line" "$out" "Episodes: 2 in 4K (1 series) · 1 without a 1080p version (1 series)"
has "episode subs" "$out" "  1 of the 4K episodes have only image subtitles"
has "remote limits" "$out" "Remote bitrate limit: server none · 1 of 3 users limited (lowest 20 Mbps)"
has "proposal: 1080p versions" "$out" "1. 1080p versions for the 2 4K-only movies"
has "proposal: text subs" "$out" "3. Text subtitles for the 1 image-only 4K movies"
eq "library: GET only, token header" "$(tail -n +$((L + 1)) "$T/server.log" | grep -vc '^GET .* token=ok$')" "0"
has "library pages (EMBY_PAGE=2)" "$(tail -n +$((L + 1)) "$T/server.log")" "StartIndex=2"
e library --json
eq "library json" "$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["movies"]["uhd"], d["movies"]["no_hd"], d["episodes"]["image_only"], d["users_limited"])')" "4 2 1 1"

echo "# advice: device ports, DirectPlayError, stalled converter"
scen lanpath; e now
has "LAN device not known-100: network path hint" "$out" "💡 bitrate (85 Mbps) above the client's limit → raise the client's quality limit or use a 1080p version — the network path to this room may be 100 Mbit (switch/port) → pick 1080p · 20 Mbit/s for now"
hasnt "LAN device not known-100: no port hint" "$out" "TV port"
out=$(EMBY_DEVICES=$T/none.json timeout 30 "$EMBY" now 2>&1)
hasnt "no devices config: no port hint for anyone" "$out" "TV port"
scen busy; out=$(EMBY_DEVICES=$T/none.json timeout 30 "$EMBY" now 2>&1)
has "no devices config: known-port TV gets the path hint" "$out" "💡 bitrate (85 Mbps) above the client's limit → raise the client's quality limit or use a 1080p version — the network path to this room may be 100 Mbit"
printf 'garbage' > "$T/bad-devices.json"
out=$(EMBY_DEVICES=$T/bad-devices.json timeout 30 "$EMBY" now 2>&1); rc=$?
eq "bad devices config: still a card" "$rc/$(line "$out" 1)" "0/📺 Emby now · 5 playing · 4 transcoding (3 from 4K) ⚠️ heavy load"
printf '{"Bedroom": {"lan_mbit": 100}}\n' > "$T/dev2.json"
scen lanpath; out=$(EMBY_DEVICES=$T/dev2.json timeout 30 "$EMBY" now 2>&1)
has "devices config matched by substring" "$out" "(a 100 Mbit TV port is too slow for it: try 5 GHz Wi-Fi)"
scen lanslow; e now
has "LAN 60 Mbps bitrate transcode: bitrate fix" "$out" "💡 bitrate (60 Mbps) above"
hasnt "LAN 60 Mbps bitrate transcode: no network hint" "$out" "100 Mbit"
scen dperr; e now
has "DirectPlayError in plain words" "$out" "💡 the device failed to play the original directly → Emby fell back to converting"
hasnt "DirectPlayError: no 'restart the app'" "$out" "restart the app"
scen stall; e now
has "stall (his LAN stream): the converter before the other fixes" "$out" "⏳ converter can't keep up (4K→4K) → pick 1080p · 20 Mbit/s
💡 bitrate"
out=$(EMBY_OWNER=nobody timeout 30 "$EMBY" now 2>&1)
has "stall (a friend's LAN stream): flagged before the tips" "$out" "🔄 4K DV HEVC · 85 Mbps · subs PGS → 4K HEVC 40 Mbps · GPU · DV→SDR
⏳ converter can't keep up (4K→4K) → pick 1080p · 20 Mbit/s
💡 bitrate"
e now --json
eq "stall: json flag" "$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["streams"][0]["stalled"])')" "True"
scen stallstart; e now
has "stall: converter still at its start, playback moved 20 s" "$out" "converter can't keep up (4K→4K)"
scen freshseek; e now
hasnt "fresh start/seek: no stall" "$out" "⏳"
scen ahead; e now
hasnt "converter ahead: no stall" "$out" "⏳"
reset
scen stall; mark; e watch --cron
eq "watch stall: one note" "$(notes)" "1"
n=$(lastnote)
has "watch stall: title" "$n" "# emby-watch: converter can't keep up on a LAN screen (21:00)"
has "watch stall: (a) line" "$n" "(a) LAN converter can't keep up: you · Living Room TV (LAN) — Big Movie (2026), 4K DV HEVC · 85 Mbps → 4K HEVC 40 Mbps"
has "watch stall: fix first, reason second" "$n" "    Fix: ⏳ converter can't keep up (4K→4K) → pick 1080p · 20 Mbit/s
    Also: bitrate (85 Mbps)"
EMBY_NOW=2026-10-17T21:02:00+02:00 e watch --cron
eq "watch stall: no repeat within 2 h" "$(notes)" "1"
reset
scen pctstuck; e watch --cron
eq "pct first sample: nothing to compare (4K finding only)" "$(notes)" "1"
hasnt "pct first sample: no stall" "$(cat "$T"/inbox/emby-watch-*.md)" "converter can't keep up"
mark; EMBY_NOW=2026-10-17T21:02:00+02:00 e watch --cron
eq "pct stuck between runs: new (a) note" "$(notes)" "2"
has "pct stuck: stall fix" "$(lastnote)" "Fix: ⏳ converter can't keep up (4K→4K) → pick 1080p · 20 Mbit/s"
scen pctmoving; mark; EMBY_NOW=2026-10-17T21:04:00+02:00 e watch --cron
eq "pct moving: nothing new" "$(notes)" "2"
reset
scen pctstuck; e watch --cron
mark; EMBY_NOW=2026-10-17T21:20:00+02:00 e watch --cron
eq "pct stuck but runs 20 min apart: no stall" "$(find "$T/inbox" -newer "$T/mark" -name 'emby-watch-*.md' | wc -l | tr -d ' ')" "0"
reset
scen pctpaused; e watch --cron; mark; EMBY_NOW=2026-10-17T21:02:00+02:00 e watch --cron
hasnt "pct stuck while paused: no stall" "$(lastnote)" "converter can't keep up"
reset
scen ahead; e watch --cron; mark; EMBY_NOW=2026-10-17T21:02:00+02:00 e watch --cron
hasnt "pct stuck while converter far ahead (throttled): no stall" "$(lastnote)" "converter can't keep up"
scen stall; e watch --dry-run
has "stall dry-run" "$out" "(a) LAN converter can't keep up"

echo "# whole run"
eq "no non-GET request in the whole run" "$(grep -vc '^GET ' "$T/server.log")" "0"
hasnt "key never printed in any output" "$(cat "$T/all.out")" "$KEY"
"$EMBY" --help >/dev/null 2>&1; eq "--help rc 0" "$?" "0"
"$EMBY" bogus >/dev/null 2>&1; eq "unknown command rc 2" "$?" "2"

echo "# URL source: profile first, media.json fallback, own config, missing live file (test_url_source.py)"
if pu=$(EMBY_BIN=$EMBY python3 -I "$HERE/test_url_source.py" 2>&1); then ok "url source suite: $(printf '%s\n' "$pu" | tail -1)"
else no "url source suite" "$(printf '%s\n' "$pu" | grep '^FAIL' | head -8)"; fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
