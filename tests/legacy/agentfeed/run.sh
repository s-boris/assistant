#!/bin/sh
# agentfeed tests (the dashboard Agents tab's privacy filter, transcript reader, list, renamed titles):
# tests/legacy/agentfeed/run.sh (~/opt/assistant). Default: the live ~/bin/lib/agentfeed.py;
# AGENTFEED_PATH=/copy/agentfeed.py (assistant ci's staged overlay) or AGENTFEED_LIB=/dir tests another copy.
exec python3 -I "$(dirname "$0")/test_agentfeed.py" "$@"
