#!/usr/bin/env bash
# Tests for ~/bin/voicecheck (offline: fake hub / voice-relay / phone / tg / sleepin in a temp dir; no Gemini, no Telegram).
# VOICECHECK_BIN=/copy tests a copy.
set -uo pipefail
B=${VOICECHECK_BIN:-/home/node/bin/voicecheck}
D=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/voicecheck-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
fail=0; ok() { echo "ok   $1"; }; no() { echo "FAIL $1"; fail=1; }
mkdir -p "$T/bin"
for n in phone tg sleepin; do ln -s "$D/fake-cmd" "$T/bin/$n"; done
export VC_FAKE=$T/fake VOICECHECK_RELAY=$D/fake-relay VOICECHECK_PHONE=$T/bin/phone VOICECHECK_TG=$T/bin/tg \
       VOICECHECK_SLEEPIN=$T/bin/sleepin VOICECHECK_STATE=$T/state VOICECHECK_INBOX=$T/inbox \
       VOICECHECK_TZ=Europe/Zurich VOICECHECK_QUIET=0-8 VOICECHECK_HUB=$D/fake-hub
at() { export VOICECHECK_NOW; VOICECHECK_NOW=$(TZ=Europe/Zurich date -d "$1" +%s); }
F=$D/fixtures
hubok() { echo '{"held":false,"why":"","loader":{"state":"running","release":"r1","age_s":4.0}}' > "$T/fake/hubvoice.json"; }
phok() { echo '{"held":false,"why":"","current":{"phone":"ok"},"loader":{"state":"running","release":"r1","age_s":2.0}}' > "$T/fake/hubphone.json"; }
hubst() { cp "$F/hub-$1.json" "$T/fake/status.json"; }
reset() {
  rm -rf "$T/fake" "$T/state" "$T/inbox"; mkdir -p "$T/fake" "$T/inbox"
  echo '{"provider":"gemini","live":0,"tg_calls":"on","call_failures":0,"via":"hub"}' > "$T/fake/status.json"
  echo '[{"tool":"weather","ms":120,"ok":true,"error":""},{"tool":"cat","ms":200,"ok":true,"error":""}]' > "$T/fake/fastlane.json"
  echo 1 > "$T/fake/sleepin.rc"; hubok; phok
}
run() { out=$("$B" "$@" 2>&1); rc=$?; }
notes() { find "$T/inbox" -name 'voicecheck-*.md' | wc -l; }
tgs() { [ -f "$T/fake/tg.log" ] && grep -c "Voice calls will fail" "$T/fake/tg.log" || echo 0; }

# 1. all ok: silent; hub voice status + voice.status, hub phone status + phone status, fastlane; no health, no canary
reset; at "2026-10-20 10:05"
run run
[ $rc = 0 ] && [ "$(notes)" = 0 ] && [ "$(tgs)" = 0 ] && grep -q "all ok" <<<"$out" && grep -q "relay up (hub), bridge on, phone up (hub)" <<<"$out" && ok "all ok: exit 0, no note, no text" || no "all ok ($rc): $out"
grep -q '^status$' "$T/fake/calls.log" && grep -q '^fastlane --json$' "$T/fake/calls.log" && ! grep -q '^health\|^canary' "$T/fake/calls.log" && grep -q '^voice status --json$' "$T/fake/hub.log" && grep -q '^phone status --json$' "$T/fake/hub.log" && grep -q '^status$' "$T/fake/phone.log" && ok "asks the hub, voice.status, phone status and fastlane; no health, no canary" || no "calls: $(cat "$T/fake/calls.log" "$T/fake/hub.log")"
grep -q "gemini (hub, from voice.status; key and model live in claude-hub-voice): no verdict (no finished call in the hub's log); provider failures since the relay started: 0" <<<"$out" && ok "gemini line names the hub" || no "gemini line: $out"

# 2. crit: one note + one text; repeat runs stay silent; recovery closes silently; a recurrence is new again
reset; at "2026-10-29 10:05"; hubst nokey; run run
[ $rc = 3 ] && [ "$(notes)" = 1 ] && [ "$(tgs)" = 1 ] && grep -q "^Telegram: sent" "$T"/inbox/voicecheck-gemini-hub-key-*.md && ok "crit: one note + one text" || no "crit ($rc, notes $(notes), tg $(tgs)): $out"
at "2026-10-29 12:35"; run run
[ "$(notes)" = 1 ] && [ "$(tgs)" = 1 ] && ok "dedupe: no 2nd note/text" || no "dedupe ($(notes)/$(tgs))"
hubst later-ok; run run
[ $rc = 0 ] && [ "$(notes)" = 1 ] && [ "$(tgs)" = 1 ] && grep -q "closed: gemini-hub-key" <<<"$out" && ok "recovery closes silently" || no "recovery ($rc): $out"
at "2026-10-30 10:05"; hubst nokey; run run
[ "$(notes)" = 2 ] && [ "$(tgs)" = 2 ] && ok "a recurrence after recovery is new again" || no "recurrence ($(notes)/$(tgs))"

