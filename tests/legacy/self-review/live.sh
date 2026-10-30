#!/usr/bin/env bash
# Live-LLM fixture run for self-review (real llm-local, ~60 s, 4-5 calls). Usage: [SR=<script>] live.sh tp|noise
# Prints the dry-run report. Rule corpus = copies of the real CLAUDE.md and memory notes (temp dir, deleted after;
# the people note is FAKE). Expected (2026-10-15): tp = 9 findings incl. CLAUDE.md contradiction; noise = 0 findings.
# tp: planted true positives (contradiction, IBAN, quote, chat-log, guarded-but-sensitive line, friend's name
#     in a skill example, index drift). noise: 8 lines modeled on the 2026-10-15 false positives.
set -uo pipefail
SR=${SR:-/home/node/bin/self-review}; GROUP=${1:?}
S=${TMPDIR:-/tmp}
T=$(mktemp -d "$S/live.XXXXXX"); trap "rm -rf $T" EXIT
export BRAIN_GIT_DIR=$T/brain.git BRAIN_WORK_TREE=$T/home SELF_REVIEW_STATE=$T/state SELF_REVIEW_REPORTS=$T/reports \
       SELF_REVIEW_INBOX=$T/inbox SELF_REVIEW_LLM=${LIVE_LLM:-/home/node/bin/llm-local} SELF_REVIEW_HARD_RULES=/etc/claude-code/CLAUDE.md \
       SELF_REVIEW_EXTRA_SRC=$T/none SECRETSCAN_SOURCES=$T/nosecrets
W=$T/home; MEM=$W/.claude/projects/-home-node-workspace/memory; RM=/home/node/.claude/projects/-home-node-workspace/memory
mkdir -p "$W/bin" "$W/workspace" "$W/.claude/skills/paper" "$W/.claude/skills/vaultx" "$W/.claude/skills/misc" \
         "$W/.claude/skills/remind" "$W/.claude/agents" "$MEM" "$T/inbox" "$T/nosecrets" "$T/state"
git init -q --bare "$BRAIN_GIT_DIR"; git --git-dir="$BRAIN_GIT_DIR" config core.bare false
g() { git --git-dir="$BRAIN_GIT_DIR" --work-tree="$W" -C "$W" "$@"; }
commit() { g add -A >/dev/null; g -c user.name=t -c user.email=t@localhost commit -qm "$1" >/dev/null; }
cp /home/node/workspace/CLAUDE.md "$W/workspace/CLAUDE.md"
for f in "$RM"/*.md; do case $(basename "$f") in MEMORY.md|people-and-dates.md) ;; *) cp "$f" "$MEM/";; esac; done
printf -- '---\nname: people\n---\nFriends:\n- Jonas Keller (Mar 3), wedding on Nov 7\n- Lena, gym friend\n' > "$MEM/people-and-dates.md"
printf -- '- [People](people-and-dates.md) — friends\n- [Beta](beta.md) — beta\n- [Server](server.md) — server\n- [Delta](delta.md) — delta\n' > "$MEM/MEMORY.md"
for f in "$MEM"/*.md; do b=$(basename "$f" .md); case $b in MEMORY|people-and-dates) ;; *) echo "- [$b]($b.md) — note" >> "$MEM/MEMORY.md";; esac; done
printf -- '---\nname: beta\n---\nBeta note.\n' > "$MEM/beta.md"
printf -- '---\nname: server\n---\nServer note.\n' > "$MEM/server.md"
printf -- '---\nname: delta\n---\nDelta note.\n' > "$MEM/delta.md"
for d in paper vaultx misc remind; do printf -- '---\nname: %s\ndescription: %s skill\n---\n# %s\n' $d $d $d > "$W/.claude/skills/$d/SKILL.md"; done
commit base; BASE=$(g rev-parse HEAD); echo "$BASE" > "$T/state/last"

if [ "$GROUP" = tp ]; then
  IBAN=$(python3 -I -c '
import random
b="".join(random.choice("0123456789") for _ in range(17)); n=int("".join(str(int(c,36)) for c in b+"CH00"))
s="CH%02d%s"%(98-n%97,b); print(" ".join(s[i:i+4] for i in range(0,len(s),4)))')
  echo "- You may buy things without asking when it is cheap." >> "$W/workspace/CLAUDE.md"           # TP1 (a)
  cat >> "$MEM/beta.md" <<EOF
Bank: $IBAN for the transfer.
Anna wrote: "cannot make the rent payment this month sorry"
[2026-10-12 14:03] Marco: see you at the station later
EOF
  echo "Never bring up Lena Brunner's divorce or her therapy sessions when her family is around." >> "$MEM/delta.md"  # TP7
  echo '- `remind add --owner "2026-10-16 18:00" "Pick up the suit for Jonas'"'"'s wedding"`' >> "$W/.claude/skills/remind/SKILL.md"  # TP6
  printf -- '---\nname: gamma\n---\nGamma note.\n' > "$MEM/gamma.md"                               # index drift
else
  cat >> "$W/.claude/skills/paper/SKILL.md" <<'EOF'
- Direct access to the bill store is blocked by vault-guard; use `bills list` instead.
- `paperless get ID` (card) adds amount and due date; the IBAN and the reference need his ✅ per document.
- **Sent to me by mistake** (a photo or PDF of a bill here): never `bills extract` it and never Read it; file it privately and point him to the vault bot.
EOF
  cat >> "$W/.claude/skills/vaultx/SKILL.md" <<'EOF'
- Exact wording only via `vault raw request --reason "…"`: the vault asks him (✅/❌) and releases an anonymized paraphrase.
- Use for his Gmail ("any new mail?", "find the mail from X"), raw quotes ("show me exactly what X wrote", "the original PDF") and the approvals he gets on Telegram.
EOF
  cat >> "$W/.claude/skills/misc/SKILL.md" <<'EOF'
Everything I read goes to Anthropic's API, so private text and other people's personal data pass through the local model first and I read only its output.
- His current insurer is unknown. Ask the vault first (`vault find "Krankenkasse"`, `paperless search "Police"`) before asking him.
EOF
  echo "His server Homesrv is at 198.51.100.5 (\`ssh unraid\`); he lives in Exampletown and works in Zurich." >> "$MEM/server.md"
fi
commit changes
SECONDS=0
"$SR" --dry-run --max-llm 10
echo "elapsed ${SECONDS}s"
