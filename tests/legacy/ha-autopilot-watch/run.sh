#!/bin/sh
# Offline tests for ~/bin/ha-autopilot-watch (folded from ~/opt/ha-autopilot-watch-tests). AUTOPILOT_BIN=<copy> tests a copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 1
exec python3 test_watch.py
