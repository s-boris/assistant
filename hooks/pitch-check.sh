#!/bin/sh
# PreToolUse hook on the Telegram reply and send_poll tools (settings.json matcher), every reply, with or without
# buttons. Two steps in one hook:
# 1. Send lint (`conduct.py pre`): a declined/skipped/scratched/removed/off ledger item, > 1
#    question on one keyboard, > 12 text lines -> the message is held ONCE with a one-line reason (deny-once: the
#    identical resend goes through); a reply that goes out also closes the ack-first turn. Holds: `conduct holds`
#    (~/.claude/hooks/conduct.py holds); switches ~/.config/conduct/ (sendlint-off, <rule>-shadow, off).
# 2. Not held: `board-lint pitch --hook`: for a reply with buttons or a poll, context naming the FACTS.md
#    ledger entries it touches, and the TODO Waiting items it asks about marked as asked (waiting-nudge). It never
#    blocks. Logic and tests: ~/bin/board-lint, ~/opt/board-lint-tests; conduct: tests/hooks in ~/opt/assistant.
# FAILS OPEN: always exits 0; a crash, a missing script or a hang (conduct 2 s, board-lint 3 s) prints nothing for
# that step (board-lint: one line in its log).
in=$(cat) || exit 0
held=$(printf '%s' "$in" | timeout -k 1 "${CONDUCT_TIMEOUT:-2}" "${CONDUCT_BIN:-$HOME/.claude/hooks/conduct.py}" pre 2>/dev/null)
case "$held" in
  *'"deny"'*) printf '%s\n' "$held"; exit 0 ;;
esac
out=$(printf '%s' "$in" | timeout -k 1 "${PITCH_CHECK_TIMEOUT:-3}" "${BOARD_LINT_BIN:-$HOME/bin/board-lint}" pitch --hook 2>/dev/null)
rc=$?
if [ "$rc" -eq 0 ]; then
  [ -n "$out" ] && printf '%s\n' "$out"
else
  printf '%s pitch-check: board-lint exit %s; message left unchanged (fail open)\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$rc" \
    >>"${PITCH_CHECK_LOG:-$HOME/.local/state/board-lint/log}" 2>/dev/null
fi
exit 0
