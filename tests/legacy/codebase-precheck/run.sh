#!/usr/bin/env bash
# The public-path pre-check on the LIVE tree: `codebase-sync precheck` (the same scrub check + public-path gate
# a sync runs, on a throwaway git tree; no commit, push, lock, state or inbox note). Fails when the next sync
# would block, so every builder's test-all run shows `file:line rule#N` before the sync's inbox note does.
# Never prints a denylist value: only the precheck's own file:line + rule-label lines are shown.
# Needs the real HOME (sources, scrub list, gate config): listed in ~/opt/test-all/real-home.txt.
# Plumbing tests (fake gate, temp HOME): ~/opt/codebase-sync-tests section 10.
set -u
CS=${CODEBASE_SYNC_BIN:-$HOME/bin/codebase-sync}
[ -x "$CS" ] || { echo "SKIP: no $CS"; exit 0; }
out=$(timeout -k 5 220 "$CS" precheck 2>&1); rc=$?
# only the precheck's hit lines and its summary line (file:line + labels, never values)
printf '%s\n' "$out" | grep -E '^  [^ ]+:[0-9]+ [^ ]+|^  gate|^  BLOCK|^codebase-sync( precheck)?:' | head -40
case $rc in
  0) echo "codebase-precheck: PASS (the next sync passes the scrub check and the public-path gate)"; exit 0 ;;
  3) echo "codebase-precheck: FAIL: the next sync would block on the lines above; fix the source (personal value ->"
     echo "  profile-get; a standalone token -> rename) or the scrub list, then rerun: codebase-sync precheck"; exit 1 ;;
  124|137) echo "codebase-precheck: FAIL: precheck timed out"; exit 1 ;;
  *) echo "codebase-precheck: FAIL: precheck could not run (exit $rc)"; exit 1 ;;
esac
