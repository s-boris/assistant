#!/bin/sh
# Offline tests for lists (temp LISTS_DB, fixed clock). LISTS_BIN=/path tests a copy.
exec python3 -I "$(dirname "$0")/test_lists.py" "$@"
