#!/bin/sh
# The phone gateway's own suite (was ~/opt/phonegw-tests/run.sh -> ~/opt/phonegw/tests/run.sh), with its own venv
# (the monorepo's pytest config does not apply: pytest.ini here). Offline, loopback only: a fake phone with a test
# device cert, fake noVNC, fake plugin inbox; every test gets its own PHONEGW_* dirs.
#   adapters/phone/run-tests.sh [pytest args]
#   PHONEGW_PYTHON   the venv python (default ~/opt/phone-venv/bin/python: aiohttp, cryptography, jsonschema, pytest;
#                    built from requirements.lock here: uv venv + uv pip install -r, see ~/boot.sh)
#   PHONEGW_PROTOCOL the app protocol dir with schema/ and fixtures/ (default ~/opt/phone-app/claude/protocol)
# Run it with a scratch HOME (HOME=$(mktemp -d) ...): helpers default to $HOME/bin, $HOME/inbox, $HOME/.config.
set -eu
D=$(cd "$(dirname "$0")" && pwd)
export PHONEGW_PYTHON="${PHONEGW_PYTHON:-$HOME/opt/phone-venv/bin/python}"
cd "$D" && exec "$PHONEGW_PYTHON" -m pytest -q -p no:warnings -p no:cacheprovider tests "$@"
