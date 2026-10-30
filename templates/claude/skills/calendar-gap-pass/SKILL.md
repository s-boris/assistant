---
name: calendar-gap-pass
description: Pass over the owner's Google Calendar (next 14 days by default) that adds missing travel-leg events and missing details (location, how to get there) without asking. Use when the weekly "calendar-gap-pass" inbox note arrives, after big plan changes, or when the owner asks to "add the trains / flights for X" or "put the travel for X in my calendar".
---
# Calendar gap pass

Conventions (titles, legs, read-back, what needs the owner's OK) are skill calendar's; this is the batch version.

1. `list_events` from now to +14 days (orderBy startTime).
2. For each event the owner attends somewhere other than home ({{home.town}}):
   - **Travel legs** (train, bus, flight, drive) get their own event each, as in skill calendar step 6: direction in the title (`Home → X`, `X → Home`), start = departure, end = arrival, location = departure stop, a short ride-chain description. Plan public transport with `route` (Switzerland) or the web. Skip when a leg already exists in the window before it.
   - **Details**: location with address, door/start times, what to bring, from the web or what the owner told me. Keep their own text, append below with "(details added by assistant)".
   - Recurring weekly events (classes, office days): no weekly travel events.
3. Unclear (which venue, whether they go at all): leave it and collect it for one question; never guess an address.
4. No message unless something needs the owner's decision. Log one line in ~/workspace/PROACTIVE.md (counts only).
5. Refresh the script cache afterwards when legs changed (skill calendar: `calcache import`).

Private details stay out of logs and notes: counts and titles only.
