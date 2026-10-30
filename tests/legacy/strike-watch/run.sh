#!/usr/bin/env bash
# Offline tests for ~/bin/strike-watch: a loopback fake of every source (Italian strike list, iRail, SNCB newsrooms,
# Google News RSS) serving saved fixture pages, a fake clock, HOME/config/state/inbox in a temp dir; nothing leaves
# the box. Exit 0 = all pass. Test a copy before installing: STRIKE_WATCH_BIN=/copy/strike-watch ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_strike_watch.py "$@"
