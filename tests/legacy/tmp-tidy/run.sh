#!/usr/bin/env bash
# Tests for tmp-tidy (scripts/tmp-tidy, deployed to ~/bin) on a temp root (TMP_TIDY_ROOT) with a faked clock (TMP_TIDY_NOW = now + 10 days, so
# every untouched entry looks 10 days old; "fresh" ones get a future mtime). Covered: old files/dirs deleted with
# --apply only, dry run deletes nothing, fresh and 2.5-day-old entries kept, a fresh file deep in an old dir keeps
# the dir, claude* protected, entries a process holds (open fd, cwd) kept, dirs with a socket kept, a symlink is
# removed without touching its target, --days below 2 rejected, --quiet silent when nothing to do, --json.
# Session scratchpads (claude-<uid>/<project>/<session>/scratchpad/*): old entries deleted, keep-file names, recent
# entries, entries with a fresh file inside and held entries kept (age by mtime only); tasks/, files beside the scratchpad and symlinked
# session dirs untouched. Stale session dirs (claude-<uid>/<project>/<uuid>/ untouched 24 h: old and 30-h-old ones go; live by the
# own or another process's CLAUDE_CODE_SESSION_ID, fresh, held, non-uuid and symlinked ones stay) and pytest-of-<user>/pytest-N.
# Env: TMP_TIDY_BIN = the copy under test (default: the repo's scripts/tmp-tidy; CI stages it). Exit 0 = all pass.
set -uo pipefail
TT=${TMP_TIDY_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/tmp-tidy}
T=$(mktemp -d "${TMPDIR:-/tmp}/tmp-tidy-tests.XXXXXX")
PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; rm -rf "${T:?}"; }
trap cleanup EXIT
R=$T/root
NOW=$(date +%s)
export TMP_TIDY_ROOT=$R TMP_TIDY_NOW=$(( NOW + 10 * 86400 ))
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
ex() { if [ -e "$R/$1" ] || [ -L "$R/$1" ]; then echo yes; else echo no; fi; }
fresh() { touch -h -d "@$(( NOW + 10 * 86400 - ${2:-3600} ))" "$1"; }   # mtime = fake now minus $2 seconds

setup() {
  rm -rf "${R:?}"; mkdir -p "$R" "$T/outside"
  mkdir -p "$R/old-test-dir/sub"; head -c 200000 /dev/zero > "$R/old-test-dir/sub/blob"
  head -c 50000 /dev/zero > "$R/.com.google.Chrome.abc123"
  mkdir -p "$R/pip-build-xyz"; echo x > "$R/pip-build-xyz/f"
  mkdir -p "$R/fresh-dir"; echo x > "$R/fresh-dir/f"; fresh "$R/fresh-dir/f"; fresh "$R/fresh-dir"
  echo x > "$R/fresh-file"; fresh "$R/fresh-file"
  echo x > "$R/twodays"; fresh "$R/twodays" $(( 5 * 43200 ))            # 2.5 days old
  mkdir -p "$R/old-with-fresh/a/b"; echo x > "$R/old-with-fresh/a/b/new"; fresh "$R/old-with-fresh/a/b/new"
  mkdir -p "$R/claude-1000/tasks"; echo x > "$R/claude-1000/tasks/out"
  mkdir -p "$R/.claude-x"; mkdir -p "$R/tmux-1000"
  mkdir -p "$R/sockdir"; python3 -I -c 'import os,socket,sys; os.chdir(sys.argv[1]); socket.socket(socket.AF_UNIX).bind("s")' "$R/sockdir"  # relative: a deep TMPDIR passes AF_UNIX's 108 bytes
  mkdir -p "$R/held-fd"; echo x > "$R/held-fd/f"
  mkdir -p "$R/held-cwd"
  mkdir -p "$T/outside/keep"; echo keep > "$T/outside/keep/f"; ln -s "$T/outside/keep" "$R/link-to-outside"
}

setup
sleep 120 3<"$R/held-fd/f" </dev/null >/dev/null 2>&1 & PIDS+=($!)
(cd "$R/held-cwd" && exec sleep 120 </dev/null >/dev/null 2>&1) & PIDS+=($!)
for i in 1 2 3 4 5 6 7 8 9 10; do                       # wait (bounded) until both sleepers are set up
  [ "$(readlink "/proc/${PIDS[1]}/cwd" 2>/dev/null)" = "$R/held-cwd" ] && [ -e "/proc/${PIDS[0]}/fd/3" ] && break
  sleep 0.2
