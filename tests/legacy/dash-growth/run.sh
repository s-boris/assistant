#!/bin/sh
# dash-growth tests (offline, fixture repos, pinned clock): ~/opt/dash-growth-tests/run.sh
exec python3 "$(dirname "$0")/test_dash_growth.py" "$@"
