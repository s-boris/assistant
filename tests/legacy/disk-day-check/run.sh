#!/bin/sh
# Offline tests for disk-day-check (fake unraid-ssh via DISK_DAY_CHECK_SSH, temp HOME per case). DDC_SCRIPT=/path
# tests a copy. Exit 0 = all pass.
exec python3 -I "$(dirname "$0")/run.py" "$@"
