#!/bin/sh
# Run ~/bin/wled-sync tests (fake WLED HTTP servers + fake ha on 127.0.0.1; no real HA or WLED).
# WLED_SYNC_BIN=/path overrides the script under test (default the repo's scripts/wled-sync).
cd "$(dirname "$0")" && PYTHONDONTWRITEBYTECODE=1 exec python3 -m unittest test_wled_sync "$@"
