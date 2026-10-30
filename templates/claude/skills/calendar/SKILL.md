---
name: calendar
description: The owner's Google Calendar (claude.ai Google Calendar tools). Use for "what's on Saturday", "what's my week", "am I free Friday evening", "when could I fit in X", "put X in my calendar", "trag das ein", "add my flight / train / dinner", "move / cancel X" and conflict checks. Adds obviously-needed events without asking; asks before changing or deleting the owner's own. Weekly travel-and-details pass → skill calendar-gap-pass; "remind me" → skill reminders; several intents in one message → skill voice-action.
---
# Calendar

Tools: `mcp__claude_ai_Google_Calendar__*` (`list_events`, `search_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `suggest_time`, `list_calendars`); load them with ToolSearch when deferred.
Times are {{locale.tz}} (`profile-get locale.tz`): pass local ISO times with no offset and no `Z`; set `timeZone` only for an event in another zone (a flight abroad). Work out "Saturday", "tomorrow", "next week" from `date`.

## Lookups
- "What's on X": `list_events` for the whole day or range, orderBy startTime. Skip declined events; all-day items first.
- "When is X": `search_events` by keyword; `get_event` for details.
- "Am I free…": list the window; busy = not declined and not marked free. Name the conflict ("19:00 🍽️ Dinner"), counting travel legs.
- "When could I…": `suggest_time` with the primary calendar id (from `list_calendars`), `durationMinutes`, `startHour`/`endHour`/`excludeWeekends`. Drop slots in quiet hours; offer 2–3 as buttons.
- Scripts read a token-free copy: `calcache` (next 21 days). With the hub, the typed helpers `mcp__hub__calendar_events|busy|cache|import` read the same cache. After a big change, refresh it (`calcache import FILE` with a saved `list_events` result).

## Adding an event
1. Duplicates first: `search_events` / `list_events` around the date. Update the existing event rather than adding a second.
2. Title: short, emoji prefix: 🚆 train, 🚌 bus, ✈️ flight, 🚗 drive; a fitting one for the rest (🍽️ 🎂 🎉 🎵 🩺 💼 🏋️).
3. Location: venue name + full address (from the web or what the owner said).
4. Description: what, who (first names), door/start times, what to bring, links, how to get there. When enriching the owner's event: keep their text, append below, end with "(details added by assistant)".
5. How to get there: in Switzerland `route home "<venue address>" --arrive HH:MM --date YYYY-MM-DD` plans door to door ("home" = the profile's home address); elsewhere a rough time from the web. Best connection + one later option.
6. Travel legs get their OWN event each (start = departure, end = arrival, location = departure stop), titled with the direction (`🚆 Home → <town>`, `🚆 <town> → Home`). Description ≤ 4 lines: the ride chain with times, lines, platforms and changes, one fallback. Return legs too when known. Recurring events (classes, office days): no weekly legs.
7. Attendees only on request. Whenever guests are on an event, set the notification level to NONE. Default reminders.
8. Read back, then confirm: after every create / update / move, `get_event` on the returned id and check title, date and time against what you meant (a mismatch → fix it first and say so). One line per event in the reply, with a URL button from the read-back's `htmlLink` (`📅 Open in Calendar`); no `htmlLink` → no button, never a made-up link.

## Ask or just do it
- Without asking: booked flights and trains, confirmed bookings and reservations, appointments the owner committed to, hard deadlines, travel legs for their events, location/details on any upcoming event.
- Ask first (buttons): events they only might attend, tickets they don't hold yet, unconfirmed invitations.
- The owner's own events (no "(details added by assistant)" marker, not a leg I created): never change time, title or attendees and never delete without their OK. Adding location/details is fine.
- My own events: fix or remove freely when plans change (a stale leg, a duplicate); mention it in the report.
- A new event overlapping another: add it if it's obvious and flag the overlap; never move the owner's events to make room.

## Reminders vs events
- "Remind me to X at T" → skill reminders, not an event.
- Something with a time and place the owner attends → event. Both when asked ("put it in and remind me the day before"): event + reminder, or `overrideReminders` on the event.

## Replies
- Phone format: one line per event, "Sat · 19:00 🍽️ Dinner, Venue"; a week = one line per day; details behind buttons.
- Unsolicited notices only outside quiet hours.

## Periodic
- skill calendar-gap-pass: weekly pass over the next 14 days for missing travel legs and details.
