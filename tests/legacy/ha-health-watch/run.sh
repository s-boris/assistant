#!/bin/sh
# Run ha-health-watch tests (fake ha-audit JSON snapshots + fake `ha list` rows, temp state/inbox; no network).
# HA_HEALTH_WATCH_BIN=/path overrides the script under test.
cd "$(dirname "$0")" && PYTHONDONTWRITEBYTECODE=1 exec python3 -m unittest test_ha_health_watch "$@"
