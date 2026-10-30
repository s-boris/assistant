#!/bin/sh
# The relay's own suite, with its own venv (the monorepo's pytest config does not apply: pytest.ini here).
#   adapters/voice/run-tests.sh [pytest args]     VOICE_RELAY_PYTHON = the venv python (default ~/opt/voice-venv, from requirements.lock)
set -eu
D=$(cd "$(dirname "$0")" && pwd)
export VOICE_RELAY_PYTHON="${VOICE_RELAY_PYTHON:-$HOME/opt/voice-venv/bin/python}"
cd "$D" && exec "$VOICE_RELAY_PYTHON" -m pytest -q -p no:cacheprovider "$@"
