---
name: research
description: On-demand deep research for the owner - a multi-agent web research run that ends in a cited, phone-friendly HTML report plus a short Telegram summary. Use for "/research <question>", "research X for me", "deep research on X", "dig into X", "find out everything about X", "compare the options for X with sources", "what's the state of X (cited)", and "what did you research about X" / "my research reports" (`research list`). Not for a quick fact one search answers (answer directly), shop prices (skill deals) or calendar plans (skill calendar). Uses ~/bin/research (new, render, summary, list).
---

# Research: question → fan-out → cited report

I am the coordinator: I plan, spawn, verify and deliver; the researchers search and read; one writer writes. I never
paste research notes into my own context beyond the agents' short final summaries.

Optional base: Anthropic's `deep-research` skill (`$DR` below), if installed, supplies `references/researcher.md`
(notes format) and `references/report-writer.md`. A new install may not have it: then use the formats quoted here.
`~/bin/research` needs markdown-it-py (its venv, see `research --help`).

## 0. Acknowledge (at once)
- React ✍ and reply with a rough duration ("On it, ~15 min"); then one new message when done, no progress pings.
- `research list` and `recall "topic"`: a recent report on the same topic → offer it or build on it.

## 1. Clarify only if truly ambiguous
Ask only when the answer changes materially and can't be inferred (two things share the name, the market or
jurisdiction differs, buy vs understand). Then ONE either/or question with buttons; start on the unambiguous part
meanwhile. Otherwise assume and state it: defaults = the owner's country (`profile-get locale.country`), home town
{{home.town}}, the setup in FACTS.md, today's date. Assumptions go into the report's front matter.

## 2. Start
`research new "<question as the owner said it>" --json` → `id`, `md` (skeleton with front matter), `notes` dir.
Reports live in ~/reports/research/<id>.md|.html, notes in ~/reports/research/work/<id>/notes/.

## 3. Plan the angles (3-6, distinct, together complete)
3 for a focused topic, 4 for options with several criteria, 5-6 for many entities or regions. Typical angles: the
options themselves (prices, specs) · official/primary sources · independent tests · user and expert experience ·
local specifics · risks and recent changes. One researcher per angle; no overlap. `agent-watch status` shows how
many subagents run; not enough room → fewer angles (min 2), or research it in-session (≤ 12 searches).

## 4. Fan out (one message, all in parallel, background)
Agent, `subagent_type: general-purpose`, `run_in_background: true`, `effort: high` (`xhigh` for technical or
numbers-heavy angles). Prompt:
```
Research {narrow angle}.
Objective: {what the notes must cover}
Key questions:
- {2-4 specific questions}
Suggested sources: {official pages, primary data, independent tests, forums}
Constraints: {"as of <month year>", region and currency, relevant setup}; public web only; never message anyone;
don't use the headed browser (shared): a blocked page is a gap.
Notes per key question: Takeaway / Cited Findings / Inferences / Gaps; every fact with [Publisher, date](URL);
flag a key fact resting on one source with "(1 source)". Save to {notes}/{angle}.md. Stay within ~15 tool calls.
```
Only the facts an angle needs go into prompts; private data never. Wait for the notifications (no polling); a
researcher silent > 12 min: proceed without it and list its angle under gaps.

## 5. Verify the key claims
Pick the 3-5 claims the answer hinges on. Any with "(1 source)", conflicting sources or only vendor backing → one
verifier agent (`effort: high`): find a second independent source or a contradiction for each, save to
{notes}/verification.md, ≤ 12 tool calls. Skip when every key claim already has two independent sources.

## 6. Write
One writer agent (`effort: xhigh`): read {notes}/ (verification.md wins), write into {md}, keeping its front matter
and adding: title (≤ 60 chars, says the answer), confidence high|medium|low, agents, assumptions, summary (≤ 4 "- "
bullets). Body: `# Title`, a 2-4 sentence bottom line, 3-5 `## ` sections (a table when comparing ≥ 3 options),
inline {high}/{medium}/{low} markers after key claims, last section `## How sure this is`. Cite inline as
([Publisher, date](URL)); no Sources section (generated); no raw HTML.

## 7. Render and check
`research render {md} --json` → HTML next to the md, numbered sources, `warnings`, `errors` and the draft `summary`.
Exit 3 = invalid (no title / nothing cited): fix the md (or send the writer back once). The bottom line must answer
the question asked.

## 8. Deliver (a new message, so it notifies)
`reply` with the render's `summary` (≤ 8 lines) and `files: [html]`. No new claims beyond the report. Far past the
ETA: one short note with a new ETA first.

## Rules
- 10-20 min wall time; researchers ≤ 15 tool calls each; one verify round; one writer.
- Setup facts learned → FACTS.md (`memory set`) with the report path; decisions the owner makes from a report →
  a ledger line.
- Reports are the owner's private files: never publish them unless they ask.
