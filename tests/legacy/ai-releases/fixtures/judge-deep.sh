#!/bin/sh
# Fake local-LLM judge for test_deep.py: logs each prompt, follows the owner's rule (frontier models only) by keywords.
# JUDGE_MODE=fail -> exit 1; JUDGE_MODE=garbage -> no JSON; JUDGE_MODE=slow -> hangs 5 s (timeout test);
# JUDGE_REPLY=<json> -> prints that verbatim. Never calls a real model.
p=$(cat); printf '%s\n----\n' "$p" >> "${JUDGE_LOG:-/dev/null}"
case "$JUDGE_MODE" in
  fail) echo 'backend down' >&2; exit 1;;
  garbage) echo 'I think this is a model release.'; exit 0;;
  slow) exec sleep 5;;
esac
[ -n "$JUDGE_REPLY" ] && { printf '%s\n' "$JUDGE_REPLY"; exit 0; }
title=$(printf '%s\n' "$p" | sed -n 's/^Title: //p' | sed 's/^Introducing //')
case "$title" in
  *mini*|*Mini*|*image*|*Image*|*Banana*|*Veo*|*shopping*|*"minor update"*)
    echo "{\"major\": false, \"kind\": \"model\", \"name\": \"$title\", \"org\": \"\", \"line\": \"not frontier\", \"availability\": \"\"}";;
  *Claude*) echo "{\"major\": true, \"kind\": \"model\", \"name\": \"$title\", \"org\": \"Anthropic\", \"line\": \"New flagship model\", \"availability\": \"API, claude.ai\"}";;
  *GPT*) echo "{\"major\": true, \"kind\": \"model\", \"name\": \"$title\", \"org\": \"OpenAI\", \"line\": \"New flagship model\", \"availability\": \"\"}";;
  *) echo "{\"major\": true, \"kind\": \"model\", \"name\": \"$title\", \"org\": \"\", \"line\": \"New model\", \"availability\": \"\"}";;
esac
