#!/usr/bin/env bash
# Tests for ~/bin/lib/botlib.py (BOTLIB_PATH=/copy/botlib.py tests a copy). Exit 0 = all pass. Temp dirs only;
# fake tg/unraid-ssh scripts; the shim must delegate to the harness core next to it.
exec python3 -I "$(dirname "$0")/test_botlib.py" "$@"
