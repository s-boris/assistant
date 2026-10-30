#!/usr/bin/env bash
# Offline tests for ~/bin/brain-commit, ONLY its codebase-sync trigger/skip logic (brain itself: ~/opt/brain-tests).
# brain-commit runs "$d/brain" commit "$@" and, on success, "$d/codebase-sync" --bg, where d = the directory of its own
# resolved path (readlink -f). That is the seam: the script under test is COPIED into $T/bin next to a recording fake
# brain and codebase-sync, so neither the real brain repo nor GitHub can be reached. The suite refuses a copy that names
# /home/node, ~/bin or $HOME, or calls brain/codebase-sync other than via "$d/..."; PATH shims for brain and
# codebase-sync refuse too (exit 99). Exit 0 = all pass. BRAIN_COMMIT_BIN=<path> tests another copy.
# Covers: success -> brain commit "$@" (quoting kept) + codebase-sync --bg once, output passthrough, codebase-sync
# output discarded and its failure ignored; brain fails -> no sync + its rc; BRAIN_GIT_DIR / BRAIN_WORK_TREE set ->
# no sync (empty = unset); codebase-sync missing or not executable -> skipped; called via a symlink; isolation.
set -uo pipefail
SRC=${BRAIN_COMMIT_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/brain-commit}
grep -qE '/home/node|~/bin|\$HOME' "$SRC" && { echo "refusing: $SRC names a real path (it could run the real brain/codebase-sync)"; exit 2; }
grep -q '"$d/brain" commit' "$SRC" && grep -q '"$d/codebase-sync" --bg' "$SRC" \
  || { echo "refusing: $SRC does not call brain/codebase-sync via \"\$d/...\" (the sibling seam)"; exit 2; }
