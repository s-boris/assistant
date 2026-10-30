---
name: kb
description: "Knowledge base: cited answers from everything the assistant knows and built (FACTS.md facts and the done/declined ledger, auto-memory, notes, reports, skills, agents, ~/bin script headers, git logs of the repos under ~/opt). Use for /kb, \"what did we decide about X\", \"did the owner say no to X\", \"how does Y work\", \"why did we do Z\", \"when did we change X\", \"where is X configured\", \"what do I know about X\", and before asking the owner anything that may already be settled. Also for the memory2 review queue (~/inbox/memory2-review-*.md notes, `memory2 review`) when memory2 is set up. Not for live state (check the thing itself)."
---

# Knowledge base (kb)

Everything the assistant knows and built is on disk; `kb ask` finds it with citations in well under a second,
token-free. FACTS.md (with its done/declined ledger) is authoritative: a fact there beats an older report or chat
line.

## Answering a question

1. Run `kb ask "the question in plain words"` (`-n 8` for more, `--chat` to include conversation hits, `--json`).
   It prints settled facts first (`F1 [fact|ledger DD.MM verb|memory|auto …] text (citation)`), then passages
   (`#ID date citation  text`). Citations: `path:line` (relative to home), `opt/REPO@HASH` for commits,
   `chat/owner tg#ID` for conversation.
2. Read what you need: `recall --show ID -C 2` for a passage, or open the cited file at the line.
3. Answer in a few lines and name the source ("FACTS ledger: declined", "report X"). When sources disagree, the
   newest settled one wins (FACTS.md, then dated notes); say so. Nothing found: say so; never guess.
4. For the owner on Telegram: short, no paths unless they ask; offer details behind a button only when useful.

An `[auto … unreviewed]` fact was learned from the owner's messages but not reviewed yet: treat it as likely, not
settled.

## Before asking the owner anything

Run `kb ask` first. A question the ledger already settles (done, declined, skipped) is never asked again; a
decision the owner already made is never relitigated without new facts.

## memory2 review queue (optional)

`memory2` keeps a fact store that learns from the owner's messages with local models only (it needs its local
model and embedding server; `memory2 stats` shows whether they run). Without it, `kb ask` still works on files.

- `memory2 review`: open items: `auto` (accepted into the store, not in FACTS.md yet), `pending` (needs a look),
  `conflict` (contradicts a stored fact; the `vs` line shows which and why).
- Durable and right → `memory2 promote ID --section "Section name"` (a fact line) or
  `memory2 promote ID --ledger "verb: thing (details)"` (verbs: declined skipped scratched removed off done decided
  kept); `--text "…"` rewrites it first. It inserts one line and verifies every existing line survives.
- Right but not FACTS-worthy → `memory2 accept ID` (stays in the store). Noise → `memory2 reject ID`.
- A real conflict: supersede the old FACTS.md line first (`memory supersede ID`, never a hand edit; the owner's
  newer word wins), then promote or reject. Never ask the owner about a queue item unless it changes a decision
  they have to make.
- `memory2 hook off|on` is the kill switch; `memory2 search "text"` shows what the hook would inject.
