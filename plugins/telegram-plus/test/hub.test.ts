// Hub mode (TELEGRAM_BRIDGE_STORE=hub, v2 Phase 2 H6) over a real MCP stdio connection: server.ts as a child process,
// a temp HOME, a fake hub (test/fakehub.ts, 127.0.0.1) holding the rows, deliveries, outbox and files. The test plays
// tg-adapter in the hub (inbound rows, outbox outcomes, files in in/) and the receipt writer. No Bot API, no token of
// a bot, no Claude Code, no network beyond 127.0.0.1.
import { afterAll, describe, expect, test } from 'bun:test'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { Database } from 'bun:sqlite'
import { createHash } from 'crypto'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs'
import { createServer } from 'net'
import { tmpdir } from 'os'
import { join } from 'path'
import { REDELIVER_ACK_KEY, REDELIVER_KEY } from '../bridgedb.ts'
import { startFakeHub, type FakeHub } from './fakehub.ts'

const DIR = join(import.meta.dir, '..')
const DDL = readFileSync(join(import.meta.dir, 'fixtures', 'events-v1.sql'), 'utf8')
const ROOT = mkdtempSync(join(tmpdir(), 'tgp-hub-'))
const live: Client[] = []
const hubs: FakeHub[] = []
afterAll(async () => {
  for (const c of live) await c.close().catch(() => {})
  for (const h of hubs) h.stop()
  rmSync(ROOT, { recursive: true, force: true })
})
const sleep = (ms: number) => Bun.sleep(ms)
async function waitFor<T>(f: () => T, ms = 5000): Promise<T> {
  const end = Date.now() + ms
  let v = f()
  while (!v && Date.now() < end) { await sleep(25); v = f() }
  return v
}

let homes = 0
/** A temp HOME with the hub token and the local events store (its meta table holds the redelivery keys). */
async function setup(): Promise<{ home: string; hub: FakeHub; local: Database }> {
  const home = join(ROOT, `h${++homes}`)
  mkdirSync(join(home, '.secrets'), { recursive: true })
  const hub = await startFakeHub()
  hubs.push(hub)
  writeFileSync(join(home, '.secrets', 'hub-token'), `${hub.token}\n`, { mode: 0o600 })
  const local = new Database(join(home, 'events.db'), { create: true })
  local.exec('PRAGMA journal_mode=WAL')
  local.exec(DDL)
  return { home, hub, local }
}

type Running = Client & { notes: any[]; log: () => string }
async function start(home: string, hub: FakeHub, extra: Record<string, string> = {}): Promise<Running> {
  const transport = new StdioClientTransport({
    command: process.execPath, args: ['server.ts'], cwd: DIR, stderr: 'pipe',
    env: {
      PATH: process.env.PATH!, HOME: home,
      TELEGRAM_BRIDGE_STORE: 'hub', HUB_URL: hub.url, TELEGRAM_BRIDGE_HUB_WAIT_S: '2',
      TELEGRAM_BRIDGE_DB: join(home, 'events.db'),
      TELEGRAM_BRIDGE_START_DELAY_MS: '0', TELEGRAM_BRIDGE_SEND_WAIT_MS: '1500',
      TELEGRAM_BRIDGE_LEASE_RETRY_MS: '150', TELEGRAM_BRIDGE_FORCE: '1', TELEGRAM_STATUS_STATE: join(home, 'board.json'),
      ...(process.env.LD_PRELOAD ? { LD_PRELOAD: process.env.LD_PRELOAD } : {}),
      ...(process.env.NETGUARD_LOG ? { NETGUARD_LOG: process.env.NETGUARD_LOG } : {}),
      ...extra,
    },
  })
  let log = ''
  transport.stderr?.on('data', (d: Buffer) => { log += d.toString() })
  const notes: any[] = []
  const client = new Client({ name: 'bridge-hub-test', version: '0' })
  client.fallbackNotificationHandler = async n => { notes.push(n) }
  await client.connect(transport)
  live.push(client)
  return Object.assign(client, { notes, log: () => log })
}
const channel = (b: Running) => b.notes.filter(n => n.method === 'notifications/claude/channel')
const byEvent = (b: Running, id: number) => channel(b).filter(n => n.params.meta.event_id === String(id))
const text = (r: any) => r.content?.[0]?.text as string
const lastOut = (hub: FakeHub) => hub.db.query('SELECT * FROM outbox ORDER BY id DESC LIMIT 1').get() as any
const tries = (hub: FakeHub, id: number) => (hub.db.query('SELECT tries FROM deliveries WHERE event_id = ?').get(id) as any)?.tries ?? 0
const call = (b: Running, name: string, args: Record<string, unknown>) => b.callTool({ name, arguments: args }) as Promise<any>
/** Play the adapter in the hub: finish the next outbox row past `after` with `fields`. */
async function adapter(hub: FakeHub, after: number, fields: Record<string, unknown>): Promise<any> {
  const row = await waitFor(() => hub.db.query('SELECT * FROM outbox WHERE id > ? ORDER BY id LIMIT 1').get(after) as any)
  if (!row) throw new Error('no outbox row')
  hub.outcome(row.id, fields)
  return row
}
const maxOut = (hub: FakeHub) => (hub.db.query('SELECT COALESCE(MAX(id), 0) AS m FROM outbox').get() as any).m as number

