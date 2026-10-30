// A fake hub for the bridge's hub-mode tests (v2 Phase 2 H6): the endpoints bridgehub.ts calls, over an in-memory
// SQLite store with the events DDL (test/fixtures/events-v1.sql + deliveries + outbox v2 columns). Bun.serve on
// 127.0.0.1, port 0. The test plays the adapter (inbound rows, outbox outcomes, files in in/) and the receipt
// writer, and can take the hub down (503) or away (stopped: connection refused) and back.
import { Database } from 'bun:sqlite'
import { createHash } from 'crypto'
import { readFileSync } from 'fs'
import { join } from 'path'
import { CHANNEL_KINDS, DELIVERIES_DDL } from '../bridgedb.ts'

const DDL = readFileSync(join(import.meta.dir, 'fixtures', 'events-v1.sql'), 'utf8')
const KIND_LIST = CHANNEL_KINDS.map(k => `'${k}'`).join(', ')
const ROW = `SELECT e.id, e.ts, e.kind, e.payload, e.trace_id, e.status, d.tries, d.receipt_ts FROM events e LEFT JOIN deliveries d ON d.event_id = e.id`

export type FakeHub = {
  url: string
  port: number
  token: string
  db: Database
  files: { in: Map<string, Uint8Array>; out: Map<string, Uint8Array> }
  calls: { method: string; path: string; body?: any }[]
  /** POST /approvals requests (the brain's mints), in order. */
  approvals: { nonce: string; action_sha: string; summary: string; ttl: number; chat: string | null; rid: string }[]
  /** 'up' answers, 'down' answers 503, 'away' = the server is stopped (connection refused). */
  setState(s: 'up' | 'down' | 'away'): Promise<void>
  /** The next n writes to this path are done but answered 503 (a lost answer). */
  loseAnswers(path: string, n?: number): void
  inbound(kind?: string, payload?: Record<string, unknown>, o?: { ts?: number; status?: string; delivery?: string }): number
  outcome(id: number, fields: Record<string, unknown>): void
  receipt(id: number, ts?: number): void
  stop(): void
}

