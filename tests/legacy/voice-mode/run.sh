#!/bin/bash
# Tests for ~/bin/voice-say (language pick, 60 s split,
# sends via tg-voice to a mock Bot API with a fake token: nothing reaches Telegram; counts-only log) and the warm
# Piper server in ~/bin/tts (start/status/stop, warm use, cold fallback, idle exit; real synthesis). ~15 s.
# VOICE_TEST_BIN = folder with the scripts (default ~/bin). (The retired /voice toggle voice-mode is in ~/attic/2026-10-20.)
set -u
B=${VOICE_TEST_BIN:-$HOME/bin}
MOCKBOT=$(cd "$(dirname "$0")/.." && pwd)/tts/mock_bot.py  # tests/legacy/tts (was ~/opt/tts-tests)
[ -f "$MOCKBOT" ] || MOCKBOT=$HOME/opt/tts-tests/mock_bot.py
T=$(mktemp -d /tmp/vmt.XXXXXX)
MOCK=
cleanup() {
  TTS_CACHE="$T/c" "$B/tts" --server stop >/dev/null 2>&1
  [ -z "$MOCK" ] || kill "$MOCK" 2>/dev/null
  find "$T" -mindepth 1 -delete; rmdir "$T"
}
trap cleanup EXIT
pass=0 fail=0
ok() { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "FAIL: $*"; }
eq() { [ "$2" = "$3" ] && ok || bad "$1: got [$2] want [$3]"; }
has() { [[ "$2" == *"$3"* ]] && ok || bad "$1: [$2] lacks [$3]"; }
hasnt() { [[ "$2" == *"$3"* ]] && bad "$1: [$2] has [$3]" || ok; }
export TZ=UTC

