// The adapter's SQLite store: the shared events DDL (schema.sql, plan §3) in its own test.db.
// Pragmas per the contract: WAL, synchronous=FULL, busy_timeout=5000; files 0600 (umask 077).
// Inbound rows and the getUpdates offset are written in ONE transaction, so a crash either keeps
// both or neither: no loss (the update comes again) and no duplicate (idem_key is UNIQUE).
import { Database } from 'bun:sqlite'
import { chmodSync, existsSync, mkdirSync } from 'fs'
import { dirname, join } from 'path'

// The DDL files are text imports: inlined by `bun build` (the hub's single-file tg/adapter.js), read from disk when
// the source runs as is.
import schemaSql from './schema.sql' with { type: 'text' }
import schemaV2Sql from './schema_v2.sql' with { type: 'text' }

export const SCHEMA_SQL: string = schemaSql
export const SCHEMA_V2_SQL: string = schemaV2Sql

/** DDL v2 on top of v1: CREATE … IF NOT EXISTS as is, each ALTER … ADD COLUMN only when the column is missing. */
export function migrateV2(db: Database): void {
  const stmts = SCHEMA_V2_SQL.split('\n').filter(l => !l.trim().startsWith('--')).map(l => l.replace(/\s+--.*$/, '')).join('\n')
    .split(';').map(s => s.trim()).filter(Boolean)
  db.transaction(() => {
    for (const st of stmts) {
      const m = /^ALTER TABLE (\w+) ADD COLUMN (\w+)/i.exec(st)
      if (m) {
        const cols = (db.query(`PRAGMA table_info(${m[1]})`).all() as { name: string }[]).map(c => c.name)
        if (cols.includes(m[2]!)) continue
      }
      db.exec(st)
    }
    db.query("INSERT INTO meta(k, v) VALUES ('schema', '2') ON CONFLICT(k) DO UPDATE SET v = '2' WHERE CAST(v AS INTEGER) < 2").run()
  }).immediate() // the write lock before reading table_info: core/db.py and the bridge add the same columns
}

export type EventRow = {
  ts: number
  source: string
  kind: string
  subject: string
  next: string
  payload: Record<string, unknown>
  labels: string[]
  delivery: string
  trace_id: string | null
  idem_key: string
}

export type OutboxRow = {
  id: number
  ts: number
  bot: string
  chat: string
  op: string
  body: string
  idem_key: string | null
  urgent: number
  not_before: number | null
  mode: string
  status: string
  attempts: number
  last_error: string | null
  sent_ts: number | null
  tg_message_id: number | null
  trace_id: string | null
  reply_to_event: number | null
  files: string | null
  result: string | null
}

export type BindingRow = {
  id: number; kind: string; key: string; tool: string; args: string; note: number; on_error: string; why: string; expires: number | null
}

export const nowS = () => Date.now() / 1000

export function openDb(path: string): Database {
  const old = process.umask(0o077)
  try {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
    const db = new Database(path, { create: true })
    db.exec('PRAGMA journal_mode=WAL')
    db.exec('PRAGMA synchronous=FULL')
    db.exec('PRAGMA busy_timeout=5000')
    db.exec(SCHEMA_SQL)
    migrateV2(db)
    for (const f of [path, `${path}-wal`, `${path}-shm`]) {
      try { if (existsSync(f)) chmodSync(f, 0o600) } catch {}
    }
    return db
  } finally {
    process.umask(old)
  }
}

/** Delivery prefix of a pending row: no reader that selects delivery 'now' (the bridge) may see it early. */
export const HELD_DELIVERY = 'held:'

export class Store {
  constructor(readonly db: Database) {}

  getMeta(k: string): string | null {
    const r = this.db.query('SELECT v FROM meta WHERE k = ?').get(k) as { v: string } | null
    return r?.v ?? null
  }

  setMeta(k: string, v: string): void {
    this.db.query('INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v').run(k, v)
  }

  bumpMeta(k: string, by = 1): void {
    this.db.query("INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = CAST(CAST(v AS INTEGER) + ? AS TEXT)").run(k, String(by), by)
  }

