#!/usr/bin/env bash
# Tests for brain-backup-verify (brain-backup --verify) on FAKE trees in a temp dir, then the backup half of
# brain-backup via ./backup-run.sh. Default: the repo's scripts/ copies; BRAIN_BACKUP_VERIFY_BIN=<path> /
# BRAIN_BACKUP_BIN=<path> test other copies. Exit 0 = all pass.
# Never touches Unraid or the real ~/inbox (BRAIN_BACKUP_LIVE / BRAIN_INBOX point into the temp dir).
set -uo pipefail
V=${BRAIN_BACKUP_VERIFY_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/brain-backup-verify}
T=$(mktemp -d "${TMPDIR:-/tmp}/bbv-tests.XXXXXX"); trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; }
export BRAIN_INBOX=$T/inbox BRAIN_BACKUP_LIVE=$T/live
mem=.claude/projects/-home-node-workspace/memory

mk() {  # mk DIR: a minimal good tree
  local d=$1; rm -rf "$d"; mkdir -p "$d/workspace" "$d/bin" "$d/.claude/skills/s" "$d/.claude/agents" "$d/$mem"
  for f in CLAUDE TODO CHANGELOG; do echo "# $f" > "$d/workspace/$f.md"; done
  head -c 150000 /dev/urandom > "$d/workspace/blob.bin"
  printf '#!/bin/sh\necho hi\n' > "$d/bin/hello"; printf '#!/usr/bin/env python3\nprint(1)\n' > "$d/bin/py"; chmod +x "$d/bin/hello" "$d/bin/py"
  echo 'x = 1' > "$d/bin/_mod.py"
  echo '{}' > "$d/.claude/settings.json"; echo s > "$d/.claude/skills/s/SKILL.md"; echo a > "$d/.claude/agents/a.md"
  echo '- m' > "$d/$mem/MEMORY.md"
  printf '#!/bin/bash\nset -e\necho boot\n' > "$d/boot.sh"
  printf 'SHELL=/bin/bash\n# c\n*/5 * * * * /bin/true\n@reboot /bin/true\n' > "$d/crontab"
  git init -q --bare "$d/.brain.git"
  git --git-dir="$d/.brain.git" --work-tree="$d/workspace" -c user.name=t -c user.email=t@t add CLAUDE.md >/dev/null
  git --git-dir="$d/.brain.git" --work-tree="$d/workspace" -c user.name=t -c user.email=t@t commit -qm init
}
pack() { tar -czf "$2" -C "$1" .; }
run() { "$V" "$@" > "$T/out" 2>&1; }
expect() {  # expect DESC RC [grep-pattern]
  local d=$1 want=$2 rc=$3; shift 3
  if [ "$rc" = "$want" ] && { [ $# -eq 0 ] || grep -qE "$1" "$T/out"; }; then ok "$d"; else no "$d (rc=$rc)"; sed 's/^/    /' "$T/out" | grep -v '^    ok' | head -5; fi
}

mk "$T/live"; mk "$T/a"; pack "$T/a" "$T/good.tgz"
run "$T/good.tgz"; expect "good archive passes" 0 $? '^PASS'
grep -q 'bin: 2 scripts executable' "$T/out" && ok "non-shebang module skipped" || no "non-shebang module skipped"

mk "$T/a"; mkdir "$T/a/.secrets"; echo "MARKER$RANDOM$RANDOM" > "$T/a/.secrets/tok"; marker=$(cat "$T/a/.secrets/tok")
mkdir -p "$T/a/.claude/projects/p"; echo '{}' > "$T/a/.claude/projects/p/t.jsonl"; pack "$T/a" "$T/sec.tgz"
run "$T/sec.tgz"; expect "secrets/jsonl in archive fail" 1 $? 'FAIL - excluded files present \(3\)'
grep -q "$marker" "$T/out" && no "secret value printed" || ok "secret value not printed"

head -c 50000 "$T/good.tgz" > "$T/trunc.tgz"; head -c 120000 /dev/urandom >> "$T/trunc.tgz"
run "$T/trunc.tgz"; expect "corrupt archive fails" 1 $? 'corrupt'

mk "$T/a"; rm "$T/a/crontab"; pack "$T/a" "$T/nocron.tgz"
run "$T/nocron.tgz"; expect "missing crontab fails" 1 $? 'missing: crontab'

mk "$T/a"; chmod -x "$T/a/bin/hello"; pack "$T/a" "$T/noexec.tgz"
run "$T/noexec.tgz"; expect "non-executable script fails" 1 $? 'not executable: bin/hello'

mk "$T/a"; printf '#!/bin/bash\nif then fi (\n' > "$T/a/boot.sh"; printf '#!/usr/bin/env python3\ndef (:\n' > "$T/a/bin/py"; pack "$T/a" "$T/syn.tgz"
run "$T/syn.tgz"; expect "boot.sh + python syntax errors fail" 1 $? 'boot.sh syntax error'
grep -q 'python syntax: bin/py' "$T/out" && ok "python syntax reported" || no "python syntax reported"

mk "$T/a"; echo '* * * /bin/true' >> "$T/a/crontab"; pack "$T/a" "$T/cron.tgz"
run "$T/cron.tgz"; expect "malformed crontab line fails" 1 $? 'crontab: 1 malformed'

mk "$T/a"; rm -rf "$T/a/.brain.git"; pack "$T/a" "$T/nogit.tgz"
run "$T/nogit.tgz"; expect "missing .brain.git fails" 1 $? 'brain.git not usable'

mk "$T/a"; pack "$T/a" "$T/g2.tgz"; for i in 1 2 3 4 5; do echo "$i" > "$T/live/.claude/skills/s/f$i"; done
run "$T/g2.tgz"; expect "far fewer files than live fails" 1 $? 'skills: archive 1 files.*under half'
rm -f "$T"/live/.claude/skills/s/f*

run "$T/good.tgz" --cron; rc=$?
[ $rc = 0 ] && [ ! -s "$T/out" ] && [ -z "$(ls -A "$BRAIN_INBOX" 2>/dev/null)" ] && ok "--cron silent on success" || no "--cron silent on success"
run "$T/sec.tgz" --cron; rc=$?
n=$(ls "$BRAIN_INBOX" 2>/dev/null | grep -c '^brain-backup-verify-failed-')
[ $rc = 1 ] && [ "$n" = 1 ] && [ ! -s "$T/out" ] && ok "--cron writes one inbox note on failure" || no "--cron writes one inbox note on failure"
grep -qr "$marker" "$BRAIN_INBOX" && no "inbox note leaks secret" || ok "inbox note has no secret value"

run "../evil" ; expect "bad archive name rejected" 1 $? 'bad archive name'
[ -z "$(ls -d "${TMPDIR:-/tmp}"/brain-verify.* 2>/dev/null)" ] && ok "temp dirs cleaned up" || no "temp dirs cleaned up"

# The backup half (upload, rotation, KEEP, verbs, archive contents, tar status): backup-run.sh, folded into this total.
# (no apostrophe inside "${VAR:-...}": bash reads it as an opening quote there, and the file stops parsing)
echo "# backup half: backup-run.sh (BRAIN_BACKUP_BIN=${BRAIN_BACKUP_BIN:-scripts/brain-backup in the repo})"
bo=$(bash "$(dirname "$0")/backup-run.sh" 2>&1)
printf '%s\n' "$bo" | grep -E '^(FAIL|       got)'
read -r bp bf <<< "$(printf '%s\n' "$bo" | tail -1 | sed -n 's/^\([0-9]*\) passed, \([0-9]*\) failed$/\1 \2/p')"
if [ -n "${bp:-}" ]; then pass=$((pass + bp)); fail=$((fail + bf)); [ "$bf" = 0 ] && t="ok  " || t=FAIL; echo "$t - backup-run.sh: $bp passed, $bf failed"
else no "backup-run.sh: no summary line"; fi

echo "$pass passed, $fail failed"; [ $fail -eq 0 ]
