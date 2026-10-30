#!/bin/sh
# Offline tests for webagent (~/bin/webagent, ~/bin/lib/webagent.py) and its PreToolUse guard hook
# (~/.claude/hooks/webagent-guard.sh). Fake journal, browser and usage spool in a temp dir. Exit 0 = all pass.
# Copies: WEBAGENT_LIB=<lib dir> WEBAGENT_CLI=<script> WEBAGENT_HOOK=<hook>.
exec python3 -I "$(dirname "$0")/test_webagent.py" "$@"