  /** One processed update: its row (if any) and the next offset, atomically. Returns the row id,
   *  or null when there was no row or the idem_key already had one. */
  commitUpdate(row: EventRow | null, offsetKey: string, nextOffset: number, status: 'new' | 'pending' = 'new', bound = false): number | null {
    let id: number | null = null
    this.db.transaction(() => {
      if (row) {
        // A bound row (P3: a binding answers it) is received at once: acked by 'binding' with a deliveries receipt
        // (how 'binding'), in the same transaction, so the bridge (receipt set) and the agenda never pick it up.
        const st = bound ? 'acked' : status
        const r = this.db.query(`INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, status, acked_ts, ack_by)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(idem_key) DO NOTHING`).run(
          row.ts, row.source, row.kind, row.subject, row.next, JSON.stringify(row.payload), JSON.stringify(row.labels),
          st === 'pending' ? HELD_DELIVERY + row.delivery : row.delivery, row.trace_id, row.idem_key, st,
          bound ? nowS() : null, bound ? 'binding' : null)
        id = r.changes ? Number(r.lastInsertRowid) : null
        if (id != null && bound) {
          this.db.query(`INSERT INTO deliveries(event_id, sent_ts, tries, receipt_ts, how) VALUES (?, ?, 0, ?, 'binding')
            ON CONFLICT(event_id) DO UPDATE SET receipt_ts = excluded.receipt_ts, how = 'binding'`).run(id, nowS(), nowS())
        }
      }
      this.setMeta(offsetKey, String(nextOffset))
    })()
    return id
  }

  /** A binding's outcome row (tg.bound). Delivered (delivery 'now', status new) when the brain should hear of it;
   *  otherwise a 'digest' row acked by 'binding' at once. null: its idem_key exists already. */
  insertBound(row: EventRow, deliver: boolean): number | null {
    const r = this.db.query(`INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, status, acked_ts, ack_by)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(idem_key) DO NOTHING`).run(
      row.ts, row.source, row.kind, row.subject, row.next, JSON.stringify(row.payload), JSON.stringify(row.labels),
      deliver ? 'now' : 'digest', row.trace_id, row.idem_key, deliver ? 'new' : 'acked', deliver ? null : nowS(), deliver ? null : 'binding')
    return r.changes ? Number(r.lastInsertRowid) : null
  }

  /** The active, unexpired bindings row of this kind and key (P3: tg-bind rows and per-card binds). */
  binding(kind: string, key: string, now = nowS()): BindingRow | null {
    return this.db.query(`SELECT id, kind, key, tool, args, note, on_error, why, expires FROM bindings
      WHERE kind = ? AND key = ? AND active = 1 AND (expires IS NULL OR expires > ?)`).get(kind, key, now) as BindingRow | null
  }

  /** The longest active 'prefix' row that `data` starts with. */
  prefixBinding(data: string, now = nowS()): BindingRow | null {
    return this.db.query(`SELECT id, kind, key, tool, args, note, on_error, why, expires FROM bindings
      WHERE kind = 'prefix' AND active = 1 AND (expires IS NULL OR expires > ?) AND length(key) >= 2
        AND substr(?, 1, length(key)) = key ORDER BY length(key) DESC LIMIT 1`).get(now, data) as BindingRow | null
  }

  /** Active rows of these kinds (the CLI's table). */
  bindings(kinds: string[], now = nowS()): BindingRow[] {
    return this.db.query(`SELECT id, kind, key, tool, args, note, on_error, why, expires FROM bindings
      WHERE active = 1 AND (expires IS NULL OR expires > ?) AND kind IN (${kinds.map(() => '?').join(', ')}) ORDER BY kind, key`)
      .all(now, ...kinds) as BindingRow[]
  }

  /** Active command rows in row order (P9: the /commands menu is built from them). */
  commandRows(now = nowS()): BindingRow[] {
    return this.db.query(`SELECT id, kind, key, tool, args, note, on_error, why, expires FROM bindings
      WHERE kind = 'command' AND active = 1 AND (expires IS NULL OR expires > ?) ORDER BY id`).all(now) as BindingRow[]
  }

