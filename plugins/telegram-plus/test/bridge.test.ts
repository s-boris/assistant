// The bridge over a real MCP stdio connection: server.ts as a child process (no TELEGRAM_MODE: the plugin is
// a bridge whatever it says), a temp HOME and a temp events.db (the Phase 0 DDL, test/fixtures/events-v1.sql);
// the test plays tg-adapter (writes inbound rows, marks outbox rows sent/failed) and the receipt writer. No Bot
// API, no token, no Claude Code. Run under the netguard preload (LD_PRELOAD, NETGUARD_LOG are passed through).
import { afterAll, describe, expect, test } from 'bun:test'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { Database } from 'bun:sqlite'
import { execFileSync } from 'child_process'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs'
import { createServer } from 'net'
import { tmpdir } from 'os'
import { join } from 'path'
import { DELIVERIES_DDL } from '../bridgedb.ts'
import { registersUsAsChannel } from '../bridgelease.ts'
import { bridgeTools, INSTRUCTIONS, SET_COMMANDS_DESCRIPTION, TOOLS_0122 } from '../tools.ts'

const DIR = join(import.meta.dir, '..')
const DDL = readFileSync(join(import.meta.dir, 'fixtures', 'events-v1.sql'), 'utf8')
/** What 0.2.0 listed over MCP (bridge mode) and what 0.1.22's poll path listed: tool names, properties and the
 *  instructions stay byte-identical (managed-settings allowlist, pitch-check matcher, evals fake MCP). */
const SURFACE = JSON.parse(readFileSync(join(import.meta.dir, 'fixtures', 'mcp-surface-0.2.0.json'), 'utf8'))
const OLD = JSON.parse(readFileSync(join(import.meta.dir, 'fixtures', 'tools-0.1.22.json'), 'utf8'))
/** A deep copy of a tool list with set_commands' description set to d. */
const withSetCommands = (tools: any[], d: string) => {
  const t = JSON.parse(JSON.stringify(tools))
  t.find((x: any) => x.name === 'set_commands').description = d
  return t
}
const ROOT = mkdtempSync(join(tmpdir(), 'tgp-bridge-'))
const OWNER = '111'
const live: Client[] = []
afterAll(async () => {
  for (const c of live) await c.close().catch(() => {})
  rmSync(ROOT, { recursive: true, force: true })
})

const sleep = (ms: number) => Bun.sleep(ms)
async function waitFor<T>(f: () => T, ms = 4000): Promise<T> {
  const end = Date.now() + ms
  let v = f()
  while (!v && Date.now() < end) { await sleep(25); v = f() }
  return v
}

let homes = 0
/** v2: the store already has the deliveries table (an earlier bridge or core/db.py created it). */
function setup(v2 = false): { home: string; dbPath: string; db: Database } {
  const home = join(ROOT, `h${++homes}`)
  mkdirSync(home, { recursive: true })
  const dbPath = join(home, 'events.db')
  const db = new Database(dbPath, { create: true })
  db.exec('PRAGMA journal_mode=WAL')
  db.exec('PRAGMA busy_timeout=5000')
  db.exec(DDL)
  if (v2) db.exec(DELIVERIES_DDL)
  return { home, dbPath, db }
}

type Running = Client & { notes: any[]; log: () => string; pid: number | null }
async function start(home: string, extra: Record<string, string> = {}, cmd: string[] = ['server.ts']): Promise<Running> {
  const transport = new StdioClientTransport({
    command: process.execPath, args: cmd, cwd: DIR, stderr: 'pipe',
    env: {
      PATH: process.env.PATH!, HOME: home,
      TELEGRAM_BRIDGE_DB: join(home, 'events.db'),
      TELEGRAM_BRIDGE_START_DELAY_MS: '0', TELEGRAM_BRIDGE_POLL_MS: '40', TELEGRAM_BRIDGE_SEND_WAIT_MS: '1500',
      TELEGRAM_BRIDGE_LEASE_RETRY_MS: '150', TELEGRAM_BRIDGE_FORCE: '1', TELEGRAM_STATUS_STATE: join(home, 'board.json'),
      ...(process.env.LD_PRELOAD ? { LD_PRELOAD: process.env.LD_PRELOAD } : {}),
      ...(process.env.NETGUARD_LOG ? { NETGUARD_LOG: process.env.NETGUARD_LOG } : {}),
      ...extra,
    },
  })
  let log = ''
  transport.stderr?.on('data', (d: Buffer) => { log += d.toString() })
  const notes: any[] = []
  const client = new Client({ name: 'bridge-test', version: '0' })
  client.fallbackNotificationHandler = async n => { notes.push(n) }
  await client.connect(transport)
  live.push(client)
  return Object.assign(client, { notes, log: () => log, pid: transport.pid })
}

