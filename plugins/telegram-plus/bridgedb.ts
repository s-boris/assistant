// Bridge mode: the events store (SQLite, shared with tg-adapter and the brain's tools).
//
// Inbound: rows with delivery 'now' and a channel kind (CHANNEL_KINDS) become channel notifications,
// in id order. payload = the 0.1.22 channel meta + `text` (the notification content), as tg-adapter
// writes it; update_id, bot and ack_ms stay out of the meta. Each notification upserts a `deliveries`
// row (sent_ts, tries); the receipt writer (the agenda hook) sets receipt_ts + how. brain-watch asks for a row to go
// out once more through meta bridge.redeliver; the bridge answers in meta bridge.redeliver.ack.
// Outbound: one `outbox` row per tool call (bot = the instance, chat, op, body = the tool's arguments).
//
// DDL v2 is additive and forward-only. The bridge creates the parts it uses when they are missing, with
// the plan's DDL text, so it works before or after core/db.py carries them: the deliveries table and the
// outbox columns reply_to_event, files and result (result: the adapter's JSON outcome of a row:
// {"ids": [...], "notes": [...], "text": "...", "path": "..."}, all optional).
import { Database } from 'bun:sqlite'
import { existsSync } from 'fs'
import { parseScanPrivate } from './scan.ts'

export const CHANNEL_KINDS = [
  'tg.message', 'tg.button', 'tg.reaction', 'tg.poll_answer', 'tg.stop', 'tg.bound', 'approval.decided', 'bridge.handoff',
] as const
export const BRIDGE_SOURCE = 'tg-bridge'
/** brain-watch's redelivery request and the bridge's answer (meta rows, JSON). */
export const REDELIVER_KEY = 'bridge.redeliver'
export const REDELIVER_ACK_KEY = 'bridge.redeliver.ack'
export const DONE_STATUSES = new Set(['sent', 'failed', 'skipped'])
/** Payload keys that are row bookkeeping, not 0.1.22 channel meta. */
const NOT_META = new Set(['text', 'update_id', 'bot', 'ack_ms', 'event_id', 'redelivered'])

export const DELIVERIES_DDL =
  'CREATE TABLE IF NOT EXISTS deliveries(event_id INTEGER PRIMARY KEY, sent_ts REAL, tries INTEGER NOT NULL DEFAULT 0, receipt_ts REAL, how TEXT)'
export const OUTBOX_V2_COLUMNS: [string, string][] = [['reply_to_event', 'INTEGER'], ['files', 'TEXT'], ['result', 'TEXT']]

export type InRow = {
  id: number; ts: number; kind: string; payload: string; trace_id: string | null; status: string
  tries: number | null; receipt_ts: number | null
}
export type RedeliverRequest = { rid: string; ids: number[]; why: string; ts: number }
export type OutRow = {
  id: number; status: string; tg_message_id: number | null; last_error: string | null; result: string | null; files: string | null
}
export type Enqueue = {
  bot: string; chat: string; op: string; body: string; idem_key: string; urgent: number
  reply_to_event: number | null; files: string | null; trace_id: string | null
}

const KIND_LIST = CHANNEL_KINDS.map(k => `'${k}'`).join(', ')
const nowS = () => Date.now() / 1000

type Awaitable<T> = T | Promise<T>
/** What the bridge needs from its store: BridgeDb (SQLite, synchronous) or HubBridgeStore (bridgehub.ts, the hub API).
 *  The bridge awaits every call. markSent's rid makes a retried hub write idempotent (SQLite ignores it); rowsAfter's
 *  waitS is the hub's long poll (SQLite answers at once). The redelivery meta keys stay in the local SQLite store in
 *  both modes (brain-watch writes them there). */
export interface BridgeStore {
  maxEventId(): Awaitable<number>
  rowsAfter(id: number, limit?: number, waitS?: number): Awaitable<InRow[]>
  unreceipted(sinceTs: number, maxId: number): Awaitable<InRow[]>
  receiptsSeen(sinceTs: number): Awaitable<boolean>
  markSent(event_id: number, rid?: string): Awaitable<number>
  receipts(ids: number[]): Awaitable<Map<number, number | null>>
  channelRow(id: number): Awaitable<InRow | null>
  metaGet(k: string): unknown
  metaSet(k: string, v: unknown): void
  redeliverRequest(): RedeliverRequest | null
  insertEvent(e: { kind: string; subject: string; payload: Record<string, unknown>; labels: string[]; delivery: string; idem_key: string; trace_id?: string | null }): Awaitable<number | null>
  latestDelivered(chat: string, sinceTs: number): Awaitable<{ id: number; trace_id: string | null } | null>
  enqueue(o: Enqueue): Awaitable<number>
  outRow(id: number): Awaitable<OutRow | null>
  close(): void
}

export class BridgeDb implements BridgeStore {
  constructor(readonly db: Database) {}

