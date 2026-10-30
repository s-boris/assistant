#!/usr/bin/env bash
# Offline tests for ~/bin/ai-releases. Exit 0 = all pass. Never touches the network, the real state, the LLM or Telegram.
d="$(dirname "$0")"
python3 -I "$d/test_ai_releases.py" && python3 -I "$d/test_deep.py"
