#!/usr/bin/env bash
# Offline tests for ~/bin/cache-ttl-watch (synthetic jsonl in a temp dir). Exit 0 = all pass.
# CTW=/path/to/copy tests an uninstalled copy.
exec python3 -I "$(dirname "$0")/test_cache_ttl_watch.py"
