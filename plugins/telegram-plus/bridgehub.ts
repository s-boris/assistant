// Hub mode (v2 Phase 2 H6): the bridge's store over the hub API, the same methods as BridgeDb (bridgedb.ts).
//
// TELEGRAM_BRIDGE_STORE=hub: tg-adapter runs in the hub (claude-hub-tg) and owns the bot; its rows and the outbox live
// in the hub's Postgres. This module is the bridge's only network client and talks to one place, the hub URL
// (HUB_URL, default http://claude-hub:8700), with the brain's hub token (HUB_TOKEN_FILE, default
// ~/.secrets/hub-token: read once on first use, never logged or sent elsewhere). It never talks to Telegram.
//
//   inbound rows   POST /tg/inbound/poll {after, limit, wait} (long poll) · POST /tg/inbound/unreceipted
//                  GET /tg/inbound/<id> · GET /tg/inbound/max · GET /tg/latest-delivered?chat=&since_ts=
//   deliveries     POST /tg/deliveries/sent {event_id, ts, rid} · POST /tg/deliveries/receipts {ids}
//                  GET /tg/deliveries/seen?since_ts=
//   own rows       POST /tg/bridge-event {kind, subject, payload, labels, delivery, idem_key, trace_id, rid}
//   outbox         POST /outbox {bot, chat, op, body (object), idem_key, urgent, reply_to_event, files, trace_id, rid}
//                  GET /outbox/<id> (404 = gone)
//   files          GET /tg/files/in|out/<name> (bytes) · POST /tg/files (bytes, X-File-Name) -> {ref}
// Errors: HubDown (unreachable, timeout, 5xx, no token: try again later) or HubRefused (4xx: the request is wrong).
// Every write carries a rid, so a retry after a lost answer writes nothing twice.
//
// Hub files: a payload value or outbox result "hubfile:in/<name>" names a file the adapter stored in the hub; the
// bridge fetches it into its inbox (STATE_DIR/inbox, 0700; files 0600; ≤ the attachment cap) and hands the session
// that local path. Local files the session attaches are uploaded first and sent as "hubfile:out/<name>" refs.
// The redelivery meta keys (bridge.redeliver, bridge.redeliver.ack) stay in the local SQLite events store
// (brain-watch writes them there in both modes).
import { createHash, randomBytes } from 'crypto'
import { chmodSync, closeSync, existsSync, mkdirSync, openSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeSync } from 'fs'
import { basename, join } from 'path'
import { BridgeDb, type BridgeStore, type Enqueue, type InRow, type OutRow, type RedeliverRequest } from './bridgedb.ts'

export const HUB_DEFAULT_URL = 'http://claude-hub:8700'
export const HUBFILE_RE = /^hubfile:(in|out)\/([A-Za-z0-9._-]{1,120})$/
export const isHubRef = (v: unknown): v is string => typeof v === 'string' && HUBFILE_RE.test(v) && !/\/\.\.?$/.test(v)
const REQ_TIMEOUT_MS = 8_000
const FILE_TIMEOUT_MS = 120_000
const INBOX_KEEP_S = 7 * 86_400

/** The hub did not answer (unreachable, timeout, 5xx) or there is no token: try again later. */
export class HubDown extends Error {}
/** The hub answered 4xx: this request is wrong (or the thing is gone: 404). */
export class HubRefused extends Error {
  constructor(readonly status: number, readonly code: string, readonly hint: string) {
    super(`hub refused (${status} ${code}${hint ? `: ${hint}` : ''})`)
  }
}

export const newRid = () => `tgb-${randomBytes(12).toString('hex')}`
/** Approval cards (H6): the brain mints the nonce on the hub before the card is queued; the hub's tg never mints. */
export const APPROVAL_TTL_S = 900
export const APPROVAL_ACTION_MAX = 8192 // as packages/approvals and the adapter
export const APPROVAL_SUMMARY_MAX = 80
const sha256hex = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex')
const oneLine = (v: unknown, n: number) => {
  const s = String(v ?? '').split(/\s+/).filter(Boolean).join(' ')
  return s.length <= n ? s : s.slice(0, n - 1) + '…'
}
const nowS = () => Date.now() / 1000

