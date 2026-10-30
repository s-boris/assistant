#!/usr/bin/env bash
# # Tests for ~/bin/tts, ~/bin/tg-voice and ~/bin/driving (voice answers while driving).
# CI: TTS_BIN DRIVING_BIN name a staged copy; the suite then runs against a temp bin of links to the live
# ~/bin with the staged copies (tts driving tg-voice) and their lib/ put in place. Without them it tests ~/bin. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 2
D=""
for v in TTS_BIN DRIVING_BIN; do p=$(printenv "$v" || true); [ -n "$p" ] && D=$(dirname "$p"); done
[ -n "$D" ] || exec bash suite.sh "$@"
LIVE=$(getent passwd "$(id -u)" | cut -d: -f6)/bin
T0=$(mktemp -d) || exit 2
for f in "$LIVE"/*; do ln -s "$f" "$T0/"; done
for n in tts driving tg-voice; do
  [ -f "$D/$n" ] && rm -f "${T0:?}/$n" && cp -p "$D/$n" "$T0/$n"
done
[ -d "$D/lib" ] && rm -f "${T0:?}/lib" && ln -s "$D/lib" "$T0/lib"
TTS_TEST_BIN="$T0" bash suite.sh "$@"; rc=$?
find "${T0:?}" -mindepth 1 -delete; rmdir "${T0:?}"
exit $rc
