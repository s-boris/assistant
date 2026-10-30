#!/bin/bash
# Tests for ~/bin/tts, ~/bin/tg-voice and ~/bin/driving (voice answers while driving).
# tts: spoken-text normalisation, real Piper synthesis (EN + DE) to valid OGG/Opus mono 48 kHz, the length cap,
# cache pruning and bad input. tg-voice: a mock Bot API on 127.0.0.1 with a fake token (nothing reaches
# Telegram). driving: a stub vault command. ~15 s.  TTS_TEST_BIN = folder with the scripts (default ~/bin).
set -u
B=${TTS_TEST_BIN:-$HOME/bin}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/tts-tests.XXXXXX")
MOCK=
cleanup() { [ -z "$MOCK" ] || kill "$MOCK" 2>/dev/null; find "$T" -mindepth 1 -delete; rmdir "$T"; }
trap cleanup EXIT
pass=0 fail=0
ok() { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "FAIL: $*"; }
check() { local name=$1; shift; if "$@"; then ok; else bad "$name"; fi; }
eq() { [ "$2" = "$3" ] && ok || bad "$1: got [$2] want [$3]"; }
no_token() { ! grep -q FAKE_test "$@"; }  # files, or stdin with -

probe() {  # codec|format|rate|channels|duration
  ffprobe -v error -show_entries stream=codec_name,sample_rate,channels:format=format_name,duration \
    -of json "$1" | jq -r '[.streams[0].codec_name, .format.format_name, .streams[0].sample_rate,
                            .streams[0].channels, .format.duration] | map(tostring) | join("|")'
}
valid_voice() {  # FILE MIN_S MAX_S
  local p; p=$(probe "$1") || return 1
  IFS='|' read -r c f r ch d <<<"$p"
  [ "$c" = opus ] && [ "$f" = ogg ] && [ "$r" = 48000 ] && [ "$ch" = 1 ] &&
    awk -v d="$d" -v a="$2" -v b="$3" 'BEGIN { exit !(d >= a && d <= b) }'
}

# --- tts: setup check and spoken text ---
check "tts --check" "$B/tts" --check >/dev/null
pt() { "$B/tts" --print-text "$@"; }
eq "markdown+emoji+time" "$(pt '**Train** leaves at 14:30 🚆 — platform 7.')" "Train leaves at two thirty p m, platform 7."
eq "times" "$(pt 'At 09:00, 12:00, 00:00, 09:05 and 23:45')" "At nine a m, noon, midnight, nine oh five a m and eleven forty-five p m."
eq "date + sentence dot" "$(pt 'Wedding on 24.10. Then home')" "Wedding on October 24th. Then home."
eq "bare dates" "$(pt 'Bern 30.10 by train, due by 1.11')" "Bern October 30th by train, due by November 1st."
eq "versions stay" "$(pt 'Emby 4.10 and Unraid 7.3.3, Ubuntu 24.04, updated to 20.11.')" "Emby 4.10 and Unraid 7.3.3, Ubuntu 24.04, updated to 20.11."
eq "date range" "$(pt 'Trip 20.11–6.12 and 20.11 to 6.12')" "Trip November 20th to December 6th and November 20th to December 6th."
eq "iso date" "$(pt 'Due 2026-10-24')" "Due October 24th, 2026."
eq "money" "$(pt "CHF 1'250.– or 12.50 CHF, €30, 1 CHF")" "1250 francs or 12 francs 50, 30 euros, 1 franc."
eq "units" "$(pt '86 % full, ~40 min, 1 km, 2.5 Gbit, 18 TB, 3–5 days, 12 °C')" "86 percent full, about 40 minutes, 1 kilometre, 2.5 gigabit, 18 terabytes, 3 to 5 days, 12 degrees."
eq "links+code+table" "$(pt $'- [Seerr](https://x.y/z) is ok\n| Day | Temp |\n|---|---|\n```\nx=1\n```\nSee https://example.com 👍')" "Seerr is ok. Day, Temp. Code in the text. See link."
eq "html+arrow+number" "$(pt '<b>Lyon</b> → Paris, filed as #42 & done')" "Lyon to Paris, filed as number 42 and done."
eq "de" "$("$B/tts" --lang de --print-text 'Zug um 14:30 🚆, Hochzeit am 24.10., CHF 250.–, 86 %, z.B. ca. 40 min')" \
  "Zug um 14 Uhr 30, Hochzeit am 24. Oktober, 250 Franken, 86 Prozent, zum Beispiel circa 40 Minuten."
eq "de Fr. date" "$("$B/tts" --lang de --print-text 'Fr. 24.10. frei')" "Fr. 24. Oktober frei."

