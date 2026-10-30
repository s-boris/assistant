# telegram-plus: launch, switches, changes, rollback

## How it loads
- A custom channel plugin passed via `--channels` must be on the channel allowlist, else Claude Code skips it ("not on
  the approved channels allowlist"). `ops/managed-settings.json` (→ /etc/claude-code/managed-settings.json) sets
  `channelsEnabled` and `allowedChannelPlugins`; that list replaces the default allowlist, so the official plugin stays
  in it. Never use `--dangerously-load-development-channels`: its confirmation dialog on every start would hang
  unattended restarts (never auto-confirm it either).
- Marketplace `home-plugins` = ~/plugins, a local directory: nothing is copied, the server runs in place
  (`CLAUDE_PLUGIN_ROOT` and cwd ~/plugins/telegram-plus). The plugin is installed and disabled at user scope
  (`claude plugin install telegram-plus@home-plugins && claude plugin disable --scope user telegram-plus@home-plugins`);
  only start-claude's `--settings` enables it, so no other claude process loads it.
- /etc and /usr/local/bin are rebuilt at container start: ~/boot.sh installs the managed settings and `ops/start-claude`
  as /usr/local/bin/start-claude (idempotent, `cmp` first). The entrypoint runs `[ -x ~/boot.sh ] && ~/boot.sh`
  before tmux and restarts start-claude whenever it exits.

## Modes (ops/start-claude)
One poller per bot token: the plugin in use decides who polls.
- **bridge**: telegram-plus is installed (`ls -d ~/.claude/plugins/cache/home-plugins/telegram-plus/*/`) and
  `~/.telegram-official` is absent. A poller left by the previous session (`bot.pid`, a Telegram plugin's server) is
  stopped, `~/.config/tg-adapter/live.off` removed, `tg-adapter ensure --force --instance live` run; the live adapter
  must be running within `BRIDGE_WAIT` (15 s).
- **official**: `~/.telegram-official` exists or telegram-plus isn't installed. `live.off` is made and a running live
  adapter stopped (SIGTERM) before claude starts.
- **fallback** (bridge → official): no `~/.config/tg-adapter/live.json`, the adapter not running in time, or two fast
  failures in a row (the session exits within 60 s) in bridge mode. The adapter goes off (`live.off`, stopped), `~/.telegram-official` is
  made (so the next starts stay official), the owner gets one text by `tg`, and
  `~/inbox/start-claude-bridge-fallback-<epoch>.md` tells the session what happened.
- The mode of the running session: `~/.local/state/start-claude/telegram-mode` (`bridge` | `official`); bridge starts
  and fallbacks are logged in `~/.local/state/start-claude/bridge.log`.

## Switches
- `touch ~/.telegram-official && restart "official telegram"`: the official plugin, the adapter off. Back:
  `rm ~/.telegram-official && restart "bridge"` (start-claude removes `live.off` and starts the adapter).
- `live.off` is tg-adapter's own off switch (checked before every poll); start-claude manages it. Made by hand in
  bridge mode, the session gets no inbound.
- `touch ~/.heartbeat-mod-off && restart "heartbeat mod off"`: the heartbeat mod off (`rm` it and restart to turn it
  back on); cron keeps beating either way. Two fast failures on the official plugin set it too.

## Changing the plugin
The live server runs straight from ~/plugins/telegram-plus: any edit there goes live at the next server start,
a container restart included. Never edit in place: ~/plugins has no git of its own, its files are deployed byte for
byte from the harness repo (~/opt/assistant, plugins/), and a deploy refuses a live-edited file. Develop on a repo
branch in a worktree:
```
git -C ~/opt/assistant worktree add -b <branch> ~/jobs/<name>
cd ~/jobs/<name>/plugins/telegram-plus
bun install --frozen-lockfile
LD_PRELOAD=~/opt/test-all/netguard.so bun test
LD_PRELOAD=~/opt/test-all/netguard.so bun test/offline.ts
bash test/start-claude.sh
claude plugin validate ~/jobs/<name>/plugins/telegram-plus
```
Bump the version (package.json and .claude-plugin/plugin.json), commit, `assistant land <branch>` (CI, then the
deploy writes ~/plugins; the running server keeps its files until the restart), then:
```
claude plugin marketplace update home-plugins
claude plugin update telegram-plus@home-plugins     # keeps the cache/version record in step
sudo install -m 755 ~/plugins/telegram-plus/ops/start-claude /usr/local/bin/start-claude   # when the launcher changed
restart "<why>"
```
After the restart: `plugin-drift` shows nothing pending; an inbound message arrives; Claude Code's MCP log for the plugin has no "Channel notifications
skipped"; `cat ~/.local/state/start-claude/telegram-mode` says `bridge`; `tg-adapter status --instance live` shows
the poller; `flock -n ~/.local/state/tg-bridge/lease true` exits 1 (the bridge holds the lease).

## Session settings
Both launchers pass `--settings` with `"disableAgentView":true,"crossSessionInbound":"refuse"` and enable the
heartbeat mod (`heartbeat@home-plugins`, source ~/plugins/heartbeat, installed but disabled at user scope). Never move
`disableAgentView` into the env (CLAUDE_CODE_DISABLE_AGENT_VIEW) or user/managed settings: children and cron lose
`claude agents --json`. start-claude also exports the OTel settings and `TELEGRAM_VOICE_INBOX_PORT`.

Heartbeat mod: empty beats double the plain idle threshold up to 60 min (120 min from 00:00 to 08:00 local time);
notes still wake the session after 2–3 min. The cron fallback follows it through ~/.local/bin/heartbeat
(`install -m 755 ~/plugins/heartbeat/ops/heartbeat-cron ~/.local/bin/heartbeat`, kept current by ~/boot.sh): it shadows
/usr/local/bin/heartbeat on cron's PATH and reads idleMin/unanswered/nextDue/lastBeat from
~/.local/state/heartbeat-mod.json while lastTick is under 3 min old, so cron beats no earlier than nextDue + 3 min
unless the mod's beats stop landing or an inbox note waits 5+ min. Changing the mod: edit plugins/heartbeat/ on a
repo branch as above, `node --test test/*.test.mjs` (offline only), bump the version, commit, `assistant land <branch>`,
`claude plugin marketplace update home-plugins && claude plugin update heartbeat@home-plugins`, restart. Without it:
`rm ~/.local/bin/heartbeat` (cron back to plain `heartbeat 10`).

## Watchdog
- The bridge: an event with no receipt 60 s after an idle point is sent once more (`redelivered="1"`); still none →
  one `bridge.undelivered` row in the events store.
- `brain-watch` (~/opt/assistant, cron) watches owner rows, receipts and replies in the events store.
- `tg-adapter status --instance live`: the poller and its last poll.

## Rollback
1. Telegram only: the kill switch (`touch ~/.telegram-official && restart "official telegram"`). No code change.
2. A broken launcher: `sudo install -m 755 ~/plugins/telegram-plus/ops/start-claude.official /usr/local/bin/start-claude
   && restart "official launcher"`. start-claude.official is the official plugin with the adapter off and the same
   session settings; the next container start reinstalls ops/start-claude.
3. A bad plugin change: revert its commit on a repo branch (`git -C ~/jobs/<name> revert <commit>`) and
   `assistant land <branch>` (or `assistant rollback` when it was the last deploy), reinstall start-claude as above,
   `restart`.
4. telegram-plus off for good: the kill switch, then optionally `claude plugin uninstall telegram-plus@home-plugins`
   (start-claude then uses the official plugin by itself) and `claude plugin marketplace remove home-plugins` (the
   heartbeat mod lives there too). The managed settings still allow the official plugin, so they can stay.

The official plugin reads the same access.json; keep `dmPolicy` at `allowlist` (in `pairing` mode it answers
strangers with pairing codes). Keys it doesn't know (`forwardReactions`) don't bother it.

## If inbound or tools break after a Claude Code update
Since Claude Code 2.1.292 local MCP servers default to protocol 2026-07-28, under which channel notifications are
dropped; the plugin's MCP SDK stays on 1.x (`test/sdk-guard.test.ts`). First try `MCP_PROTOCOL_NEGOTIATION=legacy`
(env in start-claude), restart, and check the MCP log for "Channel notifications skipped".
