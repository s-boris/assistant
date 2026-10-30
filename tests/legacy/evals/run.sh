#!/bin/sh
# Offline tests for ~/bin/evals + ~/bin/lib/evals (no model calls): the legacy engine, then the Inspect runner
# (end to end on a fake worker when the evals venv exists). EVALS_BIN=/copy tests a copy.
d="$(dirname "$(readlink -f "$0")")"
/usr/bin/python3 -I "$d/test_evals.py" || exit 1
exec /usr/bin/python3 -I "$d/test_evals_inspect.py"