type Req = { json?: unknown; body?: Blob; headers?: Record<string, string>; timeoutMs?: number; raw?: boolean }

export class HubClient {
  private token: string | null = null
  /** The last failure's reason ('' while the hub answers): the bridge logs only when this changes. */
  down = ''
  constructor(readonly url: string, readonly tokenFile: string, readonly schema = '') {}

  private bearer(): string {
    if (this.token) return this.token
    let t = ''
    try { t = readFileSync(this.tokenFile, 'utf8').trim() } catch { throw new HubDown('no hub token file') }
    if (!t) throw new HubDown('the hub token file is empty')
    this.token = t
    return t
  }

  async request(method: string, path: string, r: Req = {}): Promise<any> {
    const headers: Record<string, string> = { authorization: `Bearer ${this.bearer()}`, ...(r.headers ?? {}) }
    if (this.schema) headers['x-hub-schema'] = this.schema
    let body: string | Blob | undefined
    if (r.json !== undefined) { body = JSON.stringify(r.json); headers['content-type'] = 'application/json' }
    else if (r.body) { body = r.body; headers['content-type'] = 'application/octet-stream' }
    let res: Response
    try {
      res = await fetch(this.url + path, { method, headers, body, signal: AbortSignal.timeout(r.timeoutMs ?? REQ_TIMEOUT_MS) })
    } catch (err) {
      const why = (err as Error)?.name === 'TimeoutError' ? 'timeout' : 'unreachable'
      this.down = why
      throw new HubDown(`hub ${why}`)
    }
    if (res.status >= 500) {
      this.down = `${res.status}`
      await res.body?.cancel().catch(() => {})
      throw new HubDown(`hub answered ${res.status}`)
    }
    if (res.status >= 400) {
      this.down = ''
      let info: any = {}
      try { info = await res.json() } catch {}
      throw new HubRefused(res.status, String(info?.error ?? 'error'), String(info?.hint ?? ''))
    }
    this.down = ''
    if (r.raw) return res
    try { return await res.json() } catch { throw new HubDown('hub answer is not JSON') }
  }

  get = (path: string, timeoutMs?: number) => this.request('GET', path, { timeoutMs })
  post = (path: string, json: unknown, timeoutMs?: number) => this.request('POST', path, { json, timeoutMs })
}

const num = (v: unknown): number | null => (v == null || v === '' || !Number.isFinite(Number(v)) ? null : Number(v))
const text = (v: unknown): string | null => (v == null ? null : typeof v === 'string' ? v : JSON.stringify(v))

/** A hub row as the bridge's InRow (payload as JSON text, numbers as numbers). */
export function inRow(r: any): InRow {
  return {
    id: Number(r.id), ts: Number(r.ts), kind: String(r.kind), payload: text(r.payload) ?? '{}',
    trace_id: r.trace_id ?? null, status: String(r.status ?? 'new'), tries: num(r.tries), receipt_ts: num(r.receipt_ts),
  }
}

export type HubStoreOptions = {
  url: string
  tokenFile: string
  schema?: string
  /** The local SQLite events store: only its meta table (redelivery keys). */
  metaDbPath: string
  inboxDir: string
  maxFileBytes: number
  log: (m: string) => void
}

// The hub needs a chat on every outbox row; the adapter ignores it for these ops ('-', as `tg-adapter enqueue` writes).
const UNCHATTED = new Set(['commands', 'download'])

export class HubBridgeStore implements BridgeStore {
  readonly hub: HubClient
  private meta: BridgeDb | null = null
  private metaErr = ''

  constructor(readonly o: HubStoreOptions) {
    this.hub = new HubClient(o.url.replace(/\/+$/, ''), o.tokenFile, o.schema ?? '')
  }

  // ---- inbound rows and deliveries