  /** A card's button binds (buttons[].bind): the message's earlier binds are replaced; rows expired over a day ago go. */
  saveCardBinds(prefix: string, binds: { key: string; tool: string; args: string[]; note: boolean }[], expires: number, now = nowS()): void {
    this.db.transaction(() => {
      this.db.query("DELETE FROM bindings WHERE kind = 'card' AND (substr(key, 1, length(?)) = ? OR expires < ?)").run(prefix, prefix, now - 86_400)
      for (const b of binds) {
        this.db.query(`INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active)
          VALUES ('card', ?, ?, ?, ?, 'reply', 'card button', ?, ?, 1)
          ON CONFLICT(kind, key) DO UPDATE SET tool = excluded.tool, args = excluded.args, note = excluded.note,
            created = excluded.created, expires = excluded.expires, active = 1`).run(b.key, b.tool, JSON.stringify(b.args), b.note ? 1 : 0, now, expires)
      }
    })()
  }

  /** A pending row (inbound prep: photo download, transcript, private scan) becomes visible with its payload
   *  patch (and subject), status 'new' and its real delivery, as a NEW row (fresh id, same idem_key, ts and
   *  trace_id) in one transaction: the bridge reads channel rows by id after its cursor, so a row released in
   *  place below the cursor would never show. Returns the new id, or null when the row was not pending. */
  releaseRow(id: number, patch: Record<string, unknown>, subject?: string): number | null {
    let out: number | null = null
    this.db.transaction(() => {
      const r = this.db.query("SELECT * FROM events WHERE id = ? AND status = 'pending'").get(id) as Record<string, any> | null
      if (!r) return
      const p = { ...JSON.parse(r.payload), ...patch }
      const delivery = String(r.delivery).startsWith(HELD_DELIVERY) ? String(r.delivery).slice(HELD_DELIVERY.length) : r.delivery
      // Free the idem_key first, insert (the old row still holds the highest id: no rowid reuse), then drop it.
      this.db.query('UPDATE events SET idem_key = NULL WHERE id = ?').run(id)
      const ins = this.db.query(`INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, note_path, status, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)`).run(
        r.ts, r.source, r.kind, subject ?? r.subject, r.next, JSON.stringify(p), r.labels, delivery, r.trace_id, r.idem_key, r.note_path, nowS())
      this.db.query('DELETE FROM events WHERE id = ?').run(id)
      out = Number(ins.lastInsertRowid)
    })()
    return out
  }

  /** This bot's rows left 'pending' by an earlier process (crash, restart), oldest first. */
  pendingRows(botUsername: string): { id: number; kind: string; payload: Record<string, unknown> }[] {
    return (this.db.query(`SELECT id, kind, payload FROM events WHERE status = 'pending' AND source = 'tg-adapter'
      AND json_extract(payload, '$.bot') = ? ORDER BY id`).all(botUsername) as { id: number; kind: string; payload: string }[])
      .map(r => ({ id: r.id, kind: r.kind, payload: JSON.parse(r.payload) }))
  }

  /** Merge fields into an event's payload (e.g. the callback's ack latency). */
  patchPayload(idem_key: string, patch: Record<string, unknown>): void {
    const r = this.db.query('SELECT payload FROM events WHERE idem_key = ?').get(idem_key) as { payload: string } | null
    if (!r) return
    const p = { ...JSON.parse(r.payload), ...patch }
    this.db.query('UPDATE events SET payload = ?, updated_ts = ? WHERE idem_key = ?').run(JSON.stringify(p), nowS(), idem_key)
  }

  enqueue(o: { bot: string; chat: string; op: string; body: Record<string, unknown>; idem_key?: string | null; not_before?: number | null;
    mode?: string; urgent?: boolean; trace_id?: string | null; reply_to_event?: number | null; files?: string[] | null }): number | null {
    const r = this.db.query(`INSERT INTO outbox(ts, bot, chat, op, body, idem_key, urgent, not_before, mode, trace_id, reply_to_event, files)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(idem_key) DO NOTHING`).run(
      nowS(), o.bot, o.chat, o.op, JSON.stringify(o.body), o.idem_key ?? null, o.urgent ? 1 : 0, o.not_before ?? null,
      o.mode ?? 'live', o.trace_id ?? null, o.reply_to_event ?? null, o.files?.length ? JSON.stringify(o.files) : null)
    return r.changes ? Number(r.lastInsertRowid) : null
  }

