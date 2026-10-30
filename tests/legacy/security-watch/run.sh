#!/usr/bin/env bash
# Offline tests for ~/bin/security-watch (temp HOME, fake nmap/crontab/network). Exit 0 = all pass.
exec python3 -I "$(dirname "$0")/test_security_watch.py" "$@"
