// Hub mode (v2 Phase 2 H6), the adapter's side of hubd (the tg caller). Contract: ~/jobs/v2-h6/contract.md.
//
//   Forwarder       the local SQLite journal stays the atomic store of every update (row + offset in one transaction,
//                   db.ts, unchanged) and the spool: committed rows that are not 'pending' go to POST /tg/rows in id
//                   order (idem_key makes a repeat a no-op), later payload changes (ack_ms, …) to /tg/rows/patch, the
//                   meta counters (dropped:*, offset:*, compat_reads:*, the rest as tg.<key>) to /tg/meta. Kicked
//                   after each commit, else every 250 ms; a hub that is down only delays rows (watermarks in meta:
//                   hub:fwd_id, hub:fwd_patch_ts), across restarts too.
//   hubQueue        the outbox consumer over hubd's H5 endpoints (queued, claim, set, fail, interrupted, dupes, get);
//                   sent rows are mirrored into the local outbox table (mode 'mirror') so reaction context and the
//                   "✓ choice" edit of rich cards keep finding their text.
//   BindingsMirror  GET /tg/bindings every 5 s → the local bindings table (every kind but 'card': card binds are the
//                   adapter's own and stay local).
//   PresenceMirror  GET /tg/presence long poll (the companion posts tg-status's board, the owner's zone and sleep-in):
//                   the board goes to a local file Typing watches; zone and sleep-in feed the outbox rules.
//   HubApprovals    P7 over hubd: get, attach, decide (his tap), card, expire. New nonces: POST /approvals (needs
//                   hubd to let the tg caller request; a brain-minted {nonce} needs only GET).
//   RemoteJobs      bind runs, transcripts and private scans go to the companion as /tg/jobs; a job no companion
//                   claims comes back 'unclaimed' and the caller falls back (bind → the brain; prep → *_error).
import type { Database } from 'bun:sqlite'
import { TOAST, type ApprovalRow, type Tap, type TapOutcome } from './approvals.ts'
import { nowS, type OutboxRow, type Store } from './db.ts'
import { HubError, newRid, oneLine, writeAtomic, type HubClient } from './hub.ts'
import type { OutboxQueue } from './outbox.ts'

const TRANSPORT = /^(tg|outbox|approval)\./
export const FWD_ID = 'hub:fwd_id'
export const FWD_PATCH_TS = 'hub:fwd_patch_ts'
export const FWD_OK = 'hub:fwd_ok_ts'
export const FWD_ERR = 'hub:fwd_error'
export const BATCH = 100
export const COMPANION_SEEN = 'hub:companion_seen_ts'
const META_KEYS = /^(dropped:|offset:|compat_reads:)/
const META_EXTRA = ['duplicates', 'last_poll_ts', 'approvals:expired cards', 'approvals:expiry edit failed']

type Log = (m: string) => void

/** The SQL the hub-mode journal adds (local only): the local → hub id map. */
export function hubJournalDdl(db: Database): void {
  db.exec('CREATE TABLE IF NOT EXISTS hubmap(local_id INTEGER PRIMARY KEY, hub_id INTEGER NOT NULL, ts REAL NOT NULL)')
}

export class Forwarder {
  private timer: ReturnType<typeof setInterval> | null = null
  private running: Promise<number> | null = null
  private again = false
  private metaAt = 0
  private metaSent = new Map<string, string>()
  private okAt = 0
  failures = 0

  constructor(private readonly d: { store: Store; hub: HubClient; log: Log; intervalMs?: number; metaEveryMs?: number }) {
    hubJournalDdl(d.store.db)
  }

  start(): void {
    if (this.timer) return
    this.timer = setInterval(() => { void this.pass() }, this.d.intervalMs ?? 250)
    void this.pass()
  }

  async stop(): Promise<void> {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
    // A last try, bounded: what is committed should leave before the process does, but a hub that hangs must not hold
    // the shutdown (the rows stay in the journal and go at the next start).
    const last = (async () => { if (this.running) await this.running.catch(() => 0); await this.pass().catch(() => 0) })()
    await Promise.race([last, Bun.sleep(3_000)])
  }

