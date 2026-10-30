#!/bin/sh
# context tests: ~/opt/context-tests/run.sh
exec python3 "$(dirname "$0")/test_context.py" "$@"
