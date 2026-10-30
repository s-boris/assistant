#!/usr/bin/env bash
# Offline tests for ~/bin/gohome (instant /gohome): a loopback fake transport API with a fictional timetable,
# a fictional profile and fake place/calendar files. Exit 0 = all pass. Test a copy: GOHOME_BIN=/path/to/copy ./run.sh
# Default stays ~/bin/gohome: it imports ~/bin/lib modules (assistant_profile, home_place, sbb) that are not in
# the repo; the land CI runs it on the rendered copy in the staged overlay (GOHOME_BIN).
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_gohome.py "$@"