# 3. quiet hours / sleep-in hold the text; --if-pending sends it later; --if-pending without a held text = no checks
reset; at "2026-10-29 03:10"; hubst nokey; run run
[ "$(notes)" = 1 ] && [ "$(tgs)" = 0 ] && grep -q "^Telegram: held (quiet hours)" "$T"/inbox/voicecheck-gemini-hub-key-*.md && ok "quiet hours: note, text held" || no "quiet ($(notes)/$(tgs)): $out"
at "2026-10-29 10:35"; echo 0 > "$T/fake/sleepin.rc"; run run --if-pending
[ "$(tgs)" = 0 ] && grep -q "telegram: held" <<<"$out" && ok "sleep-in morning keeps holding" || no "sleepin: $out"
echo 1 > "$T/fake/sleepin.rc"; at "2026-10-29 12:35"; run run --if-pending
[ "$(tgs)" = 1 ] && [ "$(notes)" = 1 ] && ok "--if-pending sends the held text once" || no "if-pending ($(tgs)): $out"
: > "$T/fake/calls.log"; run run --if-pending
[ $rc = 0 ] && [ ! -s "$T/fake/calls.log" ] && [ "$(tgs)" = 1 ] && ok "--if-pending with nothing held: file-only exit" || no "if-pending idle: $(cat "$T/fake/calls.log")"
reset; at "2026-10-29 03:10"; hubst nokey; run run; hubst later-ok; at "2026-10-29 10:05"; run run --if-pending
[ "$(tgs)" = 0 ] && ok "a held text is dropped when the problem cleared" || no "held cleared ($(tgs))"

# 4. --no-tg holds, a normal later run sends; --dry-run writes nothing
reset; at "2026-10-20 10:05"; touch "$T/fake/relay-down"
run run --no-tg
[ "$(tgs)" = 0 ] && [ "$(notes)" = 1 ] && grep -q '^voicecheck-relay-down-' <<<"$(ls "$T/inbox")" && ok "--no-tg: note, no text" || no "no-tg ($(notes)/$(tgs)): $out"
run run
[ "$(tgs)" = 1 ] && [ "$(notes)" = 1 ] && ok "next run sends the held text" || no "held after no-tg ($(tgs))"
reset; touch "$T/fake/relay-down"; run run --dry-run
[ $rc = 3 ] && [ "$(notes)" = 0 ] && [ "$(tgs)" = 0 ] && [ ! -e "$T/state/state.json" ] && ok "--dry-run: nothing written or sent" || no "dry-run ($rc): $out"

# 5. warn-level: phone gateway, fast-lane tool
reset; echo 7 > "$T/fake/phone.rc"
echo '[{"tool":"weather","ms":120,"ok":false,"error":"town not found"},{"tool":"cat","ms":200,"ok":true,"error":""}]' > "$T/fake/fastlane.json"
run run
[ $rc = 3 ] && [ "$(notes)" = 2 ] && [ "$(tgs)" = 0 ] && grep -q '^voicecheck-phonegw-down-' <<<"$(ls "$T/inbox")" && grep -q '^voicecheck-fastlane-weather-' <<<"$(ls "$T/inbox")" && ok "phone + fastlane: warn notes, no text" || no "warn ($rc, $(notes)/$(tgs)): $out"
grep -q "^Telegram: not needed (warn)" "$T"/inbox/voicecheck-fastlane-weather-*.md && grep -q "town not found" "$T"/inbox/voicecheck-fastlane-weather-*.md && grep -q "adapters/voice/voice_relay/fastlane.py" "$T"/inbox/voicecheck-fastlane-weather-*.md && ok "fastlane note: tool + error only" || no "fastlane note"

# 6. status, json, usage
run status
grep -q "open warn phonegw-down" <<<"$out" && ok "status lists open problems" || no "status: $out"
reset; run run --json
echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ok"] and d["info"]["mode"]=="hub"' && ok "--json" || no "json: $out"
run bogus; [ $rc = 2 ] && ok "usage exit 2" || no "usage $rc"
[ "$(stat -c %a "$T/state")" = 700 ] && ok "state dir 0700" || no "state perms"