  /** Hub mode: the local store only for the redelivery meta keys (no DDL changes, read-write, never created). */
  static openMeta(path: string): BridgeDb {
    if (!existsSync(path)) throw new Error(`events store missing: ${path}`)
    const db = new Database(path, { readwrite: true, create: false })
    db.exec('PRAGMA busy_timeout=5000')
    return new BridgeDb(db)
  }

  close(): void {
    this.db.close()
  }

  /** Open an existing store (never creates it: the store and its 0600 mode belong to core/db.py). */
  static open(path: string): BridgeDb {
    if (!existsSync(path)) throw new Error(`events store missing: ${path}`)
    const db = new Database(path, { readwrite: true, create: false })
    db.exec('PRAGMA busy_timeout=5000')
    db.exec('PRAGMA synchronous=FULL')
    const b = new BridgeDb(db)
    b.ensureV2()
    return b
  }

  ensureV2(): void {
    this.db.exec(DELIVERIES_DDL)
    const have = new Set((this.db.query('PRAGMA table_info(outbox)').all() as { name: string }[]).map(c => c.name))
    if (!have.size) throw new Error('events store has no outbox table')
    for (const [name, type] of OUTBOX_V2_COLUMNS) {
      if (!have.has(name)) {
        try { this.db.exec(`ALTER TABLE outbox ADD COLUMN ${name} ${type}`) } catch (err) {
          if (!/duplicate column/i.test(String(err))) throw err // a concurrent writer added it first
        }
      }
    }
  }

  maxEventId(): number {
    return (this.db.query('SELECT COALESCE(MAX(id), 0) AS m FROM events').get() as { m: number }).m
  }

  /** New channel rows after `id`, oldest first. */
  rowsAfter(id: number, limit = 200, _waitS = 0): InRow[] {
    return this.db.query(`SELECT e.id, e.ts, e.kind, e.payload, e.trace_id, e.status, d.tries, d.receipt_ts
      FROM events e LEFT JOIN deliveries d ON d.event_id = e.id
      WHERE e.id > ? AND e.delivery = 'now' AND e.kind IN (${KIND_LIST}) ORDER BY e.id LIMIT ?`).all(id, limit) as InRow[]
  }

  /** Start-up replay: channel rows since `sinceTs` (≤ maxId) without a receipt that nobody acked or dismissed. */
  unreceipted(sinceTs: number, maxId: number): InRow[] {
    return this.db.query(`SELECT e.id, e.ts, e.kind, e.payload, e.trace_id, e.status, d.tries, d.receipt_ts
      FROM events e LEFT JOIN deliveries d ON d.event_id = e.id
      WHERE e.id <= ? AND e.ts >= ? AND e.delivery = 'now' AND e.kind IN (${KIND_LIST})
        AND e.status NOT IN ('acked', 'dismissed', 'gone') AND d.receipt_ts IS NULL ORDER BY e.id`).all(maxId, sinceTs) as InRow[]
  }

  /** Did any delivery get a receipt since `sinceTs`? (Receipts work, so a missing one means something.) */
  receiptsSeen(sinceTs: number): boolean {
    return !!this.db.query('SELECT 1 FROM deliveries WHERE receipt_ts IS NOT NULL AND receipt_ts >= ? LIMIT 1').get(sinceTs)
  }

  /** Record a notification; returns the row's tries so far (1 = first). The rid is the hub's (unused here). */
  markSent(event_id: number, _rid?: string, at = nowS()): number {
    this.db.query(`INSERT INTO deliveries(event_id, sent_ts, tries) VALUES (?, ?, 1)
      ON CONFLICT(event_id) DO UPDATE SET sent_ts = excluded.sent_ts, tries = tries + 1`).run(event_id, at)
    return (this.db.query('SELECT tries FROM deliveries WHERE event_id = ?').get(event_id) as { tries: number }).tries
  }

  receipts(ids: number[]): Map<number, number | null> {
    const out = new Map<number, number | null>()
    if (!ids.length) return out
    const rows = this.db.query(`SELECT event_id, receipt_ts FROM deliveries WHERE event_id IN (${ids.map(() => '?').join(', ')})`)
      .all(...ids) as { event_id: number; receipt_ts: number | null }[]
    for (const r of rows) out.set(r.event_id, r.receipt_ts)
    return out
  }

  /** One channel row by id with its delivery state, whatever its status; null when it is no channel row. */
  channelRow(id: number): InRow | null {
    return this.db.query(`SELECT e.id, e.ts, e.kind, e.payload, e.trace_id, e.status, d.tries, d.receipt_ts
      FROM events e LEFT JOIN deliveries d ON d.event_id = e.id
      WHERE e.id = ? AND e.delivery = 'now' AND e.kind IN (${KIND_LIST})`).get(id) as InRow | null
  }