done

echo "# dry run"
before=$(find "$R" | wc -l)
out=$("$TT"); rc=$?
eq "dry run rc 0" "$rc" "0"
has "dry run summary" "$out" "would delete 4 entries older than 3 d"
has "dry run lists the largest first" "$(printf '%s' "$out" | sed -n 2p)" "old-test-dir/"
has "kept counts" "$out" "kept 10 (4 too new, 2 in use, 3 protected, 1 with sockets, 0 not mine)"
eq "dry run deletes nothing" "$(find "$R" | wc -l)" "$before"

echo "# json"
out=$("$TT" --json)
eq "json: names to delete" "$(printf '%s' "$out" | python3 -I -c 'import json,sys; print(sorted(e["name"] for e in json.load(sys.stdin)["delete"]))')" "['.com.google.Chrome.abc123', 'link-to-outside', 'old-test-dir', 'pip-build-xyz']"

echo "# apply"
out=$("$TT" --apply); rc=$?
eq "apply rc 0" "$rc" "0"
has "apply summary" "$out" "tmp-tidy: deleted 4 entries older than 3 d"
for n in old-test-dir .com.google.Chrome.abc123 pip-build-xyz link-to-outside; do eq "deleted: $n" "$(ex "$n")" "no"; done
for n in fresh-dir fresh-file twodays old-with-fresh claude-1000 .claude-x tmux-1000 sockdir held-fd held-cwd; do
  eq "kept: $n" "$(ex "$n")" "yes"
done
eq "symlink target untouched" "$(cat "$T/outside/keep/f")" "keep"
eq "claude-1000 content untouched" "$(cat "$R/claude-1000/tasks/out")" "x"

echo "# second run, --quiet, --days"
out=$("$TT" --apply --quiet); rc=$?
eq "nothing left: --quiet prints nothing" "$out|$rc" "|0"
out=$("$TT" --days 2 --json | python3 -I -c 'import json,sys; print(sorted(e["name"] for e in json.load(sys.stdin)["delete"]))')
eq "--days 2: the 2.5-day-old file goes too" "$out" "['twodays']"
out=$("$TT" --days 1 2>&1); rc=$?
eq "--days 1 rejected: rc 2" "$rc" "2"
has "--days 1 message" "$out" "at least 2"
for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; PIDS=()
out=$("$TT" --json | python3 -I -c 'import json,sys; print(sorted(e["name"] for e in json.load(sys.stdin)["delete"]))')
eq "released by the process: deletable" "$out" "['held-cwd', 'held-fd']"
out=$(TMP_TIDY_ROOT=/ "$TT" 2>&1); rc=$?
eq "root / refused: rc 2" "$rc" "2"

