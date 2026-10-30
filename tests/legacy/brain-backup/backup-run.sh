#!/usr/bin/env bash
# Offline tests for the BACKUP half of ~/bin/brain-backup (the verify half is ./run.sh, brain-backup-verify).
# Exit 0 = all pass. HOME is a fake tree in a temp dir. The upload never leaves the box:
#   root mode  -> an `ssh` PATH shim runs the remote command locally with /mnt/user/backup/claude-bot rewritten to a
#                 temp folder (so the .part/mv dance and the 14-day `find -mtime +KEEP -delete` rotation really run);
#                 it refuses any command that would still touch /mnt.
#   verbs mode -> a fake unraid-ssh (UNRAID_SSH_BIN) records `run backup-store NAME` and keeps the stdin it got.
# Covers --to, root upload + rotation + KEEP, verbs/emulate upload, --list both ways, --verify passthrough, failures
# (inbox note, exit 1), what goes into the archive and what never does (secrets, transcripts, private context files),
# the age secrets escrow (only with recipients; plaintext never in the archive; owner key decrypts, others cannot).
# BRAIN_BACKUP_BIN=<path> tests another copy.
set -uo pipefail
B=${BRAIN_BACKUP_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/brain-backup}
REAL_INBOX=/home/node/inbox
T=$(mktemp -d "${TMPDIR:-/tmp}/brain-backup-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
export FAKE=$T/fake HOME=$T/home UNRAID_SSH_BIN=$T/shims/fake-unraid-ssh TMPDIR=$T/tmp TG_BIND_BIN=$T/shims/fake-tg-bind
unset KEEP
REAL_TAR=$(command -v tar)
mkdir -p "$T/shims" "$FAKE/dest" "$HOME/inbox" "$TMPDIR"
real_notes() { ls "$REAL_INBOX" 2>/dev/null | grep -c '^brain-backup-failed-'; }
real_before=$(real_notes)
TODAY=$(date +%F); NAME=claude-bot-$TODAY.tar.gz

# ---- fake HOME: every included path, plus things that must never be in the archive
w() { mkdir -p "$(dirname "$HOME/$1")"; echo "${2:-x}" > "$HOME/$1"; }
mem=.claude/projects/-home-node-workspace/memory
TGD=.claude/channels/telegram   # joined at runtime so no fixture line names the bot-token file
for f in workspace/CLAUDE.md bin/tool .config/app/conf.json .brain.git/HEAD reports/r.md .local/state/s.json \
         .local/state/context/today.md crontab boot.sh .claude/settings.json .claude/skills/s/SKILL.md \
         .claude/agents/a.md $mem/MEMORY.md plugins/p/index.js opt/foo-tests/run.sh opt/dash/index.html \
         opt/unraid-lockdown/server/x opt/assistant/core/paths.py; do w "$f"; done
NEVER=(.secrets/token workspace/session.jsonl plugins/p/node_modules/m.js bin/__pycache__/x.pyc .config/app/.env
       .config/app/prod.env .config/app/.credentials.json .claude/channels/telegram/access.json
       ".config/chrome/Crash Reports/r" .config/chrome/c.dmp .local/state/context/telegram.md
       .local/state/context/signals.md .local/state/context/signals.json .local/state/context/location.json
       .local/state/recall/index.db .local/state/vault-events/seen.json Downloads/f.txt opt/other/f
       .claude/projects/-home-node-workspace/abc.jsonl .claude/projects/-home-node-workspace/notes.txt
       $TGD/.env .claude/.credentials.json .local/state/assistant/events.db .local/state/assistant/events.db-wal
       opt/assistant/.venv/bin/python opt/assistant/.venv-3.13/bin/python)
for f in "${NEVER[@]}"; do w "$f" SECRET-ISH; done

# ---- shims
mk() { printf '#!/bin/bash\n%s\n' "$2" > "$T/shims/$1"; chmod +x "$T/shims/$1"; }
mk fake-tg-bind '[ "$1" = export ] && [ ! -e "$FAKE/tgbind-broken" ] || { echo "tg-bind: events store: broken" >&2; exit 1; }
printf "%s" "{\"rows\": [{\"kind\": \"command\", \"key\": \"party\", \"tool\": \"tg-cmd.party\", \"why\": \"test\"}]}"'
mk ssh 'echo "ssh $*" >> "$FAKE/calls.log"
[ -e "$FAKE/ssh-broken" ] && { cat > /dev/null; echo "ssh: connect to host unraid: No route to host" >&2; exit 255; }
[ "$1" = unraid ] || { echo "ssh shim: unexpected host $1" >&2; exit 99; }
cmd=${!#}; cmd=${cmd//\/mnt\/user\/backup\/claude-bot/$FAKE/dest}
case $cmd in *"/mnt"*) echo "ssh shim: refusing a command that still touches /mnt" >&2; exit 98 ;; esac
exec sh -c "$cmd"'
mk fake-unraid-ssh 'echo "us $*" >> "$FAKE/calls.log"
[ -e "$FAKE/us-mode-broken" ] && exit 255
case $1 in
  mode) cat "$FAKE/mode" 2>/dev/null || echo root ;;
  run) shift
       case $1 in
         backup-store) cat > "$FAKE/store.$2"; [ -e "$FAKE/us-broken" ] && { echo "claude-bot-cmd: failed" >&2; exit 1; }
                       echo "stored $2" ;;
         backup-list) echo "-rw-r--r-- 1 root root 5.0M claude-bot-2026-01-01.tar.gz" ;;
         *) echo "claude-bot-cmd: rejected: unknown verb" >&2; exit 1 ;;
       esac ;;
  *) exit 2 ;;
