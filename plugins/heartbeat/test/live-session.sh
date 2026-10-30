#!/bin/sh
# Live smoke test of the heartbeat mod in a THROWAWAY session (never the main one): Haiku, no user settings or
# hooks (tg-status would touch the owner's board), no MCP servers, no channels, telegram plugins off, the mod
# loaded from this source dir in test mode (a minute = 6 s: idle beat after 18 s, inbox beat after 12 s,
# >= 30 s apart). First run in a new dir: the trust dialog defaults to "No, exit": press Down, Enter.
# Checks (2026-10-11, 2.1.296): idle beat; note in $D/hb/inbox -> "1 new note(s)" beat; a Bash permission
# prompt held 55 s -> no beat, command not run; after Esc a beat only after a full idle threshold;
# `/background` absent (disableAgentView); no child processes (no telegram server).
# Watch: tmux attach -t hbtest. Beats: grep -o '\[heartbeat\][^.]*' ~/.claude/projects/-home-node-jobs-hbtest/*.jsonl
# Clean up: tmux kill-session -t hbtest; rm -rf ~/.claude/projects/-home-node-jobs-hbtest ~/jobs/hbtest/hb
D=/home/node/jobs/hbtest
mkdir -p $D/hb/inbox && cd $D || exit
exec tmux new-session -d -s hbtest -x 200 -y 50 -c $D \
  "HEARTBEAT_MOD_TEST_DIR=$D/hb HEARTBEAT_MINUTES=3 claude --model haiku --permission-mode default \
   --setting-sources project,local --strict-mcp-config --plugin-dir /home/node/plugins/heartbeat \
   --settings '{\"enabledPlugins\":{\"telegram@claude-plugins-official\":false,\"telegram-plus@home-plugins\":false},\"disableAgentView\":true,\"crossSessionInbound\":\"refuse\"}'; sleep 600"