describe('hub mode: tools → hub outbox', () => {
  test('reply: one hub outbox row (body as an object, hold/dedupe off, bot live, reply_to_event), answered as 0.1.22', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    expect((await b.listTools()).tools.length).toBe(11)
    expect(await waitFor(() => /lease held/.test(b.log()))).toBeTruthy()
    expect(b.log()).toMatch(/up \(bot live, hub store http:\/\/127\.0\.0\.1:\d+\); eligible for inbound/)
    const ev = hub.inbound('tg.message', { text: 'hi' })
    expect(await waitFor(() => byEvent(b, ev).length === 1)).toBeTruthy()
    const p = call(b, 'reply', { chat_id: '111', text: 'hello' })
    await adapter(hub, 0, { status: 'sent', tg_message_id: 4242, sent_ts: Date.now() / 1000 })
    expect(text(await p)).toBe('sent (id: 4242)')
    const row = lastOut(hub)
    expect(row).toMatchObject({ bot: 'live', chat: '111', op: 'send', mode: 'live', reply_to_event: ev, trace_id: expect.stringMatching(/^trace-/) })
    expect(JSON.parse(row.body)).toEqual({ text: 'hello', hold: false, dedupe: false })
    const post = hub.calls.find(c => c.path === '/outbox')!
    expect(typeof post.body.body).toBe('object')
    expect(post.body.rid).toMatch(/^tgb-[0-9a-f]{24}$/)
    expect(b.log()).not.toContain(hub.token)
  }, 20_000)

  test('reply with files: each local file goes up first (POST /tg/files) and the row carries hubfile:out refs', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    const f = join(home, 'pic one.png')
    writeFileSync(f, 'PNGDATA-123')
    const p = call(b, 'reply', { chat_id: '111', text: 'see', files: [f] })
    await adapter(hub, 0, { status: 'sent', tg_message_id: 7 })
    expect(text(await p)).toBe('sent (id: 7)')
    const files = JSON.parse(lastOut(hub).files)
    expect(files).toHaveLength(1)
    expect(files[0]).toMatch(/^hubfile:out\/[0-9a-f]{32}-pic_one\.png$/)
    expect(new TextDecoder().decode(hub.files.out.get(files[0].slice('hubfile:out/'.length))!)).toBe('PNGDATA-123')
    // the private-directory guard runs before any upload
    mkdirSync(join(home, '.secrets'), { recursive: true })
    writeFileSync(join(home, '.secrets', 'x'), 's')
    const bad = await call(b, 'reply', { chat_id: '111', text: 'x', files: [join(home, '.secrets', 'x')] })
    expect(bad.isError).toBe(true)
    expect(text(bad)).toMatch(/private directory/)
    expect(hub.files.out.size).toBe(1)
  }, 20_000)

  test('download_attachment: the adapter\'s hubfile:in result is fetched into the 0700 inbox and answered as a local path', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    hub.files.in.set('doc-55.pdf', new TextEncoder().encode('%PDF-1.7 test'))
    const p = call(b, 'download_attachment', { file_id: 'FILE55' })
    const row = await adapter(hub, 0, { status: 'sent', result: JSON.stringify({ path: 'hubfile:in/doc-55.pdf' }) })
    expect(row.op).toBe('download')
    expect(row.chat).toBe('-') // the hub refuses an empty chat; the adapter ignores it for downloads
    const path = text(await p)
    expect(path).toBe(join(home, '.local', 'state', 'tg-bridge', 'inbox', 'doc-55.pdf'))
    expect(readFileSync(path, 'utf8')).toBe('%PDF-1.7 test')
    expect(statSync(path).mode & 0o777).toBe(0o600)
    expect(statSync(join(home, '.local', 'state', 'tg-bridge', 'inbox')).mode & 0o777).toBe(0o700)
    // and a fetched file may be sent on again (the bridge inbox is not a private directory)
    const again = call(b, 'reply', { chat_id: '111', text: 'fwd', files: [path] })
    await adapter(hub, row.id, { status: 'sent', tg_message_id: 9 })
    expect(text(await again)).toBe('sent (id: 9)')
  }, 20_000)

  test('set_commands: the row reaches the hub with chat "-" (the hub needs a chat on every row)', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    const p = call(b, 'set_commands', { commands: [{ command: 'lights', description: 'Lights' }] })
    const row = await adapter(hub, 0, { status: 'sent', result: JSON.stringify({ commands: ['lights'] }) })
    expect(row).toMatchObject({ op: 'commands', chat: '-' })
    expect((await p).isError).toBeFalsy()
  }, 20_000)

  test('the hub down for the whole wait: a clear error, nothing sent; the hub back: the next call works', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    await hub.setState('down')
    const t0 = Date.now()
    const r = await call(b, 'reply', { chat_id: '111', text: 'x' })
    expect(r.isError).toBe(true)
    expect(text(r)).toMatch(/^reply failed: hub unavailable \(hub answered 503\): nothing was sent, try again$/)
    expect(Date.now() - t0).toBeLessThan(5000)
    await hub.setState('away')
    const r2 = await call(b, 'reply', { chat_id: '111', text: 'x' })
    expect(text(r2)).toMatch(/hub unavailable \(hub unreachable\): nothing was sent, try again/)
    expect(maxOut(hub)).toBe(0)
    await hub.setState('up')
    const p = call(b, 'reply', { chat_id: '111', text: 'back' })
    await adapter(hub, 0, { status: 'sent', tg_message_id: 11 })
    expect(text(await p)).toBe('sent (id: 11)')
  }, 30_000)

  test('a lost enqueue answer: the retry with the same rid gets the same row (one row, one send)', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    hub.loseAnswers('/outbox', 1)
    const p = call(b, 'reply', { chat_id: '111', text: 'once' })
    await adapter(hub, 0, { status: 'sent', tg_message_id: 12 })
    expect(text(await p)).toBe('sent (id: 12)')
    expect(maxOut(hub)).toBe(1)
  }, 20_000)
})

