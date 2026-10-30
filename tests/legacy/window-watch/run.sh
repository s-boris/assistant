#!/usr/bin/env bash
# window-watch tests: offline, synthetic series, fake `ha`, temp dirs. WINDOW_WATCH_BIN=/path/to/copy tests a copy.
set -u
here=$(cd "$(dirname "$0")" && pwd)
exec python3 -I "$here/test_window.py" "${WINDOW_WATCH_BIN:-$(cd "$here/../../../scripts" && pwd)/window-watch}"
