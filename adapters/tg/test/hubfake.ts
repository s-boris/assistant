// A fake hubd for the hub-mode tests (H6): in memory, served through an injected fetch (no sockets). Request and
// answer shapes follow packages/hub/hubd.py + hubstore.py + hubapprovals.py (H5: outbox consumer, approvals) and the
// H6 contract (~/jobs/v2-h6/contract.md: /tg/rows, /tg/rows/patch, /tg/meta, /tg/bindings, /tg/jobs, /tg/presence,
// /tg/files). Callers by bearer token: tg (the adapter) and brain (the companion, the tests' senders).
import { createHash, randomBytes } from 'crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'

export const TG_TOKEN = 'fake-tg-caller-token-0123456789' // secretscan:allow (fake)
export const BRAIN_TOKEN = 'fake-brain-caller-token-0123456789' // secretscan:allow (fake)
const SET_OK = new Set(['status', 'attempts', 'not_before', 'last_error', 'sent_ts', 'tg_message_id', 'result', 'files'])
const TG = 'tg', BRAIN = 'brain', BOTH = 'both'

type Row = Record<string, any>

export class FakeHub {
  events: Row[] = []
  deliveries = new Map<number, Row>()
  outbox: Row[] = []
  approvals = new Map<string, Row>()
  bindings: Row[] = []
  bindingsVersion = 1
  meta = new Map<string, string>()
  jobs: Row[] = []
  presence: { version: number; state: any } = { version: 0, state: null }
  lastClaimAt = 0
  calls: { method: string; path: string; caller: string }[] = []
  /** Network down (fetch throws) / store down (503). */
  down = false
  storeDown = false
  claimGraceMs = 5_000
  companionWindowMs = 40_000
  /** Let the tg caller request approvals (hubd: brain only, as here by default; see the README). */
  tgMayRequest = false
  private nextEvent = 10_000_000
  private nextOutbox = 10_000_000
  private nextJob = 1

  constructor(readonly filesDir: string) {
    for (const d of ['in', 'out']) mkdirSync(join(filesDir, d), { recursive: true })
  }

  // --- helpers for tests ---------------------------------------------------------------------------------------------

  enqueue(op: string, body: Record<string, unknown>, extra: Row = {}): number {
    const id = this.nextOutbox++
    this.outbox.push({ id, ts: Date.now() / 1000, bot: 'test', chat: '111', op, body: JSON.stringify(body), idem_key: null, urgent: 0,
      not_before: null, mode: 'live', status: 'queued', attempts: 0, last_error: null, sent_ts: null, tg_message_id: null, trace_id: null,
      reply_to_event: null, files: null, result: null, ...extra, ...(extra.files && typeof extra.files !== 'string' ? { files: JSON.stringify(extra.files) } : {}) })
    return id
  }

  row(id: number): Row { return this.outbox.find(r => r.id === id)! }
  kinds(kind: string): Row[] { return this.events.filter(e => e.kind === kind) }
  request(o: { action: string; summary: string; ttl?: number; chat?: string }): string {
    const nonce = randomBytes(16).toString('hex')
    const now = Date.now() / 1000
    this.approvals.set(nonce, { nonce, action_sha: createHash('sha256').update(o.action).digest('hex'), summary: o.summary, ref: null, chat: o.chat ?? null,
      message_id: null, created: now, expires: now + (o.ttl ?? 900), status: 'pending', decided_ts: null, decided_by: null, decided_via: null,
      used_ts: null, used_by: null, refused: 0, card: null })
    return nonce
  }

  // --- the server ----------------------------------------------------------------------------------------------------

  /** Lose the answer to the next n calls of one path after the hub acted on them (a cut connection, a crash mid-call). */
  lose: { path: string; n: number } | null = null

  fetch = async (req: Request): Promise<Response> => {
    const path = new URL(req.url).pathname
    const res = await this.serve(req)
    if (this.lose && this.lose.path === path && this.lose.n > 0) { this.lose.n--; throw new TypeError('fetch failed: connection reset (fake hub lost the answer)') }
    return res
  }

