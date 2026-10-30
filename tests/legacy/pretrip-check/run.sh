#!/bin/sh
# Offline tests for ~/bin/pretrip-check (synthetic data, stub tools; no ssh, no network).
# Default: the repo's scripts/ copy; PRETRIP_UNDER_TEST=/path/to/pretrip-check tests another
# (~/opt/assistant/tests/legacy/pretrip-check/run.sh).
exec python3 "$(dirname "$0")/test_pretrip.py" "$@"
