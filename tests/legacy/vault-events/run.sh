#!/usr/bin/env bash
# vault-events tests: a fake vault client and a fake bills script under a temp HOME (no vault, no tokens).
# Exit 0 = all pass. VE_BIN=/path/to/copy tests a copy before installing it; a copy whose lib/ lacks the ~/bin/lib
# modules (the CI overlay, whose lib/ holds only core + inbox) runs from a temp dir whose lib/ links ~/bin/lib with
# the copy's own lib entries (core, inbox) on top.
set -uo pipefail
cd "$(dirname "$0")" || exit 2
T=""
if [ -n "${VE_BIN:-}" ]; then
  src_lib="$(dirname "$VE_BIN")/lib"
  if [ ! -e "$src_lib/assistant_profile.py" ]; then
    real_lib="$(getent passwd "$(id -u)" | cut -d: -f6)/bin/lib"
    T=$(mktemp -d "${TMPDIR:-/tmp}/vault-events-run.XXXXXX")
    mkdir "$T/lib"
    for e in "$real_lib"/*; do
      n=$(basename "$e")
      [ "$n" = __pycache__ ] && continue
      if [ -e "$src_lib/$n" ]; then ln -s "$(readlink -f "$src_lib/$n")" "$T/lib/$n"; else ln -s "$e" "$T/lib/$n"; fi
    done
    for e in "$src_lib"/*; do
      n=$(basename "$e")
      [ -e "$T/lib/$n" ] || ln -s "$(readlink -f "$e")" "$T/lib/$n"
    done
    cp "$VE_BIN" "$T/vault-events"
    chmod 0755 "$T/vault-events"
    export VE_BIN="$T/vault-events"
  fi
fi
python3 -I test_vault_events.py
rc=$?
[ -n "$T" ] && rm -rf "${T:?}"
exit $rc
