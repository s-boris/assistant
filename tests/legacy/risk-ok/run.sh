#!/usr/bin/env bash
# Offline tests for ~/bin/risk-ok (the CLI of the risk-check PreToolUse hook, ~/.claude/hooks/risk_check.py):
# (1) the CLI here: help/usage errors, `check` dry runs (LOW/MEDIUM/HIGH/HANG, args or stdin, stable hashes, nothing
#     written), `list` (fresh/expired/corrupt codes), `revoke`, `log [N]`, and a HASH with no OK text being refused.
#     No code is ever issued here (the hook's own tests cover issuing in a temp dir).
# (2) the hook's unit tests: ~/.claude/hooks/test_risk_check.py (ratings, owner-OK flow, fail-open, prefilter).
# Hermetic: temp HOME, RISK_CHECK_STATE/RISK_CHECK_LOG in a temp dir, a placeholder profile (ASSISTANT_PROFILE,
# 192.0.2.x / example.org), --no-llm everywhere; tg/curl/ssh/docker/vault/unraid-ssh/llm-local shims refuse and record.
# RISK_OK_BIN=<path> runs only (1) against another copy of the CLI (a script taking risk_check.py's arguments).
set -uo pipefail
B=${RISK_OK_BIN:-/home/node/bin/risk-ok}
REAL_LOG=/home/node/.local/state/risk-check.log
MARK=risk-ok-tests-marker
real_marks() { grep -c "$MARK" "$REAL_LOG" 2>/dev/null || true; }
real_before=$(real_marks)
T=$(mktemp -d "${TMPDIR:-/tmp}/risk-ok-tests.XXXXXX")
trap 'chmod -R u+w "${T:?}" 2>/dev/null; rm -rf "${T:?}"' EXIT
export HOME=$T/home FAKE=$T/fake
mkdir -p "$T/shims" "$FAKE" "$HOME"
mk() { printf '#!/bin/bash\n%s\n' "$2" > "$T/shims/$1"; chmod +x "$T/shims/$1"; }
for c in tg curl ssh docker vault unraid-ssh llm-local; do mk "$c" "echo \"$c \$*\" >> \"\$FAKE/refused.log\"; echo \"$c shim: refusing\" >&2; exit 99"; done
export PATH=$T/shims:$PATH
export RISK_CHECK_STATE=$T/state RISK_CHECK_LOG=$T/risk-check.log ASSISTANT_PROFILE=$T/profile.json
printf '%s\n' '{"owner": {"github": "example-user"}, "network": {"server_name": "Testsrv", "server_host": "192.0.2.10",
 "lan_prefix": "192.0.2.", "domain": "example.org", "hosts": {"web_vm": "web@192.0.2.20"}}}' > "$ASSISTANT_PROFILE"

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
match() { if [[ "$2" =~ $3 ]]; then ok "$1"; else no "$1" "$2"; fi; }
run() { out=$(timeout 30 "$B" "$@" 2>"$T/err" </dev/null); rc=$?; err=$(cat "$T/err"); }
chk() { run check --no-llm "$1"; }

echo "# help and usage errors"
run; eq "no arguments: usage, exit 0" "$rc" 0; has "no arguments: usage text" "$out" "risk-ok check [--no-llm]"
for h in help -h --help; do run "$h"; eq "$h: exit 0" "$rc" 0; done
run frobnicate
eq "unknown verb: exit 2" "$rc" 2; eq "unknown verb: nothing on stdout" "$out" ""; has "unknown verb: usage on stderr" "$err" "risk-ok revoke HASH"
run revoke not-a-hash; eq "revoke with a malformed hash: exit 2 (usage)" "$rc" 2
run revoke; eq "revoke without a hash: exit 2 (usage)" "$rc" 2

