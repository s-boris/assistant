#!/usr/bin/env bash
# Offline tests for the shell senders on the one outbox: ~/bin/tg-buttons, ~/bin/tg-voice (tg's own
# --buttons/--voice: the repo's tests/tg/test_tg_flags.py).
# A temp HOME where the live tg-adapter "runs" (a process named tg-adapter holding the live PID file, live.json with
# send on) and a fake consumer that ends live outbox rows like the adapter; then the hub: no PID file and
# a fake hub on 127.0.0.1 (consumer, rows, uploads; a stale consumer = direct). No Bot API, no
# token file anywhere.
# Their direct paths stay covered where they were: tts-tests (tg-voice), the callers' suites (tg-buttons).
# TG_SENDERS_BIN=<dir> tests copies (the dir also needs tg-outbox, as ~/bin has it).
cd "$(dirname "$0")" && exec python3 -I -W ignore::ResourceWarning test_outbox.py "${TG_SENDERS_BIN:-$HOME/bin}"
