/**
 * telegram-plus bridge: the whole plugin (server.ts only imports this module).
 *
 * The session's Telegram channel without Telegram: tg-adapter polls the bot and sends; the bridge moves
 * events-store rows in and tool calls out. It reads no bot token and opens no network connection except
 * the voice inbox on 127.0.0.1.
 *
 *  - MCP: the same server name, the 11 tools with 0.1.22's schemas (+ optional urgent, buttons[].bind,
 *    approval: tools.ts), the same instructions, the channel capability. No permission relay.
 *  - Inbound: every POLL_MS, rows with delivery 'now' and a channel kind (bridgedb.ts) become
 *    notifications/claude/channel with 0.1.22's content + meta + event_id, in id order; each one upserts a
 *    deliveries row. At start, rows of the last REPLAY_S without a receipt are sent: never-sent ones as
 *    new, sent-once ones again (redelivered="1") once receipts are known to work.
 *  - Receipts: the receipt writer sets deliveries.receipt_ts. With receipts working and no receipt
 *    REDELIVER_MS after an idle point (tg-status board), a row is sent once more (redelivered="1"); still
 *    nothing → one bridge.undelivered event row (ids and kind only, never text).
 *  - brain-watch's redelivery request (meta bridge.redeliver {rid, ids, why, ts}; the lease says "redeliver": 1):
 *    each row still without a receipt goes out once more (redelivered="1"; not past REDELIVER_MAX_TRIES sends), the
 *    answer goes to meta bridge.redeliver.ack {rid, at, sent, skipped: {id: unknown|receipt|tries}}. Each request
 *    once, also across bridge restarts (the answer's rid).
 *  - Tools → one outbox row each (bot BOT, chat, op, body = the arguments without chat_id, plus
 *    hold:false and dedupe:false: bridge sends are never held or deduped); a row answering a channel
 *    event of the chat notified ≤ 30 min ago carries reply_to_event. The call waits ≤ SEND_WAIT_MS for the
 *    adapter (download ≤ DOWNLOAD_WAIT_MS) and answers as 0.1.22 did; a row still queued answers
 *    "queued #N (held: why)".
 *  - One bridge: only an instance whose Claude Code registers telegram-plus as a channel (or a manual run)
 *    that holds the flock lease takes inbound events and opens the voice inbox; any other serves tools.
 *  - Voice inbox (voiceinbox.ts, unchanged): each hand-off becomes a bridge.handoff row, delivered like
 *    the others (meta via=voice|app).
 *
 *  - Hub mode (TELEGRAM_BRIDGE_STORE=hub, v2 Phase 2 H6): tg-adapter runs in the hub; the same rows, deliveries and
 *    outbox come over the hub API (bridgehub.ts: HUB_URL, HUB_TOKEN_FILE, HUB_SCHEMA for tests), inbound by long
 *    poll instead of POLL_MS. Hub file refs in rows are fetched into STATE_DIR/inbox; attached files are uploaded.
 *    The hub is this module's only network peer, and only in hub mode; the redelivery meta stays in the local store.
 *
 * Env (defaults): TELEGRAM_BRIDGE_STORE (sqlite | hub), TELEGRAM_BRIDGE_HUB_WAIT_S (15), TELEGRAM_BRIDGE_DB (~/.local/state/assistant/events.db), TELEGRAM_BRIDGE_BOT (live),
 * TELEGRAM_BRIDGE_STATE_DIR (~/.local/state/tg-bridge: the lease), TELEGRAM_BRIDGE_POLL_MS (250),
 * TELEGRAM_BRIDGE_SEND_WAIT_MS (10000), TELEGRAM_BRIDGE_DOWNLOAD_WAIT_MS (60000),
 * TELEGRAM_BRIDGE_REDELIVER_MS (60000; 0 = off), TELEGRAM_BRIDGE_REPLAY_S (86400),
 * TELEGRAM_BRIDGE_LEASE_RETRY_MS (5000), TELEGRAM_BRIDGE_START_DELAY_MS (2000: after the client's
 * initialized, before the first notification), TELEGRAM_BRIDGE_FORCE=1 (skip the ancestor check; tests),
 * TELEGRAM_BRIDGE_TOOLS_ONLY=1 (never take inbound), TELEGRAM_STATUS_STATE, TELEGRAM_VOICE_INBOX_PORT,
 * TELEGRAM_VOICE_INBOX_TOKEN_FILE (as in 0.1.22).
 */
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { randomBytes } from 'crypto'
import { readFileSync, realpathSync, statSync } from 'fs'
import { homedir } from 'os'
import { join, sep } from 'path'
import { BridgeDb, DONE_STATUSES, REDELIVER_ACK_KEY, toChannel, type BridgeStore, type InRow, type OutRow } from './bridgedb.ts'
import { HUB_DEFAULT_URL, HubBridgeStore, HubDown, isHubRef, newRid } from './bridgehub.ts'
import { bridgeVeto, Lease } from './bridgelease.ts'
import { bridgeTools, CHAT_ACTIONS, INSTRUCTIONS, REACTION_EMOJI } from './tools.ts'
import { startVoiceInbox, type VoiceInboxHandle } from './voiceinbox.ts'

