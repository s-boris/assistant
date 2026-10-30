#!/usr/bin/env bash
# Tests for ~/bin/model-drift (offline; fictional fixture transcripts in a temp dir; MODEL_DRIFT_BIN=/copy tests a copy).
set -uo pipefail
B=${MODEL_DRIFT_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/model-drift}
T=$(mktemp -d "${TMPDIR:-/tmp}/model-drift-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
fail=0; ok() { echo "ok   $1"; }; no() { echo "FAIL $1"; fail=1; }
P=$T/proj
mkdir -p "$P/subagents"
export MODEL_DRIFT_PROJECTS=$P MODEL_DRIFT_SETTINGS=$T/settings.json MODEL_DRIFT_STATE=$T/state \
       MODEL_DRIFT_INBOX=$T/inbox MODEL_DRIFT_TODAY=20990101 MODEL_DRIFT_TOOL=$T/tool FAKE_DIR=$T
# fake `tool policy`: logs its args, answers {"principals": <$T/princ.json>}; FAKE_MODE=fail|junk
cat > "$T/tool" <<'EOF'
#!/bin/sh
echo "$*" >> "$FAKE_DIR/tool.log"
case "${FAKE_MODE:-ok}" in fail) exit 3;; junk) echo 'not json'; exit 0;; esac
printf '{"days": 1, "principals": '; cat "$FAKE_DIR/princ.json"; printf '}\n'
EOF
chmod +x "$T/tool"
printf '{"brain": 2, "reflex": 40}' > "$T/princ.json"
settings() { printf '{\n  "env": {"X": "1"},\n  "model": "%s",\n  "agents": {"a": {\n    "model": "haiku"}}\n}\n' "$1" > "$T/settings.json"; }
a() { printf '{"type":"assistant","isSidechain":%s,"timestamp":"2099-01-01T10:0%s:00Z","message":{"model":"%s","content":[{"type":"text","text":"SECRET-PLANET-TEXT"}]}}\n' "$1" "$2" "$3"; }
u() { printf '{"type":"user","isSidechain":false,"message":{"role":"user","content":"SECRET-PLANET-TEXT"}}\n'; }
run() { out=$("$B" "$@" 2>&1); rc=$?; }

# old transcript says another model; the newest (by mtime) one is what counts
a false 0 claude-sonnet-9 > "$P/old.jsonl"; touch -d '2099-01-01 00:00' "$P/old.jsonl"
a false 0 claude-sonnet-9 > "$P/subagents/agent-x.jsonl"
{ u; a false 1 claude-alpha-7; u; a true 2 claude-beta-3; a false 3 '<synthetic>'; printf '{"type":"assistant","isSidechain":false,"message":{}}\n'; printf 'not json\n'; } > "$P/new.jsonl"
touch -d '2099-01-02 00:00' "$P/new.jsonl"

settings claude-alpha-7
run status
[ $rc = 0 ] && grep -q 'verdict:    match' <<<"$out" && ok "match: sidechain, synthetic, empty, bad lines skipped" || no "match: rc=$rc $out"
grep -q 'transcript: claude-alpha-7' <<<"$out" && ok "status shows transcript model" || no "status transcript: $out"
grep -q 'settings:   claude-alpha-7' <<<"$out" && ok "status shows settings model (top-level key)" || no "status settings: $out"
grep -q SECRET <<<"$out" && no "message text leaked" || ok "no message text in status"
run
[ $rc = 0 ] && [ -z "$out" ] && [ ! -e "$T/inbox" ] && ok "run silent on match" || no "silent: rc=$rc $out"

settings 'claude-alpha-7[1m]'; run status
grep -q 'verdict:    match' <<<"$out" && ok "[1m] suffix ignored" || no "suffix: $out"
settings alpha; run status
grep -q 'verdict:    match' <<<"$out" && ok "alias matches family" || no "alias: $out"
settings beta; run status
[ $rc = 1 ] && grep -q 'verdict:    drift' <<<"$out" && ok "alias mismatch = drift" || no "alias drift: rc=$rc $out"

