#!/usr/bin/env bash
# Offline tests for ~/bin/transcript-compact (gzip old subagent transcripts, never delete). Exit 0 = all pass.
# HOME is a temp dir with a synthetic ~/.claude/projects tree (placeholder names and contents); the real
# transcripts are never touched. Real gzip/zcat/find run on the temp tree only.
# TRANSCRIPT_COMPACT_BIN=<path> tests another copy.
set -uo pipefail
B=${TRANSCRIPT_COMPACT_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/transcript-compact}
grep -q '/home/node' "$B" && { echo "refusing: $B names a real home path (must derive everything from \$HOME)"; exit 2; }
T=$(mktemp -d "${TMPDIR:-/tmp}/transcript-compact-tests.XXXXXX")
trap 'chmod -R u+w "${T:?}" 2>/dev/null; rm -rf "${T:?}"' EXIT
export HOME=$T/home
P=$HOME/.claude/projects
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
now=$(date +%s)
# mkf PATH AGE_HOURS [content]: a file with that mtime
mkf() {
  local c=${3-}; [ -n "$c" ] || c="{\"type\":\"assistant\",\"n\":\"$(basename "$1")\"}"
  mkdir -p "$(dirname "$1")"; printf '%s\n' "$c" > "$1"; touch -d "@$((now - $2 * 3600))" "$1"
}
tree() { (cd "$P" && find . \( -type f -o -type l \) | LC_ALL=C sort); }
run() { out=$("$B" "$@" 2>"$T/err"); rc=$?; err=$(cat "$T/err"); }
reset() { chmod -R u+w "$HOME" 2>/dev/null; rm -rf "${HOME:?}/.claude"; }

S=$P/-home-x-workspace/sess-1/subagents
echo "# arguments"
run --help; eq "--help exits 0" "$rc" 0; has "--help shows usage" "$out" "Usage: transcript-compact"
hasnt "--help has no shell code" "$out" "set -euo"
run --bogus; eq "unknown argument exits 2" "$rc" 2; has "unknown argument named" "$err" "unknown argument --bogus"
run --days abc; eq "--days abc exits 2" "$rc" 2; has "--days abc explained" "$err" "--days needs a number"
run --days; eq "--days without a value exits 2" "$rc" 2; has "--days without a value explained" "$err" "--days needs a number"
run --days -1; eq "--days -1 exits 2" "$rc" 2

echo "# no projects folder"
reset; run; eq "missing ~/.claude/projects exits 0" "$rc" 0; eq "missing ~/.claude/projects is silent" "$out$err" ""

echo "# dry run"
reset
mkf "$S/agent-old1.jsonl" 120; mkf "$S/agent-old2.jsonl" 100; mkf "$S/agent-new.jsonl" 2
before=$(tree)
run --dry-run; eq "--dry-run exits 0" "$rc" 0; has "--dry-run counts the 2 old files" "$out" "would compress 2 file(s)"
eq "--dry-run changes nothing" "$(tree)" "$before"
run -n; has "-n is --dry-run" "$out" "would compress 2 file(s)"

