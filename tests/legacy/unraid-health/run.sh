#!/usr/bin/env bash
# Offline tests for ~/bin/unraid-health: notify scenarios, sync/temp/ack scenarios, golden characterization
# (frozen clock), CPU heat line (cputemp-log's last 24 h; CPUTEMP_LOG_BIN tests a cputemp-log copy), and test_paths.py
# (parsers, collect() via stub unraid-ssh, owner-text decision via stub tg, notification forwarding, failures),
# test_degraded.py (array:degraded from the 26-30.10 disk12 shapes, away-period escalation, docker.img growth/75% band).
# Exit 0 = all pass. UH_BIN=/path/to/copy tests a copy before installing it.
# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
d=$(dirname "$0")
# no CPU heat line in the other suites (they never read the real cputemp-log state); test_cpuheat.py sets its own
export UNRAID_HEALTH_CPUTEMP=/nonexistent/cputemp-log
# never the real capacity samples, trips or calendar (docker.img growth, away periods); test_degraded.py sets its own
export UNRAID_HEALTH_CAPACITY_SAMPLES=/nonexistent/samples.jsonl UNRAID_HEALTH_TRIPS=/nonexistent/trips.json \
  UNRAID_HEALTH_CALCACHE=/nonexistent/events.json
python3 -I "$d/test_notify.py" && python3 -I "$d/test_sync_ack.py" && python3 -I "$d/test_golden.py" \
  && python3 -I "$d/test_cpuheat.py" && python3 -I "$d/test_paths.py" && python3 -I "$d/test_degraded.py"
