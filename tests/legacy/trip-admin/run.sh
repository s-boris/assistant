#!/bin/sh
# Offline tests for trip-admin (fake tools, temp files). TRIP_ADMIN_SCRIPT=path tests a staged copy.
exec timeout 200 python3 -I "$(dirname "$0")/test_trip_admin.py" "$@" </dev/null
