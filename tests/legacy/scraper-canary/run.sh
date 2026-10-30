#!/usr/bin/env bash
# Offline tests for ~/bin/scraper-canary: test_canary.py feeds every real tool its own fixture env (subprocess runs);
# test_deep.py imports the canary in-process with fake tool modules (fakes.py) and stubbed fetches (verdict paths,
# robots/HEAD, timeouts, state, note, status, CLI). Temp state + inbox only, no network. Exit 0 = pass.
# SCRAPER_CANARY_UNDER_TEST=/path/to/copy tests a copy before it is installed.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
rc=0
python3 -I "$HERE/test_canary.py" || rc=1
python3 -I "$HERE/test_deep.py" || rc=1
exit $rc
