#!/usr/bin/env bash
# Suite for ~/bin/assistant (deploy / land / rollback / CI tool of the harness repo ~/opt/assistant).
# ASSISTANT_BIN=<path> tests another copy (`assistant ci` passes its staged bin overlay + ASSISTANT_CI_TREE).
# Never touches the real HOME: part 1 runs in a temp HOME with ASSISTANT_TEST=1 (the tool refuses the real one).
#   1. end-to-end with the binary under test: fake repo -> deploy -> second deploy (bytes + mode + core change)
#      -> live edit -> drift reported, deploy refused -> rollback --force restores bytes, mode and core link
#      -> smoke failure rolls back -> cron add/rm through a stub installer.
#   2. nightly only (ASSISTANT_CI unset): `assistant ci --offline` on the repo checkout (ruff, pyright, pytest 3.11 +
#      3.13 under netguard, AST ban, schemas, ratchet, inbox-lint, secretscan); legacy + precheck skipped (recursion; the
#      codebase-precheck suite covers precheck); never stamps.
set -uo pipefail
B=${ASSISTANT_BIN:-$HOME/bin/assistant}
R=$(cd "$(dirname "$0")/../assistant" && pwd)
SRC=${ASSISTANT_CI_TREE:-$R}
T=$(mktemp -d "${TMPDIR:-/tmp}/assistant-tests.XXXXXX"); trap 'rm -rf "${T:?}"' EXIT
pass=0; fail=0
ok() { if eval "$1"; then pass=$((pass + 1)); echo "ok   - $2"; else fail=$((fail + 1)); echo "FAIL - $2"; fi; }

(
export HOME=$T/home ASSISTANT_TEST=1 ASSISTANT_REPO=$T/repo ASSISTANT_LOCK_WAIT=60 GIT_CONFIG_GLOBAL=/dev/null \
       GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
unset ASSISTANT_NOW ASSISTANT_BRAIN_COMMIT_BIN
export ASSISTANT_CRONTAB_BIN=$T/crontab-stub
printf '#!/bin/sh\necho "$1" >> %s/cron.log\n' "$T" > "$ASSISTANT_CRONTAB_BIN"; chmod +x "$ASSISTANT_CRONTAB_BIN"
mkdir -p "$HOME" "$T/repo/tools" "$T/repo/ops/deploy"
cp -r "$SRC/core" "$T/repo/core"; find "$T/repo/core" -name __pycache__ -prune -exec rm -rf {} +
cat > "$T/repo/ops/deploy/manifest.toml" <<'TOML'
[core]
src = "core"
dest = "bin/lib/core"
smoke = "python3 -I -c \"import sys; sys.path.insert(0, sys.argv[1]); import core.paths, core.clock\" {lib}"

[[file]]
src = "tools/hello"
dest = "bin/hello"
mode = "0755"
smoke = "{dest}"
TOML
printf '#!/bin/sh\necho v1\n' > "$T/repo/tools/hello"
g() { git -C "$T/repo" "$@" >/dev/null 2>&1; }
g init -b main; g add -A; g commit -m v1
H=$HOME/bin/hello
"$B" deploy > "$T/out" 2>&1; rc=$?
ok '[ $rc = 0 ] && grep -q "^# managed: ~/opt/assistant" <<<"$(sed -n 2p "$H")" && [ "$("$H")" = v1 ]' "deploy installs with the managed header (rc $rc)"
ok '[ -L "$HOME/bin/lib/core" ] && [ -f "$HOME/bin/lib/core/clock.py" ]' "core installed behind the release symlink"
v1=$(sha256sum < "$H"); l1=$(readlink "$HOME/bin/lib/core")
printf '#!/bin/sh\necho v2\n' > "$T/repo/tools/hello"; sed -i 's/mode = "0755"/mode = "0700"/' "$T/repo/ops/deploy/manifest.toml"
echo 'Z = 1' > "$T/repo/core/zz.py"; g add -A; g commit -m v2
"$B" deploy > "$T/out" 2>&1; rc=$?
ok '[ $rc = 0 ] && [ "$(stat -c %a "$H")" = 700 ] && [ "$(readlink "$HOME/bin/lib/core")" != "$l1" ]' "second deploy changes bytes, mode and core (rc $rc)"
echo '# live edit' >> "$H"
"$B" drift > "$T/out" 2>&1; rc=$?
ok '[ $rc = 3 ] && grep -q "bin/hello: modified" "$T/out"' "live edit shows as drift (rc $rc)"
printf '#!/bin/sh\necho v3\n' > "$T/repo/tools/hello"; g add -A; g commit -m v3
"$B" deploy > "$T/out" 2>&1; rc=$?
ok '[ $rc = 3 ] && grep -q "# live edit" "$H"' "deploy refused on drift, live file untouched (rc $rc)"
"$B" rollback --force > "$T/out" 2>&1; rc=$?
ok '[ $rc = 0 ] && [ "$(sha256sum < "$H")" = "$v1" ] && [ "$(stat -c %a "$H")" = 755 ] && [ "$(readlink "$HOME/bin/lib/core")" = "$l1" ]' "rollback restores bytes, mode and core link (rc $rc)"
"$B" deploy --force > /dev/null 2>&1
printf '#!/bin/sh\nexit 1\n' > "$T/repo/tools/hello"; g add -A; g commit -m broken
before=$(sha256sum < "$H")
"$B" deploy > "$T/out" 2>&1; rc=$?
ok '[ $rc = 4 ] && [ "$(sha256sum < "$H")" = "$before" ] && grep -q "rolled back" "$T/out"' "smoke failure rolls back (rc $rc)"
"$B" status > "$T/out" 2>&1; rc=$?
ok '[ $rc = 0 ] && grep -q "drift: none" "$T/out"' "status clean after the auto rollback (rc $rc)"
printf 'SHELL=/bin/bash\n*/5 * * * * /bin/true\n' > "$HOME/crontab"
"$B" cron add demo '0 4 * * *' '/bin/true' > /dev/null 2>&1 && grep -q 'assistant:demo' "$HOME/crontab" && "$B" cron rm demo > /dev/null 2>&1
ok '[ "$(cat "$HOME/crontab")" = "$(printf "SHELL=/bin/bash\n*/5 * * * * /bin/true")" ] && [ "$(wc -l < "$T/cron.log")" = 2 ]' "cron add + rm round trip, installer called twice"
HOME=/home/node "$B" status > "$T/out" 2>&1; rc=$?
ok '[ $rc = 3 ] && grep -q "real home" "$T/out"' "ASSISTANT_TEST=1 refuses the real home (rc $rc)"
echo "pass=$pass fail=$fail" > "$T/part1"
)
eval "$(tail -1 "$T/part1" 2>/dev/null || echo 'pass=0 fail=1')"

if [ -z "${ASSISTANT_CI:-}" ]; then
  out=$("$B" ci --path "$R" --offline --skip legacy,precheck --no-stamp 2>&1); rc=$?
  ok '[ $rc = 0 ]' "nightly: assistant ci on the repo (rc $rc)"
  [ $rc = 0 ] || printf '%s\n' "$out" | grep -E '^FAIL' | sed 's/^/    /'
fi
echo "assistant tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