  async maxEventId(): Promise<number> {
    return Number((await this.hub.get('/tg/inbound/max')).id) || 0
  }

  async rowsAfter(id: number, limit = 200, waitS = 0): Promise<InRow[]> {
    const wait = Math.max(0, Math.min(25, waitS))
    const r = await this.hub.post('/tg/inbound/poll', { after: id, limit, wait }, (wait + 10) * 1000)
    return (Array.isArray(r.rows) ? r.rows : []).map(inRow)
  }

  async unreceipted(sinceTs: number, maxId: number): Promise<InRow[]> {
    const r = await this.hub.post('/tg/inbound/unreceipted', { since_ts: sinceTs, max_id: maxId }, 15_000)
    return (Array.isArray(r.rows) ? r.rows : []).map(inRow)
  }

  async receiptsSeen(sinceTs: number): Promise<boolean> {
    return !!(await this.hub.get(`/tg/deliveries/seen?since_ts=${encodeURIComponent(sinceTs)}`)).seen
  }

  async markSent(event_id: number, rid = newRid()): Promise<number> {
    return Number((await this.hub.post('/tg/deliveries/sent', { event_id, ts: nowS(), rid })).tries) || 1
  }

  async receipts(ids: number[]): Promise<Map<number, number | null>> {
    const out = new Map<number, number | null>()
    if (!ids.length) return out
    const r = await this.hub.post('/tg/deliveries/receipts', { ids })
    for (const [k, v] of Object.entries(r.receipts ?? {})) out.set(Number(k), num(v))
    return out
  }

  async channelRow(id: number): Promise<InRow | null> {
    try {
      const r = await this.hub.get(`/tg/inbound/${id}`)
      return r.row ? inRow(r.row) : null
    } catch (err) {
      if (err instanceof HubRefused && err.status === 404) return null
      throw err
    }
  }

  async latestDelivered(chat: string, sinceTs: number): Promise<{ id: number; trace_id: string | null } | null> {
    const r = await this.hub.get(`/tg/latest-delivered?chat=${encodeURIComponent(chat)}&since_ts=${encodeURIComponent(sinceTs)}`)
    return r.row ? { id: Number(r.row.id), trace_id: r.row.trace_id ?? null } : null
  }

  async insertEvent(e: { kind: string; subject: string; payload: Record<string, unknown>; labels: string[]; delivery: string; idem_key: string; trace_id?: string | null }): Promise<number | null> {
    const r = await this.hub.post('/tg/bridge-event', { ...e, trace_id: e.trace_id ?? null, rid: newRid() })
    return num(r.id)
  }

  // ---- outbox

  /** An approval card's nonce, minted here with the brain's token (POST /approvals, as `approvals new` does): the hub's
   *  tg records his tap and never mints. Only the action's sha256 leaves this process. A row that names a nonce
   *  already (made with `approvals new`) passes as it is. rid: a retry gets the same nonce. */
  async mintApproval(chat: string, ap: Record<string, unknown>, rid: string): Promise<Record<string, unknown>> {
    if (typeof ap.nonce === 'string' && ap.nonce) return ap
    const action = typeof ap.action === 'string' ? ap.action : ''
    if (!action.trim()) throw new Error('approval.action is required (the canonical action string)')
    if (action.length > APPROVAL_ACTION_MAX) throw new Error(`approval.action is longer than ${APPROVAL_ACTION_MAX} characters`)
    const summary = oneLine(ap.summary, APPROVAL_SUMMARY_MAX)
    if (!summary) throw new Error(`approval.summary is required (≤ ${APPROVAL_SUMMARY_MAX} chars)`)
    const ttl = ap.ttl_s == null ? APPROVAL_TTL_S : Number(ap.ttl_s)
    if (!Number.isInteger(ttl) || ttl < 1 || ttl > 86_400) throw new Error('approval.ttl_s must be 1-86400 seconds')
    const sha = sha256hex(action)
    const r = await this.hub.post('/approvals', { action_sha: sha, summary, ttl, ref: typeof ap.ref === 'string' ? ap.ref : null, chat, rid })
    if (typeof r?.nonce !== 'string' || !r.nonce) throw new Error('the hub minted no approval nonce')
    const out: Record<string, unknown> = { nonce: r.nonce, action_sha: sha }
    for (const k of ['yes', 'no']) if (typeof ap[k] === 'string') out[k] = ap[k]
    return out
  }

