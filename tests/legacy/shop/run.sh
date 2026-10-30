#!/bin/sh
# Tests for ~/bin/shop: stubbed `ha todo` (stub/ha, a JSON file), synthetic profile, temp state/inbox; no network.
# SHOP_BIN=/path tests another copy (default: ./stage/shop when a staged copy exists, else ~/bin/shop).
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
cd "$(dirname "$0")" && PYTHONDONTWRITEBYTECODE=1 exec python3 -I test_shop.py "$@"