describe('hub mode: inbound', () => {
  test('rows → notifications with event_id in id order (long poll, no 250 ms poll), each with a deliveries row', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    expect(await waitFor(() => /lease held/.test(b.log()))).toBeTruthy()
    await sleep(300)
    const t0 = Date.now()
    const a = hub.inbound('tg.message', { text: 'first' })
    expect(await waitFor(() => byEvent(b, a).length === 1)).toBeTruthy()
    expect(Date.now() - t0).toBeLessThan(1000) // the long poll wakes on the insert
    const c = hub.inbound('tg.button', { callback_data: 'x:1', event_type: 'button' })
    const d = hub.inbound('tg.reaction', { emoji: '👍', event_type: 'reaction' })
    expect(await waitFor(() => byEvent(b, d).length === 1)).toBeTruthy()
    const order = channel(b).map(n => Number(n.params.meta.event_id))
    expect(order).toEqual([a, c, d])
    expect(byEvent(b, a)[0].params.content).toBe('first')
    expect([tries(hub, a), tries(hub, c), tries(hub, d)]).toEqual([1, 1, 1])
    // rows not for the channel: a beat-class row and a non-channel kind never show
    hub.inbound('tg.message', { text: 'beat' }, { delivery: 'beat' })
    await sleep(500)
    expect(channel(b).length).toBe(3)
    // fewer requests than a 250 ms poll would make: one long poll per ~2 s while idle
    const polls = hub.calls.filter(x => x.path === '/tg/inbound/poll').length
    expect(polls).toBeLessThan(15)
  }, 20_000)

  test('hubfile:in refs in a row are fetched first; the meta carries the local path; a gone file is named in file_error', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    hub.files.in.set('photo-1.jpg', new TextEncoder().encode('JPEGDATA'))
    const a = hub.inbound('tg.message', { text: '(photo)', image_path: 'hubfile:in/photo-1.jpg' })
    expect(await waitFor(() => byEvent(b, a).length === 1)).toBeTruthy()
    const meta = byEvent(b, a)[0].params.meta
    expect(meta.image_path).toBe(join(home, '.local', 'state', 'tg-bridge', 'inbox', 'photo-1.jpg'))
    expect(readFileSync(meta.image_path, 'utf8')).toBe('JPEGDATA')
    const g = hub.inbound('tg.message', { text: '(photo)', image_path: 'hubfile:in/gone.jpg' })
    expect(await waitFor(() => byEvent(b, g).length === 1)).toBeTruthy()
    const m2 = byEvent(b, g)[0].params.meta
    expect(m2.image_path).toBeUndefined()
    expect(m2.file_error).toBe('image_path: the file is gone from the hub')
  }, 20_000)

  test('the hub down and away: rows wait in the hub and arrive once each when it is back; no crash', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    const a = hub.inbound('tg.message', { text: 'before' })
    expect(await waitFor(() => byEvent(b, a).length === 1)).toBeTruthy()
    await hub.setState('down')
    const x = hub.inbound('tg.message', { text: 'during down' })
    await sleep(1200)
    await hub.setState('away')
    const y = hub.inbound('tg.message', { text: 'during away' })
    await sleep(1500)
    expect(channel(b).length).toBe(1)
    await hub.setState('up')
    expect(await waitFor(() => byEvent(b, y).length === 1, 10_000)).toBeTruthy()
    expect(byEvent(b, x).length).toBe(1)
    expect(channel(b).length).toBe(3)
    expect([tries(hub, x), tries(hub, y)]).toEqual([1, 1])
    expect(b.log()).toMatch(/inbound: the hub answers again/)
    // one log line per failure kind, not one per retry
    expect((b.log().match(/inbound: hub answered 503/g) ?? []).length).toBeLessThanOrEqual(2)
  }, 30_000)

  test('a lost deliveries answer: the row is notified once, and the retry with the same rid does not count a second try', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    expect(await waitFor(() => /lease held/.test(b.log()))).toBeTruthy()
    await sleep(200)
    hub.loseAnswers('/tg/deliveries/sent', 1)
    const a = hub.inbound('tg.message', { text: 'lost answer' })
    expect(await waitFor(() => byEvent(b, a).length === 1, 8000)).toBeTruthy()
    await sleep(800)
    expect(byEvent(b, a).length).toBe(1)
    expect(tries(hub, a)).toBe(1)
  }, 20_000)

  test('start-up replay from the hub; across a bridge restart nothing is sent twice', async () => {
    const { home, hub } = await setup()
    const early = hub.inbound('tg.message', { text: 'waiting' })
    const b1 = await start(home, hub)
    expect(await waitFor(() => byEvent(b1, early).length === 1)).toBeTruthy()
    const second = hub.inbound('tg.message', { text: 'second' })
    expect(await waitFor(() => byEvent(b1, second).length === 1)).toBeTruthy()
    hub.receipt(early)
    await b1.close()
    await sleep(300)
    const b2 = await start(home, hub, { TELEGRAM_BRIDGE_REDELIVER_MS: '0' })
    expect(await waitFor(() => /start: /.test(b2.log()))).toBeTruthy()
    await sleep(800)
    // early has a receipt; second was sent once but never receipted: re-sent once as redelivered (receipts work)
    expect(byEvent(b2, early).length).toBe(0)
    expect(byEvent(b2, second).length).toBe(1)
    expect(byEvent(b2, second)[0].params.meta.redelivered).toBe('1')
    expect(tries(hub, second)).toBe(2)
    hub.receipt(second)
    await b2.close()
    await sleep(300)
    const b3 = await start(home, hub, { TELEGRAM_BRIDGE_REDELIVER_MS: '0' })
    expect(await waitFor(() => /start: 0 waiting \+ 0 unreceipted/.test(b3.log()))).toBeTruthy()
    await sleep(500)
    expect(channel(b3).length).toBe(0)
  }, 30_000)

  test('receipts and redelivery: no receipt after REDELIVER_MS → once more, then a bridge.undelivered row in the hub', async () => {
    const { home, hub } = await setup()
    const old = hub.inbound('tg.message', { text: 'old' }, { ts: Date.now() / 1000 - 600 })
    hub.receipt(old) // receipts are known to work
    const b = await start(home, hub, { TELEGRAM_BRIDGE_REDELIVER_MS: '400' })
    expect(await waitFor(() => /lease held/.test(b.log()))).toBeTruthy()
    const a = hub.inbound('tg.message', { text: 'needs a receipt' })
    expect(await waitFor(() => byEvent(b, a).length === 2, 10_000)).toBeTruthy()
    expect(byEvent(b, a)[1].params.meta.redelivered).toBe('1')
    const und = await waitFor(() => hub.db.query("SELECT * FROM events WHERE kind = 'bridge.undelivered'").get() as any, 10_000)
    expect(und).toMatchObject({ source: 'tg-bridge', idem_key: `bridge.undelivered:${a}`, delivery: 'now' })
    expect(JSON.parse(und.payload)).toMatchObject({ event_id: a, kind: 'tg.message', tries: 2 })
    // a receipted row is left alone
    const r = hub.inbound('tg.message', { text: 'receipted' })
    expect(await waitFor(() => byEvent(b, r).length === 1)).toBeTruthy()
    hub.receipt(r)
    await sleep(1500)
    expect(byEvent(b, r).length).toBe(1)
  }, 30_000)

  test('brain-watch\'s redelivery request (local SQLite meta) → rows from the hub once more, the answer in local meta', async () => {
    const { home, hub, local } = await setup()
    const b = await start(home, hub, { TELEGRAM_BRIDGE_REDELIVER_MS: '0' })
    const a = hub.inbound('tg.message', { text: 'ask again' })
    expect(await waitFor(() => byEvent(b, a).length === 1)).toBeTruthy()
    const done = hub.inbound('tg.message', { text: 'has receipt' })
    expect(await waitFor(() => byEvent(b, done).length === 1)).toBeTruthy()
    hub.receipt(done)
    local.query('INSERT INTO meta(k, v) VALUES (?, ?)').run(REDELIVER_KEY, JSON.stringify({ rid: 'bw-1', ids: [a, done, 99999], why: 'test', ts: Date.now() / 1000 }))
    const ack = await waitFor(() => local.query('SELECT v FROM meta WHERE k = ?').get(REDELIVER_ACK_KEY) as any, 8000)
    expect(JSON.parse(ack.v)).toMatchObject({ rid: 'bw-1', sent: [a], skipped: { [done]: 'receipt', 99999: 'unknown' } })
    expect(byEvent(b, a).length).toBe(2)
    expect(byEvent(b, a)[1].params.meta.redelivered).toBe('1')
  }, 20_000)

  test('voice inbox: a hand-off becomes a bridge.handoff row in the hub and comes back as a channel event', async () => {
    const { home, hub } = await setup()
    const port = await new Promise<number>(res => { const s = createServer().listen(0, '127.0.0.1', () => { const p = (s.address() as any).port; s.close(() => res(p)) }) })
    const tokenFile = join(home, 'voice-token')
    writeFileSync(tokenFile, 'tok-123\n', { mode: 0o600 })
    const b = await start(home, hub, { TELEGRAM_VOICE_INBOX_PORT: String(port), TELEGRAM_VOICE_INBOX_TOKEN_FILE: tokenFile })
    expect(await waitFor(() => /voice inbox on 127\.0\.0\.1/.test(b.log()))).toBeTruthy()
    const post = () => fetch(`http://127.0.0.1:${port}/voice`, {
      method: 'POST', headers: { authorization: 'Bearer tok-123', 'content-type': 'application/json' }, body: JSON.stringify({ id: 'v0123abcd', task: 'lights off' }),
    })
    expect((await post()).status).toBe(202)
    expect((await post()).status).toBe(202) // a repeat: one row
    const row = await waitFor(() => hub.db.query("SELECT * FROM events WHERE kind = 'bridge.handoff'").get() as any)
    expect(row).toMatchObject({ source: 'tg-bridge', delivery: 'now', idem_key: 'bridge.handoff:voice:v0123abcd' })
    expect(await waitFor(() => byEvent(b, row.id).length === 1)).toBeTruthy()
    expect((hub.db.query("SELECT COUNT(*) AS c FROM events WHERE kind = 'bridge.handoff'").get() as any).c).toBe(1)
    // the hub away: the caller hears "not delivered" (500), never a false 202
    await hub.setState('away')
    const r = await post()
    expect(r.status).toBe(500)
    await hub.setState('up')
  }, 30_000)

  test('no token file: tools say the hub is unavailable, inbound waits; the token appears: both work', async () => {
    const { home, hub } = await setup()
    rmSync(join(home, '.secrets', 'hub-token'))
    const b = await start(home, hub)
    const r = await call(b, 'reply', { chat_id: '111', text: 'x' })
    expect(text(r)).toMatch(/hub unavailable \(no hub token file\)/)
    const a = hub.inbound('tg.message', { text: 'waits' })
    await sleep(600)
    expect(byEvent(b, a).length).toBe(0)
    writeFileSync(join(home, '.secrets', 'hub-token'), `${hub.token}\n`, { mode: 0o600 })
    expect(await waitFor(() => byEvent(b, a).length === 1, 10_000)).toBeTruthy()
  }, 30_000)
})