  /** One outbox row; rid makes a retry after a lost answer return the same row. An approval card's nonce is minted
   *  first (mintApproval), so the row names {nonce, action_sha}, never the action string. */
  async enqueue(o: Enqueue, rid = newRid()): Promise<number> {
    let body: unknown = {}
    try { body = JSON.parse(o.body) } catch {}
    const ap = body && typeof body === 'object' ? (body as Record<string, unknown>).approval : undefined
    if (ap != null) {
      if (typeof ap !== 'object' || Array.isArray(ap)) throw new Error('approval must be an object: {action, summary, ttl_s}')
      ;(body as Record<string, unknown>).approval = await this.mintApproval(o.chat, ap as Record<string, unknown>, `${rid}-ap`)
    }
    let files: unknown = null
    try { files = o.files ? JSON.parse(o.files) : null } catch {}
    const r = await this.hub.post('/outbox', {
      bot: o.bot, chat: o.chat || (UNCHATTED.has(o.op) ? '-' : o.chat), op: o.op, body, idem_key: o.idem_key, urgent: o.urgent, reply_to_event: o.reply_to_event,
      files, trace_id: o.trace_id, rid,
    })
    const id = num(r.id)
    if (id == null) throw new HubRefused(409, 'exists', `an outbox row with idem_key ${o.idem_key} exists already`)
    return id
  }

  async outRow(id: number): Promise<OutRow | null> {
    let r: any
    try { r = await this.hub.get(`/outbox/${id}`) } catch (err) {
      if (err instanceof HubRefused && err.status === 404) return null
      throw err
    }
    const row = r?.row ?? r
    if (!row || row.id == null) return null
    return {
      id: Number(row.id), status: String(row.status), tg_message_id: num(row.tg_message_id), last_error: row.last_error ?? null,
      result: text(row.result), files: text(row.files),
    }
  }

  // ---- the local meta keys (brain-watch's redelivery request and the bridge's answer)

  private metaDb(): BridgeDb | null {
    if (this.meta) return this.meta
    try {
      this.meta = BridgeDb.openMeta(this.o.metaDbPath)
      this.metaErr = ''
    } catch (err) {
      const m = (err as Error).message
      if (m !== this.metaErr) this.o.log(`local meta store unavailable (redelivery requests wait): ${m}`)
      this.metaErr = m
    }
    return this.meta
  }

  metaGet(k: string): unknown {
    return this.metaDb()?.metaGet(k) ?? null
  }

  metaSet(k: string, v: unknown): void {
    const m = this.metaDb()
    if (!m) throw new Error(this.metaErr || 'local meta store unavailable')
    m.metaSet(k, v)
  }

  redeliverRequest(): RedeliverRequest | null {
    return this.metaDb()?.redeliverRequest() ?? null
  }

  close(): void {
    try { this.meta?.close() } catch {}
  }

  // ---- hub files

  private inbox(): string {
    mkdirSync(this.o.inboxDir, { recursive: true, mode: 0o700 })
    try { chmodSync(this.o.inboxDir, 0o700) } catch {}
    return this.o.inboxDir
  }

