---
name: reminders
description: Set, list, move or cancel reminders for the owner, one-off or recurring ("remind me Friday 18:00 to …", "every Monday at 9 …", "erinnere mich morgen an …", "what reminders do I have", "cancel the dentist reminder"), wake-up and departure calls, and reminders by place ("remind me when I get home"). The hub's reminder timers send them to Telegram at the exact minute, with no tokens and no need for the session to be awake. Not for several intents in one message (skill voice-action) or something the owner attends with a time and place (skill calendar).
---
# Reminders (typed tools `reminders.*`; CLI `~/bin/remind`)

Times are {{locale.tz}} (`profile-get locale.tz`), `YYYY-MM-DD HH:MM` or a phrase.

Typed tools first: MCP `mcp__hub__reminders_when|list|add|snooze|move|cancel|press` (server `hub`). Until that server is loaded, the same gateway from Bash: `tool call reminders.add '{"text": "Pick up the dry cleaning", "owner": true, "when": "tomorrow evening", "via": "telegram"}'` (`tool show NAME` = every field). Errors come back as `code: message (hint: …)`. Fields: `owner` (required: true = the owner's, false = a note for me) · `when` (time or phrase) or `on` (home | leave | zone:NAME | leave:zone:NAME) · `every` + `until` · `call` · `soft` · `tag` · `via` (telegram | voice | app). Use IDs (8 hex), never list numbers, with the tools.

The `remind` CLI covers the rest (`done ID`, `skip N|ID`, `buttons ID`) and scripts; `remind help` lists every option. Cheat sheet: `add` · `list [--ids|--json]` · `when PHRASE` · `move N|ID WHEN` · `snooze ID WHEN` · `done ID` · `skip N|ID` · `cancel N|ID|--match TEXT|--tag TAG` · `press DATA`.

## Set
- Pass the owner's words as the time: `remind add --owner --via telegram "tomorrow evening" "Pick up the dry cleaning"`. remind reads English and German phrases ("in 2 hours", "friday 18:00", "morgen früh", "this weekend") and, with the calendar cache filled, calendar anchors ("2h before my flight"). It prints what it read: confirm with that.
- Unsure what a phrase means? `reminders.when` first (`remind when PHRASE`; stores nothing). Anything it can't read (exit 2): work it out yourself and pass `YYYY-MM-DD HH:MM`.
- A nudge I set on my own (not the owner's request): add `--soft`.
- A weekday name means the next one (never today); a bare "at 7" = the next 07:00/19:00 still ahead; a day without a time = 09:00 (say so).
- The text is what the owner reads ("⏰ <text>"): short, in their language.
- Confirm with 👍 or one line with the exact time ("⏰ Fri 18:00"), especially when I interpreted something.

## Recurring
- `remind add --owner --every RULE [--until YYYY-MM-DD] "first YYYY-MM-DD HH:MM" "text"`
- RULE: `daily`, `weekdays`, `weekends`, `weekly`, `weekly:mon,thu`, `monthly`, `monthly:15`, `monthly:last`, `yearly`, `every:3d` / `every:2w`.
- The first datetime is the first occurrence; a past start moves to the next future slot. Wall time is kept across DST; after downtime it fires once, not once per missed slot.
- Confirm with the rule and the first time: "⏰ every Mon 09:00, first Mon".

## Wake and departure calls (voice feature)
- "Call me at 7", "ring me when it's time to leave" → `remind add --call "YYYY-MM-DD HH:MM" "text"` (implies `--owner`; works with `--every`). It rings once via `voice-relay`, then the text goes out anyway; unanswered → the text starts "📞 tried calling". One attempt, never a retry. Without the voice feature, say calls aren't set up and set a normal reminder.
- The text is also what the call opens with: short and speakable.

## By place
- `remind add --on home "text"` (next arrival home), `--on leave` (leaving home), `--on zone:NAME` / `--on leave:zone:NAME` (a Home Assistant zone). `--every` = each time; `--urgent` = also during quiet hours.
- Needs a presence source (the phone app or the vault); it reads only a coarse zone, never coordinates. Without one, say place reminders aren't set up and offer a timed reminder instead.

## Move, list, skip, cancel
- "Move X to Friday 5pm" → `remind move N|ID "friday 17:00"` (one-offs only; never cancel + add).
- `remind list`: numbered; show the owner only `[owner]` and 📍 entries (the rest are my own follow-ups).
- `remind cancel N|ID` ends a one-off or a whole series; `remind cancel --match TEXT` removes every match (list first if it could hit more than one).
- `remind skip N` skips only the next occurrence of a series.
- `remind snooze ID WHEN` / `remind done ID`: WHEN = +1h, +30m, +2d, tonight, tomorrow or `YYYY-MM-DD HH:MM`.
- `remind cancel --tag TAG` is for scripts: don't remove tagged reminders by hand unless the owner asks.

## Delivery
- The hub's timers run `remind due` at the exact minute; `remind hub fallback` runs the pass while the hub is down. Owner reminders go out with buttons +1 h / Tonight / Tomorrow / Done.
- A failed send lands in ~/inbox for me to deliver (the note has the ID; buttons: `remind buttons ID`).
- Button presses with data `rm:…` are handled by the Telegram adapter when bindings are on; one that still arrives as a button event: run `remind press DATA`, then react 👍.
