#!/bin/sh
# securefill end-to-end tests: local test site + fake vault (real personal.py sealing) + a separate headless Chrome
# on its own port and profile (the owner's headed Chrome is never touched). Fake data only. Exit 0 = all pass.
# Copies: SECUREFILL_CLI=<script> WEBAGENT_CLI=<script> WEBAGENT_LIB=<lib dir>.
exec python3 -W ignore "$(dirname "$0")/test_securefill.py" "$@"