// The bridge never needs the bot token: if the environment carries one, drop it.
delete process.env.TELEGRAM_BOT_TOKEN

const env = process.env
const num = (k: string, def: number): number => {
  const v = env[k]
  return v != null && v.trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : def
}
const HOME = homedir()
const DB_PATH = env.TELEGRAM_BRIDGE_DB || join(HOME, '.local', 'state', 'assistant', 'events.db')
const STATE_DIR = env.TELEGRAM_BRIDGE_STATE_DIR || join(HOME, '.local', 'state', 'tg-bridge')
const BOT = env.TELEGRAM_BRIDGE_BOT || 'live'
const POLL_MS = num('TELEGRAM_BRIDGE_POLL_MS', 250)
const SEND_WAIT_MS = num('TELEGRAM_BRIDGE_SEND_WAIT_MS', 10_000)
const DOWNLOAD_WAIT_MS = num('TELEGRAM_BRIDGE_DOWNLOAD_WAIT_MS', 60_000)
const REDELIVER_MS = num('TELEGRAM_BRIDGE_REDELIVER_MS', 60_000)
const REPLAY_S = num('TELEGRAM_BRIDGE_REPLAY_S', 86_400)
const LEASE_RETRY_MS = num('TELEGRAM_BRIDGE_LEASE_RETRY_MS', 5_000)
const START_DELAY_MS = num('TELEGRAM_BRIDGE_START_DELAY_MS', 2_000)
const REPLY_WINDOW_S = 30 * 60
const PENDING_MAX_MS = 3_600_000 // without working receipts, forget a delivery after an hour
const REDELIVER_MAX_TRIES = 5 // a row sent this often is left to the bridge.undelivered row and brain-watch's notice
const STATUS_STATE = env.TELEGRAM_STATUS_STATE || join(HOME, '.local', 'state', 'tg-status', 'state.json')
const VERSION: string = (() => {
  try { return JSON.parse(readFileSync(join(import.meta.dir, 'package.json'), 'utf8')).version ?? 'unknown' } catch { return 'unknown' }
})()
const INSTANCE = `${process.pid}-${Date.now().toString(36)}`
const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
const STORE_MODE = (env.TELEGRAM_BRIDGE_STORE ?? '').trim().toLowerCase() === 'hub' ? 'hub' : 'sqlite'
const HUB_WAIT_S = num('TELEGRAM_BRIDGE_HUB_WAIT_S', 15) // under hubd's 20 s per-request budget
const INBOX_DIR = join(STATE_DIR, 'inbox')

const log = (m: string) => { process.stderr.write(`telegram bridge: ${m}\n`) }
const nowS = () => Date.now() / 1000
const errText = (err: unknown) => (err instanceof Error ? err.message : String(err))

// ---------------------------------------------------------------------------
// Events store (opened lazily: a missing store is retried, tools say why they can't work)
// ---------------------------------------------------------------------------

// Hub mode: one HubBridgeStore (no I/O until used); it never fails to "open", each call says why it can't work.
const hubStore: HubBridgeStore | null = STORE_MODE === 'hub'
  ? new HubBridgeStore({
      url: env.HUB_URL || HUB_DEFAULT_URL, tokenFile: env.HUB_TOKEN_FILE || join(HOME, '.secrets', 'hub-token'),
      schema: env.HUB_SCHEMA ?? '', metaDbPath: DB_PATH, inboxDir: INBOX_DIR, maxFileBytes: MAX_ATTACHMENT_BYTES, log: m => log(m),
    })
  : null
let store: BridgeStore | null = hubStore
let storeErr = ''
function getStore(): BridgeStore | null {
  if (store) return store
  try {
    store = BridgeDb.open(DB_PATH)
    storeErr = ''
    log('events store open')
  } catch (err) {
    const m = errText(err)
    if (m !== storeErr) log(`events store unavailable: ${m}`)
    storeErr = m
  }
  return store
}
function needStore(): BridgeStore {
  const s = getStore()
  if (!s) throw new Error(storeErr || 'events store unavailable')
  return s
}

