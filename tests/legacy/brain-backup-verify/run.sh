#!/usr/bin/env bash
# brain-backup-verify's suite is brain-backup's (its run.sh tests the verify half, backup-run.sh the backup half):
# assistant ci runs it with BRAIN_BACKUP_VERIFY_BIN set to the staged copy when scripts/brain-backup-verify changes.
exec bash "$(dirname "$0")/../brain-backup/run.sh" "$@"
