#!/bin/sh
# Offline tests for the Inspect runner of ~/bin/evals (lib/evals/inspect_task.py): the end-to-end part of the evals suite.
d="$(dirname "$(readlink -f "$0")")"
t="$d/../evals/test_evals_inspect.py"; [ -f "$t" ] || t="$d/../evals-tests/test_evals_inspect.py"  # repo: tests/legacy/evals
exec /usr/bin/python3 -I "$t"