  queued(bots: string | string[]): OutboxRow[] {
    const keys = Array.isArray(bots) ? bots : [bots]
    return this.db.query(`SELECT * FROM outbox WHERE status = 'queued' AND bot IN (${keys.map(() => '?').join(', ')}) AND mode = 'live' ORDER BY id`)
      .all(...keys) as OutboxRow[]
  }

  /** queued -> sending (attempts set) only while the row is still queued; false = someone else changed it first
   *  (a writer that withdrew it: core.tg sets queued rows it gave up on to skipped with the same condition). */
  claimOutbox(id: number, attempts: number): boolean {
    return this.db.query("UPDATE outbox SET status = 'sending', attempts = ? WHERE id = ? AND status = 'queued'").run(attempts, id).changes === 1
  }

  setOutbox(id: number, fields: Partial<Omit<OutboxRow, 'id'>>): void {
    const keys = Object.keys(fields)
    if (!keys.length) return
    this.db.query(`UPDATE outbox SET ${keys.map(k => `${k} = ?`).join(', ')} WHERE id = ?`)
      .run(...keys.map(k => (fields as any)[k] ?? null), id)
  }

  /** A row ends 'failed': the update and its outbox.failed event row in one transaction. The event
   *  carries ids, op, attempts and the (token-free) error only, never the message text; urgent rows'
   *  failures are delivery 'now', others 'beat'. Its idem_key makes a repeat a no-op. */
  failOutbox(row: OutboxRow, fields: Partial<Omit<OutboxRow, 'id'>> & { last_error: string }, partial = false): void {
    this.db.transaction(() => {
      this.setOutbox(row.id, { ...fields, status: 'failed' })
      const payload = {
        outbox_id: row.id, bot: row.bot, chat: row.chat, op: row.op, urgent: row.urgent ? 1 : 0, mode: row.mode,
        attempts: fields.attempts ?? row.attempts, error: fields.last_error, partial, trace_id: row.trace_id,
      }
      this.db.query(`INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, status)
        VALUES (?, 'tg-adapter', 'outbox.failed', ?, ?, ?, ?, ?, ?, ?, 'new') ON CONFLICT(idem_key) DO NOTHING`).run(
        nowS(), `Telegram ${row.op} failed (outbox #${row.id}, chat ${row.chat})`, 'tg-adapter status · enqueue again to retry',
        JSON.stringify(payload), JSON.stringify(['tg', row.mode, 'outbox']), row.urgent ? 'now' : 'beat', row.trace_id,
        `outbox.failed:${row.bot}:${row.id}`)
    })()
  }

  /** Text of a message the adapter sent (outbox) or received (events), for reaction context. */
  messageText(chat: string, message_id: number): { by: 'bot' | 'user'; text: string; user_id?: string; user?: string } | null {
    const o = this.db.query("SELECT body FROM outbox WHERE chat = ? AND tg_message_id = ? AND status = 'sent' ORDER BY id DESC LIMIT 1")
      .get(chat, message_id) as { body: string } | null
    if (o) {
      try {
        const b = JSON.parse(o.body)
        return { by: 'bot', text: String(b.text ?? (b.question ? `[poll] ${b.question}` : '')) }
      } catch {}
    }
    const e = this.db.query(`SELECT payload FROM events WHERE kind = 'tg.message' AND json_extract(payload, '$.chat_id') = ?
      AND json_extract(payload, '$.message_id') = ? ORDER BY id DESC LIMIT 1`).get(chat, String(message_id)) as { payload: string } | null
    if (e) {
      const p = JSON.parse(e.payload)
      return { by: 'user', text: String(p.text ?? ''), user_id: p.user_id, user: p.user }
    }
    return null
  }
}
