#!/bin/bash
# test-all entry for the phone gateway: the suite is the monorepo's adapters/phone/run-tests.sh (since P3-8; the old
# ~/opt/phonegw checkout was retired 29.10). Scratch HOME (with bin/lib -> ~/bin/lib), TMPDIR on the home disk
# (shares refuse below 20 GiB free), venv ~/opt/phone-venv (adapters/phone/requirements.lock), loopback only.
set -u
RH="$HOME"; R="$RH/opt/assistant/adapters/phone"; B="$RH/bin"
mkdir -p "$HOME/.cache" && S=$(mktemp -d "$HOME/.cache/phonegw-tests.XXXXXX") || exit 2
trap 'rm -rf -- "$S"' EXIT
mkdir -p "$S/home/bin" "$S/tmp" && ln -s "$B/lib" "$S/home/bin/lib" || exit 2
cd "$R" || exit 2
HOME="$S/home" TMPDIR="$S/tmp" PYTHONDONTWRITEBYTECODE=1 \
  PHONEGW_PYTHON="${PHONEGW_PYTHON:-$RH/opt/phone-venv/bin/python}" \
  PHONEGW_PROTOCOL="${PHONEGW_PROTOCOL:-$RH/opt/phone-app/claude/protocol}" \
  PHONEGW_FILTER="$RH/opt/assistant/adapters/voice/voice_relay/answer_filter.py" PHONE_BIN="$B/phone" \
  WHEREABOUTS_BIN="$B/whereabouts" PHONE_ROUTINE_BIN="$B/phone-routine" PUSH_BIN="$B/push" TG_STATUS_BIN="$B/tg-status" \
  REMIND_BIN="$B/remind" DRIVE_UPDATE_BIN="$B/drive-update" PHONEGW_USAGE_LIB="$B/lib/usage.py" \
  LD_PRELOAD="$RH/opt/test-all/netguard.so" timeout 400 sh ./run-tests.sh "$@"
