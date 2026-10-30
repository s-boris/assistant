#!/bin/bash
# plans-watch tests: fake vault + remind + calcache in a temp dir (no vault, no tokens). PLANS_WATCH_BIN=path tests a copy.
cd "$(dirname "$0")" && exec python3 -I test_plans_watch.py "$@"
