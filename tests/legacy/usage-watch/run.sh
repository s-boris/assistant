#!/bin/sh
# Offline tests for ~/bin/usage-watch (fixture transcripts, temp state, a fake sender; nothing real is read or sent).
# Default: the repo's scripts/ copy; USAGE_WATCH_BIN=/copy tests another (the land CI passes the staged overlay copy).
exec python3 "$(dirname "$0")/test_usage_watch.py" "$@"
