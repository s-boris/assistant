#!/bin/sh
# Offline tests for ~/bin/voice-plan (folded from ~/opt/voice-plan-tests). VOICE_PLAN_BIN=<copy> tests a copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 1
exec python3 -I test_voice_plan.py
