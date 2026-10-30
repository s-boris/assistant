#!/bin/sh
# Offline tests for ~/bin/cat-corner (folded from ~/opt/cat-corner-tests). CAT_CORNER_BIN=<copy> tests a copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 1
exec python3 -B test_cat_corner.py
