#!/usr/bin/env bash
# Tests for ~/bin/secretscan and ~/bin/brain. Exit 0 = all pass.
# Uses FAKE secrets only, generated at run time (this file contains no literal secret-shaped strings),
# in a temp dir: a fake sources dir (SECRETSCAN_SOURCES), a plain temp git repo with the hook, and a
# temp brain repo (BRAIN_GIT_DIR / BRAIN_WORK_TREE / BRAIN_INBOX). Section D only reads the real repo.
set -uo pipefail
BIN=${BRAIN_TEST_DIR:-/home/node/bin}
SCAN="python3 -I $BIN/secretscan.py"
T=$(mktemp -d "${TMPDIR:-/tmp}/brain-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; }
check() { local d=$1; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else no "$d"; fi; }
rand() { LC_ALL=C tr -dc "${2:-A-Za-z0-9}" </dev/urandom | head -c "$1"; }

# ---------------------------------------------------------------- fake secrets
FAKE_API="fk$(rand 30)"
FAKE_TG="$(rand 1 1-9)$(rand 8 0-9):AA$(rand 33)"          # in sources
FAKE_TG2="$(rand 1 1-9)$(rand 9 0-9):AA$(rand 33)"         # NOT in sources -> pattern only
FAKE_CARD="9$(rand 15 0-9)"                                 # IIN 9: value match only, never the card pattern
FAKE_ACCESS="fakeaccess$(rand 40)"
FAKE_PW="pw$(rand 14)"
LUHN="4111 1111 1111"; LUHN="$LUHN 1111"                    # the standard Luhn test number
LUHN_BAD="4111111111111"; LUHN_BAD="${LUHN_BAD}112"
PEMH="-----BEGIN OPENSSH PRIVATE"; PEMH="$PEMH KEY-----"
PEMF="-----END OPENSSH PRIVATE"; PEMF="$PEMF KEY-----"
GHP="ghp""_$(rand 36)"
ANT="sk-ant""-api03-$(rand 40)"
SSH_L2=$(rand 70); SSH_L3=$(rand 70)

S=$T/src
TGD=channels/telegram   # the bot-token dir; joined at runtime so no fixture line names its .env file
mkdir -p "$S/.secrets/sub" "$S/.claude/$TGD" "$S/.ssh"
printf '{"svc": {"url": "http://203.0.113.1:8000", "token": "%s"}, "user": "bob"}\n' "$FAKE_API" > "$S/.secrets/fake.json"
printf 'Fake card\nnumber=%s\ncvv=123\nexpiry=01/30\n' "$(echo "$FAKE_CARD" | sed 's/..../& /g; s/ $//')" > "$S/.secrets/card.txt"
printf 'DB_PASSWORD="%s"\n' "$FAKE_PW" > "$S/.secrets/sub/db.env"
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$FAKE_TG" > "$S/.claude/$TGD/.env"
printf '{"claudeAiOauth": {"accessToken": "%s", "scopes": ["user:inference"], "rateLimitTier": "default_x_20x"}}\n' "$FAKE_ACCESS" > "$S/.claude/.credentials.json"
printf '%s\n%s\n%s\n%s\n%s\n' "$PEMH" "$(rand 70)" "$SSH_L2" "$SSH_L3" "$PEMF" > "$S/.ssh/id_ed25519"
export SECRETSCAN_SOURCES=$S

echo "# A. secretscan"
src=$($SCAN --sources 2>&1)
for n in secrets/fake.json:svc.token secrets/card.txt:number secrets/sub/db.env:DB_PASSWORD \
         $TGD/.env:TELEGRAM_BOT_TOKEN claude-credentials:claudeAiOauth.accessToken \
         ssh/id_ed25519:line2 ssh/id_ed25519:line3; do
  check "--sources loads $n" grep -qxF "$n" <<<"$src"
done
check "--sources skips non-secrets (url, user, cvv, scopes, tier)" bash -c '! grep -Eq "url|user$|cvv|expiry|scopes|rateLimitTier" <<<"$1"' _ "$src"
check "--sources never prints values" bash -c '! grep -qF -e "$1" -e "$2" -e "$3" <<<"$4"' _ "$FAKE_API" "$FAKE_TG" "$FAKE_PW" "$src"

f=$T/leak.txt
printf 'a %s\nb %s\nc x%sx\nd %s\ne %s\nf %s\n' "$FAKE_API" "$FAKE_TG" "$FAKE_PW" "$(echo "$FAKE_CARD" | sed 's/..../&-/g; s/-$//')" "$FAKE_ACCESS" "$SSH_L3" > "$f"
out=$($SCAN --file "$f" 2>&1); rc=$?
check "value hits -> exit 1" test $rc -eq 1
for n in secrets/fake.json:svc.token $TGD/.env:TELEGRAM_BOT_TOKEN secrets/sub/db.env:DB_PASSWORD \
         secrets/card.txt:number claude-credentials:claudeAiOauth.accessToken ssh/id_ed25519:line3; do
  check "value detected: $n" grep -qF "value:$n" <<<"$out"
done
check "card value matched across a different separator format (line 4)" grep -qF "leak.txt:4: value:secrets/card.txt:number" <<<"$out"
check "output never contains a secret value" bash -c '! grep -qF -e "$1" -e "$2" -e "$3" -e "$4" -e "$5" <<<"$6"' _ "$FAKE_API" "$FAKE_TG" "$FAKE_PW" "$FAKE_ACCESS" "$SSH_L3" "$out"

pat() { printf '%s\n' "$1" | $SCAN --stdin 2>&1; }
check "Luhn test number (spaced) -> pattern:card-number" grep -qF pattern:card-number <<<"$(pat "card $LUHN ok")"
check "Luhn test number (contiguous) -> pattern:card-number" grep -qF pattern:card-number <<<"$(pat "${LUHN// /}")"
check "non-Luhn 16 digits -> no hit" bash -c '! grep -q card <<<"$1"' _ "$(pat "$LUHN_BAD")"
check "fake Telegram token (not in sources) -> pattern:telegram-bot-token" grep -qF pattern:telegram-bot-token <<<"$(pat "token $FAKE_TG2")"
check "PEM private key header -> pattern:private-key-pem" grep -qF pattern:private-key-pem <<<"$(pat "$PEMH")"
check "GitHub token -> pattern:github-token" grep -qF pattern:github-token <<<"$(pat "$GHP")"
check "Anthropic key -> pattern:anthropic-key" grep -qF pattern:anthropic-key <<<"$(pat "$ANT")"
check "pattern output has names only" bash -c '! grep -qF -e "$1" -e "$2" -e "$3" <<<"$4"' _ "$FAKE_TG2" "$GHP" "$ANT" "$(pat "$FAKE_TG2 $GHP $ANT")"
clean="ts 1760000000000 and 20261010123456, chat -1001234567890, ip 198.51.100.5, v2.39.5,
iban CH93 0076 2011 6238 5295 7, phone +41 79 123 45 67, sha 3f786850e387550fdab836ed7e6dc881de23001b,
bak settings.json.bak-20261009-152156, scopes user:inference, tier default_x_20x, url http://203.0.113.1:8000"
check "clean realistic text -> exit 0" bash -c 'printf "%s\n" "$1" | '"$SCAN"' --stdin' _ "$clean"
check "secretscan:allow suppresses a PATTERN hit" bash -c 'printf "%s secretscan:allow\n" "$1" | '"$SCAN"' --stdin' _ "$LUHN"
check "secretscan:allow never suppresses a VALUE hit" bash -c '! printf "%s secretscan:allow\n" "$1" | '"$SCAN"' --stdin' _ "$FAKE_API"
red=$(printf 'k=%s t=%s c=%s\n' "$FAKE_API" "$FAKE_TG2" "$LUHN" | $SCAN --redact)
check "--redact removes values and tokens" bash -c '! grep -qF -e "$1" -e "$2" -e "4111" <<<"$3" && [ "$(grep -o "\[redacted\]" <<<"$3" | wc -l)" -eq 3 ]' _ "$FAKE_API" "$FAKE_TG2" "$red"
mod=$(python3 -I -B -c 'import sys; sys.path.insert(0, "'"$BIN"'"); import secretscan as s; print(",".join(s.scan(sys.argv[1])))' "x $FAKE_API $LUHN")
check "module scan() returns names only" test "$mod" = "value:secrets/fake.json:svc.token,pattern:card-number"

echo "# B. temp git repo with the pre-commit/commit-msg hooks"
R=$T/repo; git init -q "$R"; git -C "$R" config user.email t@t; git -C "$R" config user.name t
printf '#!/bin/sh\nexec %s --scan-staged\n' "$SCAN" > "$R/.git/hooks/pre-commit"
printf '#!/bin/sh\nexec %s --file "$1"\n' "$SCAN" > "$R/.git/hooks/commit-msg"
chmod +x "$R/.git/hooks/pre-commit" "$R/.git/hooks/commit-msg"
echo "hello" > "$R/ok.txt"; git -C "$R" add ok.txt
check "clean commit succeeds" git -C "$R" commit -q -m "clean"
printf 'config\napi_token = %s\n' "$FAKE_API" > "$R/planted.txt"; git -C "$R" add planted.txt
out=$(git -C "$R" commit -q -m "planted" 2>&1); rc=$?
check "planted fake secret: commit refused" test $rc -ne 0
check "planted fake secret: no commit created" test "$(git -C "$R" rev-list --count HEAD)" -eq 1
check "refusal names the hit, not the value" bash -c 'grep -qF "planted.txt:2: value:secrets/fake.json:svc.token" <<<"$1" && ! grep -qF "$2" <<<"$1"' _ "$out" "$FAKE_API"
git -C "$R" reset -q; rm "$R/planted.txt"
echo "x" > "$R/b.txt"; git -C "$R" add b.txt
check "secret in commit message: refused" bash -c '! git -C "$1" commit -q -m "note $2"' _ "$R" "$FAKE_TG2"

echo "# C. brain end-to-end (temp git dir + temp work tree)"
H=$T/home
export BRAIN_GIT_DIR=$T/brain.git BRAIN_WORK_TREE=$H BRAIN_INBOX=$T/inbox
mkdir -p "$H/workspace/sub" "$H/bin/__pycache__" "$H/.claude/skills/x" "$H/.claude/skills/synced/y" "$H/.claude/agents" \
  "$H/.claude/projects/-home-node-workspace/memory" "$H/.claude/channels/telegram" "$H/.config/transcribe" "$H/.config/other" \
  "$H/.secrets" "$H/.ssh" "$H/plugins/p" "$H/inbox"
for p in workspace/CLAUDE.md workspace/TODO.md bin/tool .claude/skills/x/SKILL.md .claude/agents/a.md .claude/settings.json \
         .claude/projects/-home-node-workspace/memory/MEMORY.md .config/transcribe/vocab.txt crontab boot.sh; do echo "v1 $p" > "$H/$p"; done
for p in workspace/notes.txt workspace/sub/x.md bin/__pycache__/tool.pyc .claude/skills/synced/y/SKILL.md .claude/.credentials.json \
         .claude/history.jsonl .claude/projects/-home-node-workspace/s.jsonl .claude/$TGD/.env .config/other/c \
         .secrets/k.json .ssh/id_ed25519 plugins/p/f .claude.json; do echo "excluded $p" > "$H/$p"; done
check "brain init" "$BIN/brain" init
check "initial commit" "$BIN/brain" commit "initial"
want="$(printf '%s\n' .claude/agents/a.md .claude/projects/-home-node-workspace/memory/MEMORY.md .claude/settings.json \
  .claude/skills/x/SKILL.md .config/transcribe/vocab.txt bin/tool boot.sh crontab workspace/CLAUDE.md workspace/TODO.md)"
check "tracked files == allowlist exactly" test "$("$BIN/brain" files)" = "$want"
check "no secrets/transcripts/.env tracked" bash -c '! "$1" files | grep -E "(^\.secrets|^\.ssh|\.env$|credentials|\.jsonl$|^plugins|synced)"' _ "$BIN/brain"
check "second commit is a no-op" bash -c '"$1" commit x | grep -q "nothing changed"' _ "$BIN/brain"
echo "leak $FAKE_API" > "$H/workspace/NOTES.md"
out=$("$BIN/brain" commit "with secret" 2>&1); rc=$?
check "brain commit with planted secret -> exit 3 (blocked)" test $rc -eq 3
check "blocked: HEAD unchanged" test "$("$BIN/brain" git rev-list --count HEAD)" -eq 1
check "blocked: output has names only" bash -c 'grep -qF "workspace/NOTES.md:1: value:secrets/fake.json:svc.token" <<<"$1" && ! grep -qF "$2" <<<"$1"' _ "$out" "$FAKE_API"
check "blocked: secret never written to the object store" bash -c '! "$1" git cat-file --batch-all-objects --batch | grep -qaF "$2"' _ "$BIN/brain" "$FAKE_API"
"$BIN/brain" auto; rc=$?
check "brain auto blocked -> exit 3" test $rc -eq 3
"$BIN/brain" auto
check "brain auto blocked -> exactly one inbox note (deduped)" test "$(ls "$T/inbox" | wc -l)" -eq 1
check "inbox note has names, not the value" bash -c 'grep -qF "value:secrets/fake.json:svc.token" "$1"/* && ! grep -qF "$2" "$1"/*' _ "$T/inbox" "$FAKE_API"
"$BIN/brain" git add -A
check "hook backstop: raw git commit (skipping brain's pre-scan) refused" bash -c '! "$1" git commit -q -m raw' _ "$BIN/brain"
"$BIN/brain" git reset -q; "$BIN/brain" git prune --expire=now
check "hook backstop: secret pruned after unstage" bash -c '! "$1" git cat-file --batch-all-objects --batch | grep -qaF "$2"' _ "$BIN/brain" "$FAKE_API"
echo "fixed" > "$H/workspace/NOTES.md"; echo "v2" >> "$H/workspace/CLAUDE.md"
"$BIN/brain" auto; rc=$?
check "brain auto commits once clean (silent, exit 0)" test $rc -eq 0
check "auto message lists files" bash -c '"$1" log -1 --format=%s | grep -qE "^auto: .*(CLAUDE|NOTES)\.md"' _ "$BIN/brain"
check "block state cleared after success" test ! -e "$BRAIN_GIT_DIR/brain-blocked"
echo "BROKEN" > "$H/workspace/CLAUDE.md"
check "brain restore PATH HEAD~1 (from inside workspace, relative path)" bash -c 'cd "$1/workspace" && "$2" restore CLAUDE.md HEAD~1' _ "$H" "$BIN/brain"
check "restore: content is v1 again" test "$(cat "$H/workspace/CLAUDE.md")" = "v1 workspace/CLAUDE.md"
check "restore: broken state kept in a pre-restore snapshot" bash -c '"$1" log --format=%s -n 2 | grep -q "^pre-restore snapshot" && "$1" git show HEAD~1:workspace/CLAUDE.md | grep -q BROKEN' _ "$BIN/brain"
check "brain log works (initial, auto, pre-restore, restore)" bash -c '[ "$("$1" log | wc -l)" -eq 4 ]' _ "$BIN/brain"
echo "v3" >> "$H/bin/tool"
check "brain diff shows uncommitted change" bash -c '"$1" diff | grep -q "^+v3"' _ "$BIN/brain"
check "brain status shows it" bash -c '"$1" status | grep -q "bin/tool"' _ "$BIN/brain"
unset BRAIN_GIT_DIR BRAIN_WORK_TREE BRAIN_INBOX SECRETSCAN_SOURCES

echo "# D. real brain repo (read-only checks)"
if [ -d "$HOME/.brain.git" ]; then
  check "real repo: has commits (brain log)" bash -c '[ "$("$1" log | wc -l)" -ge 1 ]' _ "$BIN/brain"
  check "real repo: no forbidden paths tracked" bash -c '! "$1" files | grep -E "(^\.secrets|^\.ssh|\.env$|credentials|\.jsonl$|^plugins/|channels/)"' _ "$BIN/brain"
  check "real repo: hooks installed" test -x "$HOME/.brain.git/hooks/pre-commit" -a -x "$HOME/.brain.git/hooks/commit-msg"
  # tracked files still on disk: one deleted between brain commits (a land or rewrite removing it) made secretscan exit 2
  # on the missing path and the check red (30.10, main 9ce2d4e); its committed blob was scanned by the pre-commit hook
  check "real repo: HEAD tree scans clean with the real secrets" bash -c 'cd "$HOME" && "$1" files -z |
    while IFS= read -r -d "" f; do [ -e "$f" ] && printf "%s\0" "$f"; done | xargs -0 -r '"$SCAN"' -q --file' _ "$BIN/brain"
else
  echo "skip - no ~/.brain.git yet"
fi

echo "# $pass passed, $fail failed"
[ "$fail" -eq 0 ]
