# Secrets layout

Names and purposes only. Never commit, paste or print a secret's content; every file below is mode 0600 (directories
0700), owned by the account that runs the assistant. Check modes with `ls -l ~/.secrets` (names only) and
`stat -c '%a %n' ~/.secrets/*`.

Only two items are needed for the core setup. Everything else belongs to an optional integration and can be left
out: the tool that needs it reports a clear error ("token file not readable", "no hub token") and nothing else breaks.

## Core (required)

| Where | What | Needed by |
|---|---|---|
| Claude Code login (`claude` → `/login`, stored by Claude Code itself) | Claude subscription or API key | the main session, `claude -p` workers |
| `~/.claude/channels/telegram/.env` (key `TELEGRAM_BOT_TOKEN`) | the bot token from @BotFather | the Telegram channel plugin (official plugin or telegram-plus) and every sender script (`tg`, `tg-soft`, `tg-buttons`, `tg-voice`, `event-card`, `tg-outbox`) |
| `~/.claude/channels/telegram/access.json` | allowlist (your own Telegram user id first); not a secret, but it decides who can talk to the bot | the channel plugin; scripts read the first `allowFrom` entry as the owner's chat |

## Optional, per integration

| File under `~/.secrets/` | Format | Feature |
|---|---|---|
| `ha-admin-token` | one line: Home Assistant long-lived access token | Home Assistant (`ha`, `ha-audit`, `ha-apply`, `ha-avail`, lights, cat/shopping helpers); needs `services.ha_url` in the profile |
| `media.json` | JSON, one entry per service with its API key | media stack (`media`, `emby`, `arr-unblock`, `arr-mismatch`, `container-watch`); URLs come from `services.*_url` in the profile |
| `tracearr-key` | one line | media watch statistics (Tracearr) |
| `telegraph-media` | token file | media pages published to telegra.ph |
| `hub-token` | one line, written by the hub install (copied from the hub's `token_brain`) | the hub gateway (MCP server `hub`, `hub`, `hub-mcp-headers`, approvals, reminders, workflows) |
| `hub-tg-token` | one line | the hub's Telegram adapter (claude-hub-tg) |
| `tg-live-bot.env`, `tg-test-bot.env` | env files (bot token) | tg-adapter live/test instances (telegram-plus bridge mode) |
| `browser-ctl-token`, `browser-basicauth.txt`, `novnc.txt` | one line each | the headed browser container (claude-hub-browser) and its remote view |
| `voice-link-token`, `voice-local-token`, `voice-tg.json`, `voice-tg.session`, `gemini-api-key` | token files, a Telegram user-account session, an API key | the voice relay (claude-hub-voice): calls, spoken answers |
| `phone-cli-token`, `phonegw/`, `android/` | token file, gateway keys, app signing material | the companion phone app gateway (claude-hub-phone) |
| `vault-token` | one line | the privacy vault client (`vault ...`); the vault itself is a separate project, not in this repo |
| `jefit-token`, `jefit-token-rw` | one line each | fitness log (`gym`) |
| `unifi-vm.env` | env file | `unifi-health` (UniFi OS Server VM) |

Other credentials outside `~/.secrets`:

| Where | What | Feature |
|---|---|---|
| `~/.ssh/` (a key that the server forces to one command) | SSH key | Unraid server reads/writes through `unraid-ssh` / `unraid-do`; the server-side dispatcher is not in this repo |
| `~/.config/gh/` (`gh auth login`) | GitHub CLI login | `assistant-publish`, `container-watch` release checks |
| `~/.config/<tool>/` | per-tool settings (about 60 folders; no secrets expected, but check with `secretscan --redact` before sharing) | each tool's own options |

## Rules for an agent following this

- Ask the user to place each secret themselves (`install -m 600 /dev/stdin ~/.secrets/<name>` and paste), never echo it.
- To confirm a file exists: `test -s ~/.secrets/<name> && echo present`. Never `cat`, `head` or `grep` it.
- A JSON key file: list key names only, `python3 -c 'import json,sys; print(sorted(json.load(open(sys.argv[1]))))' FILE`.
