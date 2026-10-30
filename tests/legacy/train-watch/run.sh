#!/usr/bin/env bash
# Offline tests for ~/bin/train-watch: a loopback fake transport API (connections by from/to/date/time/limit, with
# delays, platform changes, cancellations, replacement buses) and a synthetic timetable; proxies point at the fake,
# so nothing leaves the box. Exit 0 = all pass. Test a copy before installing: TRAIN_WATCH_BIN=/path/to/copy ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_train_watch.py "$@"