  private serve = async (req: Request): Promise<Response> => {
    if (this.down) throw new TypeError('fetch failed: connection refused (fake hub down)')
    const url = new URL(req.url)
    const path = url.pathname
    const auth = req.headers.get('authorization') ?? ''
    const caller = auth === `Bearer ${TG_TOKEN}` ? TG : auth === `Bearer ${BRAIN_TOKEN}` ? BRAIN : null
    this.calls.push({ method: req.method, path, caller: caller ?? '?' })
    if (!caller) return json(401, { error: 'unauthorized', hint: 'bad token' })
    if (this.storeDown) return json(503, { error: 'store_unavailable', hint: 'fake' })
    let a: Row = {}
    if (req.method === 'POST' && path !== '/tg/files') { try { a = JSON.parse((await req.text()) || '{}') } catch { return json(400, { error: 'bad_json' }) } }
    const q = Object.fromEntries(url.searchParams)
    const route = (m: string, re: RegExp, who: string): RegExpExecArray | null => {
      if (req.method !== m) return null
      const g = re.exec(path)
      if (!g || g[0] !== path) return null
      if (who !== BOTH && who !== caller) throw new Forbidden()
      return g
    }
    try {
      let g: RegExpExecArray | null
      if (route('POST', /\/tg\/rows/, TG)) return json(200, this.rows(a))
      if (route('POST', /\/tg\/rows\/patch/, TG)) return this.patch(a)
      if (route('POST', /\/tg\/meta/, TG)) { for (const [k, v] of Object.entries(a.set ?? {})) this.meta.set(k, String(v)); return json(200, { ok: true }) }
      if (route('GET', /\/tg\/bindings/, BOTH)) return json(200, { version: this.bindingsVersion, rows: this.bindings })
      if (route('POST', /\/tg\/jobs/, TG)) return json(200, this.submit(a))
      if ((g = route('POST', /\/tg\/jobs\/(\d+)\/wait/, TG))) return this.waitJob(Number(g[1]), Number(a.wait ?? 0))
      if (route('POST', /\/tg\/jobs\/claim/, BRAIN)) return json(200, await this.claim(a))
      if ((g = route('POST', /\/tg\/jobs\/(\d+)\/result/, BRAIN))) return this.result(Number(g[1]), a)
      if (route('POST', /\/tg\/presence/, BRAIN)) { this.presence = { version: this.presence.version + 1, state: a.state }; return json(200, { version: this.presence.version }) }
      if (route('GET', /\/tg\/presence/, TG)) return json(200, await this.getPresence(Number(q.after ?? 0), Number(q.wait ?? 0)))
      if (route('POST', /\/tg\/files/, BOTH)) return this.upload(req)
      if ((g = route('GET', /\/tg\/files\/(in|out)\/([A-Za-z0-9._-]{1,120})/, BOTH))) {
        const p = join(this.filesDir, g[1]!, g[2]!)
        return existsSync(p) ? new Response(readFileSync(p)) : json(404, { error: 'not_found' })
      }
      // H5 outbox
      if (route('POST', /\/outbox/, BOTH)) return json(200, { id: this.enqueue(String(a.op), typeof a.body === 'string' ? JSON.parse(a.body) : a.body, { bot: a.bot, chat: String(a.chat), urgent: a.urgent ? 1 : 0, idem_key: a.idem_key ?? null, files: a.files ?? null, reply_to_event: a.reply_to_event ?? null }) })
      if (route('POST', /\/outbox\/queued/, TG)) return json(200, { rows: this.outbox.filter(r => r.status === 'queued' && (a.bots ?? []).includes(r.bot) && r.mode === 'live').sort((x, y) => x.id - y.id) })
      if ((g = route('POST', /\/outbox\/(\d+)\/claim/, TG))) {
        const r = this.row(Number(g[1]))
        if (r && r.status === 'queued') { r.status = 'sending'; r.attempts = Number(a.attempts ?? 1); return json(200, { claimed: true }) }
        return json(200, { claimed: false })
      }
      if ((g = route('POST', /\/outbox\/(\d+)\/set/, TG))) {
        const f = a.fields ?? {}
        if (!Object.keys(f).length || Object.keys(f).some(k => !SET_OK.has(k))) return json(400, { error: 'bad_request', hint: 'set: only ' + [...SET_OK].join(', ') })
        const r = this.row(Number(g[1]))
        if (r) Object.assign(r, f)
        return json(200, { ok: !!r })
      }
      if ((g = route('POST', /\/outbox\/(\d+)\/fail/, TG))) return json(200, this.fail(Number(g[1]), a.fields ?? {}, !!a.partial))
      if (route('POST', /\/outbox\/interrupted/, TG)) {
        const stale = this.outbox.filter(r => r.status === 'sending' && (a.bots ?? []).includes(r.bot))
        for (const r of stale) this.fail(r.id, { last_error: 'interrupted while sending (may have been delivered)' }, false)
        return json(200, { failed: stale.length })
      }
      if (route('POST', /\/outbox\/dupes/, TG)) {
        return json(200, { rows: this.outbox.filter(r => r.bot === a.bot && r.chat === a.chat && r.op === a.op && r.mode === 'live' && r.id < Number(a.id)
          && r.ts >= Number(a.since) && ['queued', 'sending', 'sent'].includes(r.status)).sort((x, y) => y.id - x.id).map(r => ({ id: r.id, body: r.body })) })
      }
      if ((g = route('GET', /\/outbox\/(\d+)/, BOTH))) { const r = this.row(Number(g[1])); return r ? json(200, r) : json(404, { error: 'not_found' }) }
      // H5 approvals
      if (route('POST', /\/approvals\/expire/, TG)) {
        const now = Date.now() / 1000
        const due = [...this.approvals.values()].filter(r => r.status === 'pending' && r.expires <= now && r.message_id != null && r.card == null)
        let n = 0
        for (const r of this.approvals.values()) if (r.status === 'pending' && r.expires <= now) { r.status = 'expired'; n++ }
        return json(200, { expired: n, cards: due.map(r => ({ nonce: r.nonce, chat: r.chat, message_id: r.message_id })) })
      }
      if (route('POST', /\/approvals/, this.tgMayRequest ? BOTH : BRAIN)) {
        const nonce = randomBytes(16).toString('hex')
        const now = Date.now() / 1000
        this.approvals.set(nonce, { nonce, action_sha: a.action_sha, summary: a.summary, ref: a.ref ?? null, chat: a.chat ?? null, message_id: null, created: now,
          expires: now + Number(a.ttl ?? 900), status: 'pending', decided_ts: null, decided_by: null, decided_via: null, used_ts: null, used_by: null, refused: 0, card: null })
        return json(200, { nonce, expires: now + Number(a.ttl ?? 900), summary: a.summary, ref: a.ref ?? null })
      }
      if ((g = route('GET', /\/approvals\/([0-9a-f]{32})/, BOTH))) {
        const r = this.approvals.get(g[1]!)
        return r ? json(200, { ...r, state: state(r) }) : json(404, { error: 'not_found', hint: 'no such approval' })
      }
      if ((g = route('POST', /\/approvals\/([0-9a-f]{32})\/attach/, BOTH))) {
        const r = this.approvals.get(g[1]!)
        const ok = !!r && r.status === 'pending' && (r.chat == null || r.chat === String(a.chat))
        if (ok) { r!.chat = String(a.chat); r!.message_id = Number(a.message_id) }
        return json(200, { ok })
      }
      if ((g = route('POST', /\/approvals\/([0-9a-f]{32})\/decide/, TG))) {
        const r = this.approvals.get(g[1]!)
        const now = Date.now() / 1000
        if (!['y', 'n'].includes(a.decision)) return json(400, { error: 'bad_request', hint: 'decision: y or n' })
        const ok = !!r && r.status === 'pending' && r.expires > now
        if (ok) Object.assign(r!, { status: a.decision === 'y' ? 'approved' : 'denied', decided_ts: now, decided_by: String(a.by), decided_via: String(a.via ?? 'adapter') })
        return json(200, { ok, state: r ? state(r) : 'unknown' })
      }
      if ((g = route('POST', /\/approvals\/([0-9a-f]{32})\/card/, TG))) {
        const r = this.approvals.get(g[1]!)
        if (r) r.card = a.card
        return json(200, { ok: !!r })
      }
      return json(404, { error: 'not_found', hint: `${req.method} ${path}` })
    } catch (e) {
      if (e instanceof Forbidden) return json(403, { error: 'forbidden', hint: "not this caller's" })
      return json(500, { error: 'internal', hint: String(e) })
    }
  }