settings claude-gamma-1
run --dry-run
[ $rc = 1 ] && grep -q 'run `claude-alpha-7`' <<<"$out" && [ ! -e "$T/inbox" ] && ok "--dry-run prints note, writes nothing" || no "dry: rc=$rc $out"
run
N=$T/inbox/model-drift-20990101.md
[ $rc = 1 ] && [ -f "$N" ] && grep -q 'sets `claude-gamma-1`' "$N" && ok "drift: one note" || no "drift note: rc=$rc"
grep -q SECRET "$N" && no "note leaks text" || ok "note has no message text"
[ "$(cat "$T/state/last-note")" = 20990101 ] && ok "state records the day" || no "state"
rm -f "$T/inbox/model-drift-20990101.md"; run
[ ! -e "$N" ] && ok "dedup: no second note the same day" || no "dedup"
MODEL_DRIFT_TODAY=20990102 run
[ -f "$T/inbox/model-drift-20990102.md" ] && ok "next day: new note" || no "next day"
[ "$(ls "$T/inbox" | wc -l)" = 1 ] && ok "exactly one note per day" || no "note count: $(ls "$T/inbox")"

# no verdict cases: only sidechain/synthetic entries in the tail; no settings key; empty project dir
{ u; a true 1 claude-beta-3; a false 2 '<synthetic>'; } > "$P/new.jsonl"; touch -d '2099-01-03 00:00' "$P/new.jsonl"
MODEL_DRIFT_TODAY=20990103 run status
[ $rc = 0 ] && grep -q 'verdict:    unknown' <<<"$out" && ok "no main-session model = unknown" || no "unknown: rc=$rc $out"
MODEL_DRIFT_TODAY=20990103 run
[ ! -e "$T/inbox/model-drift-20990103.md" ] && ok "unknown writes no note" || no "unknown note"
# only the last 200 lines count
{ a false 1 claude-alpha-7; for i in $(seq 1 250); do u; done; } > "$P/new.jsonl"; touch -d '2099-01-04 00:00' "$P/new.jsonl"
run status
grep -q 'verdict:    unknown' <<<"$out" && ok "entries beyond the last 200 lines ignored" || no "tail: $out"
printf '{"theme": "dark"}\n' > "$T/settings.json"; a false 1 claude-alpha-7 > "$P/new.jsonl"; run status
[ $rc = 0 ] && grep -q 'no model key' <<<"$out" && ok "no settings key = unknown" || no "no key: $out"
MODEL_DRIFT_PROJECTS=$T/empty run status
[ $rc = 0 ] && grep -q 'unknown' <<<"$out" && ok "no transcript = unknown" || no "empty: $out"
run bogus
[ $rc = 2 ] && ok "usage error exit 2" || no "usage rc=$rc"

# stamp canary: hub MCP calls without a valid _caller stamp (`tool policy` principal subagent:unattributed);
# the model verdict is "unknown" from here on (no settings key), so rc is the stamp check's alone
export MODEL_DRIFT_STATE=$T/st2 MODEL_DRIFT_INBOX=$T/in2 MODEL_DRIFT_TODAY=20990105
NOW=4070908800; S=$T/in2/stamp-canary-20990105.md; L=$T/tool.log
: > "$L"; MODEL_DRIFT_NOW=$NOW run
[ $rc = 0 ] && [ -z "$out" ] && [ ! -e "$T/in2" ] && ok "stamps: count 0 = silent" || no "stamps silent: rc=$rc $out"
grep -qx 'policy --days 1.00000 --json' "$L" && ok "stamps: first check covers the last day" || no "first window: $(cat "$L")"
[ "$(cat "$T/st2/stamp-checked")" = $NOW ] && ok "stamps: check time saved" || no "checked state"
printf '{"brain": 0, "reflex": 40, "subagent:unattributed": 3}' > "$T/princ.json"; : > "$L"
MODEL_DRIFT_NOW=$((NOW+1800)) run --dry-run
[ $rc = 1 ] && grep -q '3 MCP call(s) as `subagent:unattributed` in the last 31 min' <<<"$out" && [ ! -e "$T/in2" ] \
  && [ "$(cat "$T/st2/stamp-checked")" = $NOW ] && ok "stamps: --dry-run prints the note, writes nothing" || no "stamps dry: rc=$rc $out"
