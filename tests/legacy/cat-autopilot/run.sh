#!/bin/sh
# Offline tests for ~/bin/cat-autopilot (folded from ~/opt/cat-autopilot-tests). CAT_AUTOPILOT_BIN=<copy> tests a copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 1
# cat-autopilot runs cat-supplies from its own dir; the CI overlay holds only the copy under test, so that copy runs from a temp
# bin beside a link to the live cat-supplies (and the overlay's lib/).
if [ -n "${CAT_AUTOPILOT_BIN:-}" ] && [ ! -e "$(dirname "$CAT_AUTOPILOT_BIN")/cat-supplies" ]; then
  T=$(mktemp -d) || exit 1
  trap 'rm -rf "$T"' EXIT
  cp -p "$CAT_AUTOPILOT_BIN" "$T/cat-autopilot"
  [ -e "$(dirname "$CAT_AUTOPILOT_BIN")/lib" ] && ln -s "$(dirname "$CAT_AUTOPILOT_BIN")/lib" "$T/lib"
  ln -s "$HOME/bin/cat-supplies" "$T/cat-supplies"
  export CAT_AUTOPILOT_BIN="$T/cat-autopilot"
fi
python3 test_cat_autopilot.py