# --- tts: synthesis ---
j=$("$B/tts" --json --out "$T/en.ogg" "Your train leaves at 14:30 from platform 7. The drive takes about 40 min.")
check "en exit+json" [ "$(jq -r .truncated <<<"$j")" = false ]
check "en valid ogg/opus mono 48k" valid_voice "$T/en.ogg" 2 20
"$B/tts" --lang de --out "$T/de.ogg" "Dein Zug fährt um 14:30 ab Gleis 7." >/dev/null
check "de valid ogg/opus mono 48k" valid_voice "$T/de.ogg" 1 15
printf 'From stdin, hello.' | "$B/tts" --out "$T/stdin.ogg" - >/dev/null
check "stdin input" valid_voice "$T/stdin.ogg" 0.5 10
LONG=$(for i in $(seq 1 30); do printf 'Sentence number %s is here to make this answer long enough for the cap. ' "$i"; done)
j=$("$B/tts" --json --out "$T/long.ogg" "$LONG")
check "60 s cap: truncated" [ "$(jq -r .truncated <<<"$j")" = true ]
check "60 s cap: 50-60 s" valid_voice "$T/long.ogg" 50 60.1
j=$("$B/tts" --json --max-seconds 10 --out "$T/cap10.ogg" "$LONG")
check "10 s cap" valid_voice "$T/cap10.ogg" 5 10.1
"$B/tts" --max-seconds 5 --out "$T/cut.ogg" "$(printf 'word %.0s' $(seq 1 80))" >/dev/null
check "one long sentence hard-cut" valid_voice "$T/cut.ogg" 4 5.1
mkdir -p "$T/cache"; touch -d '2 days ago' "$T/cache/tts-old.ogg"; touch "$T/cache/tts-new.ogg"
f=$(TTS_CACHE="$T/cache" "$B/tts" "Cached.")
check "default out in cache" [ "$(dirname "$f")" = "$T/cache" ]
check "cache file valid" valid_voice "$f" 0.3 5
check "old cache file pruned" [ ! -e "$T/cache/tts-old.ogg" ]
check "fresh cache file kept" [ -e "$T/cache/tts-new.ogg" ]
# onnxruntime's telemetry stays off: on, every process that imports it writes a device id and an event queue under
# ~/.cache/Microsoft and uploads them over HTTPS once it lives a few seconds. --check and a cold synthesis with an
# empty HOME leave that HOME empty.
mkdir "$T/home"
ort() { env -u XDG_CACHE_HOME HOME="$T/home" TTS_SERVER=0 TTS_CACHE="$T/cache" TTS_VENV="${TTS_VENV:-$HOME/opt/piper-venv}" \
  TTS_VOICES="${TTS_VOICES:-$HOME/opt/piper-voices}" "$B/tts" "$@"; }
ort --check >/dev/null && ort --out "$T/ort.ogg" "No telemetry." >/dev/null
check "onnxruntime telemetry off: synth ok" valid_voice "$T/ort.ogg" 0.3 5
eq "onnxruntime telemetry off: nothing under HOME" "$(find "$T/home" -mindepth 1 | head -3)" ""
"$B/tts" --out "$T/x.ogg" '🚆✅ **' 2>/dev/null; eq "emoji-only = exit 2" $? 2
"$B/tts" --lang fr x 2>/dev/null; eq "bad lang = exit 2" $? 2
"$B/tts" --max-seconds 1 x 2>/dev/null; eq "bad cap = exit 2" $? 2
"$B/tts" --voice '../x' x 2>/dev/null; eq "bad voice name = exit 2" $? 2

