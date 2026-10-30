#!/usr/bin/env bash
# Offline tests for ~/bin/lib/livecount.py (the phone app's Live Update chips): a stub phone CLI, temp state, lint
# parity with phonegw's lock-screen lint (this repo's adapters/phone). Test a copy: LIVECOUNT_LIB=/path/to/livecount.py ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_livecount.py "$@"