echo "# check: dry-run ratings"
chk 'ls -la /tmp'
match "a harmless command: LOW, no rule, a 10-hex hash" "$out" '^LOW  rule=-  hash=[0-9a-f]{10}$'; eq "check exits 0" "$rc" 0
chk "rm -rf /home/node/.secrets/$MARK"
match "deleting credentials: HIGH secrets-delete" "$out" '^HIGH  rule=secrets-delete  hash=[0-9a-f]{10}'
has "HIGH comes with a why line" "$out" "why: rm -r deletes credentials"
chk 'git push --force origin main'; match "force-push to main: HIGH" "$out" '^HIGH  rule=git-force-main'
chk 'crontab -r'; match "wiping the crontab: HIGH" "$out" '^HIGH  rule=crontab-wipe'
chk 'docker rm -f web'; match "removing a container: MEDIUM" "$out" '^MEDIUM  rule=docker-remove'
chk 'rm -f $T/*'
has "an rm on a variable glob: HANG, denied whatever the level" "$out" "HANG (denied whatever the level)  rule=rm-hang-var"
chk 'rm -f "${T:?}"/x'; hasnt "the safe \${T:?} form: no HANG" "$out" "HANG"
out=$(printf '%s' 'crontab -r' | timeout 30 "$B" check --no-llm 2>&1); has "check reads the command from stdin" "$out" "rule=crontab-wipe"
chk 'ls -la /tmp'; h1=${out##*hash=}; chk 'ls -la /tmp'; h2=${out##*hash=}; chk 'ls -la /var'; h3=${out##*hash=}
eq "the same command: the same hash" "$h1" "$h2"
if [ "$h1" != "$h3" ]; then ok "another command: another hash"; else no "another command: another hash" "$h3"; fi
eq "check is a dry run: no state dir, no log" "$(ls -d "$RISK_CHECK_STATE" "$RISK_CHECK_LOG" 2>/dev/null)" ""

echo "# list and revoke"
run list; eq "list with no state dir: no pending codes" "$out|$rc" "no pending codes|0"
mkdir -p "$RISK_CHECK_STATE"
now=$(date +%s)
printf '{"code": "abc123", "reason": "owner said yes (placeholder)", "created": %s}\n' "$((now - 60))" > "$RISK_CHECK_STATE/ok-aaaaaaaaaa"
printf '{"code": "def456", "reason": "older OK", "created": %s}\n' "$((now - 3600))" > "$RISK_CHECK_STATE/ok-bbbbbbbbbb"
printf '{not json' > "$RISK_CHECK_STATE/ok-cccccccccc"
printf 'x' > "$RISK_CHECK_STATE/other-file"
run list
eq "list: two readable codes, the corrupt one and other files skipped" "$(wc -l <<<"$out")" 2
match "list: a fresh code shows its minutes left and the OK text" "$out" 'aaaaaaaaaa  (28|29) min left  ok=.*owner said yes'
has "list: an old code shows as expired" "$out" "bbbbbbbbbb  expired"
hasnt "list never prints the code itself" "$out" "abc123"
run revoke dddddddddd; eq "revoke an unknown hash" "$out|$rc" "no pending code for dddddddddd|0"
run revoke aaaaaaaaaa; eq "revoke a pending code" "$out|$rc" "revoked aaaaaaaaaa|0"
eq "revoke removes its file" "$(ls "$RISK_CHECK_STATE")" "$(printf 'ok-bbbbbbbbbb\nok-cccccccccc\nother-file')"

echo "# log"
run log; eq "log with no log yet" "$out|$rc" "no log yet|0"
for i in $(seq 1 30); do echo "2031-06-15 14:00:$(printf %02d "$i") HIGH rule=x line $i"; done > "$RISK_CHECK_LOG"
run log; eq "log: default 20 lines" "$(wc -l <<<"$out")" 20
run log 3; eq "log 3: the last 3" "$out" "$(tail -3 "$RISK_CHECK_LOG")"
run log abc; eq "log with a non-number: the default 20" "$(wc -l <<<"$out")" 20
run log 0; eq "log 0 prints nothing (not the whole log)" "$out" ""

echo "# an OK without the owner's words is refused (nothing issued)"
run eeeeeeeeee; eq "HASH without OK text: exit 2" "$rc" 2; has "HASH without OK text: says what is missing" "$err" "say what the owner OK'd"
run eeeeeeeeee "ok"; eq "HASH with a 2-char OK text: exit 2" "$rc" 2
eq "nothing was issued" "$(ls "$RISK_CHECK_STATE" | grep -c '^ok-eeeeeeeeee')" 0
hasnt "no OK-ISSUED line in the log" "$(cat "$RISK_CHECK_LOG")" "OK-ISSUED"

echo "# hermetic"
eq "no tg/curl/ssh/docker/vault/unraid-ssh/llm-local calls" "$(cat "$FAKE/refused.log" 2>/dev/null)" ""
eq "the real risk-check log got none of these commands" "$(real_marks)" "$real_before"

if [ -z "${RISK_OK_BIN:-}" ]; then
  echo "# hook unit tests: ~/.claude/hooks/test_risk_check.py"
  if r=$(cd /home/node/.claude/hooks && timeout 200 python3 -I test_risk_check.py 2>&1 </dev/null); then
    ok "hook unit tests pass ($(grep -oE '^Ran [0-9]+ tests' <<<"$r"))"
  else no "hook unit tests" "$(tail -25 <<<"$r")"; fi
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
