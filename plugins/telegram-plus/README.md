# Telegram Plus

The main session's Telegram channel, as a bridge. **tg-adapter** (`adapters/tg` in ~/opt/assistant) polls the bot and
sends; this plugin is the MCP server Claude Code runs as its channel (`--channels plugin:telegram-plus@home-plugins`).
It moves rows of the events store (SQLite, `~/.local/state/assistant/events.db`) in as channel events and tool calls
out as outbox rows. It reads no bot token (it even drops one from its env), never polls, and opens no network
connection except the voice inbox on 127.0.0.1 and, in hub mode only, the hub API (below). Marketplace: `~/plugins` (name `home-plugins`). Skills:
`/telegram-plus:access`, `/telegram-plus:configure`. Code: `server.ts` (the entry point) imports `bridge.ts`, which
uses `bridgedb.ts`, `bridgehub.ts` (hub mode), `bridgelease.ts`, `tools.ts`, `voiceinbox.ts` and `scan.ts`.

## Hub mode (v2 Phase 2 H6)

`TELEGRAM_BRIDGE_STORE=hub` (start-claude sets it while `~/.config/tg-bridge/hub` exists): tg-adapter runs in the hub
(container claude-hub-tg) and its rows, deliveries and outbox live in the hub's Postgres. The bridge does the same work
over the hub API (`bridgehub.ts`, its only network client, one peer: `HUB_URL`, default `http://claude-hub:8700`, with
the brain's token from `HUB_TOKEN_FILE`, default `~/.secrets/hub-token`, read once and never logged):
- inbound by long poll (`POST /tg/inbound/poll`, `TELEGRAM_BRIDGE_HUB_WAIT_S` 15, under hubd's 20 s per-request budget) instead of the 250 ms poll; start-up
  replay, receipts, redelivery and `bridge.undelivered` / `bridge.handoff` rows as in SQLite mode. brain-watch's
  `bridge.redeliver` request and the bridge's answer stay in the local SQLite store's meta table in both modes.
- every write carries a rid (`tgb-…`): a retry after a lost answer writes nothing twice (one outbox row, one try).
- hub files: a row's `hubfile:in/<name>` values (photos' `image_path`, download results) are fetched into
  `~/.local/state/tg-bridge/inbox/` (0700, files 0600, 50 MB cap, a week kept) before the notification and replaced by
  the local path (a file the hub no longer has: dropped, named in meta `file_error`); files the session attaches are
  uploaded first (`POST /tg/files`) and the row carries `hubfile:out/<name>` refs.
- Approval cards: the brain mints the nonce. A reply's `approval: {action, summary, ttl_s}` is first minted on the hub
  with the brain's token (`POST /approvals`: only the action's sha256 leaves the bridge, as `approvals new` sends) and
  the row carries `approval: {nonce, action_sha}`; a nonce made with `approvals new` passes as it is. The hub's tg
  never mints or consumes an approval: it records his tap (decide, card, expiry).
- hub down: inbound waits (0.5 s backoff doubling to 5 s; one log line per kind of failure) and nothing is lost or sent
  twice; a tool call keeps trying for its usual wait (10 s), then answers `<tool> failed: hub unavailable (…): nothing
  was sent, try again`; a voice/app hand-off answers 500 (not delivered) instead of a false 202.
Endpoints: `POST /tg/inbound/poll|unreceipted`, `GET /tg/inbound/<id>|max`, `GET /tg/latest-delivered`,
`POST /tg/deliveries/sent|receipts`, `GET /tg/deliveries/seen`, `POST /tg/bridge-event`, `POST /outbox`,
`GET /outbox/<id>`, `GET /tg/files/in|out/<name>`, `POST /tg/files` (contract: ~/jobs/v2-h6/contract.md; hubd's
docstring is the reference).

## MCP surface

- Server `telegram`, the channel capability (`claude/channel`), no permission relay. In the session: `<channel
  source="plugin:telegram-plus:telegram" …>` tags and the tools `mcp__plugin_telegram-plus_telegram__*`.
- 11 tools: `reply`, `edit_message`, `react`, `download_attachment`, `draft`, `chat_action`, `send_poll`,
  `send_location`, `pin_message`, `delete_message`, `set_commands`. Their names, properties and the server
  instructions are pinned by the tests (`test/fixtures`): the managed-settings allowlist, the pitch-check matcher and
  the evals fake MCP depend on them. Optional fields: `urgent` (reply, send_poll, send_location), `buttons[].bind`
  {tool, args} (reply, edit_message) and `approval` {action, summary, ttl_s} (reply).
- What an op does on Telegram (formats, splitting, buttons and their press handling, drafts, files, holds, the menu)
  is tg-adapter's. The menu has no built-in commands: `set_commands` takes any valid name; the adapter lists the bound
  commands (tg-bind rows with a menu line) first.
- No message history or search: the Bot API has neither.

## Inbound

