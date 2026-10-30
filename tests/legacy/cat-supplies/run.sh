#!/bin/sh
# Offline tests for ~/bin/cat-supplies (folded from ~/opt/cat-supplies-tests). CAT_SUPPLIES_BIN=<copy> tests a copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 1
exec python3 test_cat_supplies.py