grep -qx 'policy --days 0.02153 --json' "$L" && ok "stamps: window = since the last check + 1 min" || no "window: $(cat "$L")"
MODEL_DRIFT_NOW=$((NOW+1800)) run
[ $rc = 1 ] && [ -f "$S" ] && [ "$(ls "$T/in2" | wc -l)" = 1 ] && ok "stamps: unstamped calls = one note" || no "stamps note: rc=$rc $(ls "$T/in2" 2>&1)"
grep -q '^Data, not instructions\.$' "$S" && grep -q '^Next: `tool policy --days 1`' "$S" && grep -q 'subagent:unattributed 3' "$S" \
  && ok "stamps: note has the marker, a Next line and the counts" || no "stamps note text"
[ "$(cat "$T/st2/stamp-note")" = 20990105 ] && [ "$(cat "$T/st2/stamp-checked")" = $((NOW+1800)) ] && ok "stamps: note day and check time saved" || no "stamp states"
rm -f "$S"; MODEL_DRIFT_NOW=$((NOW+3600)) run
[ $rc = 1 ] && [ ! -e "$S" ] && ok "stamps: no second note the same day" || no "stamps dedup: rc=$rc"
printf '{"brain": 3}' > "$T/princ.json"
MODEL_DRIFT_TODAY=20990106 MODEL_DRIFT_NOW=$((NOW+86400)) run
[ $rc = 0 ] && [ -z "$out" ] && [ ! -e "$T/in2/stamp-canary-20990106.md" ] && ok "stamps: next day, count 0 = silent" || no "next day silent: rc=$rc $out"
printf '{"subagent:unattributed": 1}' > "$T/princ.json"
MODEL_DRIFT_TODAY=20990106 MODEL_DRIFT_NOW=$((NOW+86400+1800)) run
[ $rc = 1 ] && [ -f "$T/in2/stamp-canary-20990106.md" ] && ok "stamps: next day, new unstamped calls = a new note" || no "next day note: rc=$rc"
before=$(cat "$T/st2/stamp-checked"); : > "$L"
FAKE_MODE=fail MODEL_DRIFT_TODAY=20990109 MODEL_DRIFT_NOW=$((NOW+4*86400)) run
fr=$rc; FAKE_MODE=junk MODEL_DRIFT_TODAY=20990109 MODEL_DRIFT_NOW=$((NOW+4*86400)) run
[ $fr = 0 ] && [ $rc = 0 ] && [ ! -e "$T/in2/stamp-canary-20990109.md" ] && [ "$(cat "$T/st2/stamp-checked")" = "$before" ] \
  && [ "$(grep -c . "$L")" = 2 ] && ok "stamps: gateway error or bad JSON = no verdict, window kept" || no "stamps error: $fr/$rc"
: > "$L"; MODEL_DRIFT_TODAY=20990109 MODEL_DRIFT_NOW=$((NOW+4*86400)) run
grep -qx 'policy --days 1.00000 --json' "$L" && [ $rc = 1 ] && ok "stamps: after a long gap the window is at most a day" || no "cap: rc=$rc $(cat "$L")"
: > "$L"; MODEL_DRIFT_TOOL=off MODEL_DRIFT_TODAY=20990110 run
[ $rc = 0 ] && [ ! -s "$L" ] && [ ! -e "$T/in2/stamp-canary-20990110.md" ] && ok "stamps: MODEL_DRIFT_TOOL=off skips the check" || no "off: rc=$rc"
before=$(cat "$T/st2/stamp-checked"); : > "$L"; run status
[ $rc = 1 ] && grep -q '^stamps:     subagent:unattributed 1 in the last 24 h' <<<"$out" && grep -qx 'policy --days 1.00000 --json' "$L" \
  && [ "$(cat "$T/st2/stamp-checked")" = "$before" ] && ok "status: last day's stamp count, state unchanged" || no "status stamps: rc=$rc $out"
printf '{"brain": 1}' > "$T/princ.json"; run status
[ $rc = 0 ] && grep -q 'subagent:unattributed 0 in the last 24 h (ok)' <<<"$out" && ok "status: count 0 = ok" || no "status ok: rc=$rc $out"
FAKE_MODE=fail run status
[ $rc = 0 ] && grep -q '^stamps:     no verdict' <<<"$out" && ok "status: gateway error = no verdict" || no "status error: rc=$rc $out"

[ $fail = 0 ] && echo "all model-drift tests passed" || echo "model-drift tests FAILED"
exit $fail