- Every 250 ms, rows with `delivery = 'now'` and kind tg.message, tg.button, tg.reaction, tg.poll_answer, tg.stop,
  tg.bound, approval.decided or bridge.handoff become one channel notification each, in id order: content =
  `payload.text`, meta = the payload's other fields as strings (without update_id, bot, ack_ms) + `event_id`. A row
  that already has a receipt (a binding answered it) is skipped.
- **Deliveries and receipts:** each notification upserts `deliveries(event_id, sent_ts, tries)`; the receipt writer
  (the agenda hook) sets `receipt_ts` and `how`. Once any receipt has been seen, a row with none 60 s after an idle
  point (tg-status board: not working; the later of the send and the last turn end) is sent once more with meta
  `redelivered="1"` and a "(re-sent: …)" first line; still none → one `bridge.undelivered` row (source tg-bridge,
  delivery now, payload event_id, kind, tries, age_s; no text).
- **Start-up replay:** rows of the last 24 h that nobody acked or dismissed and that have no receipt: never sent →
  sent; sent once → re-sent, only while receipts are known to work. Then new rows only.
- **Private scans:** a photo or document whose caption is "/scan private" or "/sp" reaches the session only as a
  refusal note (meta `handled="scan-private-refused"`): never its file id, caption or title (`scan.ts`,
  `bridgedb.ts`). Private documents go to the vault bot.

## Tools → outbox

- One row per call: bot `live`, chat (`''` for download and commands), op, body = the arguments without chat_id +
  `hold: false` + `dedupe: false` (bridge sends are never held or deduped), `urgent` from the argument, `files` = the
  attached paths (also kept in the body), `reply_to_event` + `trace_id` = the newest channel event of that chat
  notified ≤ 30 min ago, idem_key `bridge:<instance>:<n>`. Ops: reply → send, edit_message → edit, react → react,
  send_poll → poll, send_location → location, pin_message → pin (body.unpin), delete_message → delete, draft → draft
  (body.clear), chat_action → chat_action (body.stop), set_commands → commands, download_attachment → download
  (body.file_id).