  /** A row was committed: forward it now (single flight; a kick during a pass runs one more). */
  kick(): void {
    if (this.running) { this.again = true; return }
    void this.pass()
  }

  /** One pass (rows, patches, meta). Returns the rows forwarded; never throws. */
  pass(): Promise<number> {
    if (this.running) { this.again = true; return this.running }
    this.running = (async () => {
      let n = 0
      do {
        this.again = false
        try {
          n += await this.rows()
          await this.patches()
          await this.meta()
          if (Date.now() - this.okAt >= 5_000) { this.d.store.setMeta(FWD_OK, String(nowS())); this.okAt = Date.now() }
          if (this.failures) this.d.log(`hub forwarder: back after ${this.failures} failed pass(es)`)
          this.failures = 0
        } catch (err) {
          this.failures++
          const why = oneLine(err)
          try { this.d.store.setMeta(FWD_ERR, JSON.stringify({ ts: nowS(), error: why })) } catch {}
          if (this.failures === 1 || this.failures % 40 === 0) this.d.log(`hub forwarder: ${why} (rows stay in the journal and go when the hub answers)`)
          break
        }
      } while (this.again)
      return n
    })().finally(() => { this.running = null })
    return this.running
  }

  /** Rows the hub doesn't have yet (status not pending), oldest first. */
  backlog(): number {
    const after = Number(this.d.store.getMeta(FWD_ID) ?? 0) || 0
    return (this.d.store.db.query("SELECT COUNT(*) AS n FROM events WHERE id > ? AND status != 'pending'").get(after) as { n: number }).n
  }

  private async rows(): Promise<number> {
    const db = this.d.store.db
    let total = 0
    for (;;) {
      const after = Number(this.d.store.getMeta(FWD_ID) ?? 0) || 0
      const got = db.query(`SELECT e.*, d.how AS d_how FROM events e LEFT JOIN deliveries d ON d.event_id = e.id
        WHERE e.id > ? ORDER BY e.id LIMIT ?`).all(after, BATCH) as Record<string, any>[]
      if (!got.length) return total
      // A pending row never shows under its id: its release re-inserts it under a fresh one (db.ts releaseRow).
      const send = got.filter(r => r.status !== 'pending' && TRANSPORT.test(String(r.kind)) && r.idem_key)
      const last = got[got.length - 1]!.id as number
      if (send.length) {
        const res = await this.d.hub.call<{ ids: number[] }>('POST', '/tg/rows', {
          rows: send.map(r => ({
            local_id: r.id, ts: r.ts, source: r.source, kind: r.kind, subject: r.subject, next: r.next, payload: r.payload,
            labels: r.labels, delivery: r.delivery, trace_id: r.trace_id, idem_key: r.idem_key, status: r.status,
            acked_ts: r.acked_ts, ack_by: r.ack_by, binding_receipt: r.d_how === 'binding',
          })),
        })
        const ids = Array.isArray(res?.ids) ? res.ids : []
        if (ids.length !== send.length) throw new HubError(`hub /tg/rows answered ${ids.length} ids for ${send.length} rows`, 200, 'bad_answer', true)
        const map = db.query('INSERT OR REPLACE INTO hubmap(local_id, hub_id, ts) VALUES (?, ?, ?)')
        db.transaction(() => { send.forEach((r, i) => map.run(r.id, Number(ids[i]), nowS())) })()
      }
      const skipped = got.filter(r => r.status !== 'pending' && !send.includes(r))
      for (const r of skipped) this.d.log(`hub forwarder: row ${r.id} (${r.kind}) is no transport row: kept local`)
      this.d.store.setMeta(FWD_ID, String(last))
      total += send.length
      if (got.length < BATCH) return total
    }
  }

