#!/usr/bin/env bash
# Tests for ~/bin/memory2, ~/bin/kb and the memory-recall hook. Exit 0 = all pass. Temp dirs only; the store a code
# path that ignores MEMORY2_STATE would write ($HOME/.local/state/memory2/mem.db) is checked to be untouched.
# Under test-all and CI that HOME is a temp one, so the check can't go red when a live writer (the */15
# memory2-sync cron, a `memory set`) touches the real ~/.local/state/memory2/mem.db mid-run; run by hand with the
# real HOME it checks the real store. MEMORY2_BIN / KB_BIN / M2_HOOK_SCRIPT test copies.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
STORE="$HOME/.local/state/memory2/mem.db"
before=$( [ -f "$STORE" ] && stat -c '%Y %s' "$STORE")
python3 -I "$HERE/test_memory2.py" "$@"; rc=$?
after=$( [ -f "$STORE" ] && stat -c '%Y %s' "$STORE")
if [ "$before" != "$after" ]; then echo "FAIL - store under \$HOME was touched ($STORE)"; rc=1; fi
exit $rc