# --- voice-say against a fake tts and the mock Bot API ---
ffmpeg -hide_banner -loglevel error -f lavfi -i anullsrc=r=48000:cl=mono -t 1 -c:a libopus -b:a 32k -f ogg "$T/one.ogg"
cat >"$T/fake-tts" <<EOF
#!/bin/bash
# fake tts: logs args + stdin text, "speaks" 16 chars/s, truncates past FAKE_MAX chars
if [ "\$1" = --server ]; then echo "server \$*" >>"$T/tts.log"; exit 0; fi
[ -n "\${FAKE_TTS_FAIL:-}" ] && { echo 'tts: piper failed: boom' >&2; exit 1; }
txt=\$(cat); n=\$(date +%s%N); cp "$T/one.ogg" "$T/note-\$n.ogg"
printf '%s\t%s\n' "\$*" "\$txt" >>"$T/tts.log"
len=\${#txt}; max=\${FAKE_MAX:-100000}
tr=false; [ "\$len" -gt "\$max" ] && tr=true
printf '{"file": "%s", "seconds": %s, "truncated": %s, "warm": %s}\n' "$T/note-\$n.ogg" "\$((len / 16))" "\$tr" "\${FAKE_WARM:-true}"
EOF
chmod +x "$T/fake-tts"
TOKEN='123456:FAKE_test-token_x'
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" >"$T/env"
printf '{"allowFrom": ["777"]}\n' >"$T/access.json"
echo ok >"$T/mode"
python3 -I "$MOCKBOT" "$T/port" "$T/botlog" "$T/mode" &
MOCK=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
export TG_VOICE_ENV="$T/env" TG_VOICE_ACCESS="$T/access.json" TG_VOICE_API="http://127.0.0.1:$(cat "$T/port")"
export VOICE_SAY_TTS="$T/fake-tts" VOICE_SAY_TG_VOICE="$B/tg-voice" VOICE_SAY_LOG="$T/lat.jsonl" TTS_CACHE="$T/c"
mkdir -p "$T/c"; : >"$T/c/piper.sock"   # a socket file = "server there": no warm-up spawns unless tested
vs() { "$B/voice-say" "$@"; }
syn() { grep -v '^server' "$T/tts.log"; }   # the synthesis calls (warm-up spawns log "server …" asynchronously)
srv() { grep -c '^server' "$T/tts.log"; }
srv_wait() { local want=$1 i; for i in $(seq 30); do [ "$(srv)" -ge "$want" ] && break; sleep 0.1; done; srv; }
sends() { [ -f "$T/botlog" ] && grep -c sendVoice "$T/botlog" || echo 0; }
langs() { syn | tail -n 1 | cut -f1 | grep -o -- '--lang [a-z]*' | tr '\n' ' '; }
out=$(vs "When does the next train leave? In ten minutes." --caption "Next train in 10 min" --reply-to 55); rc=$?
eq "say: exit 0" "$rc" 0
eq "say: prints message_id" "$out" 4242
eq "say: auto lang en" "$(langs)" "--lang en "
has "say: 60 s cap passed to tts" "$(tail -n 1 "$T/tts.log")" "--max-seconds 60"
last=$(tail -n 1 "$T/botlog")
eq "say: sendVoice" "$(jq -r .path <<<"$last")" "/bot$TOKEN/sendVoice"
eq "say: caption = the one line" "$(jq -r .fields.caption <<<"$last")" "Next train in 10 min"
has "say: replies to his note" "$(jq -r .fields.reply_parameters <<<"$last")" '"message_id":55'
rm -f "$T/c/piper.sock"; n=$(srv); vs "Hi." >/dev/null; eq "say: warm-up when no server socket" "$(srv_wait $((n + 1)))" "$((n + 1))"
has "say: warm-up = server start --no-wait" "$(grep '^server' "$T/tts.log" | tail -n 1)" "server --server start --no-wait"
: >"$T/c/piper.sock"
vs "Wann fährt der nächste Zug? In zehn Minuten, auf Gleis drei." >/dev/null; eq "say: auto lang de" "$(langs)" "--lang de "
vs "Ok, ich schaue nach dem Train und sage dir gleich Bescheid." >/dev/null; eq "say: mixed, mostly de" "$(langs)" "--lang de "
vs "Okay, I'll check the Zug times and tell you." >/dev/null; eq "say: mixed, mostly en" "$(langs)" "--lang en "
vs --lang de "The cat is fine." >/dev/null; eq "say: --lang forces" "$(langs)" "--lang de "
vs "Es ist 10:30." >/dev/null; eq "say: umlaut-free short de" "$(langs)" "--lang de "
vs "Server ok." >/dev/null; eq "say: tie = en" "$(langs)" "--lang en "
# long text: 2 notes, sentence boundary, caption + reply only on the first
long=$(for i in $(seq 1 14); do printf 'This is sentence number %02d of a long spoken answer about the server. ' "$i"; done)
b=$(sends); out=$(vs "$long" --caption "Long one" --reply-to 7); rc=$?
eq "long: exit 0" "$rc" 0
eq "long: two notes sent" "$(( $(sends) - b ))" 2
eq "long: two message ids" "$(wc -l <<<"$out")" 2
p1=$(syn | tail -n 2 | head -n 1 | cut -f2); p2=$(syn | tail -n 1 | cut -f2)
eq "long: part 1 ends at a sentence" "${p1: -1}" "."
eq "long: part 2 starts a sentence" "${p2:0:4}" "This"
eq "long: nothing lost" "$p1 $p2" "${long% }"
eq "long: caption only on note 1" "$(tail -n 2 "$T/botlog" | jq -r '.fields.caption // "-"' | tr '\n' ' ')" "Long one - "
eq "long: reply only on note 1" "$(tail -n 2 "$T/botlog" | jq -r '.fields.reply_parameters // "-"' | cut -c1-15 | tr '\n' ' ')" '{"message_id":7 - '
short=$(for i in $(seq 1 6); do printf 'Sentence %d is here. ' "$i"; done)
n=$(syn | wc -l); FAKE_MAX=60 vs "$short" >/dev/null
eq "cut short note: re-split into 2 (3 tts calls)" "$(( $(syn | wc -l) - n ))" 3
eq "cut short note: 2 sends" "$(tail -n 2 "$T/botlog" | grep -c sendVoice)" 2
# no warm server: warm-up again after a cold answer, unless VOICE_SAY_WARM=0
n=$(srv); FAKE_WARM=false vs "Hi." >/dev/null; eq "cold answer with a stale socket: warm-up" "$(srv_wait $((n + 1)))" "$((n + 1))"
n=$(srv); vs "Hi." >/dev/null; sleep 0.3; eq "warm answer: no warm-up" "$(srv)" "$n"
rm -f "$T/c/piper.sock"; n=$(srv)
VOICE_SAY_WARM=0 FAKE_WARM=false vs "Hi." >/dev/null; sleep 0.3; eq "VOICE_SAY_WARM=0: no warm-up" "$(srv)" "$n"
: >"$T/c/piper.sock"
# failures, dry run, json, log
b=$(sends); FAKE_TTS_FAIL=1 vs "Hello there." 2>"$T/err"; rc=$?
eq "tts fails: exit 1" "$rc" 1
eq "tts fails: nothing sent" "$(sends)" "$b"
has "tts fails: says why" "$(cat "$T/err")" "tts failed"
echo err >"$T/mode"; vs "Hello there." >/dev/null 2>"$T/err"; rc=$?; echo ok >"$T/mode"
eq "send fails: tg-voice's exit" "$rc" 1
b=$(sends); out=$(vs --dry-run "Hello there."); eq "dry run: nothing sent" "$(sends)" "$b"
has "dry run: prints the file" "$out" "$T/note-"
j=$(vs --json "Hello there, the answer is yes.")
eq "json: shape" "$(jq -c '[.lang, (.notes | length), .notes[0].message_id, (.tts_ms | type), .warm]' <<<"$j")" '["en",1,"4242","number",true]'
vs "" 2>/dev/null; eq "empty text: exit 2" "$?" 2
vs --reply-to abc "Hi." 2>/dev/null; eq "bad --reply-to: exit 2" "$?" 2
eq "stdin text" "$(echo "From stdin." | vs -)" 4242
hasnt "log has no text" "$(cat "$T/lat.jsonl")" "sentence"
hasnt "log has no caption" "$(cat "$T/lat.jsonl")" "Long one"
eq "log: one line per call (keys)" "$(tail -n 1 "$T/lat.jsonl" | jq -c 'keys')" '["audio_s","chars","lang","notes","ok","send_ms","ts","tts_ms","warm"]'
has "stats" "$(vs --stats)" "answers ("
! grep -q FAKE_test "$T/lat.jsonl" "$T/err" && ok || bad "token leaked"

# --- the warm Piper server (real synthesis; short socket path) ---
export VOICE_SAY_TTS="$B/tts"
export TTS_CACHE="$T/w"
"$B/tts" --server status >/dev/null; eq "server: cold at first (exit 1)" "$?" 1
"$B/tts" --server start; eq "server: start --wait exit 0" "$?" 0
"$B/tts" --server status >/dev/null; eq "server: status warm" "$?" 0
eq "server: socket owner-only" "$(stat -c %a "$T/w/piper.sock")" 700
"$B/tts" --server start --no-wait; eq "server: second start is a no-op" "$(pgrep -fc "$(basename "$T")/w/[p]iper.lock")" 1
s=$EPOCHREALTIME; j=$("$B/tts" --json --out "$T/w1.ogg" "The next train leaves at 18:05 from platform 3."); e=$EPOCHREALTIME
eq "server: warm synthesis used" "$(jq .warm <<<"$j")" true
eq "server: warm call < 0.8 s" "$(python3 -c "print($e - $s < 0.8)")" True
eq "server: valid voice note" "$(ffprobe -v error -show_entries stream=codec_name,sample_rate,channels -of csv=p=0 "$T/w1.ogg")" "opus,48000,1"
j=$("$B/tts" --json --lang de --out "$T/w2.ogg" "Der Zug fährt um 18:05."); eq "server: de warm" "$(jq -c '[.warm, .voice]' <<<"$j")" '[true,"de_DE-thorsten-medium"]'
L=$(for i in $(seq 1 30); do printf 'This sentence keeps going so the note gets long. '; done)
j=$("$B/tts" --json --max-seconds 10 --out "$T/w3.ogg" "$L"); eq "server: cap still applies" "$(jq -c '[.warm, .truncated, .seconds <= 10]' <<<"$j")" '[true,true,true]'
s=$EPOCHREALTIME; out=$(VOICE_SAY_LOG="$T/lat2.jsonl" "$B/voice-say" "Sure, the lights are off now. Anything else?"); e=$EPOCHREALTIME
eq "voice-say real tts + mock send" "$out" 4242
eq "voice-say end to end < 1.5 s" "$(python3 -c "print($e - $s < 1.5)")" True
echo "  (voice-say warm end to end $(python3 -c "print(round($e - $s, 2))") s; $(jq -c '{tts_ms, send_ms}' <"$T/lat2.jsonl"))"
TTS_SERVER=0 "$B/tts" --json --out "$T/w4.ogg" "Cold." | jq -e '.warm == false' >/dev/null && ok || bad "TTS_SERVER=0 forces cold"
"$B/tts" --server stop >/dev/null; sleep 0.3
[ -e "$T/w/piper.sock" ] && bad "server: socket left after stop" || ok
"$B/tts" --server status >/dev/null; eq "server: cold after stop" "$?" 1
j=$("$B/tts" --json --out "$T/w5.ogg" "Fallback works."); eq "server gone: cold fallback" "$(jq .warm <<<"$j")" false
: >"$T/w/piper.sock"; j=$("$B/tts" --json --out "$T/w6.ogg" "Stale socket."); eq "stale socket: cold fallback" "$(jq .warm <<<"$j")" false
TTS_SERVER_MIN_IDLE_S=1 "$B/tts" --server start --idle 0.02 >/dev/null; eq "idle server: starts over a stale socket" "$?" 0
for _ in $(seq 40); do [ -e "$T/w/piper.sock" ] || break; sleep 0.25; done
[ -e "$T/w/piper.sock" ] && bad "idle server: still there after 10 s" || ok

echo "voice-mode-tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