let upd = 1000
function inbound(db: Database, kind = 'tg.message', payload: Record<string, unknown> = {}, o: { ts?: number; delivery?: string; status?: string } = {}): number {
  const u = upd++
  const p = {
    chat_id: OWNER, message_id: String(u), user: 'owner', user_id: OWNER, ts: new Date().toISOString(),
    event_type: kind === 'tg.message' ? 'message' : kind.slice(3), text: `msg ${u}`, update_id: u, bot: 'testbot', ...payload,
  }
  const r = db.query(`INSERT INTO events(ts, source, kind, subject, payload, labels, delivery, trace_id, idem_key, status)
    VALUES (?, 'tg-adapter', ?, '', ?, '["tg","live"]', ?, ?, ?, ?)`).run(
    o.ts ?? Date.now() / 1000, kind, JSON.stringify(p), o.delivery ?? 'now', `tg:1:${u}`, `tg:1:${u}`, o.status ?? 'new')
  return Number(r.lastInsertRowid)
}
const channel = (b: Running) => b.notes.filter(n => n.method === 'notifications/claude/channel')
const byEvent = (b: Running, id: number) => channel(b).filter(n => n.params.meta.event_id === String(id))
const outbox = (db: Database) => db.query('SELECT * FROM outbox ORDER BY id').all() as any[]
const lastOut = (db: Database) => db.query('SELECT * FROM outbox ORDER BY id DESC LIMIT 1').get() as any
const text = (r: any) => r.content?.[0]?.text as string
/** Play the adapter: wait for the next queued row past `after`, then finish it. */
async function adapter(db: Database, after: number, fields: Record<string, unknown>): Promise<any> {
  const row = await waitFor(() => db.query('SELECT * FROM outbox WHERE id > ? ORDER BY id LIMIT 1').get(after) as any)
  if (!row) throw new Error('no outbox row')
  const keys = Object.keys(fields)
  db.query(`UPDATE outbox SET ${keys.map(k => `${k} = ?`).join(', ')} WHERE id = ?`).run(...keys.map(k => fields[k] as any), row.id)
  return row
}
const maxOut = (db: Database) => (db.query('SELECT COALESCE(MAX(id), 0) AS m FROM outbox').get() as any).m as number
const board = (home: string, mode: 'idle' | 'working', idleAt = Date.now() / 1000 - 3600) =>
  writeFileSync(join(home, 'board.json'), JSON.stringify({ mode, since: idleAt, idle_at: idleAt }))

describe('schema', () => {
  test('the MCP surface is byte-identical to 0.2.0\'s except set_commands\' description: server info, capabilities, instructions, the 11 tools', async () => {
    const { home } = setup()
    const b = await start(home)
    const now = (await b.listTools()).tools
    expect(b.getServerVersion()).toEqual(SURFACE.server)
    expect(b.getServerCapabilities()).toEqual(SURFACE.capabilities) // the channel capability, no permission relay
    expect(b.getInstructions()).toBe(SURFACE.instructions)
    expect(now.length).toBe(11)
    // key order included; the one change is the reworded set_commands description
    expect(JSON.stringify(now)).toBe(JSON.stringify(withSetCommands(SURFACE.tools, SET_COMMANDS_DESCRIPTION)))
    expect(now).toEqual(JSON.parse(JSON.stringify(bridgeTools())))
  }, 30_000)

  test('set_commands\' description names no built-in or instant commands', () => {
    const d = bridgeTools().find(t => t.name === 'set_commands')!.description as string
    expect(d).toBe(SET_COMMANDS_DESCRIPTION)
    expect(d).not.toMatch(/built in|built-in|\/status|\bstart\b|\bhelp\b|instant|pairing|plugin/i)
    expect(d.startsWith('Set the custom entries of the bot\'s "/" command menu in DMs.')).toBe(true)
  })

  test('without the optional additions the tools are 0.1.22\'s, and tools.ts\'s copy is verbatim', () => {
    const old = OLD.tools.find((t: any) => t.name === 'set_commands').description
    const stripped = withSetCommands(bridgeTools(), old)
    const props = (n: string) => stripped.find((t: any) => t.name === n).inputSchema.properties
    for (const n of ['reply', 'send_poll', 'send_location']) { expect(props(n).urgent.type).toBe('boolean'); delete props(n).urgent }
    expect(props('reply').approval.required).toEqual(['action', 'summary'])
    delete props('reply').approval
    for (const n of ['reply', 'edit_message']) {
      const item = props(n).buttons.items.items.anyOf.find((v: any) => v.type === 'object')
      expect(item.properties.bind.required).toEqual(['tool'])
      delete item.properties.bind
    }
    expect(stripped).toEqual(OLD.tools)
    expect(JSON.parse(JSON.stringify(TOOLS_0122))).toEqual(OLD.tools)
    expect(INSTRUCTIONS).toBe(OLD.instructions)
    expect(SURFACE.instructions).toBe(OLD.instructions)
  })

  test('TELEGRAM_MODE unset, "poll" or anything else: always the bridge; the old state dir and its token stay untouched', async () => {
    for (const mode of [undefined, 'poll', 'bridge', 'official', '']) {
      const { home, db } = setup()
      // what the 0.1.22 poll path used: a state dir with a token .env (it chmod'ed it to 0600 at start and wrote
      // bot.pid, health.json and the inbound journal there)
      const state = join(home, 'tgstate')
      mkdirSync(state, { recursive: true })
      writeFileSync(join(state, '.env'), 'TELEGRAM_BOT_TOKEN=123456:TEST-not-a-real-token\n', { mode: 0o644 })
      chmodSync(join(state, '.env'), 0o644)
      const b = await start(home, {
        ...(mode != null ? { TELEGRAM_MODE: mode } : {}), TELEGRAM_STATE_DIR: state, TELEGRAM_BOT_TOKEN: '123456:TEST-not-a-real-token',
      })
      expect((await b.listTools()).tools.length).toBe(11)
      expect(await waitFor(() => /lease held/.test(b.log()))).toBeTruthy()
      expect(b.log()).toMatch(/telegram bridge: \d+\.\d+\.\d+ up \(bot live\); eligible for inbound/)
      const id = inbound(db)
      expect(await waitFor(() => byEvent(b, id).length === 1)).toBeTruthy()
      await b.close()
      expect(readdirSync(state)).toEqual(['.env'])
      expect(statSync(join(state, '.env')).mode & 0o777).toBe(0o644)
    }
  }, 60_000)

  test('the evals fake MCP\'s tool names and properties are a subset of the bridge\'s', () => {
    const real = (() => { try { return readFileSync('/etc/passwd', 'utf8').split('\n').map(l => l.split(':')).find(f => Number(f[2]) === process.getuid!())?.[5] } catch { return undefined } })()
    const fake = process.env.EVALS_FAKE_MCP ?? (real ? join(real, 'bin', 'lib', 'evals', 'fake_mcp.py') : '')
    if (!fake || !existsSync(fake)) return // not on this machine
    const out = execFileSync('python3', ['-I', '-c',
      'import importlib.util,json,sys; s=importlib.util.spec_from_file_location("f", sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.TELEGRAM_TOOLS))',
      fake], { encoding: 'utf8', env: { PATH: process.env.PATH!, HOME: ROOT } })
    const fakeTools = JSON.parse(out) as any[]
    const ours = bridgeTools()
    expect(fakeTools.map(t => t.name).sort()).toEqual(ours.map(t => t.name).sort())
    for (const t of fakeTools) {
      const mine = ours.find(o => o.name === t.name)!.inputSchema.properties
      for (const k of Object.keys(t.inputSchema.properties)) expect(`${t.name}.${k}:${k in mine}`).toBe(`${t.name}.${k}:true`)
    }
  })
})

