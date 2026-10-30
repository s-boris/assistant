#!/usr/bin/env bash
# Offline tests for the shell senders on the one outbox: ~/bin/tg-buttons, ~/bin/tg-voice (tg's own
# CI: TG_BUTTONS_BIN TG_VOICE_BIN name a staged copy; the suite then runs against a temp bin of links to the live
# ~/bin with the staged copies (tg-buttons tg-voice) and their lib/ put in place. Without them it tests ~/bin. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 2
D=""
for v in TG_BUTTONS_BIN TG_VOICE_BIN; do p=$(printenv "$v" || true); [ -n "$p" ] && D=$(dirname "$p"); done
[ -n "$D" ] || exec bash suite.sh "$@"
LIVE=$(getent passwd "$(id -u)" | cut -d: -f6)/bin
T0=$(mktemp -d) || exit 2
for f in "$LIVE"/*; do ln -s "$f" "$T0/"; done
for n in tg-buttons tg-voice; do
  [ -f "$D/$n" ] && rm -f "${T0:?}/$n" && cp -p "$D/$n" "$T0/$n"
done
[ -d "$D/lib" ] && rm -f "${T0:?}/lib" && ln -s "$D/lib" "$T0/lib"
TG_SENDERS_BIN="$T0" bash suite.sh "$@"; rc=$?
find "${T0:?}" -mindepth 1 -delete; rmdir "${T0:?}"
exit $rc
