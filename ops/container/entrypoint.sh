#!/bin/sh
# Container entrypoint (ops/container/Dockerfile): start cron for the user's crontab, then wait. The long-running
# Claude Code session is the user's (QUICKSTART step 10: a tmux session started with `docker exec -u bot`).
# A command given to `docker run IMAGE CMD...` runs as the bot user instead of waiting.
set -eu
u=${ASSISTANT_USER:-bot}
cron
if [ $# -gt 0 ]; then exec runuser -u "$u" -- "$@"; fi
exec sleep infinity