describe('inbound', () => {
  test('rows → channel notifications in id order with 0.1.22 meta + event_id; deliveries rows; other rows skipped', async () => {
    const { home, db } = setup()
    const b = await start(home)
    await waitFor(() => /lease held/.test(b.log()))
    const a = inbound(db, 'tg.message', { text: 'first', reply_to_message_id: '7', reply_to_text: 'quoted' })
    inbound(db, 'outbox.failed', {}, {}) // not a channel kind
    inbound(db, 'tg.message', {}, { delivery: 'beat' }) // not delivery now
    const c = inbound(db, 'tg.button', { event_type: 'button', callback_data: 'yes', button_text: 'Yes', text: '(pressed button: Yes)', ack_ms: 12 })
    const d = inbound(db, 'tg.reaction', { event_type: 'reaction', reaction: '👍', text: 'reacted 👍' })
    expect(await waitFor(() => channel(b).length >= 3)).toBeTruthy()
    await sleep(200)
    const got = channel(b)
    expect(got.map(n => n.params.meta.event_id)).toEqual([a, c, d].map(String))
    expect(got[0].params.content).toBe('first')
    expect(got[0].params.meta).toEqual({
      chat_id: OWNER, message_id: expect.any(String), user: 'owner', user_id: OWNER, ts: expect.any(String),
      event_type: 'message', reply_to_message_id: '7', reply_to_text: 'quoted', event_id: String(a),
    })
    expect(got[1].params.meta.callback_data).toBe('yes')
    expect('ack_ms' in got[1].params.meta || 'update_id' in got[1].params.meta || 'bot' in got[1].params.meta).toBe(false)
    const dl = db.query('SELECT event_id, tries, receipt_ts FROM deliveries ORDER BY event_id').all() as any[]
    expect(dl.map(r => [r.event_id, r.tries, r.receipt_ts])).toEqual([[a, 1, null], [c, 1, null], [d, 1, null]])
  }, 20_000)

  test('a private scan never reaches the session: no file id, caption or title', async () => {
    const { home, db } = setup()
    const b = await start(home)
    const id = inbound(db, 'tg.message', { text: '/sp tax letter 2026', attachment_kind: 'document', attachment_file_id: 'BQAD-secret', attachment_name: 'letter.pdf' })
    const plain = inbound(db, 'tg.message', { text: '/scan', attachment_kind: 'photo', attachment_file_id: 'AgAD-ok' })
    expect(await waitFor(() => byEvent(b, plain).length === 1)).toBeTruthy()
    const n = byEvent(b, id)[0]
    expect(n.params.content).toMatch(/^\(A private scan .*vault bot\.\)$/)
    expect(n.params.meta).toEqual({ event_type: 'message', handled: 'scan-private-refused', chat_id: OWNER, message_id: expect.any(String), user: 'owner', user_id: OWNER, ts: expect.any(String), event_id: String(id) })
    expect(JSON.stringify(n)).not.toMatch(/tax letter|BQAD-secret|letter\.pdf/)
    expect(byEvent(b, plain)[0].params.meta.attachment_file_id).toBe('AgAD-ok') // a plain "/scan" is a normal message
  }, 20_000)

  test('replay at start: waiting rows go out, unreceipted ones once more when receipts work, the rest stay', async () => {
    const { home, db } = setup(true)
    const now = Date.now() / 1000
    const fresh = inbound(db) // never sent (no bridge was running)
    const sentOnce = inbound(db)
    db.query('INSERT INTO deliveries(event_id, sent_ts, tries) VALUES (?, ?, 1)').run(sentOnce, now - 300)
    const receipted = inbound(db)
    db.query("INSERT INTO deliveries(event_id, sent_ts, tries, receipt_ts, how) VALUES (?, ?, 1, ?, 'channel')").run(receipted, now - 200, now - 199)
    const acked = inbound(db, 'tg.message', {}, { status: 'acked' })
    const old = inbound(db, 'tg.message', {}, { ts: now - 2 * 86400 })
    const twice = inbound(db)
    db.query('INSERT INTO deliveries(event_id, sent_ts, tries) VALUES (?, ?, 2)').run(twice, now - 100)
    const b = await start(home)
    expect(await waitFor(() => channel(b).length >= 2)).toBeTruthy()
    const after = inbound(db) // arrives while running
    expect(await waitFor(() => byEvent(b, after).length === 1)).toBeTruthy()
    await sleep(200)
    expect(channel(b).map(n => n.params.meta.event_id)).toEqual([fresh, sentOnce, after].map(String))
    const again = byEvent(b, sentOnce)[0]
    expect(again.params.meta.redelivered).toBe('1')
    expect(again.params.content).toMatch(/^\(re-sent: .*\)\nmsg \d+$/)
    expect(byEvent(b, fresh)[0].params.meta.redelivered).toBeUndefined()
    for (const id of [receipted, acked, old, twice]) expect(byEvent(b, id).length).toBe(0)
  }, 20_000)

  test('without any receipt yet, start-up replay sends only never-sent rows', async () => {
    const { home, db } = setup(true)
    const sentOnce = inbound(db)
    db.query('INSERT INTO deliveries(event_id, sent_ts, tries) VALUES (?, ?, 1)').run(sentOnce, Date.now() / 1000 - 60)
    const fresh = inbound(db)
    const b = await start(home)
    expect(await waitFor(() => byEvent(b, fresh).length === 1)).toBeTruthy()
    await sleep(300)
    expect(byEvent(b, sentOnce).length).toBe(0)
    expect(b.log()).toMatch(/1 not re-sent \(no receipts seen yet\)/)
  }, 20_000)

  test('receipts: a receipt ends it; none after an idle point → one redelivery, then a bridge.undelivered row', async () => {
    const { home, db } = setup()
    board(home, 'idle')
    const b = await start(home, { TELEGRAM_BRIDGE_REDELIVER_MS: '500' })
    await waitFor(() => /lease held/.test(b.log()))
    const receipt = (id: number) => db.query("UPDATE deliveries SET receipt_ts = ?, how = 'channel' WHERE event_id = ?").run(Date.now() / 1000, id)
    // Until a receipt has been seen, a missing one proves nothing: no redelivery.
    const early = inbound(db)
    expect(await waitFor(() => byEvent(b, early).length === 1)).toBeTruthy()
    await sleep(1500)
    expect(byEvent(b, early).length).toBe(1)
    receipt(early)
    const ok = inbound(db)
    expect(await waitFor(() => byEvent(b, ok).length === 1)).toBeTruthy()
    receipt(ok)
    const lost = inbound(db)
    expect(await waitFor(() => byEvent(b, lost).length === 2, 6000)).toBeTruthy()
    const second = byEvent(b, lost)[1]
    expect(second.params.meta.redelivered).toBe('1')
    expect(second.params.meta.event_id).toBe(String(lost))
    const und = await waitFor(() => db.query("SELECT * FROM events WHERE kind = 'bridge.undelivered'").get() as any, 6000)
    expect(und).toBeTruthy()
    expect(und.source).toBe('tg-bridge')
    expect(und.delivery).toBe('now')
    expect(JSON.parse(und.payload)).toEqual({ event_id: lost, kind: 'tg.message', tries: 2, age_s: expect.any(Number) })
    expect(und.subject).not.toMatch(/msg/) // ids and kind only, never text
    expect(byEvent(b, ok).length).toBe(1)
    expect((db.query('SELECT tries FROM deliveries WHERE event_id = ?').get(lost) as any).tries).toBe(2)
  }, 30_000)

  test('a busy turn holds redelivery until the session is idle again', async () => {
    const { home, db } = setup(true)
    const now = Date.now() / 1000
    const prior = inbound(db)
    db.query("INSERT INTO deliveries(event_id, sent_ts, tries, receipt_ts, how) VALUES (?, ?, 1, ?, 'channel')").run(prior, now - 30, now - 29)
    board(home, 'working')
    const b = await start(home, { TELEGRAM_BRIDGE_REDELIVER_MS: '400' })
    const id = inbound(db)
    expect(await waitFor(() => byEvent(b, id).length === 1)).toBeTruthy()
    await sleep(1500)
    expect(byEvent(b, id).length).toBe(1)
    board(home, 'idle', Date.now() / 1000)
    expect(await waitFor(() => byEvent(b, id).length === 2, 5000)).toBeTruthy()
  }, 20_000)

  test('brain-watch\'s redelivery request: rows without a receipt once more, the answer in meta, each request once', async () => {
    const { home, db } = setup(true)
    board(home, 'working') // the bridge's own redelivery stays out of this
    const meta = (k: string) => { const r = db.query('SELECT v FROM meta WHERE k = ?').get(k) as any; return r ? JSON.parse(r.v) : null }
    const ask = (rid: string, ids: number[]) => db.query(`INSERT INTO meta(k, v) VALUES ('bridge.redeliver', ?)
      ON CONFLICT(k) DO UPDATE SET v = excluded.v`).run(JSON.stringify({ rid, ids, why: 'stalled', ts: Date.now() / 1000 }))
    const b = await start(home)
    await waitFor(() => /lease held/.test(b.log()))
    expect(JSON.parse(readFileSync(join(home, '.local/state/tg-bridge/lease'), 'utf8')).redeliver).toBe(1)
    const lost = inbound(db)
    const got = inbound(db)
    expect(await waitFor(() => byEvent(b, lost).length === 1 && byEvent(b, got).length === 1)).toBeTruthy()
    db.query("UPDATE deliveries SET receipt_ts = ?, how = 'channel' WHERE event_id = ?").run(Date.now() / 1000, got)
    ask('r1', [lost, got, 999999])
    const ack = await waitFor(() => meta('bridge.redeliver.ack'), 5000)
    expect(ack).toMatchObject({ rid: 'r1', sent: [lost], skipped: { [got]: 'receipt', 999999: 'unknown' }, version: expect.any(String) })
    const two = byEvent(b, lost)
    expect(two.length).toBe(2)
    expect(two[1].params.meta.redelivered).toBe('1')
    expect(two[1].params.content).toMatch(/^\(re-sent: no sign it reached you/)
    expect(b.log()).toMatch(/redeliver r1 \(stalled\): sent #\d+; skipped/)
    await sleep(2200)
    expect(byEvent(b, lost).length).toBe(2) // each request once
    await b.close()
    // A bridge started later never takes an answered request again; a new one it does, up to the tries cap.
    const b2 = await start(home)
    await waitFor(() => /lease held/.test(b2.log()))
    await sleep(2200)
    expect(byEvent(b2, lost).length).toBe(0) // sent twice already: not part of the start-up replay either
    ask('r2', [lost])
    expect(await waitFor(() => byEvent(b2, lost).length === 1, 5000)).toBeTruthy()
    expect((await waitFor(() => meta('bridge.redeliver.ack')?.rid === 'r2' && meta('bridge.redeliver.ack'), 3000)).sent).toEqual([lost])
    db.query('UPDATE deliveries SET tries = 5 WHERE event_id = ?').run(lost)
    ask('r3', [lost])
    expect((await waitFor(() => meta('bridge.redeliver.ack')?.rid === 'r3' && meta('bridge.redeliver.ack'), 5000)).skipped).toEqual({ [lost]: 'tries' })
    expect(byEvent(b2, lost).length).toBe(1)
  }, 30_000)
})

describe('tools → outbox', () => {
  test('reply: one row (bot live, body = arguments, never held or deduped, reply_to_event) and 0.1.22 results', async () => {
    const { home, db } = setup()
    const b = await start(home)
    await waitFor(() => /lease held/.test(b.log()))
    const ev = inbound(db)
    await waitFor(() => byEvent(b, ev).length === 1)
    let m = maxOut(db)
    let call = b.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: 'hi', format: 'rich', buttons: [['Yes', { text: 'No', data: 'n', bind: { tool: 'x' } }]], urgent: true } })
    const row = await adapter(db, m, { status: 'sent', tg_message_id: 501, sent_ts: Date.now() / 1000 })
    expect(text(await call)).toBe('sent (id: 501)')
    const evTrace = (db.query('SELECT trace_id FROM events WHERE id = ?').get(ev) as any).trace_id
    expect(row).toMatchObject({ bot: 'live', chat: OWNER, op: 'send', mode: 'live', urgent: 1, reply_to_event: ev, trace_id: evTrace })
    expect(JSON.parse(row.body)).toEqual({
      text: 'hi', format: 'rich', buttons: [['Yes', { text: 'No', data: 'n', bind: { tool: 'x' } }]], urgent: true, hold: false, dedupe: false,
    })
    expect(row.idem_key).toMatch(/^bridge:/)
    // several parts and notes from the adapter's result
    m = maxOut(db)
    call = b.callTool({ name: 'reply', arguments: { chat_id: Number(OWNER), text: 'long' } })
    await adapter(db, m, { status: 'sent', tg_message_id: 7, result: JSON.stringify({ ids: [6, 7], notes: ['sent as plain text'] }) })
    expect(text(await call)).toBe('sent 2 parts (ids: 6, 7)\nnote: sent as plain text')
    // the Phase 0 adapter's shape: last id + "note: …" in last_error
    m = maxOut(db)
    call = b.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: 'x' } })
    await adapter(db, m, { status: 'sent', tg_message_id: 8, last_error: 'note: formatting rejected' })
    expect(text(await call)).toBe('sent (id: 8)\nnote: formatting rejected')
    // failure → the tool's error, as 0.1.22
    m = maxOut(db)
    call = b.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: 'x' } })
    await adapter(db, m, { status: 'failed', last_error: 'chat 111 is not allowlisted' })
    const failed: any = await call
    expect(failed.isError).toBe(true)
    expect(text(failed)).toBe('reply failed: chat 111 is not allowlisted')
    // the adapter doesn't take it in time → queued, with the reason it gives
    m = maxOut(db)
    call = b.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: 'later' } })
    await adapter(db, m, { last_error: 'retry after 429 (12 s)' })
    expect(text(await call)).toMatch(/^queued #\d+ \(held: retry after 429 \(12 s\)\)$/)
    expect(lastOut(db).status).toBe('queued')
  }, 30_000)

  test('reply checks: empty text, private files; a reply with no recent event carries no reply_to_event', async () => {
    const { home, db } = setup()
    const b = await start(home)
    const r1: any = await b.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: '' } })
    expect(text(r1)).toBe('reply failed: text is empty and no files given')
    mkdirSync(join(home, '.secrets'), { recursive: true })
    writeFileSync(join(home, '.secrets', 'x.env'), 'X=1\n')
    mkdirSync(join(home, '.claude', 'channels', 'telegram'), { recursive: true })
    writeFileSync(join(home, '.claude', 'channels', 'telegram', 'access.json'), '{}')
    for (const f of [join(home, '.secrets', 'x.env'), join(home, '.claude', 'channels', 'telegram', 'access.json')]) {
      const r: any = await b.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: 'f', files: [f] } })
      expect(text(r)).toMatch(/^reply failed: refusing to send a file from a private directory/)
    }
    const r3: any = await b.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: 'f', files: ['relative.png'] } })
    expect(text(r3)).toMatch(/not an absolute path/)
    expect(outbox(db).length).toBe(0)
    const ok = join(home, 'pic.png')
    writeFileSync(ok, 'png')
    const m = maxOut(db)
    const call = b.callTool({ name: 'reply', arguments: { chat_id: '222', text: 'f', files: [ok] } })
    const row = await adapter(db, m, { status: 'sent', tg_message_id: 3 })
    expect(text(await call)).toBe('sent (id: 3)')
    expect(JSON.parse(row.files)).toEqual([ok])
    expect(JSON.parse(row.body).files).toEqual([ok])
    expect(row.reply_to_event).toBeNull()
  }, 20_000)

  test('the other tools: ops, checks and result texts', async () => {
    const { home, db } = setup()
    const b = await start(home)
    const run = async (name: string, args: Record<string, unknown>, fields: Record<string, unknown> = { status: 'sent', tg_message_id: 9 }) => {
      const m = maxOut(db)
      const call = b.callTool({ name, arguments: args })
      const row = await adapter(db, m, fields)
      return { row, r: (await call) as any, body: JSON.parse(row.body) }
    }
    let x = await run('react', { chat_id: OWNER, message_id: '5', emoji: '👍' })
    expect([x.row.op, text(x.r), x.body.emoji]).toEqual(['react', 'reacted 👍', '👍'])
    const bad: any = await b.callTool({ name: 'react', arguments: { chat_id: OWNER, message_id: '5', emoji: '✅' } })
    expect(text(bad)).toMatch(/^react failed: not in Telegram's reaction whitelist: ✅/)
    x = await run('edit_message', { chat_id: OWNER, message_id: '5', buttons: [] })
    expect([x.row.op, text(x.r)]).toEqual(['edit', 'edited buttons (id: 5)'])
    const noEdit: any = await b.callTool({ name: 'edit_message', arguments: { chat_id: OWNER, message_id: '5' } })
    expect(text(noEdit)).toBe('edit_message failed: pass text and/or buttons')
    x = await run('send_poll', { chat_id: OWNER, question: 'Q?', options: ['a', 'b'], multiple: true })
    expect([x.row.op, text(x.r), x.body.multiple]).toEqual(['poll', 'poll sent (id: 9)', true])
    const fewOptions: any = await b.callTool({ name: 'send_poll', arguments: { chat_id: OWNER, question: 'Q?', options: ['a'] } })
    expect(text(fewOptions)).toBe('send_poll failed: 2-12 options required')
    x = await run('send_location', { chat_id: OWNER, latitude: 1.5, longitude: 2.5 })
    expect([x.row.op, text(x.r)]).toEqual(['location', 'sent (id: 9)'])
    x = await run('pin_message', { chat_id: OWNER, message_id: '5', unpin: true })
    expect([x.row.op, text(x.r)]).toEqual(['pin', 'unpinned'])
    x = await run('delete_message', { chat_id: OWNER, message_id: '5' })
    expect([x.row.op, text(x.r)]).toEqual(['delete', 'deleted'])
    x = await run('draft', { chat_id: OWNER, text: 'working…' })
    expect([x.row.op, text(x.r)]).toEqual(['draft', 'draft updated (8 chars)'])
    x = await run('draft', { chat_id: OWNER, clear: true }, { status: 'sent', result: JSON.stringify({ text: 'no active draft' }) })
    expect([x.body, text(x.r)]).toEqual([{ clear: true, hold: false, dedupe: false }, 'no active draft'])
    x = await run('chat_action', { chat_id: OWNER, action: 'upload_photo' })
    expect([x.row.op, text(x.r)]).toEqual(['chat_action', 'showing upload_photo'])
    const badAction: any = await b.callTool({ name: 'chat_action', arguments: { chat_id: OWNER, action: 'dance' } })
    expect(text(badAction)).toBe('chat_action failed: unknown action: dance')
    x = await run('set_commands', { commands: [{ command: '/Space', description: 'Disk space' }] })
    expect([x.row.op, x.row.chat, text(x.r)]).toEqual(['commands', '', 'commands set: /space'])
    expect(x.body.commands).toEqual([{ command: 'space', description: 'Disk space' }])
    // no built-in names any more: start, help and status are ordinary custom entries
    x = await run('set_commands', { commands: [{ command: 'status', description: 'x' }, { command: '/start', description: 'y' }, { command: 'help', description: 'z' }] })
    expect([x.row.op, text(x.r)]).toEqual(['commands', 'commands set: /status /start /help'])
    expect(x.body.commands.map((c: any) => c.command)).toEqual(['status', 'start', 'help'])
    const badName: any = await b.callTool({ name: 'set_commands', arguments: { commands: [{ command: 'no-dash', description: 'x' }] } })
    expect(text(badName)).toBe('set_commands failed: invalid command name: no-dash')
    x = await run('download_attachment', { file_id: 'AgAD-1' }, { status: 'sent', result: JSON.stringify({ path: '/tmp/inbox/a.jpg' }) })
    expect([x.row.op, x.row.chat, x.body.file_id, text(x.r)]).toEqual(['download', '', 'AgAD-1', '/tmp/inbox/a.jpg'])
    x = await run('download_attachment', { file_id: 'AgAD-2' }, { status: 'sent', files: JSON.stringify(['/tmp/inbox/b.ogg']) })
    expect(text(x.r)).toBe('/tmp/inbox/b.ogg')
    const noChat: any = await b.callTool({ name: 'reply', arguments: { text: 'x' } })
    expect(text(noChat)).toBe('reply failed: chat_id is required')
    const unknown: any = await b.callTool({ name: 'nope', arguments: {} })
    expect([unknown.isError, text(unknown)]).toEqual([true, 'unknown tool: nope'])
  }, 30_000)

  test('a missing events store: tools say so, and the bridge picks the store up once it exists', async () => {
    const home = join(ROOT, `h${++homes}`)
    mkdirSync(home, { recursive: true })
    const b = await start(home)
    const r: any = await b.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: 'x' } })
    expect(text(r)).toMatch(/^reply failed: events store missing: /)
    const db = new Database(join(home, 'events.db'), { create: true })
    db.exec('PRAGMA journal_mode=WAL')
    db.exec(DDL)
    const id = inbound(db)
    expect(await waitFor(() => byEvent(b, id).length === 1, 5000)).toBeTruthy()
  }, 20_000)
})