- Checks before a row: chat_id, the reaction whitelist, chat actions, poll options, command names, files (absolute,
  ≤ 50 MB, never from ~/.secrets, ~/.claude (except the Telegram state dir's `inbox/`), tg-adapter's config or state
  (except its download inboxes) or the bridge's state).
- **Results:** the call waits ≤ 10 s (download ≤ 60 s) for `sent`, `failed` or `skipped`. The adapter may write
  `outbox.result` = JSON `{"ids": [...], "notes": [...], "text": "…", "path": "…"}`: `text` is the tool's answer as
  is; otherwise the answer is worded from `ids` (else `tg_message_id`) and `notes` (else a `note: …` last_error);
  download answers `path`, else `files[0]`. `failed` → the tool's error `<tool> failed: <last_error>`; a row still
  queued → `queued #N (held: <last_error>)`.
- **Store schema:** at open the bridge adds what it uses when missing (forward-only, additive): the `deliveries` table
  and the outbox columns `reply_to_event`, `files`, `result`. It never creates the store.

## One bridge

Only an instance whose Claude Code registers telegram-plus as a channel (or a manual run) takes inbound events and
opens the voice inbox, and only while it holds the lease, an exclusive flock on `~/.local/state/tg-bridge/lease`
(JSON: pid, version, since; `flock -n <lease> true` exits 1 while held). Others serve tools only and retry the lease
every 5 s. Stdin closed or a signal → the bridge releases the lease and exits.

## Voice inbox

Off unless `TELEGRAM_VOICE_INBOX_PORT` is set and the token file (`TELEGRAM_VOICE_INBOX_TOKEN_FILE`, default
`~/.secrets/voice-local-token`, first line, re-read per request) exists; 127.0.0.1 only. `POST /voice` with
`Authorization: Bearer <token>`:
- `{"id": "v<8 hex>", "task": "…", "urgency": "now"|"later"}` (8 KB max, 20/min) → a channel event
  `🗣️ (live voice, now) <task>` with meta `via="voice"`, `voice_id`, `chat_id="voice"`; answered by 202.
- `{"source": "app", "id": "<4–40 of A-Za-z0-9_->", "msg_id"?, "text" or "task" (≤ 4000), "reply_to"?,
  "device_id"?}` from the phone app's gateway → `📱 (app) <text>` with meta `via="app"`, `app_msg_id` (msg_id, else id
  when it is an app message id `m_<ULID>`), `chat_id="app"` (and `inbox_id`, `device_id`, `reply_to`); hand-offs
  without an app message get no `app_msg_id`. Any other `source` → 400.
Each hand-off becomes a `bridge.handoff` row (payload = its meta + text, idem_key `bridge.handoff:<via>:<id>`, so a
repeat is one row), delivered as above. A busy port is retried every 2 s for a minute. `voiceinbox.ts`.

## Launcher

`ops/start-claude` is installed over /usr/local/bin/start-claude by ~/boot.sh (with `ops/managed-settings.json` →
/etc/claude-code/managed-settings.json). One poller per bot token, so the plugin in use decides who polls:
- **Bridge** (telegram-plus installed, no `~/.telegram-official`): a poller left by the previous session (`bot.pid`:
  the official plugin's server) must be gone; then `live.off` goes and `tg-adapter ensure --force --instance live`
  must leave the live adapter running within 15 s.
- **Official plugin** (`~/.telegram-official`, the one opt-out, or telegram-plus not installed): `live.off` is made
  and a running live adapter is stopped before claude starts.
- **Fallback** (no live config, the adapter not running in time, or two fast failures in bridge mode): the adapter
  off, `~/.telegram-official` made (this start and the next ones use the official plugin), the owner texted (`tg`),
  `~/inbox/start-claude-bridge-fallback-*.md`. Back after a fix: `rm ~/.telegram-official && restart "bridge"`.
- Two fast failures on the official plugin turn the heartbeat mod off (`~/.heartbeat-mod-off`) and text the owner.
- **Hub** (`~/.config/tg-bridge/hub`): bridge mode with `TELEGRAM_BRIDGE_STORE=hub`; `live.off` and `live.hub` are made,
  a running in-container live adapter is stopped and `tg-adapter ensure --force --instance live` keeps the companion
  up. No automatic fallback (the official plugin's poller would fight the hub's tg): two fast failures write a
  bridge.log line and `~/inbox/start-claude-hub-*.md` once (then the heartbeat guard as usual). The kill switch
  stays the manual opt-out once claude-hub-tg is stopped; chosen while the hub file exists, it leaves a note saying so.
  Rollback: remove the hub file (the plain bridge path removes `live.hub` and `live.off`).
- The mode of the running session is in `~/.local/state/start-claude/telegram-mode` (`bridge` | `official`);
  `telegram-store` says `hub` in hub mode (absent otherwise).
- `ops/start-claude.official` is the last-resort launcher (official plugin, adapter off). Details and rollback:
  `ops/SWITCHOVER.md`.

## Access

tg-adapter applies the access file (`access.json`: the DM allowlist, groups, mention patterns); the bridge has no gate
of its own and delivers only what the adapter let in. [ACCESS.md](./ACCESS.md).

## Env

`TELEGRAM_BRIDGE_DB` (`~/.local/state/assistant/events.db`), `TELEGRAM_BRIDGE_BOT` (`live`), `TELEGRAM_BRIDGE_STATE_DIR`
(`~/.local/state/tg-bridge`), `TELEGRAM_BRIDGE_POLL_MS` (250), `TELEGRAM_BRIDGE_SEND_WAIT_MS` (10000),
`TELEGRAM_BRIDGE_DOWNLOAD_WAIT_MS` (60000), `TELEGRAM_BRIDGE_REDELIVER_MS` (60000; 0 off), `TELEGRAM_BRIDGE_REPLAY_S`
(86400), `TELEGRAM_BRIDGE_LEASE_RETRY_MS` (5000), `TELEGRAM_BRIDGE_START_DELAY_MS` (2000, after the client's
initialized), `TELEGRAM_BRIDGE_FORCE=1` (skip the channel check; tests), `TELEGRAM_BRIDGE_TOOLS_ONLY=1`,
`TELEGRAM_STATUS_STATE` (the tg-status board, `~/.local/state/tg-status/state.json`), `TELEGRAM_VOICE_INBOX_PORT`,
`TELEGRAM_VOICE_INBOX_TOKEN_FILE`. Hub mode: `TELEGRAM_BRIDGE_STORE` (`hub`; anything else = SQLite),
`TELEGRAM_BRIDGE_HUB_WAIT_S` (15), `HUB_URL` (`http://claude-hub:8700`), `HUB_TOKEN_FILE` (`~/.secrets/hub-token`),
`HUB_SCHEMA` (a hub test schema `t_*`; rehearsals). `TELEGRAM_MODE` is not read: unset or any value runs the bridge.

## Tests

Offline, under the netguard preload (`LD_PRELOAD=~/opt/test-all/netguard.so`):
- `bun test`: `test/bridge.test.ts` (an MCP stdio harness: temp HOME and store; the test plays the adapter and the
  receipt writer), `test/bridge-only.test.ts` (static: no poll path, no token, no network, the manifests),
  `test/voiceinbox.test.ts`, `test/scan.test.ts`, `test/sdk-guard.test.ts` (the MCP SDK stays on 1.x, without
  protocol 2026-07-28, under which Claude Code drops channel notifications), `test/hub.test.ts` (hub mode against
  `test/fakehub.ts`, a fake hub on 127.0.0.1: tools, files, inbound, replay, receipts, redelivery, hub down/away,
  lost answers, restarts, the voice inbox).
- `bun test/offline.ts`: the end-to-end scenario, started as Claude Code starts the plugin.
- `test/start-claude.sh`: both launchers, on copies with stubs.

## Origin and licence

This plugin started as a fork of the official Telegram channel plugin for Claude Code
(`telegram@claude-plugins-official`, Apache-2.0) and has been changed substantially since. It keeps that licence:
see `LICENSE` in this directory.