// ---------------------------------------------------------------------------
// Tools → outbox rows
// ---------------------------------------------------------------------------

type Outcome = { ids: number[]; notes: string[]; text?: string; path?: string; files: string[] }
type Call = {
  op: string
  chat: string
  body: Record<string, unknown>
  urgent?: boolean
  files?: string[]
  waitMs?: number
  done: (o: Outcome) => string
}
type ToolResult = { content: { type: 'text'; text: string }[]; isError?: boolean }

const reply = (text: string, isError = false): ToolResult => ({ content: [{ type: 'text', text }], ...(isError ? { isError: true } : {}) })

/** chat_id as 0.1.22 takes it: required, a string or an integer. */
function chatIdArg(args: Record<string, unknown>): string {
  const v = args.chat_id
  if (v == null || (typeof v === 'string' && v.trim() === '')) throw new Error('chat_id is required')
  if (typeof v === 'number') {
    if (!Number.isSafeInteger(v)) throw new Error(`chat_id must be an integer: ${v}`)
    return String(v)
  }
  if (typeof v !== 'string') throw new Error('chat_id must be a string or a number')
  return v.trim()
}

function need(args: Record<string, unknown>, key: string): unknown {
  if (args[key] == null || args[key] === '') throw new Error(`${key} is required`)
  return args[key]
}

/** Home directories to guard: $HOME and the account's home from /etc/passwd (they differ in tests). */
function homes(): string[] {
  const out = new Set<string>([HOME])
  try {
    const uid = process.getuid?.()
    for (const line of readFileSync('/etc/passwd', 'utf8').split('\n')) {
      const f = line.split(':')
      if (f.length > 5 && Number(f[2]) === uid && f[5]) out.add(f[5])
    }
  } catch {}
  return [...out]
}

/** Files the bridge refuses to attach: secrets, Claude Code's config (the plugin's state and token live
 *  there) and the adapter's config and state, except the two download inboxes. */
function fileProblem(f: string): string | null {
  if (!f.startsWith('/')) return `not an absolute path: ${f}`
  let real: string
  try { real = realpathSync(f) } catch { return `no such file: ${f}` }
  let st
  try { st = statSync(real) } catch { return `no such file: ${f}` }
  if (!st.isFile()) return `not a file: ${f}`
  if (st.size > MAX_ATTACHMENT_BYTES) return `file too large: ${f} (${(st.size / 1024 / 1024).toFixed(1)}MB, max 50MB)`
  for (const h of homes()) {
    const under = (...p: string[]) => { const d = join(h, ...p); return real === d || real.startsWith(d + sep) }
    const adapterInbox = /^\/.*\/\.local\/state\/tg-adapter\/[^/]+\/inbox\/[^/]+$/.test(real)
    const bridgeInbox = /^\/.*\/\.local\/state\/tg-bridge\/inbox\/[^/]+$/.test(real) // hub mode's fetched files
    if (under('.claude', 'channels', 'telegram', 'inbox') || (under('.local', 'state', 'tg-adapter') && adapterInbox) ||
      (under('.local', 'state', 'tg-bridge', 'inbox') && bridgeInbox)) continue
    if (under('.secrets') || under('.claude') || under('.config', 'tg-adapter') || under('.local', 'state', 'tg-adapter') || under('.local', 'state', 'tg-bridge')) {
      return `refusing to send a file from a private directory: ${f}`
    }
  }
  return null
}

const REACTION_SET = new Set(REACTION_EMOJI)
const normalizeReaction = (e: string) => e.replace(/️/g, '').trim()

function sendResult(ids: number[], notes: string[]): string {
  const base = ids.length === 1 ? `sent (id: ${ids[0]})` : `sent ${ids.length} parts (ids: ${ids.join(', ')})`
  return notes.length ? `${base}\nnote: ${notes.join('; ')}` : base
}
const withNotes = (s: string, o: Outcome) => (o.notes.length ? `${s}\nnote: ${o.notes.join('; ')}` : s)