describe('one bridge', () => {
  test('lease: one bridge takes inbound, a second serves tools only and takes over when the first is killed', async () => {
    const { home, db } = setup()
    const a = await start(home)
    expect(await waitFor(() => /lease held/.test(a.log()))).toBeTruthy()
    const lease = JSON.parse(readFileSync(join(home, '.local', 'state', 'tg-bridge', 'lease'), 'utf8'))
    expect(lease).toMatchObject({ pid: a.pid, mode: 'bridge', bot: 'live' })
    const b = await start(home)
    expect(await waitFor(() => /another bridge holds the lease/.test(b.log()))).toBeTruthy()
    const e1 = inbound(db)
    expect(await waitFor(() => byEvent(a, e1).length === 1)).toBeTruthy()
    await sleep(300)
    expect(channel(b).length).toBe(0)
    const m = maxOut(db)
    const call = b.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: 'from the tools-only bridge' } })
    await adapter(db, m, { status: 'sent', tg_message_id: 11 })
    expect(text(await call)).toBe('sent (id: 11)')
    process.kill(a.pid!, 'SIGKILL') // the kernel drops the flock with the process
    expect(await waitFor(() => /lease held/.test(b.log()), 5000)).toBeTruthy()
    const e2 = inbound(db)
    expect(await waitFor(() => byEvent(b, e2).length === 1)).toBeTruthy()
    expect(byEvent(b, e1).length).toBe(0) // a's row: sent by a, no receipts seen yet → not re-sent
  }, 30_000)

  test('vetoed (not the session\'s channel): tools only, no lease, no inbound', async () => {
    const { home, db } = setup()
    const b = await start(home, { TELEGRAM_BRIDGE_TOOLS_ONLY: '1' })
    const id = inbound(db)
    await sleep(800)
    expect(byEvent(b, id).length).toBe(0)
    expect(b.log()).toMatch(/tools only: TELEGRAM_BRIDGE_TOOLS_ONLY=1/)
    expect(existsSync(join(home, '.local', 'state', 'tg-bridge', 'lease'))).toBe(false)
  }, 20_000)

  test('channel registration check (lifted from 0.1.22)', () => {
    expect(registersUsAsChannel(['claude', '--channels', 'plugin:telegram-plus@home-plugins'])).toBe(true)
    expect(registersUsAsChannel(['claude', '--channels=plugin:telegram-plus@home-plugins'])).toBe(true)
    expect(registersUsAsChannel(['claude', '--dangerously-load-development-channels', 'plugin:telegram-plus@x'])).toBe(true)
    expect(registersUsAsChannel(['claude', '-p', 'hi'])).toBe(false)
    expect(registersUsAsChannel(['claude', '--channels', 'plugin:telegram@claude-plugins-official'])).toBe(false)
  })
})

