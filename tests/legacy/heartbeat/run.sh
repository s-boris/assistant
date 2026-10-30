#!/usr/bin/env bash
# Offline tests of the heartbeat mod (~/plugins/heartbeat): runs the plugin's own node:test files with `node --test`,
# the runner their headers name. Every test/*.test.mjs runs here except hb-gate.test.mjs, which the hb-gate suite runs
# next to the gate's CLI tests, so a new test file is picked up without a change here. Today that is:
#   logic.test.mjs           the mod's decision logic and a 7-day simulation (hooks/register.js)
#   register.test.mjs        the hooks module end to end with a fake `$` (virtual clock, in-memory fs, recorded submits)
#   heartbeat-cron.test.mjs  ops/heartbeat-cron, the cron fallback boot.sh installs as ~/.local/bin/heartbeat
#                            (fake real script, fake `claude agents`, temp transcripts, inbox and marker)
# The tests use temp dirs and env seams only; HOME is a temp dir here too. No claude, tmux, Telegram or network.
# A self-test proves a failing test file fails this suite. HEARTBEAT_PLUGIN=<dir> tests another copy.
set -uo pipefail
P=${HEARTBEAT_PLUGIN:-/home/node/plugins/heartbeat}
T=$(mktemp -d "${TMPDIR:-/tmp}/heartbeat-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
mkdir -p "$T/home"
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/       /'; }

# runfile DIR FILE: run one test file from DIR with the TAP reporter; sets RC, NT (tests), NP (passed), OUT
runfile() {
  OUT=$(cd "$1" && HOME=$T/home timeout 120 node --test --test-reporter=tap "$2" </dev/null 2>&1); RC=$?
  NT=$(sed -n 's/^# tests \([0-9][0-9]*\)$/\1/p' <<<"$OUT" | tail -n 1)
  NP=$(sed -n 's/^# pass \([0-9][0-9]*\)$/\1/p' <<<"$OUT" | tail -n 1)
}
passed() { [ "$RC" -eq 0 ] && [ "${NT:-0}" -gt 0 ] && [ "$NT" = "$NP" ]; }

command -v node >/dev/null || { no "node not found"; echo; echo "$pass passed, $fail failed"; exit 1; }

echo "# plugin test files (node --test)"
files=()
for f in "$P"/test/*.test.mjs; do
  [ -f "$f" ] && [ "${f##*/}" != hb-gate.test.mjs ] && files+=("${f##*/}")
done
if [ "${#files[@]}" -ge 3 ]; then ok "found ${#files[@]} test files besides hb-gate.test.mjs: ${files[*]}"
else no "expected at least 3 test files in $P/test besides hb-gate.test.mjs, found ${#files[@]}"; fi
for n in "${files[@]}"; do
  runfile "$P" "test/$n"
  if passed; then ok "$n: $NP/$NT pass"
  else no "$n: ${NP:-?}/${NT:-?} pass (rc=$RC)" "$(grep -E -A12 '^ *not ok ' <<<"$OUT" | head -40)"; fi
done
if grep -qF 'test/hb-gate.test.mjs' "$(dirname "$0")/../hb-gate/run.sh" 2>/dev/null; then
  ok "hb-gate.test.mjs is run by the hb-gate suite"
else no "hb-gate.test.mjs is not run by the hb-gate suite any more: run it here (drop the exclusion above)"; fi

echo "# self-test: a failing test file fails the check"
mkdir -p "$T/fake/test"
printf '%s\n' "import { test } from 'node:test'" "import assert from 'node:assert/strict'" \
  "test('passes', () => assert.equal(1, 1))" "test('fails', () => assert.equal(1, 2))" > "$T/fake/test/broken.test.mjs"
runfile "$T/fake" test/broken.test.mjs
if ! passed && [ "$NT" = 2 ] && [ "$NP" = 1 ]; then ok "a failing file is reported as failed (1/2 pass)"
else no "a failing file was not caught (rc=$RC, ${NP:-?}/${NT:-?})"; fi
printf '%s\n' "import { test } from 'node:test'" "test('passes', () => {})" > "$T/fake/test/good.test.mjs"
runfile "$T/fake" test/good.test.mjs
if passed; then ok "a passing file passes (1/1)"; else no "a passing file was reported as failed (rc=$RC, ${NP:-?}/${NT:-?})"; fi

echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
