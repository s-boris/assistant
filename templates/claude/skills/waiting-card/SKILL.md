---
name: waiting-card
description: "What is waiting on the owner right now, as a short card from TODO.md's Waiting on you list (token-free, same items as the pinned board). Use whenever the owner asks what is waiting on them, what they still have to decide or do, whether I need anything else from them, what they can unblock now, or offers help (\"anything else I can do?\"), and for the /waiting command and its button. Not for my own running work (the pinned board / In progress) or for reminders (skill reminders)."
---

# Waiting card

The owner asks this when they have a few minutes and want to clear my blockers. The answer is already on disk, so it
should cost one command, not a search through TODO.md, the calendar and recall.

1. Run `waiting-card` (`--json` for the raw list). It prints at most ~10 lines: the Waiting on you titles with a
   short status, plus the Due count. Send them as they are with the reply tool.
2. If the owner said they have time now, follow with ONE either/or question for the top item (buttons; one question
   per keyboard), or a multi-choice poll when they could clear several at once.
3. An empty list is an answer too: say nothing is waiting and, if useful, name the next thing I am building.

Keep the TODO.md titles owner-readable (the card shows them verbatim). Answering here counts as the waiting nudge,
so `waiting-nudge` can skip its next round.
