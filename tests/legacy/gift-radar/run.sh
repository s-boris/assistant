#!/usr/bin/env bash
# Tests for ~/bin/gift-radar: date maths (year wrap, 29 Feb), memory parsing, privacy scrub, dedup, skip, dry run,
# reminder replacement. Fake vault + llm-local, real remind on a temp REMIND_DB. Exit 0 = all pass.
# Test a copy before installing: GIFT_RADAR_BIN=/path/to/copy ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_gift_radar.py "$@"
