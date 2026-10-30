#!/usr/bin/env bash
# Offline tests for the Claude Code hooks in ~/.claude/hooks, through each hook's own test file:
#   test_vault_guard.py    vault-guard (PreToolUse privacy tripwire): block/pass samples, hook command, fail closed
#   test_vault_guard_allow.py (here) vault-guard's allow side: normal commands, loops over literal words, long
#                          commands below its check limits; its deny side runs probe_caps.py (here) on the same guard
#   test_vault_guard_personal.py (here) vault-guard's personal-data, fill-direct and secrets-subagent rules:
#                          table-driven deny and allow events in a main session and a subagent
#   test_vault_guard_approvals.py (here) vault-guard's approvals-state rule (the approvals store): table-driven deny
#                          and allow events in a main session and a subagent, through the hook command too
#   test_subagent_rules.py subagent-rules (PreToolUse on Agent): rules block, default effort, fail open
#   test_no_approval.sh    no-approval (PermissionRequest on Bash): deny JSON, log redaction
# test_risk_check.py runs in risk-ok-tests; no-ask.sh has no test file. The hooks themselves are never changed.
# Hermetic: temp HOME (test-all), TMPDIR in a temp dir; every hook keeps its logs in temp files (vault-guard via
# VAULT_GUARD_LOG_DIR, which moves its block/error logs and never changes a decision).
# HOOKS_DIR=<dir> tests another copy of the hooks.
set -uo pipefail
D=${HOOKS_DIR:-/home/node/.claude/hooks}
HT=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/hooks-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
mkdir -p "$T/tmp" "$T/vg" "$T/vg-state"
export TMPDIR=$T/tmp

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; }
run() {  # run NAME CMD...: the test file's output, then one result line
  local name=$1; shift
  echo "# $name"
  if timeout -k 5 150 "$@" </dev/null 2>&1; then ok "$name"; else no "$name (exit $?)"; fi
}

# vault-guard: the installed files in place, logs into the temp dir
run "test_vault_guard.py" env VAULT_GUARD_LOG_DIR="$T/vg-state" python3 -I "$D/test_vault_guard.py"
if [ -s "$T/vg-state/blocked.log" ]; then ok "vault-guard block log went to the temp dir"; else no "vault-guard block log not in the temp dir"; fi
run "test_vault_guard_allow.py" env VAULT_GUARD_LOG_DIR="$T/vg-state" HOOKS_DIR="$D" python3 -I "$HT/test_vault_guard_allow.py"
run "test_vault_guard_personal.py" env VAULT_GUARD_LOG_DIR="$T/vg-state" HOOKS_DIR="$D" python3 -I "$HT/test_vault_guard_personal.py"
run "test_vault_guard_approvals.py" env VAULT_GUARD_LOG_DIR="$T/vg-state" HOOKS_DIR="$D" python3 -I "$HT/test_vault_guard_approvals.py"
run "test_vault_guard_precision.py" env VAULT_GUARD_LOG_DIR="$T/vg-state" HOOKS_DIR="$D" python3 -I "$HT/test_vault_guard_precision.py"

# subagent-rules: logs and agent dirs already go to a temp dir (reads the real settings.json and agents, read-only)
run "test_subagent_rules.py" python3 -I "$D/test_subagent_rules.py"

# no-approval: log via NO_APPROVAL_LOG=$(mktemp)
run "test_no_approval.sh" sh "$D/test_no_approval.sh" "$D/no-approval.sh"

echo "hooks-tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