  /** A meta value (JSON); null when it is missing or unreadable. */
  metaGet(k: string): unknown {
    try {
      const r = this.db.query('SELECT v FROM meta WHERE k = ?').get(k) as { v: string | null } | null
      return r?.v != null ? JSON.parse(r.v) : null
    } catch {
      return null
    }
  }

  metaSet(k: string, v: unknown): void {
    this.db.query('INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v').run(k, JSON.stringify(v))
  }

  /** brain-watch's redelivery request (meta bridge.redeliver: rid, ids, why, ts), checked; null when there is none. */
  redeliverRequest(): RedeliverRequest | null {
    const v = this.metaGet(REDELIVER_KEY) as Record<string, unknown> | null
    if (!v || typeof v !== 'object' || typeof v.rid !== 'string' || !v.rid || !Array.isArray(v.ids)) return null
    const ids = v.ids.filter((i): i is number => Number.isInteger(i) && (i as number) > 0).slice(0, 100)
    return { rid: v.rid.slice(0, 80), ids, why: typeof v.why === 'string' ? v.why.slice(0, 200) : '', ts: Number(v.ts) || 0 }
  }

  /** An event row of the bridge's own (hand-offs, undelivered notices). null: its idem_key already exists. */
  insertEvent(e: { kind: string; subject: string; payload: Record<string, unknown>; labels: string[]; delivery: string; idem_key: string; trace_id?: string | null }): number | null {
    const r = this.db.query(`INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, status)
      VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, 'new') ON CONFLICT(idem_key) DO NOTHING`).run(
      nowS(), BRIDGE_SOURCE, e.kind, e.subject, JSON.stringify(e.payload), JSON.stringify(e.labels), e.delivery, e.trace_id ?? null, e.idem_key)
    return r.changes ? Number(r.lastInsertRowid) : null
  }

  /** The newest channel event of `chat` notified since `sinceTs`: what a reply in that chat answers. */
  latestDelivered(chat: string, sinceTs: number): { id: number; trace_id: string | null } | null {
    return this.db.query(`SELECT e.id, e.trace_id FROM events e JOIN deliveries d ON d.event_id = e.id
      WHERE d.sent_ts >= ? AND e.kind IN (${KIND_LIST}) AND json_extract(e.payload, '$.chat_id') = ?
      ORDER BY e.id DESC LIMIT 1`).get(sinceTs, chat) as { id: number; trace_id: string | null } | null
  }

  enqueue(o: Enqueue): number {
    const r = this.db.query(`INSERT INTO outbox(ts, bot, chat, op, body, idem_key, urgent, not_before, mode, status, reply_to_event, files, trace_id)
      VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'live', 'queued', ?, ?, ?)`).run(
      nowS(), o.bot, o.chat, o.op, o.body, o.idem_key, o.urgent, o.reply_to_event, o.files, o.trace_id)
    return Number(r.lastInsertRowid)
  }

  outRow(id: number): OutRow | null {
    return this.db.query('SELECT id, status, tg_message_id, last_error, result, files FROM outbox WHERE id = ?').get(id) as OutRow | null
  }
}

/** What the session gets for a "/scan private" or "/sp" document: never the file, caption or title (0.1.22
 *  files those privately itself; until the adapter does, the owner is pointed to the vault bot). */
export const PRIVATE_SCAN_NOTE = '(A private scan ("/scan private" or "/sp"): not shown to the session, and not filed: this Telegram path has no private intake yet. Tell him to send it to the vault bot.)'
const PRIVATE_SCAN_META = ['chat_id', 'message_id', 'user', 'user_id', 'ts']

/** A row as the 0.1.22 channel notification: content = payload.text, meta = the other scalar payload
 *  fields as strings, plus event_id. A private scan keeps only who/where/when. */
export function toChannel(row: Pick<InRow, 'id' | 'payload'>): { content: string; meta: Record<string, string> } {
  let p: Record<string, unknown> = {}
  try { p = JSON.parse(row.payload) ?? {} } catch {}
  if (p.attachment_file_id != null && typeof p.text === 'string' && parseScanPrivate(p.text, typeof p.bot === 'string' ? p.bot : '')) {
    const meta: Record<string, string> = { event_type: 'message', handled: 'scan-private-refused' }
    for (const k of PRIVATE_SCAN_META) if (typeof p[k] === 'string') meta[k] = p[k] as string
    meta.event_id = String(row.id)
    return { content: PRIVATE_SCAN_NOTE, meta }
  }
  const content = typeof p.text === 'string' ? p.text : ''
  const meta: Record<string, string> = {}
  for (const [k, v] of Object.entries(p)) {
    if (NOT_META.has(k) || v == null) continue
    meta[k] = typeof v === 'string' ? v : typeof v === 'number' || typeof v === 'boolean' ? String(v) : JSON.stringify(v)
  }
  meta.event_id = String(row.id)
  return { content, meta }
}
