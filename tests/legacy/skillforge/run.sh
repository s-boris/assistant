#!/bin/sh
# Offline tests for ~/bin/skillforge (fake transcripts + usage store, hashed vectors). Exit 0 = all pass.
# Copy: SKILLFORGE_BIN=<copy> [SKILLFORGE_LIB=<lib dir>].
exec python3 -I -W ignore::ResourceWarning "$(dirname "$0")/test_skillforge.py" "$@"
