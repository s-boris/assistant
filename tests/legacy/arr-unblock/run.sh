#!/bin/sh
# Offline tests for ~/bin/arr-unblock (folded from ~/opt/arr-unblock-tests). ARR_UNBLOCK_BIN=<copy> tests a copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 1
exec python3 test_arr_unblock.py