  private async patches(): Promise<void> {
    const db = this.d.store.db
    const upto = Number(this.d.store.getMeta(FWD_ID) ?? 0) || 0
    const since = Number(this.d.store.getMeta(FWD_PATCH_TS) ?? 0) || 0
    const got = db.query(`SELECT id, idem_key, payload, status, updated_ts FROM events
      WHERE id <= ? AND updated_ts IS NOT NULL AND updated_ts > ? AND status != 'pending' AND idem_key IS NOT NULL ORDER BY updated_ts LIMIT ?`)
      .all(upto, since, BATCH) as { id: number; idem_key: string; payload: string; status: string; updated_ts: number }[]
    for (const r of got) {
      if (!db.query('SELECT 1 FROM hubmap WHERE local_id = ?').get(r.id)) { this.d.store.setMeta(FWD_PATCH_TS, String(r.updated_ts)); continue }
      let payload: Record<string, unknown> = {}
      try { payload = JSON.parse(r.payload) } catch {}
      await this.d.hub.call('POST', '/tg/rows/patch', { idem_key: r.idem_key, payload_patch: payload })
      this.d.store.setMeta(FWD_PATCH_TS, String(r.updated_ts))
    }
  }

  /** Send the changed meta counters now (status, tests); pass() sends them every metaEveryMs. */
  flushMeta(): Promise<void> { return this.meta(true) }

  private async meta(force = false): Promise<void> {
    const every = this.d.metaEveryMs ?? 5_000
    if (!force && Date.now() - this.metaAt < every) return
    const all = this.d.store.db.query('SELECT k, v FROM meta').all() as { k: string; v: string }[]
    const set: Record<string, string> = {}
    for (const { k, v } of all) {
      const key = META_KEYS.test(k) ? k : META_EXTRA.some(x => x.endsWith(':') ? k.startsWith(x) : k === x) ? `tg.${k}` : null
      if (key && this.metaSent.get(key) !== v) set[key] = v
    }
    if (Object.keys(set).length) {
      await this.d.hub.call('POST', '/tg/meta', { set })
      for (const [k, v] of Object.entries(set)) this.metaSent.set(k, v)
    }
    this.metaAt = Date.now()
  }
}

// --- outbox -------------------------------------------------------------------------------------------------------

const HUB_SET_OK = new Set(['status', 'attempts', 'not_before', 'last_error', 'sent_ts', 'tg_message_id', 'result', 'files'])

/** hubd's outbox as the consumer's queue (rows by id are kept from queued() for the local mirror). */
export function hubQueue(hub: HubClient, store: Store, log: Log): OutboxQueue & { seen: Map<number, OutboxRow> } {
  const seen = new Map<number, OutboxRow>()
  const norm = (r: Record<string, any>): OutboxRow => ({
    id: Number(r.id), ts: Number(r.ts), bot: String(r.bot), chat: String(r.chat), op: String(r.op), body: String(r.body),
    idem_key: r.idem_key ?? null, urgent: Number(r.urgent ?? 0), not_before: r.not_before == null ? null : Number(r.not_before),
    mode: String(r.mode ?? 'live'), status: String(r.status), attempts: Number(r.attempts ?? 0), last_error: r.last_error ?? null,
    sent_ts: r.sent_ts == null ? null : Number(r.sent_ts), tg_message_id: r.tg_message_id == null ? null : Number(r.tg_message_id),
    trace_id: r.trace_id ?? null, reply_to_event: r.reply_to_event == null ? null : Number(r.reply_to_event), files: r.files ?? null,
    result: r.result ?? null, // a retrying row's window (outbox.ts retryPlan)
  } as OutboxRow)
  const mirror = (row: OutboxRow, fields: Record<string, unknown>) => {
    try {
      store.db.query(`INSERT OR REPLACE INTO outbox(id, ts, bot, chat, op, body, idem_key, urgent, not_before, mode, status, attempts,
        last_error, sent_ts, tg_message_id, trace_id, reply_to_event, files) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, 'mirror', 'sent', ?, ?, ?, ?, ?, ?, ?)`)
        .run(row.id, row.ts, row.bot, row.chat, row.op, row.body, row.urgent, Number(fields.attempts ?? row.attempts), (fields.last_error as string) ?? null,
          Number(fields.sent_ts ?? nowS()), (fields.tg_message_id as number) ?? null, row.trace_id, row.reply_to_event, row.files)
      store.db.query("DELETE FROM outbox WHERE mode = 'mirror' AND ts < ?").run(nowS() - 7 * 86_400)
    } catch (err) { log(`outbox mirror: ${oneLine(err)}`) }
  }
  return {
    seen,
    async queued(bots) {
      const r = await hub.call<{ rows: Record<string, any>[] }>('POST', '/outbox/queued', { bots })
      const rows = (r?.rows ?? []).map(norm)
      seen.clear()
      for (const x of rows) seen.set(x.id, x)
      return rows
    },
    async claim(id, attempts) {
      return !!(await hub.call<{ claimed: boolean }>('POST', `/outbox/${id}/claim`, { attempts }))?.claimed
    },
    async set(id, fields, row) {
      const f: Record<string, unknown> = {}
      for (const [k, v] of Object.entries(fields)) if (HUB_SET_OK.has(k)) f[k] = v
      if (!Object.keys(f).length) return
      await hub.call('POST', `/outbox/${id}/set`, { fields: f }, { retries: 4 })
      const base = row ?? seen.get(id)
      if (f.status === 'sent' && base) mirror(base, f)
    },
    async fail(row, fields, partial = false) {
      const f: Record<string, unknown> = {}
      for (const [k, v] of Object.entries(fields)) if (HUB_SET_OK.has(k)) f[k] = v
      await hub.call('POST', `/outbox/${row.id}/fail`, { fields: f, partial }, { retries: 4 })
    },
    async status(id) {
      try { return String((await hub.call<Record<string, any>>('GET', `/outbox/${id}`))?.status ?? '') || null } catch (err) {
        if (err instanceof HubError && err.status === 404) return null
        throw err
      }
    },
    async earlier(row, since) {
      const r = await hub.call<{ rows: { id: number; body: string }[] }>('POST', '/outbox/dupes', { bot: row.bot, chat: row.chat, op: row.op, id: row.id, since })
      return (r?.rows ?? []).map(x => ({ id: Number(x.id), body: String(x.body) }))
    },
    async interrupted(bots) {
      return Number((await hub.call<{ failed: number }>('POST', '/outbox/interrupted', { bots }, { retries: 4 }))?.failed ?? 0)
    },
  }
}

