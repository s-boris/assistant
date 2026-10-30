#!/usr/bin/env bash
# Tests for ~/bin/recall. Exit 0 = all pass. Temp dirs only (RECALL_HOME, SECRETSCAN_SOURCES); fixture transcript in
# fixtures/session.jsonl, fake secrets assembled at runtime. The real ~/.local/state/recall is checked to be untouched.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REAL=/home/node/.local/state/recall/index.db
before=$( [ -f "$REAL" ] && stat -c '%Y %s' "$REAL")
RUN_TMP=$(mktemp -d "${TMPDIR:-/tmp}/recall-tests-run.XXXXXX")
trap 'rm -rf "$RUN_TMP"' EXIT
TMPDIR=$RUN_TMP python3 -I "$HERE/test_recall.py" "$@"; rc=$?
after=$( [ -f "$REAL" ] && stat -c '%Y %s' "$REAL")
if [ "$before" != "$after" ]; then echo "FAIL - real index was touched"; rc=1; fi
exit $rc