  private rows(a: Row): Row {
    const ids: number[] = []
    for (const r of a.rows ?? []) {
      if (!/^(tg|outbox|approval)\./.test(String(r.kind))) throw new Error('kind')
      const have = this.events.find(e => e.idem_key === r.idem_key)
      if (have) { ids.push(have.id); continue }
      const id = this.nextEvent++
      const { local_id: _l, binding_receipt, ...rest } = r
      this.events.push({ id, ...rest })
      if (binding_receipt) this.deliveries.set(id, { event_id: id, sent_ts: r.ts, tries: 0, receipt_ts: r.ts, how: 'binding' })
      ids.push(id)
    }
    return { ids }
  }

  private patch(a: Row): Response {
    const e = this.events.find(x => x.idem_key === a.idem_key)
    if (!e) return json(404, { error: 'not_found' })
    e.payload = JSON.stringify({ ...JSON.parse(e.payload || '{}'), ...(a.payload_patch ?? {}) })
    if (a.status) e.status = a.status
    e.updated_ts = Date.now() / 1000
    return json(200, { id: e.id })
  }

  private fail(id: number, fields: Row, partial: boolean): Row {
    const r = this.row(id)
    if (!r) return { ok: false }
    Object.assign(r, fields, { status: 'failed', last_error: String(fields.last_error ?? 'failed') })
    const idem = `outbox.failed:${r.bot}:${r.id}`
    if (!this.events.some(e => e.idem_key === idem)) {
      this.events.push({ id: this.nextEvent++, ts: Date.now() / 1000, source: 'tg-adapter', kind: 'outbox.failed', idem_key: idem,
        payload: JSON.stringify({ outbox_id: r.id, error: r.last_error, partial }), status: 'new' })
    }
    return { ok: true }
  }