// --- bindings and presence mirrors --------------------------------------------------------------------------------

const BIND_COLS = ['kind', 'key', 'tool', 'args', 'note', 'on_error', 'why', 'created', 'expires', 'active'] as const

export class BindingsMirror {
  version: string | null = null
  syncedAt = 0
  private timer: ReturnType<typeof setInterval> | null = null
  private busy = false

  constructor(private readonly d: { store: Store; hub: HubClient; log: Log; everyMs?: number }) {}

  start(): void {
    if (this.timer) return
    this.timer = setInterval(() => { void this.sync() }, this.d.everyMs ?? 5_000)
    void this.sync()
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
  }

  /** One pull: replace every non-card row when the hub's version moved. Never throws; true = in sync now. */
  async sync(): Promise<boolean> {
    if (this.busy) return false
    this.busy = true
    try {
      const r = await this.d.hub.call<{ version: unknown; rows: Record<string, any>[] }>('GET', '/tg/bindings', undefined, { retries: 0 })
      const v = String(r?.version ?? '')
      if (v && v === this.version) { this.syncedAt = Date.now(); return true }
      const rows = (r?.rows ?? []).filter(x => x && x.kind !== 'card')
      const db = this.d.store.db
      db.transaction(() => {
        db.query("DELETE FROM bindings WHERE kind != 'card'").run()
        const ins = db.query(`INSERT INTO bindings(${BIND_COLS.join(', ')}) VALUES (${BIND_COLS.map(() => '?').join(', ')})
          ON CONFLICT(kind, key) DO NOTHING`)
        for (const x of rows) {
          ins.run(String(x.kind), String(x.key), String(x.tool), typeof x.args === 'string' ? x.args : JSON.stringify(x.args ?? []),
            Number(x.note ?? 0), String(x.on_error ?? 'reply'), String(x.why ?? ''), Number(x.created ?? nowS()),
            x.expires == null ? null : Number(x.expires), Number(x.active ?? 1))
        }
      })()
      this.version = v
      this.syncedAt = Date.now()
      return true
    } catch (err) {
      this.d.log(`bindings mirror: ${oneLine(err)} (the last copy stays in use)`)
      return false
    } finally { this.busy = false }
  }
}

export type Presence = { board?: unknown; zone?: string; sleeping?: boolean; ts?: number }

