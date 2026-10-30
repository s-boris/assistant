#!/bin/sh
# Offline tests for ~/bin/arr-mismatch (folded from ~/opt/arr-mismatch-tests). ARR_MISMATCH_BIN=<copy> tests a copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 1
# arr-mismatch runs arr-unblock from its own dir; the CI overlay holds only the copy under test, so that copy runs from a temp
# bin beside a link to the live arr-unblock (and the overlay's lib/).
if [ -n "${ARR_MISMATCH_BIN:-}" ] && [ ! -e "$(dirname "$ARR_MISMATCH_BIN")/arr-unblock" ]; then
  T=$(mktemp -d) || exit 1
  trap 'rm -rf "$T"' EXIT
  cp -p "$ARR_MISMATCH_BIN" "$T/arr-mismatch"
  [ -e "$(dirname "$ARR_MISMATCH_BIN")/lib" ] && ln -s "$(dirname "$ARR_MISMATCH_BIN")/lib" "$T/lib"
  ln -s "$HOME/bin/arr-unblock" "$T/arr-unblock"
  export ARR_MISMATCH_BIN="$T/arr-mismatch"
fi
python3 test_arr_mismatch.py && python3 test_arr_mismatch_drops.py && python3 test_arr_mismatch_grabs.py
