#!/usr/bin/env bash
# Offline tests for ~/bin/push (prototype, dry run): fake probes and senders, fictional config, isolated HOME and
# state, simulated clock; nothing is ever sent. Test a copy: PUSH_BIN=/path/to/copy ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_push.py "$@"
