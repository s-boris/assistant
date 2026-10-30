#!/bin/sh
# Offline tests for the usage store + product report (~/bin/usage, ~/bin/product, ~/bin/lib/usage.py, product.py),
# cron-fail's spool line, tg-cmd /usage and the usage PostToolUse hook. Fake fixtures in a temp dir. Exit 0 = all pass.
# Copies: USAGE_LIB=<lib dir> CRON_FAIL_BIN TGCMD_BIN USAGE_HOOK USAGE_CLI PRODUCT_CLI.
exec python3 "$(dirname "$0")/test_usage.py" "$@"