/** Validate a tool call as 0.1.22 does (where the bridge can) and describe its outbox row. */
function toCall(name: string, args: Record<string, unknown>): Call | null {
  const { chat_id: _chat, ...rest } = args
  switch (name) {
    case 'reply': {
      const chat = chatIdArg(args)
      const text = typeof args.text === 'string' ? args.text : ''
      const files = Array.isArray(args.files) ? args.files.map(String) : []
      for (const f of files) {
        const p = fileProblem(f)
        if (p) throw new Error(p)
      }
      const details = typeof args.details === 'string' && args.details !== ''
      if (text === '' && files.length === 0 && !details) throw new Error('text is empty and no files given')
      return { op: 'send', chat, body: rest, urgent: args.urgent === true, files, done: o => o.text ?? sendResult(o.ids, o.notes) }
    }
    case 'edit_message': {
      const chat = chatIdArg(args)
      const message_id = need(args, 'message_id')
      if (args.text == null) {
        if (typeof args.details === 'string' && args.details !== '') throw new Error('details needs text: pass the full new text together with details')
        if (args.buttons == null) throw new Error('pass text and/or buttons')
      }
      return { op: 'edit', chat, body: rest, done: o => o.text ?? withNotes(args.text == null ? `edited buttons (id: ${message_id})` : `edited (id: ${message_id})`, o) }
    }
    case 'react': {
      const chat = chatIdArg(args)
      need(args, 'message_id')
      if (args.emoji == null && args.emojis == null && args.remove !== true) throw new Error('pass emoji, emojis or remove: true')
      const raw = args.remove === true ? [] : Array.isArray(args.emojis) ? (args.emojis as unknown[]).map(String) : [String(args.emoji ?? '')]
      const list = raw.map(normalizeReaction).filter(e => e !== '')
      const bad = list.filter(e => !REACTION_SET.has(e))
      if (bad.length) throw new Error(`not in Telegram's reaction whitelist: ${bad.join(' ')} — allowed: ${REACTION_EMOJI.join(' ')}`)
      return { op: 'react', chat, body: rest, done: o => o.text ?? (list.length ? `reacted ${list.join(' ')}` : 'reaction removed') }
    }
    case 'download_attachment': {
      const file_id = need(args, 'file_id')
      if (typeof file_id !== 'string') throw new Error('file_id must be a string')
      return {
        op: 'download', chat: '', body: { file_id }, waitMs: DOWNLOAD_WAIT_MS,
        done: o => {
          const path = o.path ?? o.files[0]
          if (!path) throw new Error('download finished without a path')
          return path
        },
      }
    }
    case 'draft': {
      const chat = chatIdArg(args)
      if (args.clear) return { op: 'draft', chat, body: { clear: true }, done: o => o.text ?? 'draft cleared' }
      return { op: 'draft', chat, body: rest, done: o => o.text ?? withNotes(`draft updated (${String(args.text ?? '').length} chars)`, o) }
    }
    case 'chat_action': {
      const chat = chatIdArg(args)
      if (args.stop) return { op: 'chat_action', chat, body: { stop: true }, done: () => 'stopped' }
      const action = typeof args.action === 'string' ? args.action : 'typing'
      if (!CHAT_ACTIONS.includes(action)) throw new Error(`unknown action: ${action}`)
      return { op: 'chat_action', chat, body: { ...rest, action }, done: o => o.text ?? `showing ${action}` }
    }
    case 'send_poll': {
      const chat = chatIdArg(args)
      need(args, 'question')
      const options = Array.isArray(args.options) ? args.options : []
      if (options.length < 2 || options.length > 12) throw new Error('2-12 options required')
      return { op: 'poll', chat, body: rest, urgent: args.urgent === true, done: o => o.text ?? `poll sent (id: ${o.ids[0]})` }
    }
    case 'send_location': {
      const chat = chatIdArg(args)
      if (typeof args.latitude !== 'number' || typeof args.longitude !== 'number') throw new Error('latitude and longitude must be numbers')
      return { op: 'location', chat, body: rest, urgent: args.urgent === true, done: o => o.text ?? `sent (id: ${o.ids[0]})` }
    }
    case 'pin_message': {
      const chat = chatIdArg(args)
      need(args, 'message_id')
      return { op: 'pin', chat, body: rest, done: o => o.text ?? (args.unpin ? 'unpinned' : 'pinned') }
    }
    case 'delete_message': {
      const chat = chatIdArg(args)
      need(args, 'message_id')
      return { op: 'delete', chat, body: rest, done: o => o.text ?? 'deleted' }
    }
    case 'set_commands': {
      const list = (args.commands as { command: string; description: string }[] | undefined) ?? []
      if (!Array.isArray(list)) throw new Error('commands must be an array')
      const clean = list.map(c => ({ command: String(c.command).replace(/^\//, '').toLowerCase(), description: String(c.description) }))
      for (const c of clean) {
        if (!/^[a-z0-9_]{1,32}$/.test(c.command)) throw new Error(`invalid command name: ${c.command}`)
        if (c.description.length < 1 || c.description.length > 256) throw new Error(`description for /${c.command} must be 1-256 chars`)
      }
      return {
        op: 'commands', chat: '', body: { commands: clean },
        done: o => o.text ?? (clean.length ? `commands set: ${clean.map(c => '/' + c.command).join(' ')}` : 'menu cleared (no commands)'),
      }
    }
    default:
      return null
  }
}

function outcome(r: OutRow): Outcome {
  let res: Record<string, unknown> = {}
  try { res = r.result ? JSON.parse(r.result) ?? {} : {} } catch {}
  let files: string[] = []
  try { files = r.files ? JSON.parse(r.files) : [] } catch {}
  const ids = Array.isArray(res.ids) && res.ids.length ? res.ids.map(Number) : r.tg_message_id != null ? [r.tg_message_id] : []
  const notes = Array.isArray(res.notes) ? res.notes.map(String)
    : r.last_error?.startsWith('note: ') ? [r.last_error.slice(6)] : []
  return {
    ids, notes, files: Array.isArray(files) ? files.map(String) : [],
    ...(typeof res.text === 'string' ? { text: res.text } : {}),
    ...(typeof res.path === 'string' ? { path: res.path } : {}),
  }
}

let seq = 0
const hubWhy = (err: unknown) => (err instanceof HubDown ? `hub unavailable (${errText(err)})` : errText(err))
/** Hub mode: a hub call that found the hub down is tried again until the deadline (its rid makes a repeat harmless). */
async function untilDeadline<T>(deadline: number, f: () => Promise<T>): Promise<T> {
  for (;;) {
    try { return await f() } catch (err) {
      if (!(err instanceof HubDown) || Date.now() + 250 >= deadline) throw err
      await Bun.sleep(250)
    }
  }
}

async function runCall(name: string, c: Call): Promise<ToolResult> {
  const s = needStore()
  let files = c.files
  if (hubStore && files?.length) {
    const up: string[] = []
    try { for (const f of files) up.push(await untilDeadline(Date.now() + SEND_WAIT_MS, () => hubStore.upload(f))) } catch (err) {
      throw new Error(`${hubWhy(err)}: the files could not be handed to the hub; nothing was sent, try again`)
    }
    files = up
  }
  const deadline = Date.now() + (c.waitMs ?? SEND_WAIT_MS)
  let answering: { id: number; trace_id: string | null } | null = null
  if (c.chat) {
    try { answering = await s.latestDelivered(c.chat, nowS() - REPLY_WINDOW_S) } catch (err) {
      if (!(hubStore && err instanceof HubDown)) throw err // hub mode: the reply goes out without reply_to_event
    }
  }
  const o = {
    bot: BOT, chat: c.chat, op: c.op,
    body: JSON.stringify({ ...c.body, hold: false, dedupe: false }),
    idem_key: `bridge:${INSTANCE}:${++seq}`,
    urgent: c.urgent ? 1 : 0,
    reply_to_event: answering?.id ?? null,
    files: files?.length ? JSON.stringify(files) : null,
    trace_id: answering?.trace_id ?? null,
  }
  let id: number
  if (hubStore) {
    const rid = newRid()
    try { id = await untilDeadline(deadline, () => hubStore.enqueue(o, rid)) } catch (err) {
      throw new Error(err instanceof HubDown ? `${hubWhy(err)}: nothing was sent, try again` : errText(err))
    }
  } else {
    id = await s.enqueue(o)
  }
  // undefined: the hub did not answer this read (hub mode only)
  const read = async (): Promise<OutRow | null | undefined> => {
    try { return await s.outRow(id) } catch (err) {
      if (hubStore && err instanceof HubDown) return undefined
      throw err
    }
  }
  let row = await read()
  while (row !== null && !(row && DONE_STATUSES.has(row.status)) && Date.now() < deadline) {
    await Bun.sleep(hubStore ? 80 : 40)
    row = await read()
  }
  if (row === undefined) return reply(`queued #${id} (held: the hub is not answering, not confirmed yet)`)
  if (!row) throw new Error(`outbox row #${id} is gone`)
  if (row.status === 'failed') throw new Error(row.last_error || `outbox #${id} failed`)
  if (row.status === 'skipped') return reply(`${name}: not sent, outbox #${id} skipped (${row.last_error ?? 'no reason given'})`, true)
  if (!DONE_STATUSES.has(row.status)) {
    const why = row.status === 'sending' ? 'sending, not confirmed yet' : row.last_error || 'not sent yet'
    return reply(`queued #${id} (held: ${why})`)
  }
  const out = outcome(row)
  if (hubStore) {
    // a download's file is in the hub: fetch it into the inbox and answer with the local path
    try {
      if (out.path && isHubRef(out.path)) out.path = await hubStore.fetchRef(out.path)
      if (!out.path && c.op === 'download' && out.files[0] && isHubRef(out.files[0])) out.files[0] = await hubStore.fetchRef(out.files[0])
    } catch (err) {
      throw new Error(`${hubWhy(err)}: the file could not be fetched from the hub; try again`)
    }
  }
  return reply(c.done(out))
}

// ---------------------------------------------------------------------------
// MCP server
// ---------------------------------------------------------------------------

const mcp = new Server(
  { name: 'telegram', version: '1.0.0' },
  { capabilities: { tools: {}, experimental: { 'claude/channel': {} } }, instructions: INSTRUCTIONS },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: bridgeTools() }))

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  const name = req.params.name
  const args = (req.params.arguments ?? {}) as Record<string, unknown>
  try {
    const call = toCall(name, args)
    if (!call) return reply(`unknown tool: ${name}`, true)
    return await runCall(name, call)
  } catch (err) {
    return reply(`${name} failed: ${errText(err)}`, true)
  }
})

