#!/usr/bin/env bash
# Offline tests for ~/bin/lastcall: fake gohome/vault/relay/tg, fictional profile and calendar, isolated HOME,
# simulated clock; never rings or texts for real. Test a copy: LASTCALL_BIN=/path/to/copy ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_lastcall.py "$@"
