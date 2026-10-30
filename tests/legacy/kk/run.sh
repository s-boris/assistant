#!/bin/bash
# Tests for ~/bin/kk-compare. Exit 0 = pass. No network: fixture + the cached BAG data (if present).
# KK_PDF_CANTON=<canton>: also cross-check every adult and young-adult premium of that canton in the cached CSV
# against the BAG premium guide ~/.cache/kk/ref/wegweiser_<canton>-2027.pdf (needs pdftotext).
set -u
cd "$(dirname "$0")" || exit 1
rc=0
timeout 300 python3 -I test_kk.py || rc=1

C=${KK_PDF_CANTON:-}
PDF="$HOME/.cache/kk/ref/wegweiser_${C}-2027.pdf"
if [ -n "$C" ] && [ -f "$HOME/.cache/kk/praemien_ch.csv" ] && [ -f "$PDF" ] && command -v pdftotext >/dev/null; then
  for age in adult young; do
    KK_OFFLINE=1 timeout 300 python3 -I verify_pdf.py "$PDF" "$C" --age "$age" || rc=1
  done
else
  echo "skip PDF cross-check (set KK_PDF_CANTON; needs the cached data, the guide PDF and pdftotext)"
fi
[ $rc -eq 0 ] && echo "kk-tests: PASS" || echo "kk-tests: FAIL"
exit $rc
