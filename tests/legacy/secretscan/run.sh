#!/usr/bin/env bash
# Tests for ~/bin/secretscan.py (the hub fingerprint engine). SECRETSCAN_UNDER_TEST=<path> tests a copy. Stays in
# ~/opt: its synthetic tokens would trip the repo's own secretscan step. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 2
exec python3 -I -m unittest discover -s . -v
