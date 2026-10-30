#!/bin/sh
# Offline tests for ~/bin/vision (synthetic images drawn at run time). VISION_BIN=/copy to test a copy.
cd "$(dirname "$0")" && exec timeout 300 python3 -I test_vision.py "$@"
