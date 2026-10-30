# Telegram — Access & Delivery

A Telegram bot is publicly addressable. Anyone who finds its username can DM it, and without a gate those messages would flow straight into your assistant session. The access file decides who gets through.

**tg-adapter** (the poller) applies it: its live config names the file (`allow_from_file`), and it reads it at every update, so changes take effect without a restart. Unknown senders are dropped silently: nobody gets a reply or a pairing code. The bridge (this plugin) has no gate of its own; it delivers only what the adapter let in. The official plugin, which start-claude falls back to, reads the same file.

All state lives in `~/.claude/channels/telegram/access.json`. The `/telegram-plus:access` skill commands edit it.

## At a glance

| | |
| --- | --- |
| Policy | `allowlist` (unknown senders dropped silently) |
| Sender ID | Numeric user ID (e.g. `412587349`) |
| Group key | Supergroup ID (negative, `-100…` prefix) |
| `ackReaction` quirk | Fixed whitelist only; non-whitelisted emoji silently do nothing |
| Config file | `~/.claude/channels/telegram/access.json` (missing or unreadable: nobody gets in) |

## DM policies

`dmPolicy` controls how DMs from senders not on the allowlist are handled.

| Policy | Behavior |
| --- | --- |
| `allowlist` | Drop silently. No reply. |
| `disabled` | Drop everything, including allowlisted users and groups. |
| `pairing` | tg-adapter treats it as `allowlist`. The official plugin (the fallback) would answer strangers with pairing codes, so keep `allowlist`. |

```
/telegram-plus:access policy allowlist
```

## User IDs

Telegram identifies users by **numeric IDs** like `412587349`. Usernames are optional and mutable; numeric IDs are permanent. The allowlist stores numeric IDs.

To find one, have the person message [@userinfobot](https://t.me/userinfobot), which replies with their ID. Forwarding any of their messages to @userinfobot also works.

```
/telegram-plus:access allow 412587349
/telegram-plus:access remove 412587349
```

## Groups

Groups are off by default. Opt each one in individually.

```
/telegram-plus:access group add -1001654782309
```

Supergroup IDs are negative numbers with a `-100` prefix, e.g. `-1001654782309`. They're not shown in the Telegram UI. To find one, add [@RawDataBot](https://t.me/RawDataBot) to the group temporarily (it dumps a JSON blob including the chat ID).

With the default `requireMention: true`, the bot responds only when @mentioned or replied to. Pass `--no-mention` to process every message, or `--allow id1,id2` to restrict which members can trigger it.

```
/telegram-plus:access group add -1001654782309 --no-mention
/telegram-plus:access group add -1001654782309 --allow 412587349,628194073
/telegram-plus:access group rm -1001654782309
```

**Privacy mode.** Telegram bots default to a server-side privacy mode that filters group messages before they reach your code: only @mentions and replies are delivered. This matches the default `requireMention: true`, so it's normally invisible. Using `--no-mention` requires disabling privacy mode as well: message [@BotFather](https://t.me/BotFather), send `/setprivacy`, pick your bot, choose **Disable**. Without that step, Telegram never delivers the messages regardless of local config.

## Mention detection

In groups with `requireMention: true`, any of the following triggers the bot:

- A structured `@botusername` mention
- A reply to one of the bot's messages
- A match against any regex in `mentionPatterns`

```
/telegram-plus:access set mentionPatterns '["^hey claude\\b", "\\bassistant\\b"]'
```

## Delivery

Configure delivery with `/telegram-plus:access set <key> <value>`.

**`ackReaction`** reacts to inbound messages on receipt (tg-adapter, while it sends). Telegram accepts only a **fixed whitelist** of reaction emoji; anything else is silently ignored. The full Bot API list:

> 👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡

```
/telegram-plus:access set ackReaction 👀
/telegram-plus:access set ackReaction ""
```

**`forwardReactions`** controls inbound emoji reactions: `true` (default) forwards added or changed reactions to the assistant, `"all"` also forwards pure removals, `false` forwards none.

**`replyToMode`**, **`textChunkLimit`** and **`chunkMode`** are read only by the official plugin (the fallback): threading of chunked replies (`first` default, `all`, `off`), the split threshold (Telegram rejects over 4096 characters) and the split strategy (`length` or `newline`). tg-adapter splits by itself.

## Skill reference

| Command | Effect |
| --- | --- |
| `/telegram-plus:access` | Print current state: policy, allowlist, enabled groups. |
| `/telegram-plus:access allow 412587349` | Add a user ID. |
| `/telegram-plus:access remove 412587349` | Remove from the allowlist. |
| `/telegram-plus:access policy allowlist` | Set `dmPolicy`. Values: `allowlist`, `disabled`. |
| `/telegram-plus:access group add -1001654782309` | Enable a group. Flags: `--no-mention` (also requires disabling privacy mode), `--allow id1,id2`. |
| `/telegram-plus:access group rm -1001654782309` | Disable a group. |
| `/telegram-plus:access set ackReaction 👀` | Set a config key: `ackReaction`, `forwardReactions`, `replyToMode`, `textChunkLimit`, `chunkMode`, `mentionPatterns`. |

## Config file

`~/.claude/channels/telegram/access.json`. An absent or unreadable file lets nobody in.

```jsonc
{
  // Handling for DMs from senders not in allowFrom: allowlist | disabled.
  "dmPolicy": "allowlist",

  // Numeric user IDs allowed to DM.
  "allowFrom": ["412587349"],

  // Groups the bot is active in. Empty object = DM-only.
  "groups": {
    "-1001654782309": {
      // true: respond only to @mentions and replies.
      // false also requires disabling privacy mode via BotFather.
      "requireMention": true,
      // Restrict triggers to these senders. Empty = any member (subject to requireMention).
      "allowFrom": []
    }
  },

  // Case-insensitive regexes that count as a mention.
  "mentionPatterns": ["^hey claude\\b"],

  // Emoji from Telegram's fixed whitelist. Empty string disables.
  "ackReaction": "👀",

  // true | "all" | false — forward emoji reactions to the assistant.
  "forwardReactions": true,

  // Official plugin only: threading on chunked replies (first | all | off), split threshold, split strategy.
  "replyToMode": "first",
  "textChunkLimit": 4096,
  "chunkMode": "newline"
}
```