# --- tg-voice against the mock Bot API (its sends go through python urllib via core.http: no curl needed) ---
TOKEN='123456:FAKE_test-token_x'
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" >"$T/env"
printf '{"allowFrom": ["777"]}\n' >"$T/access.json"
echo ok >"$T/mode"
python3 -I "$HERE/mock_bot.py" "$T/port" "$T/log" "$T/mode" &
MOCK=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
export TG_VOICE_ENV="$T/env" TG_VOICE_ACCESS="$T/access.json" TG_VOICE_API="http://127.0.0.1:$(cat "$T/port")"
size=$(stat -c %s "$T/en.ogg")
out=$("$B/tg-voice" "$T/en.ogg" "Train at 14:30" 2>"$T/err"); rc=$?
eq "send: exit 0" "$rc" 0
eq "send: prints message_id" "$out" 4242
last=$(tail -n 1 "$T/log")
eq "send: method + token path" "$(jq -r .path <<<"$last")" "/bot$TOKEN/sendVoice"
eq "send: owner chat" "$(jq -r .fields.chat_id <<<"$last")" 777
eq "send: caption" "$(jq -r .fields.caption <<<"$last")" "Train at 14:30"
eq "send: duration" "$(jq -r .fields.duration <<<"$last")" "$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$T/en.ogg" | awk '{printf "%d", $1 + 0.5}')"
eq "send: voice part size" "$(jq -r .files.voice.size <<<"$last")" "$size"
eq "send: voice part type" "$(jq -r .files.voice.type <<<"$last")" audio/ogg
check "send: token not in output" no_token - <<<"$out$(cat "$T/err")"
"$B/tg-voice" "$T/en.ogg" --silent --reply-to 99 >/dev/null
last=$(tail -n 1 "$T/log")
eq "send: silent" "$(jq -r .fields.disable_notification <<<"$last")" true
eq "send: reply_to" "$(jq -r '.fields.reply_parameters | fromjson | .message_id' <<<"$last")" 99
eq "send: no caption field" "$(jq -r '.fields.caption // "none"' <<<"$last")" none
"$B/tg-voice" "$T/en.ogg" "$(printf 'x%.0s' $(seq 1 1500))" >/dev/null
eq "send: long caption cut" "$(tail -n 1 "$T/log" | jq -r '.fields.caption | length')" 1000
echo forbid >"$T/mode"; n=$(wc -l <"$T/log")
out=$("$B/tg-voice" "$T/en.ogg" 2>"$T/err"); rc=$?
eq "forbidden: exit 0 via sendAudio" "$rc" 0
eq "forbidden: two calls" "$(($(wc -l <"$T/log") - n))" 2
eq "forbidden: second is sendAudio" "$(tail -n 1 "$T/log" | jq -r '.path | split("/") | last')" sendAudio
eq "forbidden: audio part" "$(tail -n 1 "$T/log" | jq -r '.files.audio.size')" "$size"
check "forbidden: note on stderr" grep -q 'audio file' "$T/err"
echo err >"$T/mode"
out=$("$B/tg-voice" "$T/en.ogg" 2>"$T/err"); rc=$?
eq "telegram error: exit 1" "$rc" 1
check "telegram error: reason shown" grep -q 'chat not found' "$T/err"
check "telegram error: token not in output" no_token - <<<"$out$(cat "$T/err")"
echo ok >"$T/mode"; n=$(wc -l <"$T/log")
"$B/tg-voice" 2>/dev/null; eq "no args = exit 2" $? 2
"$B/tg-voice" "$T/missing.ogg" 2>/dev/null; eq "missing file = exit 2" $? 2
ffmpeg -v error -f lavfi -i 'sine=frequency=440:duration=1' "$T/tone.wav"
"$B/tg-voice" "$T/tone.wav" 2>/dev/null; eq "wav = exit 2" $? 2
"$B/tg-voice" "$T/en.ogg" --reply-to abc 2>/dev/null; eq "bad reply-to = exit 2" $? 2
eq "bad input sends nothing" "$(wc -l <"$T/log")" "$n"
TG_VOICE_API="http://127.0.0.1:9" "$B/tg-voice" "$T/en.ogg" >"$T/out" 2>"$T/err"; rc=$?
eq "no server: exit 1" "$rc" 1
check "no server: token not in output" no_token "$T/out" "$T/err"

# --- driving with a stub vault ---
stub() { printf '#!/bin/sh\n%s\n' "$1" >"$T/vault"; chmod +x "$T/vault"; }
dv() { DRIVING_VAULT="$T/vault" "$B/driving" "$@"; }
stub 'echo "{\"drive\":{\"active\":true,\"since\":\"x\",\"minutes\":12},\"age_min\":2}"'
dv; eq "driving: active = 0" $? 0
eq "driving -v" "$(dv -v)" "driving 12 min"
stub 'echo "{\"drive\":{\"active\":false,\"since\":null,\"minutes\":null},\"age_min\":2}"'
dv; eq "driving: inactive = 1" $? 1
eq "driving -v inactive" "$(dv -v)" "not driving"
stub 'echo "{\"drive\":{\"active\":true,\"minutes\":5},\"age_min\":120}"'
dv; eq "driving: stale feed = 1" $? 1
stub 'echo "{\"drive\":{\"active\":true,\"minutes\":5},\"age_min\":60}"'
eq "driving: 60 min old still counts (core.drive)" "$(dv -v)" "driving 5 min"
stub 'echo "{\"drive\":{\"active\":true,\"minutes\":5},\"age_min\":61}"'
eq "driving: 61 min old = unknown (core.drive)" "$(dv -v; echo $?)" "unknown
1"
stub 'echo "{\"drive\":null,\"age_min\":1}"'
dv; eq "driving: no sensor = 1" $? 1
stub 'echo "{\"drive\":true}"'
dv; eq "driving: boolean form = 0" $? 0
stub 'echo garbage'
dv; eq "driving: garbage = 1" $? 1
stub 'exit 3'
eq "driving: vault down = 1, unknown" "$(dv -v; echo $?)" "unknown
1"

echo "tts-tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
