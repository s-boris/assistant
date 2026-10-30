#!/bin/sh
# Real tools boot.sh needs (install, chmod, mkdir, mktemp, rm, ln, timeout): logged, and refused for any absolute
# path outside the test dir (mktemp: TMPDIR must be inside it).
n=${0##*/}
printf '%s\n' "real $n $*" >> "$BT_LOG"
for a; do case $a in
  /*) case $a in "$BT_ROOT"/*) ;; *) echo "OUTSIDE $n $a" >> "$BT_VIOL"; exit 1;; esac;;
esac; done
if [ "$n" = mktemp ]; then case ${TMPDIR:-/tmp} in "$BT_ROOT"/*) ;; *) echo "OUTSIDE mktemp TMPDIR=${TMPDIR:-/tmp}" >> "$BT_VIOL"; exit 1;; esac; fi
exec "$BT_REALBIN/$n" "$@"
