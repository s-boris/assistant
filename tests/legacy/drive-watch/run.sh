#!/usr/bin/env bash
# Offline tests for drive-watch (scripts/drive-watch): fictional towns/events, fixture profile, fake Waze routes and
# sleepin, isolated HOME and temp state; nothing leaves the box. Exit 0 = all pass. Test another copy:
#   DRIVE_WATCH_BIN=/copy/drive-watch ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_drive_watch.py "$@"