echo "# session scratchpads"
R2=$T/root2; S=$R2/claude-$(id -u)/proj-a/sess-1; SP=$S/scratchpad
mkdir -p "$SP" "$S/tasks" "$R2/claude-$(id -u)/proj-b/sess-2/scratchpad"
printf 'keep-me\n# comment\n\n' > "$T/keep.txt"
mkdir -p "$SP/old-dir/sub"; head -c 100000 /dev/zero > "$SP/old-dir/sub/blob"
echo x > "$SP/old-file"
echo x > "$R2/claude-$(id -u)/proj-b/sess-2/scratchpad/old-b"
mkdir -p "$SP/keep-me"; echo x > "$SP/keep-me/f"
echo x > "$SP/recent"; fresh "$SP/recent"
mkdir -p "$SP/old-with-new/a"; echo x > "$SP/old-with-new/a/new"; fresh "$SP/old-with-new/a/new"
mkdir -p "$SP/held"; echo x > "$SP/held/f"
echo x > "$SP/old-mtime-new-ctime"; touch -d "@$(( NOW - 86400 ))" "$SP/old-mtime-new-ctime"   # ctime now, mtime 11 d (fake clock)
echo x > "$S/tasks/old-task-out"; echo x > "$S/old-beside-scratchpad"
mkdir -p "$T/outside2"; echo keep > "$T/outside2/f"; ln -s "$T/outside2" "$R2/claude-$(id -u)/proj-b/sess-3"
sleep 120 3<"$SP/held/f" </dev/null >/dev/null 2>&1 & PIDS+=($!)
for i in 1 2 3 4 5 6 7 8 9 10; do [ -e "/proc/${PIDS[0]}/fd/3" ] && break; sleep 0.2; done
export TMP_TIDY_ROOT=$R2 TMP_TIDY_KEEP=$T/keep.txt
names() { python3 -I -c 'import json,sys; print(sorted(e["name"].split("/",1)[1] for e in json.load(sys.stdin)["delete"]))'; }
out=$("$TT" --json | names)
eq "scratch: only old, unkept, unheld scratchpad entries" "$out" "['proj-a/sess-1/scratchpad/old-dir', 'proj-a/sess-1/scratchpad/old-file', 'proj-a/sess-1/scratchpad/old-mtime-new-ctime', 'proj-b/sess-2/scratchpad/old-b']"
out=$("$TT"); has "scratch: dry-run summary" "$out" "would delete 4 entries older than 3 d"
has "scratch: keep list counted" "$out" "1 on the keep list"
out=$("$TT" --apply); rc=$?
eq "scratch: apply rc 0" "$rc" "0"
has "scratch: apply summary" "$out" "(4 in session scratchpads)"
for n in old-dir old-file old-mtime-new-ctime; do eq "scratch deleted: $n" "$([ -e "$SP/$n" ] && echo yes || echo no)" "no"; done
eq "scratch deleted: other project" "$([ -e "$R2/claude-$(id -u)/proj-b/sess-2/scratchpad/old-b" ] && echo yes || echo no)" "no"
for n in keep-me recent old-with-new held; do eq "scratch kept: $n" "$([ -e "$SP/$n" ] && echo yes || echo no)" "yes"; done
eq "scratchpad dir itself kept" "$([ -d "$SP" ] && echo yes || echo no)" "yes"
eq "tasks/ untouched" "$(cat "$S/tasks/old-task-out")" "x"
eq "file beside scratchpad untouched" "$(cat "$S/old-beside-scratchpad")" "x"
eq "symlinked session not followed" "$(cat "$T/outside2/f")" "keep"
out=$(TMP_TIDY_KEEP=$T/none.txt "$TT" --json | names)
eq "scratch: without keep-file the kept name goes" "$out" "['proj-a/sess-1/scratchpad/keep-me']"

echo "# stale session dirs and pytest dirs (24 h)"
for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; PIDS=()
R3=$T/root3; C=$R3/claude-$(id -u)/proj-a; U=$(id -un)
OLD=11111111-1111-4111-8111-111111111111; LIVE=22222222-2222-4222-8222-222222222222
OTHER=33333333-3333-4333-8333-333333333333; FRESH=44444444-4444-4444-8444-444444444444
DAY=55555555-5555-4555-8555-555555555555; HELD=66666666-6666-4666-8666-666666666666
mkdir -p "$C/$OLD/scratchpad/old-sub" "$C/$OLD/tasks" "$C/$LIVE/scratchpad" "$C/$OTHER" "$C/$FRESH" "$C/$DAY" "$C/$HELD" \
  "$C/notes-not-a-session" "$T/outside3" "$R3/pytest-of-$U/pytest-5/t" "$R3/pytest-of-$U/pytest-6" "$R3/pytest-of-$U/other"
