#!/bin/sh
# Offline tests for ~/bin/health + ~/bin/lib/healthlib.py (synthetic nights in a temp dir; nothing real is read).
# Copies: HEALTH_LIB=<lib dir> HEALTH_CLI=<script>. Exit 0 = all pass.
exec python3 "$(dirname "$0")/test_health.py" "$@"
