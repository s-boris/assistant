#!/usr/bin/env bash
# Offline tests for ~/bin/paperless-inbox-review. PINB_BIN=<path> tests another copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 2
python3 -I test_paperless_inbox_review.py && python3 -I test_paperless_inbox_large.py
