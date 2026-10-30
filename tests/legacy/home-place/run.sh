#!/usr/bin/env bash
# Offline tests for ~/bin/lib/home_place.py (home as a door-to-door place for route, night-plan, train-watch):
# fictional profile, a loopback fake swisstopo geocoder, temp cache; proxies point at the fake, so nothing leaves
# the box. Exit 0 = all pass. Test a copy: HOME_PLACE_LIB=/path/to/lib-dir ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_home_place.py "$@"