describe('hub mode: approval cards (the brain mints, the hub tg only records his tap)', () => {
  const sha = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex')
  const ACTION = 'web:book table 4 at 19:00 (test)'

  test('reply with approval {action, summary, ttl_s}: minted via POST /approvals first, the row names {nonce, action_sha} only', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    const p = call(b, 'reply', { chat_id: '111', text: 'Book it?', approval: { action: ACTION, summary: 'Book the table', ttl_s: 600 } })
    const row = await adapter(hub, 0, { status: 'sent', tg_message_id: 21 })
    expect(text(await p)).toContain('sent (id: 21)')
    expect(hub.approvals.length).toBe(1)
    const a = hub.approvals[0]!
    expect([a.action_sha, a.summary, a.ttl, a.chat]).toEqual([sha(ACTION), 'Book the table', 600, '111'])
    const body = JSON.parse(row.body)
    expect(body.approval).toEqual({ nonce: a.nonce, action_sha: sha(ACTION) })
    expect(row.body).not.toContain(ACTION) // the action string never reaches the hub
    const order = hub.calls.filter(c => c.method === 'POST' && (c.path === '/approvals' || c.path === '/outbox')).map(c => c.path)
    expect(order).toEqual(['/approvals', '/outbox'])
  }, 20_000)

  test('a card naming a nonce made with `approvals new` passes as it is (no second mint)', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    const p = call(b, 'reply', { chat_id: '111', text: 'Approve?', approval: { nonce: 'b'.repeat(32), action: ACTION, summary: 'x' } })
    const row = await adapter(hub, 0, { status: 'sent', tg_message_id: 22 })
    await p
    expect(hub.approvals.length).toBe(0)
    expect(JSON.parse(row.body).approval.nonce).toBe('b'.repeat(32))
  }, 20_000)

  test('a lost mint answer: the retry gets the same nonce (one approval, one row)', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    hub.loseAnswers('/approvals', 1)
    const p = call(b, 'reply', { chat_id: '111', text: 'Book it?', approval: { action: ACTION, summary: 'Book' } })
    const row = await adapter(hub, 0, { status: 'sent', tg_message_id: 23 })
    await p
    expect(hub.approvals.length).toBe(1)
    expect(JSON.parse(row.body).approval.nonce).toBe(hub.approvals[0]!.nonce)
    expect(maxOut(hub)).toBe(1)
  }, 20_000)

  test('no summary: the tool fails, nothing minted, nothing queued', async () => {
    const { home, hub } = await setup()
    const b = await start(home, hub)
    const r = await call(b, 'reply', { chat_id: '111', text: 'x', approval: { action: ACTION } })
    expect(JSON.stringify(r)).toMatch(/summary/)
    expect(hub.approvals.length).toBe(0)
    expect(maxOut(hub)).toBe(0)
  }, 20_000)
})
