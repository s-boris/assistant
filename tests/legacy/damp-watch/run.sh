#!/usr/bin/env bash
# damp-watch tests: offline, fake `ha`, temp dirs. DAMP_WATCH_BIN=/path/to/copy tests a copy.
set -u
here=$(cd "$(dirname "$0")" && pwd)
exec python3 -I "$here/test_damp.py" "${DAMP_WATCH_BIN:-$HOME/bin/damp-watch}"