  /** A hubfile ref → a local path in the inbox (fetched once; a complete local copy is reused). */
  async fetchRef(ref: string): Promise<string> {
    const m = HUBFILE_RE.exec(ref)
    if (!m || !isHubRef(ref)) throw new HubRefused(400, 'bad_ref', `not a hub file ref: ${ref.slice(0, 80)}`)
    const [, dir, name] = m
    const local = join(this.inbox(), dir === 'in' ? name! : `out-${name}`)
    if (existsSync(local)) return local
    const res: Response = await this.hub.request('GET', `/tg/files/${dir}/${encodeURIComponent(name!)}`, { raw: true, timeoutMs: FILE_TIMEOUT_MS })
    const len = Number(res.headers.get('content-length') ?? NaN)
    if (Number.isFinite(len) && len > this.o.maxFileBytes) {
      await res.body?.cancel().catch(() => {})
      throw new HubRefused(413, 'too_large', `${ref}: ${(len / 1024 / 1024).toFixed(1)}MB is over the ${this.o.maxFileBytes / 1024 / 1024}MB cap`)
    }
    const part = `${local}.part-${process.pid}`
    const fd = openSync(part, 'w', 0o600)
    let total = 0
    try {
      for await (const chunk of res.body as unknown as AsyncIterable<Uint8Array>) {
        total += chunk.byteLength
        if (total > this.o.maxFileBytes) throw new HubRefused(413, 'too_large', `${ref} is over the ${this.o.maxFileBytes / 1024 / 1024}MB cap`)
        writeSync(fd, chunk)
      }
    } catch (err) {
      closeSync(fd)
      rmSync(part, { force: true })
      if (err instanceof HubRefused) throw err
      throw new HubDown(`hub file ${ref}: transfer broke off`)
    }
    closeSync(fd)
    renameSync(part, local)
    return local
  }

  /** A local file → a hubfile:out ref (the hub keeps one copy per content). */
  async upload(path: string): Promise<string> {
    const name = (basename(path).replace(/[^A-Za-z0-9._-]/g, '_').replace(/^\.+/, '_') || 'file').slice(-120)
    const r = await this.hub.request('POST', '/tg/files', { body: Bun.file(path), headers: { 'x-file-name': name }, timeoutMs: FILE_TIMEOUT_MS })
    if (!isHubRef(r?.ref) || !String(r.ref).startsWith('hubfile:out/')) throw new HubDown('the hub answered no file ref')
    return r.ref as string
  }

  /** A row with every hubfile ref in its payload (top-level strings and string lists) fetched and replaced by the
   *  local path. A file the hub no longer has (404) or one over the cap drops out with payload.file_error saying
   *  which; an unreachable hub throws HubDown (the row waits and is tried again). */
  async resolveRow(row: InRow): Promise<InRow> {
    let p: Record<string, unknown>
    try { p = JSON.parse(row.payload) } catch { return row }
    if (!p || typeof p !== 'object' || Array.isArray(p)) return row
    let changed = false
    const errors: string[] = []
    const one = async (k: string, v: string): Promise<string | null> => {
      try { return await this.fetchRef(v) } catch (err) {
        if (err instanceof HubRefused) { errors.push(`${k}: ${err.status === 404 ? 'the file is gone from the hub' : err.hint || err.code}`); return null }
        throw err
      }
    }
    for (const [k, v] of Object.entries(p)) {
      if (isHubRef(v)) {
        const local = await one(k, v)
        if (local) p[k] = local
        else delete p[k]
        changed = true
      } else if (Array.isArray(v) && v.some(isHubRef)) {
        const out: unknown[] = []
        for (const x of v) {
          if (!isHubRef(x)) { out.push(x); continue }
          const local = await one(k, x)
          if (local) out.push(local)
        }
        p[k] = out
        changed = true
      }
    }
    if (!changed) return row
    if (errors.length) p.file_error = errors.join('; ')
    return { ...row, payload: JSON.stringify(p) }
  }

  /** Remove inbox files older than a week (and broken-off partial ones). */
  pruneInbox(now = Date.now()): number {
    let n = 0
    try {
      for (const f of readdirSync(this.o.inboxDir)) {
        const p = join(this.o.inboxDir, f)
        try {
          const st = statSync(p)
          if (st.isFile() && (now - st.mtimeMs > INBOX_KEEP_S * 1000 || (f.includes('.part-') && now - st.mtimeMs > 3_600_000))) { rmSync(p, { force: true }); n++ }
        } catch {}
      }
    } catch {}
    return n
  }
}
