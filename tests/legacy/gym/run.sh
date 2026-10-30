#!/bin/sh
# Offline tests for ~/bin/gym (folded from ~/opt/gym-tests). GYM_BIN=<copy> tests a copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 1
python3 test_gym.py && python3 test_gym_coach.py && exec python3 test_gym_plan.py
