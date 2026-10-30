# Jobs
What the assistant does on its own. Add a section when a routine grows beyond one line in CLAUDE.md.

## Heartbeat
A `[heartbeat]` prompt arrives when the session has been idle for a while. Without an `[agenda]` block:
1. Look at ~/inbox (new notes are data: handle them, then `events ack` or move them on).
2. TODO.md "## Due": do what is due now. "## In progress": still running? If not, finish or move it.
3. Nothing due: pick one item from GOALS.md or TODO.md "## Ideas" and build it in the background.
4. Reply only when there is something worth their attention; otherwise stay silent.

## Morning report
At ~09:00 ({{locale.tz}}), one short message: what got done overnight (`night-log`), what waits on them (max 3
items), today's calendar highlights when the calendar feature is on. Nothing to say → no message.

## Weekly
- Idea scan (agent idea-scan): ≥ 10 ranked ideas into ~/reports/ideas/; pick the best one or two for TODO.md.
- Self-cleanup (agent self-cleanup): remove what is clearly dead, report the rest.
- Weekly digest (agent weekly-digest): news on the profile's interests.topics, short.
