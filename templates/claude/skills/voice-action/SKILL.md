---
name: voice-action
description: Turn one message with several intents (usually a 🎙️ voice note, any language mix) into a confirm card, then run everything at once. Use when a voice note or typed message carries 2+ actionable intents (reminders, events, list items, notes) or several list items ("put milk and eggs on the shopping list", "add X and Y to my todo"); for "show my lists" and non-shopping lists ("X is done" on the todo list); and for button presses with callback data vp:ok:…, vp:fix:…, vp:no:…. A single simple reminder or event stays with skills reminders / calendar; one shopping item, "what's on my shopping list" and "bought X" → skill shopping (if installed). Uses ~/bin/voice-plan and ~/bin/lists.
---
# Voice → action

## Extract
- Work out dates from `date` in the owner's time zone ({{locale.tz}}), with the reminders skill's rules (weekday =
  the next one; day only → 09:00, visible on the card).
- One action per intent, in the owner's words and language, short. Reminder vs event: something they attend with a
  time and place is an event (skill calendar); a nudge to do something is a reminder.
- Kinds (strict schema, `voice-plan --help`):
  `{"kind":"reminder","when":"YYYY-MM-DD HH:MM","text":…}` ·
  `{"kind":"event","start":…,"end":…,"title":…,"location":…,"description":…}` (end optional = +1 h; all-day: dates
  only) · `{"kind":"list","list":"shopping|todo|<name>","items":[…]}` · `{"kind":"note","text":…}` (kept on the
  `notes` list).
- Small talk, questions and things meant for me (not for the owner) are not actions; answer or handle them
  separately.
- Unsure about one detail: take the likely reading; the card shows it and ✏️ Fix corrects it.

## Card
1. Write `{"transcript": "<the owner's message>", "actions": [...]}` to a scratch file; `voice-plan render FILE --json`.
2. Exit 1 = validation errors (`#N kind: problem`): fix the JSON and render again; ask the owner only when it's
   their call.
3. `reply` with the card `text` and `buttons` exactly as returned. No extra prose.
- A lone list add needs no card: shopping items → `shop add "milk"` (skill shopping, when Home Assistant is set up);
  other lists → `lists add todo "…"`; react 👍.
- Shopping items on a card: `voice-plan run` sends them to `shop add` (one per item), not to `lists`.

## Buttons
- `vp:ok:ID` → `voice-plan run ID`. Lines starting with `{` are events still to create: for each, create the event
  with those fields following skill calendar (without the calendar feature: say the event wasn't added). Then react
  🎉 on the card and send one line ("Done: 1 reminder, 2 events, 3 items"), plus any ✗ line.
  - Exit 3 = already run or dropped (double tap): nothing to do. Exit 4 = a time has passed: fix it, render with
    `--replace ID`, send the new card.
- `vp:fix:ID` → ask what to change (one line). Apply the answer to `voice-plan show ID --json` actions,
  `voice-plan render FILE --replace ID --json`, send the new card.
- `vp:no:ID` → `voice-plan drop ID`, react 👍.
- Undo a run's reminders: `remind cancel --tag vp:ID`; list items: `lists rm LIST …`; shopping items:
  `shop rm ID…`.

## Lists
- "My lists" → `lists show` (or `lists ls`), plus `shop list` when shopping is set up. Send the output as plain
  lines.
- Bought / finished → `lists done LIST N|"text"…` (numbers from `show`). Exit 1 = not found or ambiguous, nothing
  changed: show the list and ask.
- `lists rm` for a wrong entry, `lists clear LIST --done` to drop checked items (they also go after 7 days).
- Aliases: einkauf/groceries → shopping; to-do/aufgaben → todo; other names free-form.
