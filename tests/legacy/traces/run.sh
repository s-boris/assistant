#!/bin/sh
# traces tests: synthetic transcripts only (no real data). Usage: run.sh [path to traces script]
here=$(dirname "$(readlink -f "$0")")
TRACES_BIN="${1:-${TRACES_BIN:-$HOME/bin/traces}}" exec python3 -I "$here/test_traces.py" -v
