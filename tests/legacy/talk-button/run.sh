#!/bin/bash
# Tests for ~/bin/talk-button (or $1): a fake voice-relay, an isolated HOME; never rings anyone.
set -u
BIN="${1:-${TALK_BUTTON_BIN:-$HOME/bin/talk-button}}"
T="$(mktemp -d)"
trap 'find "${T:?}" -mindepth 1 -delete; rmdir "${T:?}"' EXIT
export HOME="$T/home" TALK_STATE_DIR="$T/state" TALK_RELAY="$T/relay" FAKE_LOG="$T/relay.log"
mkdir -p "$HOME"
cat > "$T/relay" <<'R'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_LOG"
[ -n "${FAKE_SLEEP:-}" ] && sleep "$FAKE_SLEEP"
case "${FAKE_STATUS:-answered}" in
  answered) echo '{"status": "answered", "ring_s": 3.1}'; exit 0 ;;
  declined) echo '{"status": "declined", "ring_s": 2}'; exit 3 ;;
  no_answer) echo '{"status": "no_answer", "ring_s": 45}'; exit 3 ;;
  in_call) echo '{"status": "in_call", "detail": "a call is already on"}'; exit 4 ;;
  rate_limited) echo '{"status": "rate_limited", "detail": "max 3 outbound calls per hour"}'; exit 4 ;;
  unavailable) echo '{"status": "unavailable", "detail": "call bridge off"}'; exit 5 ;;
  provider) echo '{"status": "failed", "detail": "voice provider did not start (x)"}'; exit 5 ;;
  failed) echo '{"status": "failed", "detail": "TimeoutError"}'; exit 5 ;;
  down) echo 'voice-relay not reachable: ConnectionRefusedError' >&2; exit 2 ;;
  garbage) echo 'not json'; exit 0 ;;
esac
R
chmod +x "$T/relay"
fail=0
check() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got [$2] want [$3]"; fail=1; fi; }
run() { FAKE_STATUS="$1" "$BIN"; echo "rc=$?"; }

check 'answered: silent, exit 0' "$(run answered)" 'rc=0'
check 'the relay is asked to ring with the reason' "$(tail -1 "$FAKE_LOG")" 'call --reason you tapped Talk --say you tapped Talk, so I am here. What is up?'
check 'declined: silent' "$(run declined)" 'rc=0'
check 'no answer: silent' "$(run no_answer)" 'rc=0'
check 'already in a call: silent (a tap during a call does nothing)' "$(run in_call)" 'rc=0'
check 'rate limited: one line, exit 0 (no session turn)' "$(run rate_limited)" $'📞 Call limit reached (3 per hour). Try again a bit later.\nrc=0'
check 'relay down: one line, exit 1' "$(run down)" $'📞 Can\'t ring you: the voice relay is down.\nrc=1'
check 'bridge off: one line, exit 1' "$(run unavailable)" $'📞 Can\'t ring you: the call bridge is off.\nrc=1'
check 'provider failed: one line, exit 1' "$(run provider)" $'📞 Can\'t ring you: the voice service didn\'t start.\nrc=1'
check 'other failure: one line, exit 1' "$(run failed)" $'📞 The call didn\'t go through.\nrc=1'
check 'unparseable output with exit 0 counts as answered' "$(run garbage)" 'rc=0'
check '--json' "$(FAKE_STATUS=rate_limited "$BIN" --json)" '{"status": "rate_limited", "exit": 0, "say": "📞 Call limit reached (3 per hour). Try again a bit later."}'
check 'unknown argument: usage, exit 2' "$("$BIN" --bogus 2>/dev/null; echo "rc=$?")" 'rc=2'
check 'relay hangs past TALK_TIMEOUT: one line, exit 1' "$(TALK_TIMEOUT=1 FAKE_SLEEP=3 run answered)" $'📞 The voice relay didn\'t answer in time.\nrc=1'
TALK_RELAY="$T/missing" check 'relay binary missing: down' "$(TALK_RELAY="$T/missing" run answered)" $'📞 Can\'t ring you: the voice relay is down.\nrc=1'

# A second tap while the first still rings: silent, the relay is called once.
: > "$FAKE_LOG"
FAKE_SLEEP=2 FAKE_STATUS=answered "$BIN" > "$T/first.out" & p=$!
for _ in $(seq 50); do [ -s "$FAKE_LOG" ] && break; sleep 0.05; done
check 'second tap while ringing: silent, exit 0' "$(FAKE_STATUS=answered "$BIN"; echo "rc=$?")" 'rc=0'
wait "$p"
check 'relay rang once for two taps' "$(wc -l < "$FAKE_LOG" | tr -d ' ')" '1'
check 'log: one line per tap, outcome only' "$(tail -2 "$TALK_STATE_DIR/log" | sed 's/^[0-9-]* [0-9:]* //' | sort | tr '\n' ' ')" 'tap -> answered tap -> running '
check 'state dir is 0700' "$(stat -c %a "$TALK_STATE_DIR")" '700'

[ "$fail" = 0 ] && echo 'all passed' || { echo FAILED; exit 1; }