// ---------------------------------------------------------------------------
// Inbound: rows → channel notifications, receipts, redelivery, replay
// ---------------------------------------------------------------------------

type Pending = { row: InRow; firstAt: number; lastAt: number; tries: number }
const pending = new Map<number, Pending>()
let active = false // eligible, lease held, client initialized, start delay over
let cursor = -1 // last event id looked at (-1: start-up replay not done yet)
let receiptsLive = false
const unsentRid = new Map<number, string>() // hub mode: rows whose deliveries write got no answer (same rid next time)
const sentHere = new Set<number>() // rows this process notified (a replay cut short by the hub is resumed without repeats)

const hhmm = (s: number) => {
  const d = new Date(s * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function notify(s: BridgeStore, row: InRow, redelivered: boolean): Promise<void> {
  if (hubStore) row = await hubStore.resolveRow(row) // hub files → local paths first: no notification without them
  const rid = unsentRid.get(row.id) ?? newRid()
  let tries: number
  try {
    tries = await s.markSent(row.id, rid)
    unsentRid.delete(row.id)
  } catch (err) {
    if (hubStore && err instanceof HubDown) unsentRid.set(row.id, rid)
    throw err
  }
  const { content, meta } = toChannel(row)
  const now = Date.now()
  pending.set(row.id, { row, firstAt: pending.get(row.id)?.firstAt ?? now, lastAt: now, tries })
  const params = redelivered
    ? { content: `(re-sent: no sign it reached you; first sent ${hhmm(row.ts)}; if you already handled it, ignore it)\n${content}`, meta: { ...meta, redelivered: '1' } }
    : { content, meta }
  mcp.notification({ method: 'notifications/claude/channel', params }).catch(err => log(`notify #${row.id} failed: ${errText(err)}`))
  if (sentHere.size >= 5000) sentHere.clear()
  sentHere.add(row.id)
  log(`delivered #${row.id} ${row.kind}${redelivered ? ' (redelivered)' : ''}`)
}

async function replayAtStart(s: BridgeStore): Promise<void> {
  const maxId = await s.maxEventId()
  const since = nowS() - REPLAY_S
  receiptsLive = await s.receiptsSeen(since)
  let fresh = 0
  let again = 0
  let held = 0
  const rows = await s.unreceipted(since, maxId)
  cursor = maxId // a row that fails below has no receipt: the next start sends it
  for (const r of rows) {
    if (sentHere.has(r.id)) continue // sent by this process before the hub broke off the last replay
    try {
      if (!r.tries || unsentRid.has(r.id)) { await notify(s, r, false); fresh++ }
      else if (receiptsLive && r.tries < 2) { await notify(s, r, true); again++ }
      else held++
    } catch (err) {
      // hub mode: the hub broke off; the whole replay runs again once it answers (rows sent so far are skipped)
      if (hubStore && err instanceof HubDown) { cursor = -1; throw err }
      log(`start: #${r.id} not sent: ${errText(err)}`)
    }
  }
  log(`start: ${fresh} waiting + ${again} unreceipted row(s) sent${held ? `, ${held} not re-sent (${receiptsLive ? 'already re-sent once' : 'no receipts seen yet'})` : ''}`)
}

let ticking = false
let inboundErr = ''
/** One look for new rows (hub mode: a long poll of up to waitS). 'busy': another look is running. */
async function tick(waitS = 0): Promise<'ok' | 'busy' | 'error'> {
  if (!active || shuttingDown) return 'ok'
  if (ticking) return 'busy'
  const s = getStore()
  if (!s) return 'error'
  ticking = true
  try {
    if (cursor < 0) await replayAtStart(s)
    for (const row of await s.rowsAfter(cursor, 200, waitS)) {
      if (shuttingDown) break
      // Already received (a binding answered it) or sent by another bridge: not ours to send.
      if (row.receipt_ts == null && (!row.tries || unsentRid.has(row.id))) await notify(s, row, false)
      cursor = row.id // only once recorded: a row whose deliveries write failed is tried again next tick
    }
    if (hubStore && inboundErr) log('inbound: the hub answers again')
    inboundErr = ''
    return 'ok'
  } catch (err) {
    const m = errText(err)
    if (!hubStore || m !== inboundErr) log(`inbound: ${m}`)
    inboundErr = m
    return 'error'
  } finally {
    ticking = false
  }
}

/** Hub mode: inbound is one long poll after another; a failure backs off 0.5 s doubling to 5 s. */
let hubLooping = false
async function hubLoop(): Promise<void> {
  if (hubLooping) return
  hubLooping = true
  let backoff = 0
  while (!shuttingDown) {
    const r = await tick(HUB_WAIT_S)
    if (r === 'error') { backoff = Math.min(backoff ? backoff * 2 : 500, 5_000); await Bun.sleep(backoff) }
    else { backoff = 0; if (r === 'busy') await Bun.sleep(50) }
  }
}

type Board = { working: boolean; idleAt: number }
/** tg-status's view of the session (0.1.22's readBoard, uncached); null when unreadable. */
function readBoard(): Board | null {
  try {
    const st = statSync(STATUS_STATE)
    if (!st.isFile() || st.size > 1 << 20) return null
    const s = JSON.parse(readFileSync(STATUS_STATE, 'utf8'))
    const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)
    const idle = n(s?.idle_at) ?? (s?.mode === 'idle' ? n(s?.since) : undefined)
    if (s?.mode === 'working') return { working: true, idleAt: (idle ?? 0) * 1000 }
    if (s?.mode === 'idle' && idle !== undefined) return { working: false, idleAt: idle * 1000 }
  } catch {}
  return null
}

let checking = false
let receiptsErr = ''
async function receiptCheck(): Promise<void> {
  if (!active || shuttingDown || !pending.size || checking) return
  const s = getStore()
  if (!s) return
  checking = true
  try {
    const got = await s.receipts([...pending.keys()])
    for (const id of [...pending.keys()]) {
      if (got.get(id) != null) { pending.delete(id); receiptsLive = true }
    }
    const now = Date.now()
    if (!REDELIVER_MS || !receiptsLive) {
      for (const [id, p] of pending) if (now - p.firstAt > PENDING_MAX_MS) pending.delete(id)
      return
    }
    const board = readBoard()
    if (board?.working) return // a busy turn: the notification waits for its end
    for (const [id, p] of pending) {
      if (now - Math.max(p.lastAt, board?.idleAt ?? 0) < REDELIVER_MS) continue
      if (p.tries < 2) {
        await notify(s, p.row, true)
      } else {
        pending.delete(id)
        const rowId = await s.insertEvent({
          kind: 'bridge.undelivered', subject: `undelivered owner event #${p.row.id} (${p.row.kind})`,
          payload: { event_id: p.row.id, kind: p.row.kind, tries: p.tries, age_s: Math.round(nowS() - p.row.ts) },
          labels: ['tg', 'bridge'], delivery: 'now', idem_key: `bridge.undelivered:${p.row.id}`, trace_id: p.row.trace_id,
        })
        log(`no receipt for #${p.row.id} after ${p.tries} tries${rowId ? `: bridge.undelivered #${rowId}` : ''}`)
      }
    }
    receiptsErr = ''
  } catch (err) {
    const m = errText(err)
    if (!hubStore || m !== receiptsErr) log(`receipts: ${m}`)
    receiptsErr = m
  } finally {
    checking = false
  }
}

let answeredRid = ''
let redelivering = false
/** brain-watch's redelivery request: rows still without a receipt once more; the answer in meta. */
async function redeliverCheck(): Promise<void> {
  if (!active || shuttingDown || redelivering) return
  const s = getStore()
  if (!s) return
  redelivering = true
  try {
    const req = s.redeliverRequest()
    if (!req || req.rid === answeredRid) return
    const prev = s.metaGet(REDELIVER_ACK_KEY) as { rid?: unknown } | null
    if (prev?.rid === req.rid) { answeredRid = req.rid; return } // answered by an earlier bridge
    const sent: number[] = []
    const skipped: Record<string, string> = {}
    for (const id of req.ids) {
      const row = await s.channelRow(id)
      if (!row) skipped[id] = 'unknown'
      else if (row.receipt_ts != null) skipped[id] = 'receipt'
      else if ((row.tries ?? 0) >= REDELIVER_MAX_TRIES) skipped[id] = 'tries'
      else { await notify(s, row, true); sent.push(id) }
    }
    s.metaSet(REDELIVER_ACK_KEY, { rid: req.rid, at: nowS(), sent, skipped, pid: process.pid, version: VERSION })
    answeredRid = req.rid
    const sk = Object.entries(skipped).map(([i, w]) => `#${i} ${w}`).join(' ')
    log(`redeliver ${req.rid} (${req.why}): sent ${sent.length ? sent.map(i => `#${i}`).join(' ') : 'none'}${sk ? `; skipped ${sk}` : ''}`)
  } catch (err) {
    log(`redeliver: ${errText(err)}`)
  } finally {
    redelivering = false
  }
}

// ---------------------------------------------------------------------------
// Voice inbox: hand-offs → bridge.handoff rows (delivered like any other row)
// ---------------------------------------------------------------------------

let voice: VoiceInboxHandle | null = null
async function handoff(content: string, meta: Record<string, string>): Promise<void> {
  const s = needStore()
  const via = meta.via === 'app' ? 'app' : 'voice'
  const key = meta.voice_id ?? meta.inbox_id ?? meta.app_msg_id ?? randomBytes(6).toString('hex')
  const e = {
    kind: 'bridge.handoff', subject: `${via} hand-off`, payload: { ...meta, text: content },
    labels: ['tg', 'bridge', via], delivery: 'now', idem_key: `bridge.handoff:${via}:${key}`,
  }
  // hub mode: ride over a short hub gap (the idem key makes a repeat a no-op); still down → the caller hears "not delivered"
  const id = hubStore ? await untilDeadline(Date.now() + 5_000, async () => s.insertEvent(e)) : await s.insertEvent(e)
  log(id ? `hand-off #${id} (${via})` : `hand-off (${via}) repeated: already queued`)
  if (!hubStore) queueMicrotask(() => { void tick() }) // hub mode: the hub wakes the long poll
}
function startVoice(): void {
  if (!env.TELEGRAM_VOICE_INBOX_PORT) return
  voice = startVoiceInbox({
    port: Number(env.TELEGRAM_VOICE_INBOX_PORT),
    tokenFile: env.TELEGRAM_VOICE_INBOX_TOKEN_FILE ?? join(HOME, '.secrets', 'voice-local-token'),
    deliver: handoff,
    retries: 30,
    log: m => log(m),
    onListen: h => { voice = h },
  })
}

// ---------------------------------------------------------------------------
// One bridge: ancestor check + lease; start and shutdown
// ---------------------------------------------------------------------------

const lease = new Lease(join(STATE_DIR, 'lease'))
const veto = bridgeVeto()
let initialized = false
let leaseNoted = false

function activate(): void {
  if (active || shuttingDown || veto || !initialized) return
  if (!lease.tryAcquire({ version: VERSION, mode: 'bridge', bot: BOT, redeliver: 1 })) {
    if (!leaseNoted) log('another bridge holds the lease: tools only until it ends')
    leaseNoted = true
    return
  }
  log(`lease held (pid ${process.pid}): taking inbound events`)
  startVoice()
  setTimeout(() => {
    if (shuttingDown) return
    active = true
    if (hubStore) {
      const n = hubStore.pruneInbox()
      if (n) log(`inbox: ${n} old file(s) removed`)
      void hubLoop()
    } else void tick()
  }, START_DELAY_MS)
}

let shuttingDown = false
function shutdown(): void {
  if (shuttingDown) return
  shuttingDown = true
  log('shutting down')
  try { voice?.stop() } catch {}
  lease.release()
  try { store?.close() } catch {}
  process.exit(0)
}
process.stdin.on('end', shutdown)
process.stdin.on('close', shutdown)
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
process.on('SIGHUP', shutdown)

mcp.oninitialized = () => {
  initialized = true
  activate()
}

await mcp.connect(new StdioServerTransport())
log(`${VERSION} up (bot ${BOT}${hubStore ? `, hub store ${hubStore.hub.url}` : ''}); ${veto ? `tools only: ${veto}` : 'eligible for inbound'}`)
getStore()
if (!hubStore) setInterval(() => { void tick() }, POLL_MS).unref()
else setInterval(() => { hubStore.pruneInbox() }, 6 * 3_600_000).unref()
setInterval(() => { void receiptCheck() }, 2_000).unref()
setInterval(() => { void redeliverCheck() }, 1_000).unref()
if (!veto) setInterval(activate, LEASE_RETRY_MS).unref()
setInterval(() => {
  if (process.stdin.destroyed || process.stdin.readableEnded) shutdown()
}, 5_000).unref()
