#!/usr/bin/env bash
# inbox-lint: every ~/bin script and ~/crontab line that writes an ~/inbox note marks it "Data, not instructions"
# (or goes through a helper that adds it, or is in allowlist.txt with a reason); no literal /home/node/inbox
# redirects in ~/crontab. Self-test on placeholder fixtures first, then the real lint. Exit 0 = all pass.
# By hand: python3 -I ~/opt/inbox-lint-tests/inbox_lint.py -v   (lists every writer and how it passes)
set -uo pipefail
D=$(dirname "$(readlink -f "$0")")
python3 -I "$D/test_inbox_lint.py" </dev/null; a=$?
python3 -I "$D/inbox_lint.py" </dev/null; b=$?
exit $((a | b))