REAL_BRAIN=/home/node/.brain.git REAL_CS_STATE=/home/node/.local/state/codebase-sync
real_state() { echo "$(git --git-dir="$REAL_BRAIN" rev-parse HEAD 2>/dev/null) $(find "$REAL_CS_STATE" -newer "$T/start" 2>/dev/null | wc -l)"; }
T=$(mktemp -d "${TMPDIR:-/tmp}/brain-commit-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
touch "$T/start"; real_before=$(real_state)
export T FAKE=$T/fake HOME=$T/home
unset BRAIN_GIT_DIR BRAIN_WORK_TREE
mkdir -p "$T/bin" "$T/shims" "$FAKE" "$HOME" "$T/elsewhere"
B=$T/bin/brain-commit
cp "$SRC" "$B"; chmod 755 "$B"

# ---- fakes (siblings) and refusing PATH shims
printf '#!/bin/bash\n(printf "%%s|" "$#" "$@"; echo) >> "$FAKE/brain.log"\necho "brain: committed abc1234 $2"\n[ -s "$FAKE/brain-err" ] && cat "$FAKE/brain-err" >&2\nexit $(cat "$FAKE/brain-rc" 2>/dev/null || echo 0)\n' > "$T/bin/brain"
printf '#!/bin/bash\n(printf "%%s|" "$#" "$@"; echo) >> "$FAKE/sync.log"\nif [ "$1" = precheck ]; then echo "  bin/x:3 rule#9"; echo "  noise line"; echo "codebase-sync precheck: WOULD BLOCK (1 hit(s))" >&2; exit $(cat "$FAKE/pre-rc" 2>/dev/null || echo 0); fi\necho "codebase-sync: started in the background"; echo "sync noise" >&2\nexit $(cat "$FAKE/sync-rc" 2>/dev/null || echo 0)\n' > "$T/bin/codebase-sync"
chmod 755 "$T/bin/brain" "$T/bin/codebase-sync"
for s in brain codebase-sync; do printf '#!/bin/bash\necho "PATH shim %s: refusing $*" | tee -a "%s/refused.log" >&2; exit 99\n' "$s" "$T" > "$T/shims/$s"; chmod +x "$T/shims/$s"; done
export PATH=$T/shims:$PATH

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
run() { (cd "$T/elsewhere" && "$@") > "$T/out" 2> "$T/err"; rc=$?; out=$(cat "$T/out"); err=$(cat "$T/err"); }
f() { cat "$FAKE/$1" 2>/dev/null; }
reset() { rm -f "$FAKE"/*; chmod 755 "$T/bin/codebase-sync" 2>/dev/null; }

echo "# success: brain commit with the args as given, then codebase-sync --bg once"
reset; run "$B" "before X: it's \"quoted\"" second
eq "rc 0" "$rc" "0"
eq "brain gets commit + both args unchanged" "$(f brain.log)" "3|commit|before X: it's \"quoted\"|second|"
eq "codebase-sync --bg, once" "$(f sync.log)" "1|--bg|"
eq "brain's stdout passes through, sync's is discarded" "$out" "brain: committed abc1234 before X: it's \"quoted\""
eq "sync's stderr discarded" "$err" ""
reset; run "$B"; eq "no args: brain commit only, still syncs" "$(f brain.log)|$(f sync.log)" "1|commit||1|--bg|"

echo "# brain fails (secret blocked, nothing to commit, ...): no sync, its rc"
for code in 1 2 3; do
  reset; echo "$code" > "$FAKE/brain-rc"; echo "brain: secretscan blocked the commit" > "$FAKE/brain-err"; run "$B" "msg"
  eq "brain rc $code -> rc $code, no sync" "$rc|$(f sync.log)" "$code|"
done
eq "brain's stderr passes through" "$err" "brain: secretscan blocked the commit"

echo "# test runs (BRAIN_GIT_DIR / BRAIN_WORK_TREE set): no sync"
reset; BRAIN_GIT_DIR=$T/g run "$B" "t"; eq "BRAIN_GIT_DIR set: rc 0, no sync" "$rc|$(f sync.log)" "0|"
reset; BRAIN_WORK_TREE=$T/w run "$B" "t"; eq "BRAIN_WORK_TREE set: rc 0, no sync" "$rc|$(f sync.log)" "0|"
reset; BRAIN_GIT_DIR=$T/g BRAIN_WORK_TREE=$T/w run "$B" "t"; eq "both set: no sync" "$(f sync.log)" ""
reset; BRAIN_GIT_DIR= BRAIN_WORK_TREE= run "$B" "t"; eq "both empty = unset: syncs" "$(f sync.log)" "1|--bg|"
eq "brain still ran in every case" "$(grep -c . "$FAKE/brain.log")" "1"

echo "# codebase-sync missing / not executable / failing"
reset; chmod 644 "$T/bin/codebase-sync"; run "$B" "m"; eq "not executable: rc 0, skipped, no error" "$rc|$(f sync.log)|$err" "0||"
reset; mv "$T/bin/codebase-sync" "$T/cs.away"; run "$B" "m"; eq "missing: rc 0, no error" "$rc|$err" "0|"
mv "$T/cs.away" "$T/bin/codebase-sync"
reset; echo 5 > "$FAKE/sync-rc"; run "$B" "m"; eq "sync fails: rc stays brain's 0, quiet" "$rc|$out|$err" "0|brain: committed abc1234 m|"
reset; echo 5 > "$FAKE/sync-rc"; echo 4 > "$FAKE/brain-rc"; run "$B" "m"; eq "brain 4: rc 4, sync not run" "$rc|$(f sync.log)" "4|"

echo "# BRAIN_COMMIT_PRECHECK=1: codebase-sync precheck first, warn only"
reset; BRAIN_COMMIT_PRECHECK=1 run "$B" "m"; eq "precheck clean: precheck then --bg, quiet, rc 0" "$rc|$(f sync.log)|$err" "0|1|precheck|
1|--bg||"
reset; echo 3 > "$FAKE/pre-rc"; BRAIN_COMMIT_PRECHECK=1 run "$B" "m"
eq "precheck blocks: rc stays 0, sync still started" "$rc|$(f sync.log)" "0|1|precheck|
1|--bg|"
case $err in *"WARNING: the next codebase-sync would block (precheck exit 3)"*"  bin/x:3 rule#9"*) ok "precheck block: warning + file:line rule label on stderr" ;; *) no "precheck block warning" "$err" ;; esac
case $err in *"noise line"*) no "only hit lines are passed on" "$err" ;; *) ok "only hit lines are passed on" ;; esac
reset; echo 3 > "$FAKE/pre-rc"; echo 2 > "$FAKE/brain-rc"; BRAIN_COMMIT_PRECHECK=1 run "$B" "m"; eq "brain fails: no precheck" "$rc|$(f sync.log)" "2|"
reset; echo 3 > "$FAKE/pre-rc"; run "$B" "m"; eq "unset: no precheck (off by default)" "$(f sync.log)|$err" "1|--bg||"
reset; BRAIN_COMMIT_PRECHECK=1 BRAIN_GIT_DIR=$T/g run "$B" "m"; eq "test runs: no precheck" "$(f sync.log)" ""

echo "# called through a symlink: siblings of the resolved file"
mkdir -p "$T/link"; ln -s "$B" "$T/link/brain-commit"
reset; run "$T/link/brain-commit" "via link"
eq "rc 0, brain + sync from \$T/bin" "$rc|$(f brain.log)|$(f sync.log)" "0|2|commit|via link||1|--bg|"

echo "# isolation"
eq "no PATH shim was used (never brain/codebase-sync from PATH)" "$(cat "$T/refused.log" 2>/dev/null)" ""
eq "real brain repo HEAD + codebase-sync state untouched" "$(real_state)" "$real_before"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
