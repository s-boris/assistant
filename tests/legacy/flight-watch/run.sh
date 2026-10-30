#!/usr/bin/env bash
# Offline tests for ~/bin/flight-watch (+ calcache's flight legs): a loopback fake of the FlightStats and FlightAware
# pages, a fake clock, HOME and every state dir in a temp dir, proxies at the fake; nothing leaves the box. Exit 0 =
# all pass. Test copies before installing: FLIGHT_WATCH_BIN=/copy/flight-watch CALCACHE_BIN=/copy/calcache ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_flight_watch.py "$@"