echo "# compress"
reset
mkf "$S/agent-old1.jsonl" 120 '{"type":"assistant","text":"placeholder one"}'
mkf "$S/agent-old1.meta.json" 120 '{"description":"placeholder"}'
mkf "$S/agent-old 2.jsonl" 100                                     # space in the name
mkf "$S/agent-new.jsonl" 2                                          # recent: stays
mkf "$S/notes.txt" 200                                              # not a transcript
mkf "$P/-home-x-workspace/sess-1.jsonl" 300                         # top-level session file: recall indexes it
mkf "$P/-home-x-workspace/other/agent-x.jsonl" 300                  # old, but not under subagents/
mkf "$P/-home-x-jobs-y/sess-2/subagents/agent-job.jsonl" 96         # other project, old
mkf "$T/outside.jsonl" 300; ln -s "$T/outside.jsonl" "$S/agent-link.jsonl"   # symlink: never followed
orig=$(cat "$S/agent-old1.jsonl")
run; eq "run exits 0" "$rc" 0; has "summary line" "$out" "compressed 3 file(s)"; eq "no stderr" "$err" ""
[ -f "$S/agent-old1.jsonl.gz" ] && [ ! -e "$S/agent-old1.jsonl" ] && ok "old transcript gzipped in place" || no "old transcript gzipped in place" "$(tree)"
eq "zcat gives the original content back" "$(zcat "$S/agent-old1.jsonl.gz")" "$orig"
[ -f "$S/agent-old 2.jsonl.gz" ] && ok "name with a space handled" || no "name with a space handled" "$(tree)"
[ -f "$P/-home-x-jobs-y/sess-2/subagents/agent-job.jsonl.gz" ] && ok "every project scanned" || no "every project scanned" "$(tree)"
[ -f "$S/agent-new.jsonl" ] && ok "recent transcript untouched" || no "recent transcript untouched"
[ -f "$S/agent-old1.meta.json" ] && ok "meta.json untouched" || no "meta.json untouched"
[ -f "$S/notes.txt" ] && ok "non-transcript untouched" || no "non-transcript untouched"
[ -f "$P/-home-x-workspace/sess-1.jsonl" ] && ok "top-level session transcript untouched" || no "top-level session transcript untouched"
[ -f "$P/-home-x-workspace/other/agent-x.jsonl" ] && ok "jsonl outside subagents/ untouched" || no "jsonl outside subagents/ untouched"
[ -L "$S/agent-link.jsonl" ] && [ -f "$T/outside.jsonl" ] && [ ! -e "$T/outside.jsonl.gz" ] && ok "symlink not followed" || no "symlink not followed"
gzmt=$(stat -c %Y "$S/agent-old1.jsonl.gz"); [ "$gzmt" -le $((now - 119 * 3600)) ] && ok "gz keeps the old mtime" || no "gz keeps the old mtime" "$gzmt"
run; has "second run finds nothing" "$out" "compressed 0 file(s)"
eq "second run leaves .gz files alone" "$(zcat "$S/agent-old1.jsonl.gz")" "$orig"

echo "# age threshold: older than N days"
reset
mkf "$S/agent-84h.jsonl" 84; mkf "$S/agent-70h.jsonl" 70
run; has "default 3 days: a 3.5-day-old file is compressed, a 2.9-day-old one is not" "$out" "compressed 1 file(s)"
[ -f "$S/agent-84h.jsonl.gz" ] && [ -f "$S/agent-70h.jsonl" ] && ok "3 days = 72 h" || no "3 days = 72 h" "$(tree)"
reset
mkf "$S/agent-30h.jsonl" 30; mkf "$S/agent-20h.jsonl" 20
run --days 1; has "--days 1: only the 30 h file" "$out" "compressed 1 file(s)"
[ -f "$S/agent-30h.jsonl.gz" ] && [ -f "$S/agent-20h.jsonl" ] && ok "--days 1 = 24 h" || no "--days 1 = 24 h" "$(tree)"

echo "# a .gz of the same name already exists (never overwrite, never delete)"
reset
mkf "$S/agent-dup.jsonl" 120 '{"new":"placeholder"}'
printf '{"old":"placeholder"}\n' | gzip > "$S/agent-dup.jsonl.gz"
mkf "$S/agent-zz.jsonl" 120
run; eq "existing .gz: run still exits 0" "$rc" 0
[ -f "$S/agent-zz.jsonl.gz" ] && ok "existing .gz does not stop the other files" || no "existing .gz does not stop the other files" "$(tree) / $err"
eq "existing .gz kept as it was" "$(zcat "$S/agent-dup.jsonl.gz")" '{"old":"placeholder"}'
eq "plain file kept as it was" "$(cat "$S/agent-dup.jsonl" 2>/dev/null)" '{"new":"placeholder"}'
has "skip reported on stderr" "$err" "agent-dup.jsonl"
has "summary counts only the compressed file" "$out" "compressed 1 file(s)"

echo "# gzip fails on one file (hard link) -> the others still run, exit 1"
reset
mkf "$S/agent-hl.jsonl" 120; ln "$S/agent-hl.jsonl" "$T/hardlink.jsonl"
mkf "$S/agent-ok.jsonl" 120
run; eq "a failed file exits 1" "$rc" 1
[ -f "$S/agent-ok.jsonl.gz" ] && ok "a failed file does not stop the others" || no "a failed file does not stop the others" "$(tree) / $err"
[ -f "$S/agent-hl.jsonl" ] && ok "the failed file stays as it was" || no "the failed file stays as it was" "$(tree)"
has "failure reported on stderr" "$err" "1 failed"
has "summary still printed" "$out" "compressed 1 file(s)"

echo "# never deletes anything"
reset
for i in 1 2 3 4 5; do mkf "$S/agent-n$i.jsonl" $((100 + i)); done; mkf "$S/agent-r.jsonl" 1
names() { (cd "$S" && ls | sed 's/\.gz$//' | LC_ALL=C sort); }
b=$(names); run; eq "same set of transcripts after the run (plain or .gz)" "$(names)" "$b"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
