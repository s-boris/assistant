#!/bin/sh
# Offline tests for ~/bin/missing-docs (folded from ~/opt/missing-docs-tests). MISSING_DOCS_BIN=<copy> tests a copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 1
exec python3 -I test_missing_docs.py
