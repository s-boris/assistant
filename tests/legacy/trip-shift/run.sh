#!/bin/sh
# Offline tests for trip-shift (a temp reminder store, the live ~/bin/remind or REMIND_BIN on it).
# Default: the repo's scripts/ copy; TRIP_SHIFT_BIN=/path tests another.
cd "$(dirname "$0")" && exec python3 -B test_trip_shift.py "$@"
