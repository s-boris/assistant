#!/usr/bin/env bash
# Offline tests for lint.py (TOOLS.md <-> ~/bin and skills <-> ~/bin drift lint): the live ~/bin + TOOLS.md + skills
# must pass, and fixtures with an undocumented script, a removed script, a dead ~/bin/ path, an unknown subcommand,
# a missing skill, agent drift (dead script/subcommand, bogus tool, unknown agent type) and allowlist mistakes must fail.
# Read-only on the live files. DOCS_LINT_BIN / DOCS_LINT_DOC override the live inputs. Exit 0 = all pass.
set -uo pipefail
cd "$(dirname "$0")" || exit 2
L=$PWD/lint.py
T=$(mktemp -d "${TMPDIR:-/tmp}/docs-lint-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
pass=0 fail=0
ok()  { pass=$((pass+1)); }
bad() { fail=$((fail+1)); echo "FAIL: $*"; }
expect() { # expect RC GREP-PATTERN|"" DESC -- lint args...
  local want=$1 pat=$2 desc=$3; shift 4
  out=$(python3 -I "$L" "$@" 2>&1); rc=$?
  [ "$rc" = "$want" ] || { bad "$desc: rc $rc, want $want: $out"; return; }
  [ -z "$pat" ] || grep -qE -- "$pat" <<<"$out" || { bad "$desc: no /$pat/ in: $out"; return; }
  ok
}

# 1. live (TOOLS.md, JOBS.md, CLAUDE.md, every SKILL.md and every subagent definition)
expect 0 "" "live ~/bin vs TOOLS.md + skills" -- --bin "${DOCS_LINT_BIN:-/home/node/bin}" \
  --doc "${DOCS_LINT_DOC:-/home/node/workspace/TOOLS.md}" --skills "${DOCS_LINT_SKILLS:-/home/node/.claude/skills}" \
  --agents "${DOCS_LINT_AGENTS:-/home/node/.claude/agents}"

# fixture bin
B=$T/bin; mkdir -p "$B/lib" "$B/__pycache__"
for s in alpha beta helper real.py; do printf '#!/bin/sh\n' > "$B/$s"; chmod 755 "$B/$s"; done
ln -s real.py "$B/real"
for s in alpha.orig alpha.new beta.bak alpha.bak.20261019 swap~; do printf 'x\n' > "$B/$s"; chmod 755 "$B/$s"; done
printf 'x\n' > "$B/notes.txt"; printf 'x\n' > "$B/lib/sbb.py"; chmod 755 "$B/lib/sbb.py"
EMPTY=$T/empty.tsv; : > "$EMPTY"
GOOD=$T/good.md
cat > "$GOOD" <<'MD'
# Scripts
- `alpha run|show` (skill a), `beta` + `real --x`: things.
- ~/bin/lib/sbb.py: a library (`python3 -I ~/bin/lib/sbb.py`).
MD
ALLOW=$T/allow.tsv; printf '# c\nundocumented\thelper\tinternal helper of alpha\n' > "$ALLOW"

# 2. clean fixture passes (copies, lib/, non-exec, symlink alias ignored or resolved)
expect 0 "" "clean fixture" -- --bin "$B" --doc "$GOOD" --allow "$ALLOW"
# 3. undocumented script
expect 1 "undocumented: ~/bin/helper" "undocumented helper without allowlist" -- --bin "$B" --doc "$GOOD" --allow "$EMPTY"
# 4. removed script still presented, plus a dead ~/bin/ path
DRIFT=$T/drift.md; cp "$GOOD" "$DRIFT"
printf -- '- `gone --flag` (skill g): removed long ago.\n- Hook `~/bin/ghost`: dead path.\n' >> "$DRIFT"
expect 1 "presents .gone. as a script" "removed script presented" -- --bin "$B" --doc "$DRIFT" --allow "$ALLOW"
expect 1 "names ~/bin/ghost" "dead ~/bin path" -- --bin "$B" --doc "$DRIFT" --allow "$ALLOW"
# 5. the fixture the TODO asked for: one undocumented AND one removed script -> both reported
out=$(python3 -I "$L" --bin "$B" --doc "$DRIFT" --allow "$EMPTY"); rc=$?
[ "$rc" = 1 ] && grep -q "undocumented: ~/bin/helper" <<<"$out" && grep -q "gone" <<<"$out" && ok || bad "both directions: $out"
# 6. a name only in prose (not backticked) does not count; a substring of another name does not count
P=$T/prose.md; printf -- '- `alpha-beta` and `real`: alpha in prose only.\n' > "$P"; printf '#!/bin/sh\n' > "$B/alpha-beta"; chmod 755 "$B/alpha-beta"
expect 1 "undocumented: ~/bin/alpha has" "prose-only mention" -- --bin "$B" --doc "$P" --allow "$ALLOW"
expect 1 "undocumented: ~/bin/beta has" "substring of alpha-beta" -- --bin "$B" --doc "$P" --allow "$ALLOW"
rm -f "$T/bin/alpha-beta"
# 7. a chained span inside the description is not a presented script (`health` + `capacity` verbs)
V=$T/verbs.md; cp "$GOOD" "$V"; printf -- '- `alpha` (skill x): after-check (`health` + `capacity` verbs).\n' >> "$V"
expect 0 "" "verbs inside description" -- --bin "$B" --doc "$V" --allow "$ALLOW"
# 8. allowlist: notscript passes; reason missing, stale undocumented, stale notscript fail
N=$T/ns.md; cp "$GOOD" "$N"; printf -- '- `tg "..."`: system-wide.\n' >> "$N"
printf 'undocumented\thelper\tinternal\nnotscript\ttg\tsystem-wide\n' > "$T/ns.tsv"
expect 0 "" "notscript allowlisted" -- --bin "$B" --doc "$N" --allow "$T/ns.tsv"
expect 1 "presents .tg." "notscript not allowlisted" -- --bin "$B" --doc "$N" --allow "$ALLOW"
printf 'undocumented\thelper\t\n' > "$T/noreason.tsv"
expect 1 "needs kind" "allowlist line without reason" -- --bin "$B" --doc "$GOOD" --allow "$T/noreason.tsv"
printf 'undocumented\thelper\tx\nundocumented\tvanished\tx\n' > "$T/stale.tsv"
expect 1 "stale allowlist: vanished" "stale undocumented entry" -- --bin "$B" --doc "$GOOD" --allow "$T/stale.tsv"
printf 'undocumented\thelper\tx\nundocumented\talpha\tx\n' > "$T/stale2.tsv"
expect 1 "stale allowlist: alpha is documented" "allowlisted but documented" -- --bin "$B" --doc "$GOOD" --allow "$T/stale2.tsv"
printf 'undocumented\thelper\tx\nnotscript\tzzz\tx\n' > "$T/stale3.tsv"
expect 1 "notscript zzz" "stale notscript entry" -- --bin "$B" --doc "$GOOD" --allow "$T/stale3.tsv"
# 9. usage errors
expect 2 "" "missing doc" -- --bin "$B" --doc "$T/nope.md"
expect 2 "" "unknown arg" -- --frob
# 11. skills fixture: own bin, docs, skills and system PATH
K=$T/sk; KB=$K/bin; KS=$K/skills; KP=$K/sys; mkdir -p "$KB" "$KS/good" "$KS/other" "$KP"
printf '#!/bin/sh\ncase "$1" in run|show) ;; sub-thing) ;; esac\n' > "$KB/alpha"
printf '#!/bin/sh\nexec other "$@"\n' > "$KB/fwd"; printf '#!/bin/sh\n' > "$KP/curl"; chmod 755 "$KB/alpha" "$KB/fwd" "$KP/curl"
KD=$K/TOOLS.md; printf -- '- `alpha run|show` (skill good) + `fwd`: things.\n' > "$KD"
KN=$K/JOBS.md; printf -- '- job x: skill other; skills good / other; plugin skill telegram-plus:access; docs/skills cleanup.\n' > "$KN"
cat > "$KS/good/SKILL.md" <<'MD'
Uses ~/bin/alpha. Run `alpha run` or `alpha show --x`; `curl -s x | alpha run`; then `sub-thing --now`.
Prose words: `status`, `get`, `reply`. Forwarder: `fwd anything`. See skill other, button `x`; skill only.
```
alpha run
curl -s x
```
MD
printf -- '- Related: skills good / other.\n' > "$KS/other/SKILL.md"
KA=$T/k.tsv; printf 'nosubcheck\tfwd\tforwards to other\n' > "$KA"
ks() { local want=$1 pat=$2 desc=$3 f=$4; shift 4; expect "$want" "$pat" "$desc" -- --bin "$KB" --doc "$KD" --allow "${f:-$KA}" \
  --skills "$KS" --names "$KD" --names "$KN" --syspath "$KP" "$@"; }
ks 0 "" "clean skills fixture" ""
sk_add() { cp "$KS/good/SKILL.md" "$K/good.bak"; printf '%s\n' "$1" >> "$KS/good/SKILL.md"; }
sk_undo() { mv "$K/good.bak" "$KS/good/SKILL.md"; }
sk_add 'Run ~/bin/ghost first.';            ks 1 "skill good names ~/bin/ghost" "dead ~/bin path in a skill" ""; sk_undo
sk_add 'Then `oldtool run --x`.';           ks 1 "run .oldtool., which is not" "removed script with args" ""; sk_undo
sk_add 'Then `old-tool`.';                  ks 1 "run .old-tool." "removed hyphenated script" ""; sk_undo
sk_add 'Then `alpha frob 3`.';              ks 1 "has no 'frob'" "unknown subcommand" ""; sk_undo
sk_add 'Then `alpha run | deadpipe -x`.';   ks 1 "run .deadpipe." "dead command after a pipe" ""; sk_undo
printf '```\nghost-cmd --x\n```\n' >> "$KS/other/SKILL.md"
ks 1 "skill other:3 tells me to run .ghost-cmd." "dead command in a fenced block" ""
printf -- '- Related: skills good / other.\n' > "$KS/other/SKILL.md"
sk_add 'See skills good / gone.';           ks 1 "skill good:.* names skill gone" "missing skill named by a skill" ""; sk_undo
printf -- '- also skill nosuch here\n' >> "$KN"; ks 1 "JOBS.md:2 names skill nosuch" "missing skill named by a doc" ""
printf 'nosubcheck\tfwd\tx\nnotskill\tnosuch\tprose in a fixture\n' > "$T/k2.tsv"; ks 0 "" "notskill allowlisted" "$T/k2.tsv"
printf -- '- job x: skill other; skills good / other; plugin skill telegram-plus:access; docs/skills cleanup.\n' > "$KN"
ks 1 "notskill nosuch is no longer needed" "stale notskill" "$T/k2.tsv"
ks 1 "has no 'anything'" "forwarder without nosubcheck" "$EMPTY"
printf 'nosubcheck\tfwd\tx\nskillcmd\tzz\tx\n' > "$T/k3.tsv"; ks 1 "skillcmd zz is no longer needed" "stale skillcmd" "$T/k3.tsv"
sk_add 'Then `zz --go`.'; ks 0 "" "skillcmd allowlisted" "$T/k3.tsv"; ks 1 "run .zz." "skillcmd not allowlisted" ""; sk_undo
# skill allowlist kinds are ignored (not stale) without --skills; a skills dir without SKILL.md is a usage error
expect 0 "" "skill kinds without --skills" -- --bin "$KB" --doc "$KD" --allow "$T/k3.tsv"
mkdir -p "$K/noskills"; expect 2 "" "empty skills dir" -- --bin "$KB" --doc "$KD" --allow "$KA" --skills "$K/noskills"
# 12. agents fixture: same bin/skills; agents dir with one clean agent
KG=$K/agents; mkdir -p "$KG"
cat > "$KG/scan-bot.md" <<'MD'
---
name: scan-bot
description: x
tools: Read, Bash, WebFetch, mcp__x__y
---
Run `alpha show --x`, then `curl -s x | alpha run`; skill good. Output lines `<label> | <url>`.
MD
printf -- '- Sun: `scan-bot` subagent at high; the scan-bot agent; subagent_type general-purpose; agent type Explore.\n' > "$K/AJ.md"
ka() { local want=$1 pat=$2 desc=$3 f=$4; shift 4; expect "$want" "$pat" "$desc" -- --bin "$KB" --doc "$KD" --allow "${f:-$KA}" \
  --skills "$KS" --agents "$KG" --names "$KD" --names "$K/AJ.md" --syspath "$KP" "$@"; }
ka 0 "" "clean agents fixture" ""
ag_add() { cp "$KG/scan-bot.md" "$K/ag.bak"; printf '%s\n' "$1" >> "$KG/scan-bot.md"; }
ag_undo() { mv "$K/ag.bak" "$KG/scan-bot.md"; }
ag_add 'Then `oldtool run --x`.';   ka 1 "agent scan-bot:7 tells me to run .oldtool." "dead script in an agent" ""; ag_undo
ag_add 'Read ~/bin/ghost.';         ka 1 "agent scan-bot names ~/bin/ghost" "dead ~/bin path in an agent" ""; ag_undo
ag_add 'Then `alpha frob`.';        ka 1 "agent scan-bot:7 runs .alpha frob." "unknown subcommand in an agent" ""; ag_undo
ag_add 'See skill nosuch.';         ka 1 "agent scan-bot:7 names skill nosuch" "missing skill named by an agent" ""; ag_undo
ag_add 'Hand off to `ghost-bot` subagent.'; ka 1 "agent scan-bot:7 names agent ghost-bot" "missing agent named by an agent" ""; ag_undo
cp "$KG/scan-bot.md" "$K/ag.bak"; sed -i 's/^tools: .*/tools: Read, Bash, Teleport/' "$KG/scan-bot.md"
ka 1 "lists tool Teleport" "bogus tool" ""
printf 'nosubcheck\tfwd\tx\nagenttool\tTeleport\tfixture\n' > "$T/k4.tsv"; ka 0 "" "agenttool allowlisted" "$T/k4.tsv"; ag_undo
ka 1 "agenttool Teleport is no longer needed" "stale agenttool" "$T/k4.tsv"
cp "$KG/scan-bot.md" "$K/ag.bak"; sed -i 's/^name: .*/name: scanbot/' "$KG/scan-bot.md"
ka 1 "name: scanbot != file name" "frontmatter name mismatch" ""; ag_undo
printf -- '- the idea-scan agent; subagent `weekly-digest`; agent type nobody.\n' >> "$K/AJ.md"
ka 1 "AJ.md:2 names agent idea-scan" "missing agent (x-y agent) named by a doc" ""
ka 1 "AJ.md:2 names agent weekly-digest" "missing agent (subagent \`X\`) named by a doc" ""
ka 1 "AJ.md:2 names agent nobody" "missing agent (agent type X) named by a doc" ""
printf 'nosubcheck\tfwd\tx\nnotagent\tidea-scan\tx\nnotagent\tweekly-digest\tx\nnotagent\tnobody\tx\n' > "$T/k5.tsv"
ka 0 "" "notagent allowlisted" "$T/k5.tsv"
printf -- '- Sun: `scan-bot` subagent at high.\n' > "$K/AJ.md"; ka 1 "notagent nobody is no longer needed" "stale notagent" "$T/k5.tsv"
mkdir -p "$K/noagents"; expect 2 "" "empty agents dir" -- --bin "$KB" --doc "$KD" --allow "$KA" --agents "$K/noagents"
# agent kinds are ignored without --agents
expect 0 "" "agent kinds without --agents" -- --bin "$KB" --doc "$KD" --allow "$T/k5.tsv"
# 10. the live run touched nothing (read-only): lint.py opens files only for reading
grep -qE "open\([^)]*['\"][wa]" "$L" && bad "lint.py opens a file for writing" || ok

echo "docs-lint-tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
