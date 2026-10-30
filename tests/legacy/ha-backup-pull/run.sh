#!/bin/sh
# Run ha-backup-pull tests (fake HA on 127.0.0.1, fake ssh/unraid-ssh, temp HOME). HA_BACKUP_PULL_BIN=/path
# overrides the script under test. Synthetic profile (tests never read the real one).
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$HOME/opt/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
cd "$(dirname "$0")" && PYTHONDONTWRITEBYTECODE=1 exec python3 -m unittest test_ha_backup_pull "$@"
