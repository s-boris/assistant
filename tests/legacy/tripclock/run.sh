#!/bin/sh
# Offline tests for ~/bin/tripclock (fake trips config and flight watches). Exit 0 = all pass.
# Default stays ~/bin/tripclock (it imports ~/bin/lib/tripclock.py, not in the repo); the land CI runs it on the
# rendered copy in the staged overlay. TRIPCLOCK_BIN / TRIPCLOCK_LIB point at a copy.
exec python3 -I "$(dirname "$0")/test_tripclock.py" "$@"
