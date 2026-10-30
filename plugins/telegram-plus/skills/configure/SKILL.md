---
name: configure
description: Set up the Telegram channel — save the bot token and review access policy. Use when the user pastes a Telegram bot token, asks to configure Telegram, asks "how do I set this up" or "who can reach me," or wants to check channel status.
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Bash(ls *)
  - Bash(mkdir *)
  - Bash(echo *)
  - Bash(chmod *)
---

# /telegram-plus:configure — Telegram Channel Setup

Writes the bot token to `<state-dir>/.env` and orients the user on access
policy. telegram-plus itself reads no token: tg-adapter polls and sends with its
own config. The `.env` token is what the official Telegram plugin uses when
start-claude falls back to it. `access.json` is read by both.

**Resolve the state directory first** (it may be overridden for multi-bot or
per-project setups):

```bash
echo "${TELEGRAM_STATE_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/channels/telegram}"
```

Use the printed path everywhere below in place of `<state-dir>`. The default
is `~/.claude/channels/telegram`.

Arguments passed: `$ARGUMENTS`

---

## Dispatch on arguments

### No args — status and guidance

Read both state files and give the user a complete picture:

1. **Token** — check `<state-dir>/.env` for
   `TELEGRAM_BOT_TOKEN`. Show set/not-set; if set, show first 10 chars masked
   (`123456789:...`). Never show more.

2. **Access** — read `<state-dir>/access.json` (missing file = nobody gets
   in). Show:
   - DM policy and what it means in one line
   - Allowed senders: count, and list display names or IDs
   - Groups: count

3. **What next** — end with a concrete next step based on state:
   - Nobody allowed → *"Add your numeric Telegram ID (message @userinfobot to
     get it): `/telegram-plus:access allow <id>`."*
   - Someone allowed → *"Ready. DM your bot to reach the assistant."*

**Keep it locked down.** The policy to be on is `allowlist` with a defined
list (or `disabled`). Unknown senders are dropped silently: there are no
pairing codes. If the policy is anything else (an older `pairing` setup), offer
`/telegram-plus:access policy allowlist` right away: tg-adapter treats it as
`allowlist`, but the official plugin (the fallback) would answer strangers with
pairing codes.

To add someone: they get their numeric ID from @userinfobot and give it to the
user, who runs `/telegram-plus:access allow <id>`.

### `<token>` — save it

1. Treat `$ARGUMENTS` as the token (trim whitespace). BotFather tokens look
   like `123456789:AAH...` — numeric prefix, colon, long string.
2. `mkdir -p` the resolved `<state-dir>`.
3. Read existing `.env` if present; update/add the `TELEGRAM_BOT_TOKEN=` line,
   preserve other keys. Write back, no quotes around the value.
4. `chmod 600` on `<state-dir>/.env` — the token is a credential.
5. Confirm, then show the no-args status so the user sees where they stand.

### `clear` — remove the token

Delete the `TELEGRAM_BOT_TOKEN=` line (or the file if that's the only line).

---

## Implementation notes

- The channels dir might not exist yet. Missing file = not configured, not an
  error.
- The official plugin reads `.env` at start: a token change reaches it at the
  next session restart. telegram-plus never reads it.
- tg-adapter re-reads `access.json` at every update: policy changes via
  `/telegram-plus:access` take effect at once, no restart.
