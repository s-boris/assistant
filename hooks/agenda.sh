#!/bin/sh
# UserPromptSubmit hook: on a heartbeat beat in the main session, adds the agenda (new/updated/open events, Due,
# running work, waiting count, startable counts, last compaction, wake rules) as additionalContext and records a
# beats row (`events agenda --hook`; packages/inbox/hook.py in ~/opt/assistant). Titles and Next lines only.
# On a Telegram channel prompt it records delivery receipts instead (packages/inbox/receipts.py) and prints nothing.
# FAILS OPEN: always exits 0, prints nothing on any error or past 2 s. Switches: ~/.config/assistant/agenda-off,
# ~/.config/assistant/events-off. Tests: ~/opt/assistant tests/inbox (test_hook.py).
[ -e "$HOME/.config/assistant/agenda-off" ] && exit 0
py=/usr/local/bin/python3.13  # the scripts' interpreter here and in the hub CI runner (python:3.13 image)
[ -x "$py" ] || py=/usr/bin/python3
timeout -k 0.2 2 "$py" -I -S "${EVENTS_BIN:-$HOME/bin/events}" agenda --hook 2>/dev/null
exit 0