# 7. the relay in claude-hub-voice: held, stale or down loader, hub unreachable, voice.status down, bridge
reset; at "2026-10-20 10:05"; hubok
echo '{"held":true,"why":"brain: emergency stop","loader":{"state":"held","age_s":3}}' > "$T/fake/hubvoice.json"
: > "$T/fake/calls.log"; run run
[ $rc = 3 ] && [ "$(tgs)" = 1 ] && grep -q "is held (brain: emergency stop)" "$T"/inbox/voicecheck-relay-down-*.md && grep -q "hub voice start" "$T"/inbox/voicecheck-relay-down-*.md && ! grep -q '^status$' "$T/fake/calls.log" && ok "hub mode, held: crit relay-down, hub fix, no voice.status" || no "held ($rc): $out"
hubok; run run
[ $rc = 0 ] && grep -q "closed: relay-down" <<<"$out" && ok "hub mode: released hold closes silently" || no "hub close ($rc): $out"
reset; echo '{"held":false,"loader":{"state":"running","age_s":900}}' > "$T/fake/hubvoice.json"; run run
grep -q "last report 15 min ago" "$T"/inbox/voicecheck-relay-down-*.md 2>/dev/null && ok "hub mode: a silent voiceloader is down" || no "stale loader: $out"
reset; echo '{"held":false,"loader":{"state":"down","why":"exit 1"}}' > "$T/fake/hubvoice.json"; run run
grep -q "not running (down: exit 1)" "$T"/inbox/voicecheck-relay-down-*.md 2>/dev/null && ok "hub mode: loader down = relay-down, with the loader's reason" || no "loader down: $out"
reset; echo 'hub voice: hub unreachable' > "$T/fake/hubvoice.json"; echo 3 > "$T/fake/hubvoice.rc"; run run
[ $rc = 3 ] && grep -q "the hub does not answer" "$T"/inbox/voicecheck-relay-down-*.md && ok "hub mode: hub unreachable = relay-down" || no "hub down ($rc): $out"
reset; hubok; touch "$T/fake/relay-down"; run run
grep -q "does not answer through the hub gateway" "$T"/inbox/voicecheck-relay-down-*.md 2>/dev/null && ok "hub mode: voice.status fails = relay-down" || no "voice.status down: $out"
reset; hubok; echo '{"live":0,"tg_calls":"failed: OSError","via":"hub"}' > "$T/fake/status.json"; run run
grep -q '^voicecheck-bridge-off-' <<<"$(ls "$T/inbox")" && grep -q "docker logs claude-hub-voice" "$T"/inbox/voicecheck-bridge-off-*.md && ok "hub mode: bridge not up = bridge-off" || no "hub bridge: $out"
reset; hubok; echo '{"live":0,"tg_calls":"refused: no session file: log in first (voice-tg login start / finish)","via":"hub"}' > "$T/fake/status.json"; run run
[ $rc = 3 ] && [ "$(tgs)" = 1 ] && grep -q "session is missing in its session dir, HUB_ROOT/secrets/voice" "$T"/inbox/voicecheck-bridge-off-*.md && ok "hub mode: the hub relay's refused bridge (no session file) stays a real crit, fix names the hub's session dir" || no "hub session refused ($rc): $out"

# 8. the phone gateway in claude-hub-phone: hub phone status first, then phone status
reset; at "2026-10-20 10:05"; phok; run run
[ $rc = 0 ] && [ "$(notes)" = 0 ] && grep -q "phone up (hub)" <<<"$out" && grep -q '^phone status --json$' "$T/fake/hub.log" && grep -q '^status$' "$T/fake/phone.log" && ok "phone hub, all fine: hub phone status + phone status, silent" || no "phone hub ok ($rc): $out"
echo '{"held":false,"why":"","current":{"phone":"ok"},"loader":{"state":"down","release":null,"age_s":0.2,"why":"exit 1; retry in 60 s"}}' > "$T/fake/hubphone.json"
: > "$T/fake/phone.log"; run run
[ $rc = 3 ] && [ "$(tgs)" = 0 ] && grep -q "the phone gateway in the hub is not running (down: exit 1; retry in 60 s)" "$T"/inbox/voicecheck-phonegw-down-*.md && grep -q "hub phone status" "$T"/inbox/voicecheck-phonegw-down-*.md && ! grep -q "phonegw-up" "$T"/inbox/voicecheck-phonegw-down-*.md && [ ! -s "$T/fake/phone.log" ] && grep -q "^Telegram: not needed (warn)" "$T"/inbox/voicecheck-phonegw-down-*.md && ok "phone hub, phoneloader down: warn with its reason + hub fix, no text, no phone status" || no "phone hub down ($rc): $out"
phok; run run
[ $rc = 0 ] && grep -q "closed: phonegw-down" <<<"$out" && ok "phone hub: loader back closes silently" || no "phone hub close ($rc): $out"
reset; echo '{"held":true,"why":"brain: rollback test","loader":{"state":"held","age_s":3}}' > "$T/fake/hubphone.json"; run run
grep -q "the phone gateway in the hub is held (brain: rollback test)" "$T"/inbox/voicecheck-phonegw-down-*.md 2>/dev/null && [ "$(tgs)" = 0 ] && ok "phone hub, held: warn" || no "phone held: $out"
reset; echo 'hub phone: hub unreachable' > "$T/fake/hubphone.json"; echo 3 > "$T/fake/hubphone.rc"; run run
grep -q "the hub does not answer (the phone gateway runs there)" "$T"/inbox/voicecheck-phonegw-down-*.md 2>/dev/null && ok "phone hub: hub unreachable = phonegw-down" || no "phone hub unreachable: $out"
reset; phok; echo 7 > "$T/fake/phone.rc"; run run
grep -q "the phone gateway in the hub is not up (exit 7)" "$T"/inbox/voicecheck-phonegw-down-*.md 2>/dev/null && grep -q "docker logs claude-hub-phone" "$T"/inbox/voicecheck-phonegw-down-*.md && ok "phone hub: loader runs but the gateway does not answer = phonegw-down, hub fix" || no "phone hub gw: $out"

