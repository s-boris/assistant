#!/usr/bin/env bash
# Tests for mail-migration-check (default: the repo's scripts/ copy; MMC_SCRIPT=path tests another). Exit 0 = all pass. fake-vault stands in for
# the vault CLI; temp dirs only (MMC_HOME), so the real vault and ~/reports stay untouched.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
python3 -I "$HERE/test_mmc.py" "$@"
