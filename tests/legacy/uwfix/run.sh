#!/usr/bin/env bash
# Tests for ~/bin/uwfix (underwater colour correction: per-scene red restoration + gray world + levels via ffmpeg).
# Exit 0 = all pass. UWFIX_BIN=<path> tests another copy. Uses the real local ffmpeg on tiny generated clips
# (a red-starved testsrc2 scene + a greenish smptebars scene, with audio) in a temp dir; ~10 s.
exec "${UWFIX_TEST_PY:-$HOME/opt/dive-venv/bin/python}" -I "$(dirname "$0")/test.py" "$@"
