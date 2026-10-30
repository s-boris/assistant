#!/usr/bin/env bash
# Offline tests for ~/bin/event-card: loopback mock Bot API, fake train-watch/gohome/vault, fictional profile,
# calendar and forecast; no real sends. Test a copy: EVENT_CARD_BIN=/path/to/copy ./run.sh
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_event_card.py "$@"