esac'
mk tar "[ -e \"\$FAKE/tar-fatal\" ] && { echo 'tar: fatal' >&2; exit 2; }
$REAL_TAR \"\$@\"; rc=\$?
[ -e \"\$FAKE/tar-changed\" ] && exit 1; exit \$rc"
mkdir -p "$HOME/bin"; printf '#!/bin/bash\necho "verify $*" >> "$FAKE/calls.log"; exit ${VERIFY_RC:-0}\n' > "$HOME/bin/brain-backup-verify"
chmod +x "$HOME/bin/brain-backup-verify"
export PATH=$T/shims:$PATH

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
reset() { rm -rf "$FAKE"; mkdir -p "$FAKE/dest"; rm -f "$HOME"/inbox/*; }
notes() { ls "$HOME/inbox" | grep -c '^brain-backup-failed-[0-9]*\.md$'; }
calls() { cat "$FAKE/calls.log" 2>/dev/null; }
age() { touch -d "@$(( $(date +%s) - $1 ))" "$2"; }   # age SECONDS FILE
D=86400; H=3600

echo "# --to FILE (local, no upload)"
reset; mkdir -p "$T/cwd"
out=$(cd "$T/cwd" && "$B" --to out.tgz 2>&1); rc=$?
eq "--to: rc 0, silent" "$rc:$out" "0:"
eq "--to: relative path resolved against the caller's cwd" "$([ -s "$T/cwd/out.tgz" ] && echo yes)" "yes"
eq "--to: no ssh, no upload (only the mode probe at most)" "$(calls | grep -vc '^us mode')" "0"
eq "--to: never asks the lockdown mode" "$(calls | grep -c '^us mode')" "0"
list=$(tar -tzf "$T/cwd/out.tgz" 2>&1); tr=$?
eq "--to: a valid gzip tarball" "$tr" "0"
missing=
for f in workspace/CLAUDE.md bin/tool .config/app/conf.json .brain.git/HEAD reports/r.md .local/state/s.json \
         .local/state/context/today.md crontab boot.sh .claude/settings.json .claude/skills/s/SKILL.md \
         .claude/agents/a.md $mem/MEMORY.md plugins/p/index.js opt/foo-tests/run.sh opt/dash/index.html \
         opt/unraid-lockdown/server/x opt/assistant/core/paths.py .local/state/assistant/tg-bind-export.json; do
  grep -qx "$f" <<< "$list" || missing="$missing $f"
done
eq "archive: every included path is there" "$missing" ""
leaked=
for f in "${NEVER[@]}"; do grep -qxF "$f" <<< "$list" && leaked="$leaked [$f]"; done
eq "archive: no secrets, transcripts, private context, recall, other dirs" "$leaked" ""
eq "archive: relative paths (restores into ~)" "$(grep -c '^/' <<< "$list")" "0"
eq "archive: nothing from the shims or temp dirs" "$(grep -c 'fake\|shims\|cwd' <<< "$list")" "0"
out=$("$B" --to "$T/nodir/sub/out.tgz" 2>/dev/null); rc=$?
eq "--to unwritable target: rc 1" "$rc" "1"
eq "--to failure: no inbox note (it's a test path)" "$(notes)" "0"
"$B" --to > /dev/null 2>&1; rc=$?
eq "--to without FILE: rc 1" "$rc" "1"
eq "--to without FILE: nothing uploaded" "$(calls | grep -c '^ssh\|run backup-store')" "0"

echo "# secrets escrow (age, to the owner's public key only)"
AGEB=${AGE_TEST_BIN:-$(type -P age)}; AGEK=${AGE_KEYGEN_TEST_BIN:-$(type -P age-keygen)}
if [ -z "$AGEB$AGEK" ]; then   # no age here (the CI runner): fake_age.py stands in, for the wiring checks
  FA=$(cd "$(dirname "$0")" && pwd)/fake_age.py; mkdir -p "$T/agebin"
  printf '#!/bin/sh\nexec python3 -I "%s" age "$@"\n' "$FA" > "$T/agebin/age"
  printf '#!/bin/sh\nexec python3 -I "%s" keygen "$@"\n' "$FA" > "$T/agebin/age-keygen"
  chmod +x "$T/agebin/age" "$T/agebin/age-keygen"; AGEB=$T/agebin/age AGEK=$T/agebin/age-keygen
  echo "# (age stand-in: fake_age.py)"
fi
if [ -x "$AGEB" ] && [ -x "$AGEK" ]; then
  export AGE_BIN=$AGEB
  w .ssh/id_ed25519 SSH-KEY; w .ssh/id_ed25519.pub pub; w .config/gh/hosts.yml GH-TOKEN
  w .secrets/payment-card.txt CARD; w .secrets/retired/old-token OLD
  reset; "$B" --to "$T/e0.tgz" >/dev/null 2>&1; l0=$(tar -tzf "$T/e0.tgz")
  eq "no recipients: no escrow member, no plain ssh key / gh login" "$(grep -c -e escrow -e '^.ssh/' -e '^.config/gh/hosts.yml$' <<< "$l0")" "0"
  "$AGEK" -o "$T/owner.key" 2>/dev/null; mkdir -p "$HOME/.config/escrow"; "$AGEK" -y "$T/owner.key" > "$HOME/.config/escrow/recipients"
  reset; out=$("$B" --to "$T/e1.tgz" 2>&1); rc=$?; l1=$(tar -tzf "$T/e1.tgz")
  eq "recipients: rc 0, silent" "$rc:$out" "0:"
  eq "escrow member in the archive" "$(grep -cx '.local/state/escrow/secrets.tar.gz.age' <<< "$l1")" "1"
  eq "plaintext stays out: .secrets, ssh key, gh login" "$(grep -c -e '^.secrets' -e '^.ssh/' -e '^.config/gh/hosts.yml$' <<< "$l1")" "0"
  eq "no .part or temp file left" "$(ls -A "$HOME/.local/state/escrow" | tr '\n' ' ')|$(ls -A "$TMPDIR" | grep -c esc)" "secrets.tar.gz.age |0"
  mkdir -p "$T/x1"; tar -xzf "$T/e1.tgz" -C "$T/x1" .local/state/escrow/secrets.tar.gz.age
  eq "the member is age data" "$(head -c 21 "$T/x1/.local/state/escrow/secrets.tar.gz.age")" "age-encryption.org/v1"
  inner=$("$AGEB" -d -i "$T/owner.key" "$T/x1/.local/state/escrow/secrets.tar.gz.age" | tar -tzf - | sort | tr '\n' ' ')
  for f in .secrets/token .ssh/id_ed25519 .ssh/id_ed25519.pub .config/gh/hosts.yml; do has "escrow holds $f (owner key decrypts)" "$inner" "$f "; done
  for f in payment-card.txt retired .credentials.json telegram; do hasnt "escrow never holds $f" "$inner" "$f"; done
  "$AGEK" -o "$T/other.key" 2>/dev/null
  "$AGEB" -d -i "$T/other.key" "$T/x1/.local/state/escrow/secrets.tar.gz.age" >/dev/null 2>&1; eq "another key cannot decrypt" "$?" "1"
  reset; out=$(AGE_BIN=false "$B" 2>&1); rc=$?
  eq "encryption fails: rc 1, inbox note, nothing uploaded" "$rc|$(notes)|$(calls | grep -c 'backup-store\|^ssh')" "1|1|0"
  has "note says why" "$(cat "$HOME"/inbox/brain-backup-failed-*.md)" "secrets escrow: encryption failed"
  rm "$HOME/.config/escrow/recipients"; reset; "$B" --to "$T/e2.tgz" >/dev/null 2>&1
  eq "recipients removed: stale escrow deleted, not in the archive" "$([ -e "$HOME/.local/state/escrow/secrets.tar.gz.age" ] && echo there || echo gone)|$(tar -tzf "$T/e2.tgz" | grep -c escrow/secrets)" "gone|0"
  rm -rf "$HOME/.ssh" "$HOME/.config/gh" "$HOME/.secrets/payment-card.txt" "$HOME/.secrets/retired"; unset AGE_BIN
else
  no "secrets escrow: age/age-keygen not found (set AGE_TEST_BIN/AGE_KEYGEN_TEST_BIN)"
fi

echo "# missing optional paths are skipped"
reset; mv "$HOME/opt" "$T/opt.away"
"$B" --to "$T/partial.tgz" 2>/dev/null; rc=$?
eq "partial tree: rc 0" "$rc" "0"
has "partial tree: still has the workspace" "$(tar -tzf "$T/partial.tgz")" "workspace/CLAUDE.md"
mv "$T/opt.away" "$HOME/opt"

echo "# tg-bind export (the ledger's bot commands; events.db itself never rides along)"
X=$HOME/.local/state/assistant/tg-bind-export.json
reset; rm -f "$X"; "$B" --to "$T/tb.tgz" 2>"$T/tb.err"; rc=$?
eq "export: rc 0" "$rc" "0"
eq "export: the file holds tg-bind's export" "$(cat "$X" 2>&1)" '{"rows": [{"kind": "command", "key": "party", "tool": "tg-cmd.party", "why": "test"}]}'
eq "export: mode 600" "$(stat -c %a "$X")" "600"
grep -qx .local/state/assistant/tg-bind-export.json <<< "$(tar -tzf "$T/tb.tgz")" && ok "export: in the archive (events.db* exclude spares it)" || no "export: in the archive"
eq "export: no .part left" "$(ls "$HOME/.local/state/assistant" | grep -c part)" "0"
reset; touch "$FAKE/tgbind-broken"; "$B" --to "$T/tb2.tgz" 2>"$T/tb.err"; rc=$?
eq "export fails: backup still rc 0" "$rc" "0"
has "export fails: says so on stderr" "$(cat "$T/tb.err")" "tg-bind export failed; the archive keeps the previous"
eq "export fails: the previous export kept" "$(cat "$X")" '{"rows": [{"kind": "command", "key": "party", "tool": "tg-cmd.party", "why": "test"}]}'
grep -qx .local/state/assistant/tg-bind-export.json <<< "$(tar -tzf "$T/tb2.tgz")" && ok "export fails: the previous one is archived" || no "export fails: previous archived"
rm -f "$FAKE/tgbind-broken"

echo "# root mode: upload over ssh"
reset
out=$("$B" 2>"$T/err"); rc=$?
eq "root: rc 0" "$rc" "0"
eq "root: no stderr" "$(cat "$T/err")" ""
eq "root: asked the lockdown mode first" "$(calls | sed -n 1p)" "us mode"
eq "root: exactly one ssh, to unraid" "$(calls | grep -c '^ssh unraid ')" "1"
eq "root: no verbs used" "$(calls | grep -c 'us run')" "0"
eq "root: archive stored under today's name" "$(ls -A "$FAKE/dest")" "$NAME"
eq "root: stored archive is a valid tarball with the workspace" \
   "$(tar -tzf "$FAKE/dest/$NAME" 2>/dev/null | grep -cx workspace/CLAUDE.md)" "1"
has "root: prints the stored file (cron log)" "$out" "$FAKE/dest/$NAME"
has "root: uploads via .part + mv (no half files on failure)" "$(calls)" "cat > /mnt/user/backup/claude-bot/.$NAME.part && mv"
eq "root: no inbox note" "$(notes)" "0"
"$B" > /dev/null 2>&1
eq "root: second run the same day replaces, still one file" "$(ls -A "$FAKE/dest")" "$NAME"

echo "# rotation (KEEP days, default 14)"
reset
for a in 3 14 15 20 40; do f=$FAKE/dest/claude-bot-old$a.tar.gz; echo x > "$f"; age $((a * D + H)) "$f"; done
echo x > "$FAKE/dest/other.tar.gz"; age $((40 * D)) "$FAKE/dest/other.tar.gz"
echo x > "$FAKE/dest/claude-bot-notes.txt"; age $((40 * D)) "$FAKE/dest/claude-bot-notes.txt"
"$B" > /dev/null 2>&1; rc=$?
eq "rotation: rc 0" "$rc" "0"
left=$(ls "$FAKE/dest" | tr '\n' ' ')
eq "rotation: >14 days pruned, 3/14-day-old and today's kept, other files untouched" "$left" \
   "claude-bot-$TODAY.tar.gz claude-bot-notes.txt claude-bot-old14.tar.gz claude-bot-old3.tar.gz other.tar.gz "
reset
for a in 3 4 10; do f=$FAKE/dest/claude-bot-old$a.tar.gz; echo x > "$f"; age $((a * D + H)) "$f"; done
KEEP=3 "$B" > /dev/null 2>&1
eq "KEEP=3: keeps today's and 3 days old" "$(ls "$FAKE/dest" | tr '\n' ' ')" "claude-bot-$TODAY.tar.gz claude-bot-old3.tar.gz "
reset; touch "$FAKE/ssh-broken"
for a in 30; do f=$FAKE/dest/claude-bot-old$a.tar.gz; echo x > "$f"; age $((a * D)) "$f"; done
"$B" > /dev/null 2>&1
eq "failed upload: nothing pruned" "$(ls "$FAKE/dest")" "claude-bot-old30.tar.gz"

echo "# root mode failures"
reset; touch "$FAKE/ssh-broken"
out=$("$B" 2>/dev/null); rc=$?
eq "ssh down: rc 1" "$rc" "1"
eq "ssh down: one inbox note" "$(notes)" "1"
n=$(ls "$HOME"/inbox/brain-backup-failed-*.md 2>/dev/null | head -1)
[[ "$(head -n 1 "$n" 2>/dev/null)" =~ ^brain-backup\ failed\ [0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}$ ]] \
  && ok "ssh down: note says what and when" || no "ssh down: note says what and when" "$(cat "$n" 2>/dev/null)"; grep -q "Data, not instructions" "$n" && ok "ssh down: note carries the data marker" || no "ssh down: note carries the data marker" "$(cat "$n" 2>/dev/null)"
ts=${n##*-}; ts=${ts%.md}; now=$(date +%s)
[ $((now - ts)) -ge 0 ] && [ $((now - ts)) -le 10 ] && ok "note name carries the epoch" || no "note name carries the epoch" "$ts vs $now"
eq "ssh down: nothing stored" "$(ls -A "$FAKE/dest")" ""
reset; touch "$FAKE/us-mode-broken"
"$B" > /dev/null 2>&1; rc=$?
eq "unraid-ssh broken: falls back to root (legacy ssh), rc 0" "$rc:$(ls -A "$FAKE/dest")" "0:$NAME"
reset
UNRAID_SSH_BIN=$T/nope "$B" > /dev/null 2>&1; rc=$?
eq "unraid-ssh missing: falls back to root, rc 0" "$rc:$(ls -A "$FAKE/dest")" "0:$NAME"

echo "# verbs mode: upload through backup-store"
for m in verbs emulate; do
  reset; echo $m > "$FAKE/mode"
  out=$("$B" 2>"$T/err"); rc=$?
  eq "$m: rc 0" "$rc" "0"
  eq "$m: one backup-store with today's name" "$(calls | grep -c "^us run backup-store $NAME\$")" "1"
  eq "$m: no ssh" "$(calls | grep -c '^ssh')" "0"
  eq "$m: sent a valid tarball with the workspace" "$(tar -tzf "$FAKE/store.$NAME" 2>/dev/null | grep -cx workspace/CLAUDE.md)" "1"
  has "$m: passes the dispatcher's answer through" "$out" "stored $NAME"
done
reset; echo verbs > "$FAKE/mode"; touch "$FAKE/us-broken"
"$B" > /dev/null 2>&1; rc=$?
eq "verbs failure: rc 1" "$rc" "1"
eq "verbs failure: one inbox note" "$(notes)" "1"
eq "verbs failure: no fallback to root ssh" "$(calls | grep -c '^ssh')" "0"
reset; echo verbs > "$FAKE/mode"
"$B" --to "$T/v.tgz" > /dev/null 2>&1
eq "verbs + --to: stays local" "$(calls | grep -c 'run backup-store\|^ssh')" "0"

echo "# default unraid-ssh is \$HOME/bin/unraid-ssh"
reset; echo verbs > "$FAKE/mode"; cp "$T/shims/fake-unraid-ssh" "$HOME/bin/unraid-ssh"
env -u UNRAID_SSH_BIN "$B" > /dev/null 2>&1
eq "default: uses \$HOME/bin/unraid-ssh" "$(calls | grep -c "^us run backup-store $NAME\$")" "1"
rm "$HOME/bin/unraid-ssh"

echo "# --list"
reset
out=$("$B" --list 2>&1); rc=$?
eq "--list root: rc 0" "$rc" "0"
eq "--list root: ssh ls -lh of the backup folder" "$(calls | grep '^ssh')" "ssh unraid ls -lh /mnt/user/backup/claude-bot"
eq "--list root: nothing stored" "$(ls -A "$FAKE/dest")" ""
reset; echo verbs > "$FAKE/mode"
out=$("$B" --list 2>&1); rc=$?
eq "--list verbs: rc 0, the dispatcher's listing" "$rc:$out" "0:-rw-r--r-- 1 root root 5.0M claude-bot-2026-01-01.tar.gz"
eq "--list verbs: backup-list, no ssh" "$(calls | grep -v '^us mode' | tr '\n' '|')" "us run backup-list|"

echo "# --verify passes through to brain-backup-verify"
reset
"$B" --verify "$T/some file.tgz" --cron > /dev/null 2>&1; rc=$?
eq "--verify: args passed as-is" "$(calls)" "verify $T/some file.tgz --cron"
eq "--verify: rc 0" "$rc" "0"
VERIFY_RC=3 "$B" --verify > /dev/null 2>&1; rc=$?
eq "--verify: exit code passed through" "$rc" "3"
eq "--verify: no upload, no mode probe" "$(calls | grep -c '^ssh\|^us')" "0"

echo "# tar's own exit code"
reset; touch "$FAKE/tar-changed"
out=$("$B" 2>&1); rc=$?
eq "tar exit 1 (file changed while read): still a success" "$rc:$(notes)" "0:0"
eq "tar exit 1: archive stored" "$(tar -tzf "$FAKE/dest/$NAME" 2>/dev/null | grep -cx workspace/CLAUDE.md)" "1"
reset; touch "$FAKE/tar-fatal"
"$B" > /dev/null 2>&1; rc=$?
eq "tar fatal (exit 2), root: rc 1" "$rc" "1"
eq "tar fatal, root: one inbox note" "$(notes)" "1"
has "tar fatal: the note says why" "$(cat "$HOME"/inbox/brain-backup-failed-*.md 2>/dev/null)" ": tar exit 2, archive incomplete"
reset; touch "$FAKE/tar-fatal"; echo verbs > "$FAKE/mode"
"$B" > /dev/null 2>&1; rc=$?
eq "tar fatal, verbs (dispatcher took it): rc 1 + note" "$rc:$(notes)" "1:1"
reset; touch "$FAKE/tar-fatal"
"$B" --to "$T/fatal.tgz" > /dev/null 2>&1; rc=$?
eq "tar fatal, --to: rc 1, no note" "$rc:$(notes)" "1:0"
eq "tar status temp files cleaned up" "$(ls -A "$TMPDIR" | grep -c brain-backup-tar)" "0"

echo "# isolation"
eq "no note in the real inbox" "$(real_notes)" "$real_before"
grep -q '^dest=/mnt/user/backup/claude-bot$' "$B" && ok "real destination unchanged (shim rewrites exactly this)" \
  || no "real destination unchanged (shim rewrites exactly this)"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