export class PresenceMirror {
  version = 0
  state: Presence | null = null
  receivedAt = 0
  private stopped = false
  private loop: Promise<void> | null = null

  /** seen: a fresh state arrived (the companion posts on change and every 30 s): the status's "companion seen". */
  constructor(private readonly d: { hub: HubClient; boardFile: string; log: Log; waitS?: number; seen?: () => void }) {}

  start(): void {
    if (this.loop) return
    this.stopped = false
    this.loop = this.run()
  }

  async stop(): Promise<void> {
    this.stopped = true
    // the long poll ends by itself (≤ waitS); don't hold the shutdown for it
  }

  /** One fetch (tests); the loop calls it with a wait. */
  async once(waitS = 0): Promise<boolean> {
    const r = await this.d.hub.call<{ version: number; state: Presence | null }>('GET', `/tg/presence?after=${this.version}&wait=${waitS}`, undefined,
      { timeoutMs: (waitS + 10) * 1000, retries: 0 })
    const v = Number(r?.version ?? 0)
    if (!(v > this.version) || !r?.state) return false
    this.version = v
    this.state = r.state
    this.receivedAt = Date.now()
    try { this.d.seen?.() } catch {}
    if (r.state.board !== undefined) {
      try { writeAtomic(this.d.boardFile, JSON.stringify(r.state.board)) } catch (err) { this.d.log(`presence mirror: ${oneLine(err)}`) }
    }
    return true
  }

  /** Fresh within 2 min (the companion re-posts every 30 s), else unknown. */
  fresh(): Presence | null {
    return this.state && Date.now() - this.receivedAt < 120_000 ? this.state : null
  }

  private async run(): Promise<void> {
    let fails = 0
    while (!this.stopped) {
      try { await this.once(this.d.waitS ?? 25); fails = 0 } catch (err) {
        if (++fails === 1 || fails % 30 === 0) this.d.log(`presence mirror: ${oneLine(err)}`)
        await Bun.sleep(Math.min(1000 * fails, 10_000))
      }
    }
  }
}

// --- approvals ----------------------------------------------------------------------------------------------------

export type Due = { nonce: string; chat: string; message_id: number }

/** The adapter's P7 operations over hubd (decide and card are the tg caller's; hub states as hubapprovals._state).
 *  No create: the hub's tg never mints approvals (POST /approvals is the brain's; Cedar Binding: decide only). */
export class HubApprovals {
  constructor(private readonly hub: HubClient, private readonly clock: () => number) {}

  now(): number { return this.clock() }

  async get(nonce: string): Promise<(ApprovalRow & { state?: string }) | null> {
    if (!/^[0-9a-f]{32}$/.test(nonce)) return null
    try { return await this.hub.call('GET', `/approvals/${nonce}`) } catch (err) {
      if (err instanceof HubError && err.status === 404) return null
      throw err
    }
  }

  async attach(nonce: string, chat: string, message_id: number): Promise<boolean> {
    return !!(await this.hub.call<{ ok: boolean }>('POST', `/approvals/${nonce}/attach`, { chat, message_id }))?.ok
  }

  /** His tap (owner, chat and choice checks as Approvals.tap; the decision itself is hubd's, conditional there). */
  async tap(nonce: string, choice: 'y' | 'n' | 'x', who: { userId: string; chatId: string; owner: string }): Promise<Tap> {
    const out = (outcome: TapOutcome, row: ApprovalRow | null, repeat = false): Tap =>
      ({ nonce, choice, outcome, repeat, row, toast: repeat ? `✓ Already ${outcome}` : TOAST[outcome] })
    if (!who.owner || who.userId !== who.owner || who.chatId !== who.owner) return out('not owner', null)
    let r = await this.get(nonce)
    if (!r) return out('unknown', null)
    if (r.chat && r.chat !== who.chatId) return out('not owner', r)
    if (r.status === 'pending' && choice !== 'x') {
      const d = await this.hub.call<{ ok: boolean; state: string }>('POST', `/approvals/${nonce}/decide`, { decision: choice, by: who.userId, via: 'adapter' })
      r = (await this.get(nonce)) ?? r
      if (d?.ok) return out(choice === 'y' ? 'approved' : 'declined', r)
    }
    const st = r.state ?? r.status
    if (r.status === 'approved') return out('approved', r, true)
    if (r.status === 'denied') return out('declined', r, true)
    if (r.status === 'cancelled') return out('cancelled', r)
    if (st === 'used') return out('approved', r, true)
    return out('expired', r)
  }

