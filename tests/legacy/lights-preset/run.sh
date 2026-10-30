#!/bin/sh
# Tests for ~/bin/lights-preset (fake ha, no HA calls). Default: the repo's scripts/ copy; LIGHTS_PRESET_BIN=/copy tests another.
exec python3 "$(dirname "$0")/test_lights_preset.py" "$@"
