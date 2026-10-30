#!/bin/sh
# dash-data whole-run tests (offline, temp HOME, pinned clock): tests/legacy/dash-data/run.sh
exec python3 "$(dirname "$0")/test_dash_data.py" "$@"
