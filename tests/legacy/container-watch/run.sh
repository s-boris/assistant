#!/usr/bin/env bash
# Offline tests for container-watch (scripts/container-watch; CW_BIN=<path> tests another copy). Exit 0 = all pass.
# Never touches the network, ssh unraid / the UniFi OS VM, the real state/cache/inbox or Telegram (fake ssh,
# unraid-ssh, ha and tg in a temp dir; sockets blocked in the cron runs). Synthetic profile (never the real one).
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$HOME/opt/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
python3 -I "$(dirname "$0")/test_url_source.py" || exit 1   # URL source (profile first)
exec python3 -I "$(dirname "$0")/test_container_watch.py" "$@"
