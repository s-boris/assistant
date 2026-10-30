#!/usr/bin/env bash
# The telegram-plus bridge's own tests (~/plugins/telegram-plus/test): (1) `bun test` (bridge, bridge-only, hub mode
# against test/fakehub.ts, scan, sdk-guard, voiceinbox; ~1 min), (2) `bun test/offline.ts` (the offline end-to-end
# scenario over stdio: the script plays tg-adapter; a mock Bot API on 127.0.0.1 the bridge must never call).
# Hermetic: temp HOME and store, loopback only; never polls api.telegram.org, never reads a real token or
# ~/.claude/channels/telegram/. Exit 0 = all pass.
set -uo pipefail
PLUGIN=/home/node/plugins/telegram-plus
cd "$PLUGIN" || exit 2
rc=0
echo "# (1) bun test"
timeout 120 bun test </dev/null || rc=1
echo "# (2) bun test/offline.ts"
timeout 200 bun test/offline.ts </dev/null || rc=1
[ "$rc" -eq 0 ] && echo "telegram-plus-tests: all passed" || echo "telegram-plus-tests: FAILED"
exit "$rc"
