#!/usr/bin/env bash
# Offline tests for ~/bin/night-plan: a loopback fake transport API (locations, connections by departure/arrival,
# paging) with a synthetic timetable, profile and rave-radar state; proxies point at the fake, so nothing leaves the
# box. Exit 0 = all pass. Test a copy before installing: NIGHT_PLAN_BIN=/path/to/copy ./run.sh
cd "$(dirname "$0")" || exit 1
python3 -I -W ignore::ResourceWarning test_night_plan.py "$@" || exit 1
# night-plan settime (lib/night_settime.py): fake event pages, fake night-plan, isolated HOME
exec python3 -I -W ignore::ResourceWarning test_settime.py "$@"
