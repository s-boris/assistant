#!/bin/sh
# Offline tests for ha-dashboard-gen (build step + stubbed HA: fake ha-audit websocket, stubbed urlopen; no network). GEN_BIN=/path tests a copy.
cd "$(dirname "$0")" && PYTHONDONTWRITEBYTECODE=1 exec python3 -m unittest test_dashboard_gen test_deep "$@"
