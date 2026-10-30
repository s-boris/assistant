#!/bin/sh
# Fake local-LLM judge: logs each prompt, answers by keywords.
p=$(cat); printf '%s\n----\n' "$p" >> "${JUDGE_LOG:-/dev/null}"
case "$p" in
  *"Page:"*"Pro and Ultra"*) echo '{"change": true, "kind": "availability", "name": "Gemini 4 Argon", "line": "now available for Google AI Pro and Ultra subscribers"}';;
  *"Page:"*) echo '{"change": false, "kind": "availability", "name": "", "line": ""}';;
  *"Kimi K3"*) echo '{"major": true, "kind": "model", "name": "Kimi K3", "org": "Moonshot", "line": "Open frontier model", "availability": "API and weights"}';;
  *"Argon"*) echo '{"major": true, "kind": "model", "name": "Gemini 4 Argon", "org": "Google", "line": "Frontier model", "availability": ""}';;
  *"Mistral Large 5"*|*"GPT-7"*|*"Grok 6"*) n=$(printf '%s' "$p" | sed -n 's/^Title: Introducing //p'); echo "{\"major\": true, \"kind\": \"model\", \"name\": \"$n\", \"org\": \"\", \"line\": \"new model\", \"availability\": \"\"}";;
  *) echo '{"major": false, "kind": "model", "name": "", "org": "", "line": "not a release", "availability": ""}';;
esac
