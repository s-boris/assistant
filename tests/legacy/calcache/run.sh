#!/usr/bin/env bash
# Offline tests for ~/bin/calcache and `train-watch sync --from-cache`: fictional events, a fixture profile, temp
# state dirs; nothing leaves the box. Exit 0 = all pass. Test copies before installing:
#   CALCACHE_BIN=/copy/calcache TRAIN_WATCH_BIN=/copy/train-watch ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_calcache.py "$@"
