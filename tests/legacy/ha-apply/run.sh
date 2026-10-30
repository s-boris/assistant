#!/bin/sh
# Offline tests for ha-apply (Guard allowlists). HA_APPLY_BIN=/path overrides the script under test.
# Synthetic profile (tests never read the real one): tests/legacy/profile once that suite moves here, else ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$HOME/opt/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
export HA_APPLY_PLAN="$(cd "$(dirname "$0")" && pwd)/fixtures/plan.json"
cd "$(dirname "$0")" && PYTHONDONTWRITEBYTECODE=1 exec python3 -m unittest test_guard test_round2 test_round3 test_autopilot test_package_scripts test_lint test_registry test_automation_delete test_group_helper "$@"
