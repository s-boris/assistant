#!/usr/bin/env bash
# Offline tests for ~/bin/trip-recap (offline trip recap: EXIF/GPS grouping, best-photo picks, map page, montage).
# Exit 0 = all pass. TRIP_RECAP_BIN=<path> tests another copy. Fixtures are synthetic and live in a temp dir;
# config, dives and HOME are temp too, so no live state is read. `run.sh --fixtures DIR` only writes the fixtures.
exec "$HOME/opt/dive-venv/bin/python" -I "$(dirname "$0")/test.py" "$@"
