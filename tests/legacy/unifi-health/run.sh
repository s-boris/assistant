#!/usr/bin/env bash
# Offline tests for ~/bin/unifi-health (stubbed ssh, unraid-do and tg; HTTP stubs on 127.0.0.1).
# Exit 0 = all pass. UNIFI_HEALTH_BIN=/path/to/copy tests a copy before installing it.
python3 -I "$(dirname "$0")/test_unifi_health.py"
