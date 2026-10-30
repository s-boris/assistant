#!/usr/bin/env bash
# Offline tests for ~/bin/board-lint (board staleness lint, heartbeat line, pitch check, compaction hand-off) and its
# hook wrapper (the tree's hooks/pitch-check.sh, deployed as ~/.claude/hooks/pitch-check.sh). Synthetic fixtures only;
# every path goes through the script's env seams into a temp dir; the real state files are checksummed before and after.
# BOARD_LINT_BIN=<path> tests another copy, BOARD_LINT_HOOKS=<dir> another hook dir. Exit 0 = all pass.
set -uo pipefail
cd "$(dirname "$0")" || exit 2
exec /usr/local/bin/python3.13 -I test_board_lint.py "$@"
