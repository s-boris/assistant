#!/bin/sh
# Offline tests for home-ctx (~/bin/home-ctx, ~/bin/lib/homectx.py, homectx_sim.py): feed compaction, night gating,
# screen-off tracking, heartbeat, dry-run/disabled writes, the scene rules and a synthetic replay. Synthetic fixtures
# only (no real entity ids). Copies: HOME_CTX_LIB=<lib dir> HOME_CTX_BIN=<cli>. Exit 0 = all pass.
exec python3 "$(dirname "$0")/test_homectx.py" "$@"
