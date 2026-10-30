---
name: night-log
description: "What the assistant did overnight, as a short card from its own logs (finished and started changes to its setup, helpers it launched; token-free). Use whenever the owner asks what I did or worked on during the night, while they slept, since yesterday evening or this morning, for a night report or recap, and for the /night command. Also the seed for the morning report. Not for what is waiting on the owner (skill waiting-card) or their own plans (skill calendar)."
---

# Night log

The owner asks this in the morning or during a sleepless night. The answer is already on disk: brain commits
("before X" / "after X") and the Agent descriptions in my transcript.

1. Run `night-log` (default window: last night 22:00 to 09:00 in {{locale.tz}}; `--since "YYYY-MM-DD HH:MM"` for
   another window, `--json` for the raw items). Send the card as it is with the reply tool.
2. Add at most two lines of my own: what is useful to the owner from the night (a finished feature they can try now,
   one decision needed). No list of internal fixes beyond the card.
3. For the morning report, start from `night-log --json` instead of re-reading PROACTIVE.md and CHANGELOG.md.

The card holds only my own commit messages and helper titles: never put private data or message text into them,
so the card stays safe to send.