  /** Pending past expiry → expired (hubd, one statement); the cards to edit. */
  async expireDue(): Promise<Due[]> {
    const r = await this.hub.call<{ expired: number; cards: Record<string, any>[] }>('POST', '/approvals/expire', {})
    return (r?.cards ?? []).filter(c => c.chat && c.message_id).map(c => ({ nonce: String(c.nonce), chat: String(c.chat), message_id: Number(c.message_id) }))
  }

  async markCard(nonce: string, state: 'edited' | 'failed'): Promise<void> {
    await this.hub.call('POST', `/approvals/${nonce}/card`, { card: state })
  }
}

// --- jobs ---------------------------------------------------------------------------------------------------------

export type JobKind = 'bind' | 'transcribe' | 'scan'
export type JobState = 'queued' | 'running' | 'done' | 'failed' | 'unclaimed' | 'expired'
export type JobOutcome = { state: JobState; result?: Record<string, unknown>; error?: string }
/** What bind/transcribe/scan jobs answer (the companion runs runLocal). */
export type RunResult = { ok: boolean; stdout: string; error?: string; unclaimed?: boolean }

export class RemoteJobs {
  unclaimedAt = 0
  doneAt = 0

  constructor(private readonly hub: HubClient, private readonly log: Log) {}

  /** Submit and wait (≤ timeoutMs + 15 s). Never throws: hub trouble becomes state 'expired' with the reason. */
  async run(kind: JobKind, args: Record<string, unknown>, timeoutMs: number): Promise<JobOutcome> {
    const deadline = Date.now() + timeoutMs + 15_000
    let id: string | number
    try {
      const r = await this.hub.call<{ id: string | number; state: JobState }>('POST', '/tg/jobs', { kind, args, timeout_s: Math.max(1, Math.ceil(timeoutMs / 1000)) })
      id = r.id
      if (r.state === 'unclaimed') { this.unclaimedAt = Date.now(); return { state: 'unclaimed', error: 'the brain-side companion is offline' } }
    } catch (err) {
      return { state: 'expired', error: `the hub took no job (${oneLine(err)})` }
    }
    let lastErr = ''
    while (Date.now() < deadline) {
      const wait = Math.max(1, Math.min(25, Math.floor((deadline - Date.now()) / 1000)))
      try {
        const r = await this.hub.call<{ state: JobState; result?: Record<string, unknown>; error?: string }>('POST', `/tg/jobs/${id}/wait`, { wait },
          { timeoutMs: (wait + 10) * 1000, retries: 0 })
        if (r.state === 'unclaimed') { this.unclaimedAt = Date.now(); return { state: 'unclaimed', error: r.error || 'the brain-side companion is offline' } }
        if (r.state === 'done' || r.state === 'failed' || r.state === 'expired') { this.doneAt = Date.now(); return { state: r.state, result: r.result, error: r.error } }
      } catch (err) {
        lastErr = oneLine(err)
        await Bun.sleep(500)
      }
    }
    return { state: 'expired', error: `no answer from the companion in time${lastErr ? ` (${lastErr})` : ''}` }
  }

  /** A job as runLocal's result: done → its {ok, stdout, error}; anything else → a failure (unclaimed flagged). */
  async runLocalLike(kind: JobKind, args: Record<string, unknown>, timeoutMs: number): Promise<RunResult> {
    const o = await this.run(kind, args, timeoutMs)
    if (o.state === 'done' && o.result) {
      return { ok: o.result.ok === true, stdout: typeof o.result.stdout === 'string' ? o.result.stdout : '', ...(typeof o.result.error === 'string' ? { error: o.result.error } : {}) }
    }
    return { ok: false, stdout: '', error: o.error ?? `job ${o.state}`, ...(o.state === 'unclaimed' ? { unclaimed: true } : {}) }
  }
}

export { newRid }
