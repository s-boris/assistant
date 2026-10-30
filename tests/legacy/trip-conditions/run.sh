#!/bin/sh
# Offline tests for trip-conditions (fixture responses of a fictional trip; no network).
# Default: the repo's scripts/ copy; TRIP_CONDITIONS_BIN=/path tests another.
cd "$(dirname "$0")" && exec python3 -I run.py "$@"
