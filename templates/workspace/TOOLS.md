# Tools
How the assistant reaches the owner and its tools. Short, pointers only; add what you learn.

## Owner channel
- Telegram through the channel plugin: `reply` (text, files), `react` (👍 got it, 👀 looking), `edit_message` for
  progress. The access file (~/.claude/channels/telegram/access.json): read only its `allowFrom` list, never edit it.
- These need the telegram-plus plugin (ships in ~/opt/assistant/plugins/telegram-plus, the recommended install):
  `reply` with format "rich" (Markdown: headings, lists, tables) and `details` (text folded behind a tap), inline
  buttons with `on_press` (mark / remove / keep after a press) and URL buttons, `send_poll`, `draft` (live progress),
  `pin_message`, `send_location`, `set_commands`. Presses and poll answers arrive as events with their callback data.
- The official plugin (telegram@claude-plugins-official, checked at 0.0.7) has only `reply` (plain text or escaped
  MarkdownV2, files), `react`, `edit_message` and `download_attachment`: no buttons, polls, drafts, folds or rich
  format. With it, ask with numbered options in plain text.
- One question per keyboard; several options that combine → a poll (telegram-plus).
- Quiet hours (see CLAUDE.md): no messages, unless something is urgent and they asked for that kind of alert.

## Tools
- The command-line tools live in ~/bin; each one documents itself: `<tool> --help`.
- Useful first: `memory` (facts and the ledger), `kb ask` and `recall` (what you already know), `events` (the agenda
  and ~/inbox notes), `watch` (page watches), `research`, `night-log`, `profile-get` (the owner's settings).
- Typed hub tools (only with the hub feature): ~/opt/assistant/docs/tools.
- Feature switches: `profile-get --features`; a tool of a feature that is off stops with "feature X is off".

## Private data
- Their documents, mails and chats are answered from, not copied: summaries and facts, never the full text unless
  they ask for it.
- Nothing private in notes, memory, commit messages or anything that leaves the machine.

## Session plumbing
- Background work: subagents (Agent tool) for anything longer than a few tool calls; give each a clear brief.
- Tools change in ~/opt/assistant (git branch → `assistant land BRANCH`); `assistant drift` shows hand edits in ~/bin.
- Scheduled jobs: `assistant cron list|add|rm`; `assistant cron check` compares the crontab with the registry.
- Restart after settings, plugin or CLAUDE.md changes.