let upd = 900_000
export async function startFakeHub(token = 'hub-test-token-0123456789abcdef'): Promise<FakeHub> {
  const db = new Database(':memory:')
  db.exec(DDL)
  db.exec(DELIVERIES_DDL)
  for (const c of ['reply_to_event INTEGER', 'files TEXT', 'result TEXT']) db.exec(`ALTER TABLE outbox ADD COLUMN ${c}`)
  const files = { in: new Map<string, Uint8Array>(), out: new Map<string, Uint8Array>() }
  const calls: FakeHub['calls'] = []
  const requests = new Map<string, unknown>()
  const approvals: FakeHub['approvals'] = []
  const lose = new Map<string, number>()
  let waiters: (() => void)[] = []
  const wake = () => { const w = waiters; waiters = []; for (const f of w) f() }
  let state: 'up' | 'down' | 'away' = 'up'
  const now = () => Date.now() / 1000
  const json = (status: number, o: unknown) => Response.json(o, { status })
  /** A write once per rid (a repeat answers the stored result). */
  const once = (rid: unknown, f: () => unknown) => {
    if (typeof rid === 'string' && requests.has(rid)) return requests.get(rid)
    const r = f()
    if (typeof rid === 'string') requests.set(rid, r)
    return r
  }
  const rows = (sql: string, ...p: any[]) => db.query(sql).all(...p) as any[]

  async function handle(req: Request): Promise<Response> {
    if (state === 'down') return json(503, { error: 'unavailable', hint: 'test: hub down' })
    const u = new URL(req.url)
    const path = u.pathname
    if (req.headers.get('authorization') !== `Bearer ${token}`) return json(401, { error: 'unauthorized' })
    const isJson = (req.headers.get('content-type') ?? '').includes('application/json')
    const body: any = req.method === 'POST' && isJson ? await req.json() : undefined
    calls.push({ method: req.method, path, ...(body !== undefined ? { body } : {}) })
    const lost = (lose.get(path) ?? 0) > 0
    const answer = (status: number, o: unknown) => {
      if (lost) { lose.set(path, lose.get(path)! - 1); return json(503, { error: 'unavailable', hint: 'test: answer lost' }) }
      return json(status, o)
    }
    let m: RegExpExecArray | null
    if (req.method === 'POST' && path === '/tg/inbound/poll') {
      const q = () => rows(`${ROW} WHERE e.id > ? AND e.delivery = 'now' AND e.kind IN (${KIND_LIST}) ORDER BY e.id LIMIT ?`, Number(body.after), Number(body.limit ?? 200))
      let got = q()
      if (!got.length && Number(body.wait) > 0) {
        await Promise.race([new Promise<void>(r => waiters.push(r)), Bun.sleep(Math.min(25, Number(body.wait)) * 1000)])
        if (state !== 'up') return json(503, { error: 'unavailable' })
        got = q()
      }
      return json(200, { rows: got })
    }
    if (req.method === 'POST' && path === '/tg/inbound/unreceipted') {
      return json(200, { rows: rows(`${ROW} WHERE e.id <= ? AND e.ts >= ? AND e.delivery = 'now' AND e.kind IN (${KIND_LIST})
        AND e.status NOT IN ('acked', 'dismissed', 'gone') AND d.receipt_ts IS NULL ORDER BY e.id`, Number(body.max_id), Number(body.since_ts)) })
    }
    if (req.method === 'GET' && path === '/tg/inbound/max') return json(200, { id: (db.query('SELECT COALESCE(MAX(id), 0) AS m FROM events').get() as any).m })
    if (req.method === 'GET' && (m = /^\/tg\/inbound\/(\d+)$/.exec(path))) {
      return json(200, { row: db.query(`${ROW} WHERE e.id = ? AND e.delivery = 'now' AND e.kind IN (${KIND_LIST})`).get(Number(m[1])) ?? null })
    }
    if (req.method === 'GET' && path === '/tg/latest-delivered') {
      const r = db.query(`SELECT e.id, e.trace_id FROM events e JOIN deliveries d ON d.event_id = e.id WHERE d.sent_ts >= ? AND e.kind IN (${KIND_LIST})
        AND json_extract(e.payload, '$.chat_id') = ? ORDER BY e.id DESC LIMIT 1`).get(Number(u.searchParams.get('since_ts')), u.searchParams.get('chat'))
      return json(200, { row: r ?? null })
    }
    if (req.method === 'POST' && path === '/tg/deliveries/sent') {
      const r = once(body.rid, () => {
        db.query(`INSERT INTO deliveries(event_id, sent_ts, tries) VALUES (?, ?, 1)
          ON CONFLICT(event_id) DO UPDATE SET sent_ts = excluded.sent_ts, tries = tries + 1`).run(Number(body.event_id), Number(body.ts ?? now()))
        return { tries: (db.query('SELECT tries FROM deliveries WHERE event_id = ?').get(Number(body.event_id)) as any).tries }
      })
      return answer(200, r)
    }
    if (req.method === 'POST' && path === '/tg/deliveries/receipts') {
      const ids: number[] = (body.ids ?? []).map(Number)
      const out: Record<string, number | null> = {}
      for (const r of ids.length ? rows(`SELECT event_id, receipt_ts FROM deliveries WHERE event_id IN (${ids.map(() => '?').join(', ')})`, ...ids) : []) out[r.event_id] = r.receipt_ts
      return json(200, { receipts: out })
    }
    if (req.method === 'GET' && path === '/tg/deliveries/seen') {
      return json(200, { seen: !!db.query('SELECT 1 FROM deliveries WHERE receipt_ts IS NOT NULL AND receipt_ts >= ? LIMIT 1').get(Number(u.searchParams.get('since_ts'))) })
    }
    if (req.method === 'POST' && path === '/tg/bridge-event') {
      if (!String(body.kind).startsWith('bridge.')) return json(400, { error: 'bad_kind' })
      const r = once(body.rid, () => {
        const x = db.query(`INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, trace_id, idem_key, status)
          VALUES (?, 'tg-bridge', ?, ?, '', ?, ?, ?, ?, ?, 'new') ON CONFLICT(idem_key) DO NOTHING`).run(
          now(), body.kind, body.subject, JSON.stringify(body.payload), JSON.stringify(body.labels), body.delivery, body.trace_id ?? null, body.idem_key)
        if (x.changes) wake()
        return { id: x.changes ? Number(x.lastInsertRowid) : null }
      })
      return answer(200, r)
    }
    if (req.method === 'POST' && path === '/approvals') {
      if (typeof body.action_sha !== 'string' || !/^[0-9a-f]{64}$/.test(body.action_sha)) return json(400, { error: 'bad_request', hint: 'action_sha' })
      if (typeof body.summary !== 'string' || !body.summary) return json(400, { error: 'bad_request', hint: 'summary' })
      const r = once(body.rid, () => {
        const nonce = createHash('sha256').update(String(body.rid) + String(approvals.length)).digest('hex').slice(0, 32)
        approvals.push({ nonce, action_sha: body.action_sha, summary: body.summary, ttl: Number(body.ttl), chat: body.chat ?? null, rid: String(body.rid) })
        return { nonce, expires: now() + Number(body.ttl ?? 900), summary: body.summary, ref: body.ref ?? null, status: 'pending' }
      })
      return json(200, r)
    }
    if (req.method === 'POST' && path === '/outbox') {
      if (![body.op, body.bot, body.chat].every(x => typeof x === 'string' && x)) return json(400, { error: 'bad_request', hint: 'op, bot and chat needed' })
      if (typeof body.body !== 'object' || body.body == null) return json(400, { error: 'bad_request', hint: 'body must be an object' })
      const r = once(body.rid, () => {
        const x = db.query(`INSERT INTO outbox(ts, bot, chat, op, body, idem_key, urgent, not_before, mode, status, reply_to_event, files, trace_id)
          VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'live', 'queued', ?, ?, ?) ON CONFLICT(idem_key) DO NOTHING`).run(
          now(), body.bot, body.chat, body.op, JSON.stringify(body.body), body.idem_key ?? null, body.urgent ? 1 : 0, body.reply_to_event ?? null,
          body.files ? JSON.stringify(body.files) : null, body.trace_id ?? null)
        return { id: x.changes ? Number(x.lastInsertRowid) : null }
      })
      return answer(200, r)
    }
    if (req.method === 'GET' && (m = /^\/outbox\/(\d+)$/.exec(path))) {
      const r = db.query('SELECT * FROM outbox WHERE id = ?').get(Number(m[1]))
      return r ? json(200, r) : json(404, { error: 'not_found' })
    }
    if (req.method === 'POST' && path === '/tg/files') {
      const bytes = new Uint8Array(await req.arrayBuffer())
      const name = `${createHash('sha256').update(bytes).digest('hex').slice(0, 32)}-${req.headers.get('x-file-name') ?? 'file'}`
      files.out.set(name, bytes)
      return json(200, { ref: `hubfile:out/${name}` })
    }
    if (req.method === 'GET' && (m = /^\/tg\/files\/(in|out)\/([A-Za-z0-9._-]+)$/.exec(path))) {
      const f = files[m[1] as 'in' | 'out'].get(m[2]!)
      return f ? new Response(f, { headers: { 'content-type': 'application/octet-stream' } }) : json(404, { error: 'not_found' })
    }
    return json(404, { error: 'no_route', hint: `${req.method} ${path}` })
  }

  let server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: handle })
  const port = server.port
  const hub: FakeHub = {
    url: `http://127.0.0.1:${port}`, port, token, db, files, calls, approvals,
    async setState(s) {
      if (s === 'away' && state !== 'away') { server.stop(true); wake() }
      if (s !== 'away' && state === 'away') server = Bun.serve({ hostname: '127.0.0.1', port, fetch: handle })
      state = s
      if (s === 'down') wake()
    },
    loseAnswers(path, n = 1) { lose.set(path, n) },
    inbound(kind = 'tg.message', payload = {}, o = {}) {
      const u = upd++
      const p = { chat_id: '111', message_id: String(u), user: 'owner', user_id: '111', ts: new Date().toISOString(), text: `msg ${u}`, ...payload }
      const r = db.query(`INSERT INTO events(ts, source, kind, subject, payload, labels, delivery, trace_id, idem_key, status)
        VALUES (?, 'tg-adapter', ?, 'x', ?, '[]', ?, ?, ?, ?)`).run(o.ts ?? now(), kind, JSON.stringify(p), o.delivery ?? 'now', `trace-${u}`, `tg:live:${u}`, o.status ?? 'new')
      wake()
      return Number(r.lastInsertRowid)
    },
    outcome(id, fields) {
      const keys = Object.keys(fields)
      db.query(`UPDATE outbox SET ${keys.map(k => `${k} = ?`).join(', ')} WHERE id = ?`).run(...keys.map(k => fields[k] as any), id)
    },
    receipt(id, ts = now()) {
      db.query(`INSERT INTO deliveries(event_id, receipt_ts, how) VALUES (?, ?, 'channel') ON CONFLICT(event_id) DO UPDATE SET receipt_ts = excluded.receipt_ts, how = 'channel'`).run(id, ts)
    },
    stop() { try { server.stop(true) } catch {} },
  }
  return hub
}
