#!/bin/sh
# Run ha-avail tests (pure analysis + ha-audit's fake HA from tests/legacy/ha-audit; no network beyond 127.0.0.1).
# HA_AVAIL_BIN=/path overrides the script under test.
cd "$(dirname "$0")" && PYTHONDONTWRITEBYTECODE=1 exec python3 -m unittest test_ha_avail "$@"