echo x > "$C/$OLD/scratchpad/old-sub/f"; echo x > "$C/$OLD/tasks/out"; echo x > "$C/$LIVE/scratchpad/f"
mkdir -p "$C/$LIVE/tasks"; echo x > "$C/$LIVE/tasks/out"
echo x > "$C/$OTHER/f"; echo x > "$C/$HELD/f"; echo x > "$C/notes-not-a-session/f"; echo keep > "$T/outside3/f"
echo x > "$C/$FRESH/f"; fresh "$C/$FRESH/f" 43200                  # 12 h old: kept
echo x > "$C/$DAY/f"; fresh "$C/$DAY/f" 108000; fresh "$C/$DAY" 108000   # 30 h old: goes (24 h rule, not --days)
ln -s "$T/outside3" "$C/77777777-7777-4777-8777-777777777777"
echo x > "$R3/pytest-of-$U/pytest-5/t/f"; echo x > "$R3/pytest-of-$U/other/f"
echo x > "$R3/pytest-of-$U/pytest-6/f"; fresh "$R3/pytest-of-$U/pytest-6/f" 43200; fresh "$R3/pytest-of-$U/pytest-6" 43200
fresh "$R3/pytest-of-$U"   # the parent itself is fresh: only its old pytest-N go
ln -s pytest-6 "$R3/pytest-of-$U/pytest-current"
(export CLAUDE_CODE_SESSION_ID=$OTHER; exec sleep 120 </dev/null >/dev/null 2>&1) & PIDS+=($!)
sleep 120 3<"$C/$HELD/f" </dev/null >/dev/null 2>&1 & PIDS+=($!)
for i in 1 2 3 4 5 6 7 8 9 10; do
  grep -qa "CLAUDE_CODE_SESSION_ID=$OTHER" "/proc/${PIDS[0]}/environ" 2>/dev/null \
    && [ -e "/proc/${PIDS[1]}/fd/3" ] && break
  sleep 0.2
done
export TMP_TIDY_ROOT=$R3 TMP_TIDY_KEEP=$T/none.txt
full() { python3 -I -c 'import json,sys; print(sorted((e["kind"], e["name"].split("/")[-1]) for e in json.load(sys.stdin)["delete"]))'; }
out=$(CLAUDE_CODE_SESSION_ID=$LIVE "$TT" --json | full)
eq "sessions: old + 30-h-old session dirs, the old pytest dir (+ the live session's old scratchpad entry)" "$out" \
  "[('pytest', 'pytest-5'), ('scratch', 'f'), ('session', '$OLD'), ('session', '$DAY')]"
out=$(CLAUDE_CODE_SESSION_ID=$LIVE "$TT" --json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["kept"]["live_session"])')
eq "sessions: own + another process's session id are live" "$out" "2"
out=$(CLAUDE_CODE_SESSION_ID=$LIVE "$TT" --apply); rc=$?
eq "sessions: apply rc 0" "$rc" "0"
has "sessions: summary names them" "$out" "(1 in session scratchpads, 2 stale session dirs, 1 pytest dirs)"
for n in "$OLD" "$DAY"; do eq "session dir gone: $n" "$([ -e "$C/$n" ] && echo yes || echo no)" "no"; done
eq "live session: its tasks/ untouched" "$(cat "$C/$LIVE/tasks/out")" "x"
for n in "$LIVE" "$OTHER" "$FRESH" "$HELD" notes-not-a-session; do
  eq "kept: $n" "$([ -e "$C/$n" ] && echo yes || echo no)" "yes"
done
eq "symlinked session dir: link and target kept" "$([ -L "$C/77777777-7777-4777-8777-777777777777" ] && cat "$T/outside3/f")" "keep"
eq "pytest-5 gone" "$([ -e "$R3/pytest-of-$U/pytest-5" ] && echo yes || echo no)" "no"
for n in pytest-6 other pytest-current; do
  eq "pytest kept: $n" "$([ -e "$R3/pytest-of-$U/$n" ] || [ -L "$R3/pytest-of-$U/$n" ] && echo yes || echo no)" "yes"
done
for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; PIDS=()
out=$(CLAUDE_CODE_SESSION_ID=$LIVE "$TT" --json | full)
eq "released: the other and the held session go next time" "$out" "[('session', '$OTHER'), ('session', '$HELD')]"
mkdir -p "$R3/pytest-of-$U/pytest-9"; echo x > "$R3/pytest-of-$U/pytest-9/f"; rm -f "$R3/pytest-of-$U/pytest-current"
rm -rf "${R3:?}/pytest-of-$U/pytest-6"
out=$(CLAUDE_CODE_SESSION_ID=$LIVE "$TT" --json | full)
has "old pytest-of-<user> as a whole: listed once (no pytest-N twice)" "$out" "('top', 'pytest-of-$U')"
eq "  ... and not its pytest-9" "$(printf '%s' "$out" | grep -c "pytest-9")" "0"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
