#!/bin/sh
# phonegw tests: pytest, offline, loopback only. The monorepo entry is ../run-tests.sh (PHONEGW_PYTHON, PHONEGW_PROTOCOL).
exec sh "$(cd "$(dirname "$0")/.." && pwd)/run-tests.sh" "$@"
