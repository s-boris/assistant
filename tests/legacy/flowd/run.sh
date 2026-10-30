#!/bin/sh
# flowd tests: ~/opt/flowd-tests/run.sh   (FLOWD_BIN=path tests another copy)
exec python3 -I "$(dirname "$0")/test_flowd.py" "$@"
