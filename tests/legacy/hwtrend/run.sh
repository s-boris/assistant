#!/usr/bin/env bash
# Offline tests for ~/bin/hwtrend. Exit 0 = all pass. Never touches SSH, the real state or Telegram.
exec python3 -I "$(dirname "$0")/test_hwtrend.py"