# 9. an open problem takes the latest wording (no 2nd note)
reset; at "2026-10-20 10:05"; echo '{"held":true,"why":"brain: test","loader":{"state":"held","age_s":3}}' > "$T/fake/hubphone.json"; run run
echo '{"held":false,"loader":{"state":"down","why":"exit 1; retry in 60 s"}}' > "$T/fake/hubphone.json"
run run; [ "$(notes)" = 1 ] && run status && grep -q "open warn phonegw-down: the phone gateway in the hub is not running (down: exit 1; retry in 60 s)" <<<"$out" && ok "open problem: status shows the latest title, no 2nd note" || no "refresh: $out"

# 10. the Gemini verdict from voice.status (fixtures/hub-*.json: the hub relay's call digest + 7-day numbers;
#     read-only, no call)
reset; at "2026-10-29 10:05"; hubok; hubst ok; run run
[ $rc = 0 ] && [ "$(notes)" = 0 ] && grep -q "gemini (hub, from voice.status; key and model live in claude-hub-voice): last call 2026-10-28 15:17 started ok; provider failures since the relay started: 1" <<<"$out" && ! grep -q '^canary' "$T/fake/calls.log" && ok "hub gemini: newest call started (an older failure is history): ok, no canary" || no "hub gemini ok ($rc): $out"
python3 -c 'import json,sys; i=json.load(open(sys.argv[1]))["last"]["info"]; assert "_status" not in i and i["gemini_hub"].startswith("last call"), i' "$T/state/state.json" && ok "hub gemini: state keeps the verdict, not the raw voice.status" || no "hub state: $(cat "$T/state/state.json")"
reset; at "2026-10-29 10:05"; hubok; hubst nokey; run run
[ $rc = 3 ] && [ "$(notes)" = 1 ] && [ "$(tgs)" = 1 ] && grep -q '^voicecheck-gemini-hub-key-' <<<"$(ls "$T/inbox")" && grep -q "CRIT the Gemini API key is missing in the hub" <<<"$out" && ok "hub gemini: newest finished call failed with no key = crit gemini-hub-key, one note + one text (a live call is skipped)" || no "hub no key ($rc, $(notes)/$(tgs)): $out"
N=$(ls "$T"/inbox/voicecheck-gemini-hub-key-*.md 2>/dev/null | head -1)
grep -q "voice_gemini_key" "$N" && grep -q "docker logs claude-hub-voice" "$N" && grep -q "provider failures since the relay started: 2" "$N" && ! grep -q "secrets/gemini-api-key" "$N" && ok "hub gemini note: hub key + logs in the fix, no local key file" || no "hub key note: $(cat "$N")"
grep -q "Gemini key on the server is missing" "$T/fake/tg.log" && ! grep -q "voice_gemini_key\|provider failed" "$T/fake/tg.log" && ok "hub gemini text: plain words, no internals" || no "hub key text: $(cat "$T/fake/tg.log")"
at "2026-10-29 12:35"; run run
[ $rc = 3 ] && [ "$(notes)" = 1 ] && [ "$(tgs)" = 1 ] && ok "hub gemini: dedupe while open" || no "hub key dedupe ($(notes)/$(tgs))"
hubst restarted; run run
[ $rc = 0 ] && [ "$(notes)" = 1 ] && grep -q "gemini-hub-key" "$T/state/state.json" && grep -q "no verdict (last call 2026-10-28 15:17 failed at the start; the relay restarted since)" <<<"$out" && ok "hub gemini: relay restarted since the failure = no verdict, the problem stays open" || no "hub restarted ($rc): $out"
echo '{"held":true,"why":"brain: test","loader":{"state":"held","age_s":3}}' > "$T/fake/hubvoice.json"; run run
[ $rc = 3 ] && ! grep -q "closed: gemini-hub-key" <<<"$out" && grep -q "gemini-hub-key" "$T/state/state.json" && grep -q "no verdict (voice.status did not answer)" <<<"$out" && ok "hub gemini: hub voice down = relay-down crit, gemini no verdict (stays open)" || no "hub held gemini ($rc): $out"
hubok; hubst later-ok; run run
[ $rc = 0 ] && grep -q "closed: .*gemini-hub-key" <<<"$out" && [ "$(notes)" = 2 ] && [ "$(tgs)" = 2 ] && ok "hub gemini: a later call that started closes it silently" || no "hub key close ($rc, $(notes)/$(tgs)): $out"
reset; at "2026-10-29 10:05"; hubok; hubst rejected; run run
[ $rc = 3 ] && [ "$(tgs)" = 1 ] && grep -q "Gemini did not start on the last call (setup rejected (close" "$T"/inbox/voicecheck-gemini-hub-start-*.md 2>/dev/null && grep -q "AI Studio > Billing" "$T"/inbox/voicecheck-gemini-hub-start-*.md && ok "hub gemini: another start failure = crit gemini-hub-start (billing / key / model in the fix)" || no "hub rejected ($rc): $out"
reset; at "2026-10-29 10:05"; hubok; hubst restarted; run run
[ $rc = 0 ] && [ "$(notes)" = 0 ] && ok "hub gemini: an old failure before a relay restart opens nothing" || no "hub restarted fresh ($rc): $out"
reset; at "2026-10-29 10:05"; hubok; hubst fallback; run run
[ $rc = 3 ] && [ "$(tgs)" = 0 ] && grep -q "refused on 2 call(s) in 7 days" "$T"/inbox/voicecheck-gemini-hub-fallback-*.md 2>/dev/null && grep -q "^Telegram: not needed (warn)" "$T"/inbox/voicecheck-gemini-hub-fallback-*.md && ok "hub gemini: model fallbacks in 7 days = warn, no text" || no "hub fallback ($rc): $out"
hubst ok; run run
[ $rc = 0 ] && grep -q "closed: gemini-hub-fallback" <<<"$out" && ok "hub gemini: no fallbacks closes the warn" || no "hub fallback close ($rc): $out"