describe('voice inbox', () => {
  test('a hand-off becomes a bridge.handoff row and the usual via=voice channel event', async () => {
    const { home, db } = setup()
    const port = await new Promise<number>(res => { const s = createServer().listen(0, '127.0.0.1', () => { const p = (s.address() as any).port; s.close(() => res(p)) }) })
    const tokenFile = join(home, 'voice-token')
    writeFileSync(tokenFile, 'tok-123\n', { mode: 0o600 })
    const b = await start(home, { TELEGRAM_VOICE_INBOX_PORT: String(port), TELEGRAM_VOICE_INBOX_TOKEN_FILE: tokenFile })
    expect(await waitFor(() => /voice inbox on 127\.0\.0\.1/.test(b.log()))).toBeTruthy()
    const post = (body: unknown, token = 'tok-123') => fetch(`http://127.0.0.1:${port}/voice`, {
      method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(body),
    })
    expect((await post({ id: 'v0123abcd', task: 'turn off the lights' }, 'wrong')).status).toBe(401)
    expect((await post({ id: 'v0123abcd', task: 'turn off the lights' })).status).toBe(202)
    expect((await post({ id: 'v0123abcd', task: 'turn off the lights' })).status).toBe(202) // a repeat: one row
    const row = await waitFor(() => db.query("SELECT * FROM events WHERE kind = 'bridge.handoff'").get() as any)
    expect(row).toMatchObject({ source: 'tg-bridge', delivery: 'now', idem_key: 'bridge.handoff:voice:v0123abcd' })
    expect(await waitFor(() => byEvent(b, row.id).length === 1)).toBeTruthy()
    const n = byEvent(b, row.id)[0]
    expect(n.params.content).toMatch(/^🗣️ \(live voice, now\) turn off the lights\n/)
    expect(n.params.meta).toMatchObject({ chat_id: 'voice', via: 'voice', voice_id: 'v0123abcd', event_id: String(row.id) })
    await sleep(300)
    expect((db.query("SELECT COUNT(*) AS c FROM events WHERE kind = 'bridge.handoff'").get() as any).c).toBe(1)
    expect((await post({ source: 'app', id: 'm_01JAAAAAAAAAAAAAAAAAAAAAAA', text: 'hi from the app' })).status).toBe(202)
    const app = await waitFor(() => channel(b).find(x => x.params.meta.via === 'app'))
    expect(app.params.meta).toMatchObject({ chat_id: 'app', app_msg_id: 'm_01JAAAAAAAAAAAAAAAAAAAAAAA' })
  }, 20_000)
})
