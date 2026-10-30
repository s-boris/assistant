#!/bin/sh
# Dashboard tests: ~/opt/dash-tests/run.sh [--no-render]
exec python3 "$(dirname "$0")/test_dash.py" "$@"