  private submit(a: Row): Row {
    const id = this.nextJob++
    const now = Date.now()
    const offline = now - this.lastClaimAt > this.companionWindowMs
    this.jobs.push({ id, kind: a.kind, args: a.args ?? {}, timeout_s: Number(a.timeout_s ?? 60), created: now, state: offline ? 'unclaimed' : 'queued' })
    return { id, state: offline ? 'unclaimed' : 'queued' }
  }

  private async waitJob(id: number, waitS: number): Promise<Response> {
    const j = this.jobs.find(x => x.id === id)
    if (!j) return json(404, { error: 'not_found' })
    const until = Date.now() + Math.min(waitS, 25) * 1000
    for (;;) {
      if (j.state === 'queued' && Date.now() - j.created > this.claimGraceMs) j.state = 'unclaimed'
      if (['done', 'failed', 'unclaimed', 'expired'].includes(j.state) || Date.now() >= until) break
      await Bun.sleep(10)
    }
    return json(200, { id, state: j.state, ...(j.result ? { result: j.result } : {}), ...(j.error ? { error: j.error } : {}) })
  }

  private async claim(a: Row): Promise<Row> {
    const until = Date.now() + Math.min(Number(a.wait ?? 0), 25) * 1000
    for (;;) {
      this.lastClaimAt = Date.now()
      const j = this.jobs.find(x => x.state === 'queued' && (a.kinds ?? []).includes(x.kind))
      if (j) { j.state = 'running'; return { job: { id: j.id, kind: j.kind, args: j.args, timeout_s: j.timeout_s, created: j.created / 1000 } } }
      if (Date.now() >= until) return { job: null }
      await Bun.sleep(10)
    }
  }

  private result(id: number, a: Row): Response {
    const j = this.jobs.find(x => x.id === id)
    if (!j) return json(404, { error: 'not_found' })
    j.state = a.ok ? 'done' : 'failed'
    j.result = a.result
    j.error = a.error
    return json(200, { ok: true })
  }

  private async getPresence(after: number, waitS: number): Promise<Row> {
    const until = Date.now() + Math.min(waitS, 25) * 1000
    while (this.presence.version <= after && Date.now() < until) await Bun.sleep(10)
    return { version: this.presence.version, state: this.presence.state }
  }

  private async upload(req: Request): Promise<Response> {
    const bytes = new Uint8Array(await req.arrayBuffer())
    const name = (req.headers.get('x-file-name') ?? 'file').replace(/[^A-Za-z0-9._-]/g, '_')
    const out = `${createHash('sha256').update(bytes).digest('hex').slice(0, 32)}-${name}`
    writeFileSync(join(this.filesDir, 'out', out), bytes)
    return json(200, { ref: `hubfile:out/${out}` })
  }
}

class Forbidden extends Error {}

function state(r: Row): string {
  if (r.used_ts != null) return 'used'
  if (r.status === 'pending' && Date.now() / 1000 >= r.expires) return 'expired'
  return r.status
}

function json(status: number, obj: unknown): Response {
  return new Response(JSON.stringify(obj), { status, headers: { 'content-type': 'application/json' } })
}
