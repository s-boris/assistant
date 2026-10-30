# tg-adapter

Telegram plumbing for the harness, lifted from the telegram-plus channel plugin
(`~/plugins/telegram-plus` at commit `974bed5`, version 0.1.22; `lifted.json` records the commit,
the verbatim files with their sha256 and where each adapted file came from).

The plugin hands Telegram updates to one Claude Code session over MCP. The adapter instead turns
every update into an **event row** in SQLite (same DDL as the events DB, `src/schema.sql`) and sends
what the **outbox** table holds. No code here decides what an event means: inbound rows are written
as they come, and the brain reads and acks them.

What it holds: the managed `~/bin/tg-adapter` (ensure | status | stop), instances (test | live), the bridge's ops
(files, drafts, chat actions, delete, pin, location, commands, downloads), inbound parity (eager photos, transcripts,
private scans, Stop on drafts), board-driven typing, the compat journal and the file guard; bindings: commands and
buttons answered by running a registered tool without a brain turn (below); one-time approvals (✅/❌ cards with a
nonce, expiry and single use; below); the outbox the script senders (tg, tg-buttons, tg-voice, event-card,
tg-status's board) write into (below). An instance runs only while its config exists; the in-container live
instance also needs `live.off` gone (in hub mode the adapter runs in claude-hub-tg).

## Run

```
bun install
bun test                      # offline: mock Bot API, temp HOME, no TCP port
bun src/cli.ts check          # validate config + token file (no network); every command takes --instance test|live
bun src/cli.ts run            # interlocks, then poll (foreground)
bun src/cli.ts status [--json]
bun src/cli.ts enqueue send --chat ID --body '{"text":"hi","buttons":[{"text":"Yes","data":"y"}]}'
bun src/cli.ts bindings [--json]                       # every binding and what it runs (or "BRAIN")
bun src/cli.ts bindings resolve /emby | --data rm:x    # what this command or tap would do now
```

Exit codes: 0 ok · 1 error · 2 config/usage · 3 409 conflict · 4 401 · 5 wrong bot · 6 already
running · 7 switch file present.

Managed, from cron and by hand: `~/bin/tg-adapter ensure | status | stop [--keep-down]`
(source `packages/tgadapter`, `scripts/tg-adapter`). `ensure` does nothing while the config is absent;
otherwise it runs `cli.ts check`, then starts `bun src/cli.ts run` detached from the repo's main checkout
(`bun install --frozen-lockfile` first when `node_modules` is missing; restarted when `adapters/tg`
changed on main). It never restarts after exits that won't heal by themselves (2, 3 = 409, 4, 5) until
the config or token file changes or `--force`, backs off after 3 starts in 15 min, and reports each
held-down episode once as an event + `~/inbox` note. `status` says whether the token file is present
(yes/no) and never names it. `check`, `run` and `enqueue` are handed to this CLI.

Files (all `$HOME`-relative; the env overrides are for tests):

| Path | What |
|---|---|
| `~/.config/tg-adapter/<instance>.json` (`TG_ADAPTER_CONFIG`) | required config, see below; test also reads the older `config.json` while `test.json` is absent |
| `~/.config/tg-adapter/<instance>.off` (`TG_ADAPTER_OFF_FILE`) | switch: present = stay down (checked at start and every poll round); test also honours the older `off` |
| test: `~/.local/state/tg-adapter/test.db` · live: `~/.local/state/assistant/events.db` (`ASSISTANT_EVENTS_DB`) | events + outbox + meta (getUpdates offset, poll ids), 0600, WAL; DDL v1 + v2 (`src/schema_v2.sql` = `schemas/events-v2.sql`) |
| `~/.local/state/tg-adapter/<instance>/` (`TG_ADAPTER_STATE_DIR` = the parent) | `adapter.pid` (`<pid>\ntg-adapter`), `buttons.json` (on_press modes), `inbox/` (0700: downloads), `adapter.log`, `ensure.log` |

Under `ASSISTANT_TEST=1` or `TG_ADAPTER_TEST=1`, a config, state or switch path inside the real home
directory (from `/etc/passwd`, not `$HOME`) is refused.

## Config

```json
{
  "mode": "test",
  "bot": "<test bot username>",
  "token_file": "~/.secrets/<test bot env file>",
  "allow_from_file": "<an access.json-shaped file>",
  "send": false
}
```

| Key | |
|---|---|
| `mode` | `"test"` or `"live"`, equal to the instance. live: rows go to the main events DB (`db` absent or that path), the real Bot API only (`api_root` / `api_unix_socket` refused outside test runs) |
| `bot` | the bot's username; `getMe` must return exactly this (and the token's bot id) before the first `getUpdates` |
| `token_file` | KEY=VALUE file with `TELEGRAM_BOT_TOKEN`, mode 0600, never inside a Claude config dir (where the live plugin keeps its token; checked by path and by real path). The process env's `TELEGRAM_BOT_TOKEN` is never read |
| `allow_from` / `allow_from_file` | exactly one: inline user ids, or an access.json-shaped file read live and never written (`allowFrom`, `groups`, `mentionPatterns`, `forwardReactions`; `dmPolicy` "pairing" counts as "allowlist") |
| `api_root` | Bot API base URL (default `https://api.telegram.org`) |
| `send` | outbox consumer on; default false (receive-only) |
| `quiet_hours` | outbox rule: non-urgent send/poll rows wait inside this window on the owner's wall clock; default `"0-8"`, `"22-6"` wraps, `""` / `"off"` = none |
| `sleepin` | outbox rule: hold non-urgent send/poll rows while `sleepin active` exits 0; default true |
| `dedupe_window_s` | outbox rule: a repeat of the same text to the same chat within this many seconds is skipped; default 600, 0 = off |
| `poll_timeout_s`, `outbox_interval_ms` | 30, 500 |
| `bindings` | bindings on (default true; they run only while `send` is on) |
| `instant_commands`, `side_effect_callbacks` | the 0.1.22 compat tables (instant.json, callbacks.json) as bindings; default true, false = ignore that table |
| `compat_instant_file`, `compat_callbacks_file` | those tables (default: beside `compat_journal`, i.e. the plugin's state dir; `""` = none) |
| `tools_registry` | the tools registry (default: the repo's `schemas/tools.toml`; env `TG_ADAPTER_TOOLS` when the key is absent) |
| `transcribe` | voice notes, audio, video notes transcribed before their row shows (default true; `~/bin/transcribe`, `TG_ADAPTER_TRANSCRIBE_CMD`) |
| `scan_private` | `/scan private` and `/sp` files go to paperless (`paperless`, `TG_ADAPTER_SCAN_CMD`) and never reach the brain; default live true, test false (off: refused, content still never shown) |
| `typing` | board-driven "typing…" (send on only; default true); `status_state` = tg-status's state.json |
| `compat_journal`, `compat_buttons_file` | 0.1.22's inbound-journal.jsonl (appended in its shape, capped 100 / 200 KB) and the on_press store to use instead of the instance's; until compat is retired (bridge-only 0.3.0) |
| `api_unix_socket` | tests only: Bot API over a unix socket |

## Interlocks

- Config file required; mode equal to the instance.
- `getMe` username (and id) must match the config, else it refuses to start (exit 5).
- Own PID file: a live holder → refuse (exit 6); a stale one (crash, SIGKILL) is replaced.
- 409 Conflict on `getUpdates` (another poller, or a webhook) → stop (exit 3). It never fights
  for the poller and never kills anything; 401 → stop (exit 4).
- Binds no ports (the live plugin owns its local port). Error text never carries the token.
- Switch files `~/.config/tg-adapter/<instance>.off` (test: also `off`); `bindings-off` beside them sends every
  command and tap to the brain (no restart).

## Inbound → rows

Parity with 0.1.22 (prep.ts): a photo is downloaded at once (`image_path`); a voice note, audio or video note is
transcribed first (`🎙️ (voice, 12 s) …`, `transcript: auto`, or `transcribe_error`); a `/scan private` file is
filed into paperless and its row keeps only the command, `handled: scan-private` and `doc_id` or `scan_error`.
Such rows are committed with status `pending` and delivery `held:now` (readers of delivery `now`, such as the
bridge, never see them early) and are re-inserted under a fresh id as `new` when done (the bridge reads by id
after its cursor); later rows of the chat wait behind them (0.1.22's per-chat order). A process killed meanwhile releases them at the next start as interrupted.
Stop on a streaming draft is a `tg.stop` row. With send on, owner messages and presses show "typing…" while
tg-status says the session works, and the access file's `ackReaction` is set on each message.


One `getUpdates` loop (grammY's `Api` as the transport, `allowed_updates` = message,
callback_query, message_reaction, poll_answer). Per update:

- Callback queries of a batch are answered **at once**, concurrently, before any DB work (toast
  `✓ <label>`, or `Not authorized.`); the latency goes into the row (`payload.ack_ms`). Then the
  plugin's on_press handling: `mark` (default) appends `✓ <label>` and removes the keyboard,
  `remove` removes it, `keep` leaves it.
- The row and the next offset are committed in one transaction: a crash or kill mid-stream loses
  nothing (Telegram sends the update again) and duplicates nothing (`idem_key` is UNIQUE).
- Row: `source` tg-adapter · `kind` tg.message | tg.button | tg.reaction | tg.poll_answer ·
  `idem_key` = `tg:<bot id>:<update_id>` (update ids are per bot, so the bot id keeps two bots
  apart in one DB) · `delivery` now · `labels` ["tg", mode] · `payload` = the plugin's channel meta
  (chat_id, message_id, user, user_id, ts, event_type, callback_data, button_text, attachment_*,
  reply_to_*, reaction, reacted_to_*, poll_*) plus `text` (the plugin's notification content),
  `update_id` and `bot`.
- Dropped updates (stranger, group without mention, unsupported type, unknown poll) only move the
  offset and count in `meta` (`dropped:<reason>`).

## Outbox

The consumer (only with `send: true`) takes `queued` rows with `mode` live whose `bot` is the config's bot
username (CLI, tg wrapper) or the instance name (`live`/`test`: the telegram-plus 0.2.0 bridge), in id
order. A row's `body` is the plugin tool's arguments; `chat` is the chat id; `urgent` and `not_before`
are the writer's.

| op | plugin tool | body |
|---|---|---|
| `send` | reply (text only) | text, format (text, markdownv2, html, rich), details + details_title, buttons + on_press, keyboard / remove_keyboard / force_reply, reply_to, silent, link_preview, effect; `dedupe: false` opts out of the text dedupe |
| `edit` | edit_message | message_id, text and/or buttons, format, details, on_press, link_preview |
| `react` | react | message_id, emoji / emojis / remove, big |
| `poll` | send_poll | question, options (2-12), multiple, anonymous, quiz_correct, silent |
| `send` + files | reply | the row's `files` column (JSON list), else body.files (never both: the bridge writes both); album; images as photos (photo.ts), .ogg/.oga/.opus via sendVoice (sendAudio when refused), the rest as documents; `caption` and (voice files) `duration` go on the first file; the file guard refuses `~/.secrets`, `~/.claude` (except the plugin's inbox), the adapter's config and state dirs (except the instance inbox), the token and access files |
| `draft` | draft | text, append, format, can_stop, clear; flushes ≥ 1.2 s apart, bursts coalesced; the next send ends it |
| `chat_action` | chat_action | action (default typing), stop |
| `delete`, `pin`, `location` | delete_message, pin_message, send_location | message_id; message_id + unpin/notify; latitude, longitude (+ title, address = venue) |
| `commands` | set_commands | commands: the bound instant commands first (while bindings run), then the given ones not already taken; chat ignored (`-` or empty) |
| `download` | download_attachment | file_id → `result.path` in the instance inbox (≤ 60 s, beside the queue); chat ignored |

Every sent row gets `result` (JSON: ids, notes and the op's extras such as path, draft_mode, commands) for the
bridge; where 0.1.22's tool answer is not the bridge's default wording (draft, react, chat_action) `result.text`
carries it. Rows with `reply_to_event` set, and bridge rows (`body.bridge: true`), are never held by quiet hours or
sleep-in and never text-deduped (plan D-i); `body.hold: false` turns off the holds and `body.dedupe: false` the
text dedupe for any row (the bridge sets both).

Rules (`src/rules.ts`; data the writer sets decides, the rules only hold, skip and record):

- **Quiet hours**: non-urgent `send`/`poll` rows wait while the owner's wall clock is inside
  `quiet_hours` (default 0-8). The zone is `tripclock tz` (`quiet.ts`'s QuietClock, looked up in the
  background every 10 min; the home zone on any error; `TELEGRAM_TRIPCLOCK_CMD` / `TELEGRAM_QUIET_TZ`).
- **Sleep-in**: non-urgent `send`/`poll` rows wait while `sleepin active` exits 0 (checked in the
  background every minute; `TG_ADAPTER_SLEEPIN_CMD`, default `~/bin/sleepin active`). Exit 1 = send;
  any other answer or a timeout never holds (quiet hours still cover the night).
- **Urgent** (`urgent = 1`, `enqueue --urgent`) bypasses both holds, never a retry wait. `edit` and
  `react` are never held (they don't notify).
- **Dedupe**: `idem_key` is UNIQUE (a repeat is never inserted). A `send` whose text + details +
  reply_to (a `poll`: question + options) equals an earlier queued/sending/sent row of the same bot,
  chat and op enqueued at most `dedupe_window_s` before it → `skipped`, `last_error` "duplicate of #N".
- **Claims**: a row is taken (`queued` → `sending`) only while it is still queued, so a writer that withdrew it
  (core.tg sets a queued row it gave up on to `skipped`, `last_error` "withdrawn: …") always wins: never both.
- **Errors**: 429 → back in the queue until `retry_after`; network trouble (no answer: fetch failure,
  DNS, reset, timeout; or a 5xx) → backoff 2, 4, 8, … s capped at 60 s, until the row's retry window
  ends: 6 h of retrying for messages, 10 min for edits, reactions, drafts, typing and downloads (time
  held by quiet hours or sleep-in, or with the adapter down, does not count). The window lives in the
  row's `result` (`{"retry": {since, at, next, strikes}}`) until the sent result replaces it. A row that
  keeps failing while Telegram answers other calls (any Bot API answer below 500 since its previous
  failure) has a message-specific problem, not an outage: it fails after 5 such failures. Anything else
  (4xx) → `failed` at once. A message split into parts that fails half way is never retried. A row
  stays `sending` while its calls run; one found at start was interrupted → `failed` (it may have
  arrived, so it is never re-sent blind).
- **outbox.failed**: every row that ends `failed` gets one event row (at the end, never per retry) in the same DB (source
  tg-adapter, kind `outbox.failed`, idem_key `outbox.failed:<bot>:<row id>`, delivery `now` for urgent
  rows, else `beat`; payload: ids, op, chat, attempts, error, partial; never message text).
- **Per-chat order**: a row waiting for a retry holds back that chat's later rows. Rows held by quiet
  hours / sleep-in and scheduled rows (`not_before` set at enqueue, no attempt yet) wait alone.

Only chats the inbound gate would deliver from are sent to; a rejected format goes again as plain
text (note in `last_error`).

## Bindings

A binding answers a command or a button tap by running a tool without a brain turn. Tools live in the registry
`schemas/tools.toml` (argv, effect, timeout, text or picture answer, note); a binding names a tool, never a command
line. Resolution, first match wins:

- command `/name` (or `/name@thisbot`; text after it only when the tool takes text, ≤ 512 chars, one argv element):
  a `tg-bind` row, then the 0.1.22 compat table (`/status` built in, then instant.json).
- button data: a `tg-bind` button row, then the longest `tg-bind` prefix row, then a card bind (a button's `bind:
  {tool, args}` in a send/edit row, kept 7 days, replaced by the next edit of that message), then callbacks.json.

A compat entry runs only when its argv equals a registered tool's argv exactly; otherwise it goes to the brain as
before. Effects gate: a tool above `send_owner` never runs from a command or tap (it needs an approval, below); it goes to the
brain with `binding_gated` in the row. Runs: argv only (no shell), cwd HOME, env without the bot token or any value
that contains it, own process group (killed at stop), the tool's timeout. Answers keep 0.1.22's shapes: commands
reply in the chat (a picture JSON `{"photo","caption","text"}` under `~/.local/state/cardimg` or `/tmp` becomes a
photo), buttons get the toast and a silent reply to the card, the keyboard stays; a second tap while one runs gets
"⏳ Already running".

Rows: the source row (tg.message / tg.button) is committed acked by `binding`, with a `deliveries` receipt (how
`binding`), so the bridge, agenda and receipts treat it as handled. Each run writes a `tg.bound` row (payload: the
0.1.22 meta keys plus ok, outcome ok|failed|forwarded|send_failed, ms, tool, binding source/name/row); delivery
`now` when the binding's note is on or the run failed or was forwarded, else `digest` acked by `binding`. A failed
command with `on_error: forward` reaches the brain as the original message.

Off: `bindings: false`, `send: false`, or `~/.config/tg-adapter/bindings-off` (`tg-bind off`; env
`TG_ADAPTER_BINDINGS_OFF`) → everything goes to the brain. Rows are edited with `tg-bind` (scripts/tg-bind:
add | rm | list | tools | off | on | status | table | resolve).

## Approvals

A side effect that needs his yes (a booking, a payment step) asks for it with a one-time approval: a 128-bit nonce
bound to the action's sha256, a summary of ≤ 80 chars, an expiry (default 15 min, 1 s–24 h) and single use. The
store is `approvals.db` (0600): live beside the main events DB (`~/.local/state/assistant/`), the test instance in
its own state dir, `APPROVALS_DB` overrides. Only this adapter and the `approvals` CLI write it (`packages/approvals`,
`scripts/approvals`, same DDL: `schemas/approvals.sql` = `src/approvals.sql`); vault-guard denies Bash and the file
tools on it.

- Card: a send row with `approval: {action, summary, ttl_s?, ref?, yes?, no?}` (the adapter makes the nonce) or
  `approval: {nonce, action?}` (a nonce from `approvals new`; `action`, when given, must match). The adapter appends a
  ✅/❌ row (`ap:<nonce>:y|n`, labels `yes`/`no`) to the row's buttons. The action never stays in the outbox: the
  stored body keeps its sha256, then only the nonce, summary and expiry. Owner's private chat only; no reply
  keyboard, remove_keyboard, force_reply or album. Approval cards are never deduped. The tool's answer notes the
  nonce and its expiry. A card that carries explicit `ap:<nonce>:y|n` buttons (a nonce from `approvals new`, e.g. a
  script's own card) is recorded the same way when sent or edited in the owner's chat.
- Tap: decided while the callback is answered. Only his tap (sender = the private chat = the first `allowFrom` entry)
  decides; a stranger's tap is dropped (no row). Toasts: "✅ Approved", "❌ Declined", "⌛ Expired: nothing was
  approved", "⌛ Replaced by a newer card: …", "⌛ No longer valid: …"; a store error answers "⚠️ Not recorded: tap
  again" and leaves the card as it was. The first decision marks the card like any button; a repeat tap clears the
  keyboard.
- Row: `approval.decided` (delivery `now`; payload event_type `approval`, nonce, decision yes|no, outcome, repeat,
  summary, expires, ref; `next` names the consume step). The compat journal records it as a `button` press, as 0.1.22
  did, so poll-mode readers keep working.
- Use: the flow that asked runs `approvals consume NONCE --action "…"` right before the side effect: exit 0 exactly
  once, 3 expired, 4 already used, 5 unknown, not decided, declined, replaced or another action. In poll mode
  (telegram-plus) the CLI decides from
  the plugin's journal instead: his button entry for that nonce in the card's chat (or his private chat) between
  creation and expiry.
- Expiry: every 5 s (send instances only) pending approvals past their expiry are marked expired and their card is
  edited to one inert "⌛ expired" button (a replaced one: "⌛ replaced"); a tap on it answers the toast only. The
  sweep also runs at start, so cards that expired while the adapter was down are edited then. Rows older than 30 days
  are pruned.
- `ap:` data is never a binding (`tg-bind` and card binds refuse it). Off: `~/.config/tg-adapter/approvals-off`
  (env `TG_ADAPTER_APPROVALS_OFF`) → `ap:` taps go to the brain as plain button rows (journaled as before) and the
  CLI decides from the journal.

## Script senders (one sender)

`core/tg.py` (stdlib) puts the script senders' messages into this outbox under the key `live` (body.sender names the
writer): `tg`, `tg-buttons`, `tg-voice` (through `~/bin/tg-outbox send SENDER …`), `event-card` and `tg-status`'s
board (core.tg.bot_call: sendMessage, editMessageText, pin/unpin, deleteMessage answered like the Bot API). Gate,
read on every send: no `~/.config/tg-adapter/direct-<sender>` / `direct-all`, and either hub mode (the hub
answers and its adapter drained the hub outbox within 30 s, by `GET /outbox/consumers`) or the live instance runs
here (its PID file) with `send: true`, no `live.off` and no ledger switch `events-off`; otherwise each sender keeps
its direct Bot API call (nothing written first, so nothing goes twice). `tg` checks for the PID file or a hub (HUB_URL or the hub
token file) in shell before it asks the gate. Plain `tg`, `tg-buttons`, `tg-voice` and event-card alerts are urgent (alerts,
answers); `tg --soft` / `TG_SOFT=1` are not, so quiet hours and sleep-in hold them; the board and the live card are
`hold: false`, `dedupe: false`. A row still queued when the writer's wait ends while its adapter is gone (this one, or
the hub's by its consumer age) is withdrawn and sent directly; `tg-outbox gate SENDER` shows the path a sender takes,
`tg-outbox status` the gates, the hub consumer and the rows per sender (the hub's in hub mode).

## Lifted vs dropped

| Part of telegram-plus | Here |
|---|---|
| grammY transport (`Api`, raw calls for methods newer than its types, `GrammyError`) | lifted; the `Bot` runner is replaced by the adapter's own poll loop (offset in the DB) |
| Access gate: DM allowlist, groups with allowFrom + mention-triggering, gate for buttons/reactions/polls, outbound gate | lifted (`src/access.ts`), static |
| Pairing (pending codes, "Pairing required" replies, approved/ dir, "Paired!") | dropped: strangers are dropped silently; the access file is never written |
| Grapheme-safe chunking, the four formats, rich hard breaks, plain fallback for rejected formatting | lifted (`src/format.ts`) |
| Details fold (`fold.ts`), on_press persistence (`buttons.ts`), the "✓ choice" mark after a press | lifted; fold.ts and buttons.ts verbatim |
| Inline/reply keyboards, link previews, effects, reaction whitelist | lifted (`src/format.ts`) |
| Quiet hours on the owner's clock (`quiet.ts`, tripclock) | lifted verbatim; drives the outbox quiet-hours rule |
| reply (text), edit_message, react, send_poll tools | lifted as outbox ops send / edit / react / poll |
| Inbound handlers: text, photo, document, voice, audio, video, video note, sticker, venue, location, contact; buttons; reactions with the recent-message context; poll answers | lifted as event rows (recent context also from the DB, so it survives a restart; poll ids persist in `meta`) |
| Token redaction in error text (`cleanErr`) | lifted |
| MCP server, tools, instructions, channel notifications | dropped |
| Poll veto for claude processes (`--channels` check), bot.pid election, takeover, SIGTERM of foreign pollers, 409 retry/yield | dropped: one adapter per bot, own PID file, 409 = stop |
| Delivery guard (MCP log reading, inbound journal, health.json, replay requests, delivery alerts, owner alerts) | dropped: the DB row is the delivery record |
| Permission relay ("yes abcde" replies, `perm:` buttons) | dropped (`perm:` presses are acked without a row) |
| Instant commands (/status, instant.json), instant buttons (`callbacks.ts`) | bindings (`src/bind.ts`, `src/instant.ts`; callbacks.ts verbatim): each entry runs only when its exact argv is a registered tool at or below send_owner |
| Reaction shortcuts (`shortcuts.ts`) | dropped |
| Private scans (`scan.ts`), voice inbox (`voiceinbox.ts`, local port), transcription, downloads, download_attachment | dropped: attachments are recorded by file id only |
| Draft streaming, stop events, typing/chat actions (board-driven), ack reaction | dropped |
| Files/albums/photos (`photo.ts`, `picture.ts`), send_location, pin, delete, set_commands, custom command menu | dropped from the outbox v0 |
| access.json textChunkLimit / chunkMode / replyToMode | dropped: fixed 4096 / length / first |
| Tests: fold, buttons, quiet unit tests; buttons TTL clock test | lifted (import paths only) |
| Tests: offline.ts mock Bot API | ported to `test/mockbot.ts` (in-process fetch or unix socket; no TCP port) |
| Tests: callbacks | lifted (import path only) |
| Tests: paths, shortcuts, voiceinbox, sdk-guard, channel-check | dropped with their code |

## Hub mode (H6)

With `hub: {url, token_file, files_dir, schema?}` in the instance config the adapter runs in the hub's tg container and
talks to hubd as the tg caller (contract: `~/jobs/v2-h6/contract.md`; code: `src/hub.ts`, `src/hubsync.ts`,
`src/companion.ts`). `token_file` holds the tg caller's bearer token (0600, never under a Claude config dir),
`files_dir` is the shared hub files dir (`/srv/tgfiles`), `schema` (a `t_*` test schema) is for the test instance and
test runs only. `compat_*` keys are refused; there are no compat writers, brain paths or `~/bin` lookups.

- Journal: the instance's own SQLite (`<state dir>/journal.db`, or `db`) stays the atomic store (row + offset in one
  transaction) and the spool. The forwarder posts committed rows that are not `pending` to `POST /tg/rows` in id order
  (≤ 100 a call; kinds `tg.*`, `outbox.*`, `approval.*`, others stay local), kicked after each commit plus a 250 ms
  poll. Later payload changes go to `POST /tg/rows/patch` (the whole payload as the patch), changed meta counters to
  `POST /tg/meta` every 5 s (`dropped:*`, `offset:*`, `compat_reads:*` as they are; `duplicates`, `last_poll_ts` and
  the approval counters as `tg.<key>`). Watermarks in meta (`hub:fwd_id`, `hub:fwd_patch_ts`), the local→hub id map in
  `hubmap`. A hub that is down or an answer that is lost only delays rows (the idem_key makes a repeat a no-op);
  repeated failures log once, then every 40th. Stop makes one last pass, bounded to 3 s.
- Outbox: the same consumer over hubd's H5 endpoints (`POST /outbox/queued {bots}`, `/outbox/<id>/claim {attempts}`,
  `/set {fields}`, `/fail {fields, partial}`, `/outbox/interrupted {bots}`, `/outbox/dupes {bot, chat, op, id, since}`,
  `GET /outbox/<id>`). Sent rows are mirrored into the local outbox (mode `mirror`, kept 7 days) for reaction context
  and card edits. Outbound files are `hubfile:out/<name>` refs (resolved under files_dir/out); the file guard allows
  only files_dir.
- Bindings: `GET /tg/bindings` every 5 s replaces the local rows of every kind but `card` when its version changes.
- Presence: a long poll on `GET /tg/presence?after=&wait=25` writes the companion's tg-status board to
  `<state dir>/presence-board.json` (Typing watches it); its zone and sleep-in feed the outbox rules while fresh
  (< 2 min), else the local defaults apply.
- Approvals: get, attach, decide, card and expire over hubd (`/approvals/<nonce>…`, `POST /approvals/expire`). His tap:
  owner and chat checks here, then `POST /approvals/<nonce>/decide {decision, by, via}`; that tap's ack waits up to 3 s
  for the answer (its toast says what happened), every other tap is acked without the hub. The adapter journals one
  `approval.decided` row per nonce, as in local mode, and the forwarder sends it.
- Jobs: bind runs, voice transcripts and private scans go to the companion (`POST /tg/jobs {kind, args, timeout_s}`,
  then `POST /tg/jobs/<id>/wait` in ≤ 25 s steps until the timeout + 15 s). Unclaimed (no companion polled in 40 s):
  a bound command or button goes to the brain as an unbound row with `instant_error`, a voice note gets
  `transcribe_error`, a scan `scan_error`. Polling, Typing and callback acks never wait on a job.
- Downloads (photos, voice, documents, `download` ops) land in files_dir/in and appear as `hubfile:in/<name>`.
- `tg-adapter status` adds the hub host (and schema), the last forwarded row, the backlog, the age of the last good
  pass, the last error and when the companion was last seen (its presence posts).

The companion, `tg-adapter companion --instance <i>`, runs in the brain container with the brain's hub token and no
bot token:

- Env: `HUB_URL` (default `http://claude-hub:8700`, required under `TG_ADAPTER_TEST`), `HUB_TOKEN_FILE` (default
  `~/.secrets/hub-token`, 0600), `HUB_SCHEMA` (tests), the Binder's tools registry (`TG_ADAPTER_TOOLS` or the
  default), `TG_ADAPTER_TRANSCRIBE_CMD`, `TG_STATUS_STATE` (default `~/.local/state/tg-status/state.json`), and
  tripclock / sleepin as the rules use them.
- Jobs: `POST /tg/jobs/claim {kinds, wait}` (at most 4 binds, 2 transcripts and 1 scan at a time), answered with
  `POST /tg/jobs/<id>/result {ok, result: {ok, stdout, error?}, error?}`. Job args: bind `{tool, extra, arg, env,
  timeout_ms}`, transcribe `{file}`, scan `{file, name, title}`. Bind keeps the Binder's rules: a registry tool by
  name, effect at most send_owner, ≤ 16 extra args of ≤ 500 chars, the text arg only for text tools, only
  `TELEGRAM_CALLBACK_*` env extras, the Binder's timeout cap, no token variables, `~/bin` first on PATH. Picture
  output is uploaded (`POST /tg/files`, raw body + `X-File-Name`) and returned as its `hubfile:out` ref. Transcribe
  and scan fetch `GET /tg/files/in/<name>` into a 0700 temp dir and run Prep's commands.
- Presence: `POST /tg/presence {state: {board, zone, sleeping, ts}}` on change and every 30 s.
- Files: `<state dir>/companion.pid` (single instance: pid + marker) and `<state dir>/companion.json` (counters).
- Switch: `~/.config/tg-adapter/<instance>.companion-off` keeps it down (exit 7). `live.off` does not: in hub mode it
  keeps the in-container adapter off while the companion runs.
- `--once [--wait S]`: one presence post and one claim round. `status [--json]`: pid, running, counters, last error.
- Start: `tgadapter/ctl.py ensure` starts the companion instead of the adapter while `~/.config/tg-adapter/live.hub`
  exists (brain side).

Beyond the contract:

- Approvals: the hub's tg never creates or mints one (it has no `POST /approvals` call; the gateway's Cedar policy gives Binding `decide` only). The brain mints the nonce (`approvals new`, brain token) and the card row names it: `approval: {nonce, action?}` (the action, when given, must match the nonce's sha256). A card row without a nonce fails as outbox.failed with "hub mode: the brain mints approvals; send approval:{nonce}". His tap: owner and chat checks, then `POST /approvals/<nonce>/decide` and the adapter's own `approval.decided` row (hubd's decide writes no event).
- hubd's outbox `set` takes no `body`, so the nonce made for a card row is kept in memory per row id; a retry after
  a restart makes a new one and the first expires unused.
- `/tg/rows` rows also carry `local_id` (hubd ignores unknown keys).

## Tests

`bun test` (offline; verified under the netguard preload with no blocked connects):

- `inbound.test.ts`: message / button / reaction / poll answer → rows; gate (stranger, group
  mention, static pairing); attachments as file ids; callback ack < 200 ms on the mock; presses of
  one batch answered concurrently; on_press keep and rich marks.
- `outbox.test.ts`: the four ops, fallbacks, the outbound gate, 429 / 5xx / partial sends,
  network outages (heals after 10 min, permanent 400, the 10 min / 6 h deadlines, retry gaps, strikes
  while Telegram answers), per-chat order, interrupted rows, receive-only default.
- `rules.test.ts`: quiet hours on tripclock's zone (and the home zone when it fails), sleep-in
  (exit 0 / 1 / other / timeout), urgent bypass, edit/react never held, dedupe (idem key, same text,
  window, reply_to, opt-out, held rows), 5xx backoff timings, 429 per chat, outbox.failed rows for every
  failure kind, scheduled rows, the consumer on its own, config keys.
- `test/mockserve.ts`: the mock as a process on a unix socket, for the Python e2e
  (`tests/tg/test_e2e_bun.py`: `tg-adapter ensure` → the real adapter → rows, rules, stop).
- `lifecycle.test.ts`: stop mid-stream + restart (exactly once, stored offset used), Telegram
  re-sending updates, lagging offset vs UNIQUE idem_key; wrong username, 409, 401, switch file,
  PID file.
- `process.test.ts`: the CLI as a process against a unix-socket mock: SIGKILL mid-stream +
  restart, no listening sockets, DB 0600, SIGTERM, exit codes, check / enqueue / status.
- `config.test.ts`: config and token interlocks, test-run path guard, schema contract (and the same
  DDL as `schemas/events.sql` once it exists), static guards over `src/`, lifted file hashes.
- `bind.test.ts`: registry and gate, parity of every 0.1.22 compat entry (`test/compat-tables.ts`) with the
  repo registry, bound runs (receipt, tg.bound now/digest, args, @otherbot, failure, timeout, /status, pictures),
  gated and unregistered entries, bindings-off, send off, rm:* env without the token, busy toast, tg-bind rows
  (args, longest prefix, expiry), card binds, the command menu, process groups killed at stop, a broken registry.
- `approvals.test.ts`: cards (fields, scrubbed body, notes, 0600, labels), refusals, no dedupe, his ✅ and ❌,
  stranger taps, store errors, expiry sweep and late taps, nonces from the CLI, replaced cards, bindings and
  approvals-off, the compat journal, a CLI `approvals wait` across an adapter restart, cards that expired while down,
  send off never edits, same DDL as `schemas/approvals.sql`.
- `hub.test.ts` (H6): hub mode against a fake hubd (`test/hubfake.ts`, the H5/H6 endpoint shapes) and the mock: config,
  rows once and in order across a lost watermark, a restart, a lost answer and a 30 s outage (polling and taps go on,
  the log stays quiet), patches and meta, reaction / poll answer / stop rows, the outbox (interrupted, dupes, mirror,
  every op with hub file refs, the file guard, download refs), photo refs, voice transcripts, bound commands, buttons
  and pictures through the companion (the ack before the job ends), the offline fallbacks, approvals (decide, one
  `approval.decided`, expiry, brain-minted nonces, no minting in hub mode), the bindings and presence mirrors, status,
  and the companion CLI (`--once`, status, the off switch) against the fake on loopback.
- `fold`, `buttons`, `quiet`, `clock`, `callbacks`: lifted unit tests.

## In the hub (H6)

Hub releases carry the adapter as ONE file, `tg/adapter.js` (`bun build src/cli.ts --target=bun`: grammy and the
DDL files inlined, no node_modules), plus `tg/tools.toml`; `ops/hub/tgloader.ts` runs it in claude-hub-tg with
TG_ADAPTER_TOOLS pointing at that registry. The DDL files are text imports (`import … with { type: 'text' }`), so the
source runs unchanged and the bundle needs no data files beside it. `packages/hubkit/release.py` builds the bundle in a
scratch copy with a real copy of node_modules (checked against bun.lock first), so it names no build path and the
same inputs give the same bytes.

## Licence

Derived from the telegram-plus channel plugin (`plugins/telegram-plus`, itself a modified fork of the official
Telegram channel plugin for Claude Code); Apache-2.0, see `LICENSE` in this directory.
