#!/usr/bin/env bash
# Offline tests for ~/bin/divelog (local dive logbook: store, quick parser, Telegram form, Subsurface XML + PDF).
# Exit 0 = all pass. DIVELOG_BIN=<path> tests another copy. Every run uses a temp DIVELOG_DIR/DIVELOG_CONFIG and a
# pinned clock (DIVELOG_NOW), so the live store in ~/.local/state/divelog is never touched.
exec python3 -I "$(dirname "$0")/test.py" "$@"
