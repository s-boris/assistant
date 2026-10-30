#!/bin/sh
# self-report tests (stub traces, fixture workspace). Usage: run.sh [path to self-report script]
here=$(dirname "$(readlink -f "$0")")
SELF_REPORT_BIN="${1:-${SELF_REPORT_BIN:-$(cd "$here/../../../scripts" && pwd)/self-report}}" exec python3 -I "$here/test_self_report.py" -v