# 9. call quality (stats_7d from the same voice.status): a failed ring, lost audio and a model drop = warns, no text;
#    his unanswered rings are no fault; the info line counts outgoing answered / not answered; clean numbers close them
reset; at "2026-10-29 10:05"; hubok; hubst callfaults; run run
[ $rc = 3 ] && [ "$(tgs)" = 0 ] && [ "$(notes)" = 3 ] && grep -q "1 call(s) to him could not ring in 7 days" "$T"/inbox/voicecheck-call-ring-failed-*.md 2>/dev/null && grep -q "his audio did not reach the voice model on 2 call(s)" "$T"/inbox/voicecheck-call-audio-lost-*.md 2>/dev/null && grep -q "did not come back on 1 call(s)" "$T"/inbox/voicecheck-call-model-drop-*.md 2>/dev/null && grep -q "^Telegram: not needed (warn)" "$T"/inbox/voicecheck-call-ring-failed-*.md && ok "calls: ring failed, audio lost, model drop = 3 warns, no text" || no "call faults ($rc, notes $(notes)): $out"
grep -q "calls (the hub relay's log, metadata only): 7 in 7 days, outgoing 5: answered 2, not answered 3; newest 28.10 22:19 tg-out (no_answer)" <<<"$out" && ! grep -q "no_answer\|not answered" "$T"/inbox/voicecheck-*.md && ok "calls: info line with outgoing answered / not answered; unanswered rings open nothing" || no "calls info: $out"
hubst ok; run run
[ $rc = 0 ] && grep -q "closed: .*call-ring-failed" <<<"$out" && grep -q "closed: .*call-audio-lost" <<<"$out" && grep -q "closed: .*call-model-drop" <<<"$out" && ok "calls: clean numbers close the warns" || no "calls close ($rc): $out"
exit $fail
