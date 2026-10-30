// Offline end-to-end scenario: server.ts started the way Claude Code starts the plugin (no TELEGRAM_MODE, a bot
// token in the env, the old state dir with a token .env beside it), driven over stdio as an MCP client. The script
// plays tg-adapter (inbound rows in the events store, outbox rows marked sent) and the receipt writer, and serves a
// mock Bot API on 127.0.0.1 that the bridge must never call. Temp HOME and store; never touches the real state dir,
// a real token, api.telegram.org or a live adapter.   Run:  bun test/offline.ts   (exit 0 = all passed)
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { Database } from 'bun:sqlite'
import { spawnSync } from 'child_process'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs'
import { createServer } from 'net'
import { tmpdir } from 'os'
import { join } from 'path'

const DIR = join(import.meta.dir, '..')
const OWNER = '111'
const ROOT = mkdtempSync(join(tmpdir(), 'tgp-offline-'))
const HOME = join(ROOT, 'home')
const STATE = join(ROOT, 'tgstate') // what the poll path used (TELEGRAM_STATE_DIR): must stay untouched
const DB = join(HOME, '.local', 'state', 'assistant', 'events.db')
const BOARD = join(HOME, 'board.json')
const LEASE = join(HOME, '.local', 'state', 'tg-bridge', 'lease')
const SURFACE = JSON.parse(readFileSync(join(import.meta.dir, 'fixtures', 'mcp-surface-0.2.0.json'), 'utf8'))
const VERSION = JSON.parse(readFileSync(join(DIR, 'package.json'), 'utf8')).version

let passed = 0
let failed = 0
function ok(name: string, cond: unknown, detail: unknown = ''): void {
  if (cond) { passed++; console.log(`  ok   ${name}`) } else { failed++; console.log(`  FAIL ${name}${detail !== '' ? `  [${typeof detail === 'string' ? detail : JSON.stringify(detail)}]` : ''}`) }
}
const sleep = (ms: number) => Bun.sleep(ms)
async function waitFor<T>(f: () => T, ms = 6000): Promise<T> {
  const end = Date.now() + ms
  let v = f()
  while (!v && Date.now() < end) { await sleep(25); v = f() }
  return v
}
const freePort = () => new Promise<number>(res => { const s = createServer().listen(0, '127.0.0.1', () => { const p = (s.address() as any).port; s.close(() => res(p)) }) })

// ---- the scratch world
mkdirSync(join(HOME, '.local', 'state', 'assistant'), { recursive: true })
mkdirSync(STATE, { recursive: true })
writeFileSync(join(STATE, '.env'), 'TELEGRAM_BOT_TOKEN=123456:TEST-not-a-real-token\n', { mode: 0o644 })
chmodSync(join(STATE, '.env'), 0o644)
const db = new Database(DB, { create: true })
db.exec('PRAGMA journal_mode=WAL')
db.exec('PRAGMA busy_timeout=5000')
db.exec(readFileSync(join(import.meta.dir, 'fixtures', 'events-v1.sql'), 'utf8'))
const idle = (at = Date.now() / 1000 - 600) => writeFileSync(BOARD, JSON.stringify({ mode: 'idle', since: at, idle_at: at }))
idle()
const voiceToken = join(ROOT, 'voice-token')
writeFileSync(voiceToken, 'tok-offline\n', { mode: 0o600 })
const voicePort = await freePort()
const botCalls: string[] = []
const bot = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(req) { botCalls.push(new URL(req.url).pathname); return Response.json({ ok: true, result: [] }) } })

let upd = 5000
function inbound(kind = 'tg.message', payload: Record<string, unknown> = {}): number {
  const u = upd++
  const p = { chat_id: OWNER, message_id: String(u), user: 'owner', user_id: OWNER, ts: new Date().toISOString(),
    event_type: kind === 'tg.message' ? 'message' : kind.slice(3), text: `msg ${u}`, update_id: u, bot: 'testbot', ...payload }
  const r = db.query(`INSERT INTO events(ts, source, kind, subject, payload, labels, delivery, trace_id, idem_key, status)
    VALUES (?, 'tg-adapter', ?, '', ?, '["tg","live"]', 'now', ?, ?, 'new')`).run(Date.now() / 1000, kind, JSON.stringify(p), `tg:1:${u}`, `tg:1:${u}`)
  return Number(r.lastInsertRowid)
}
const maxOut = () => (db.query('SELECT COALESCE(MAX(id), 0) AS m FROM outbox').get() as any).m as number
/** Play the adapter: wait for the next outbox row past `after`, then finish it with `fields`. */
async function adapter(after: number, fields: Record<string, unknown>): Promise<any> {
  const row = await waitFor(() => db.query('SELECT * FROM outbox WHERE id > ? ORDER BY id LIMIT 1').get(after) as any)
  if (!row) return null
  const keys = Object.keys(fields)
  db.query(`UPDATE outbox SET ${keys.map(k => `${k} = ?`).join(', ')} WHERE id = ?`).run(...keys.map(k => fields[k] as any), row.id)
  return row
}
const text = (r: any) => String(r?.content?.[0]?.text ?? '')

// ---- start it as Claude Code would (cwd = the plugin root, no TELEGRAM_MODE)
const env: Record<string, string> = {
  PATH: process.env.PATH!, HOME, CLAUDE_PLUGIN_ROOT: DIR,
  TELEGRAM_BOT_TOKEN: '123456:TEST-not-a-real-token', TELEGRAM_STATE_DIR: STATE, TELEGRAM_API_ROOT: `http://127.0.0.1:${bot.port}`,
  TELEGRAM_BRIDGE_FORCE: '1', TELEGRAM_BRIDGE_START_DELAY_MS: '0', TELEGRAM_BRIDGE_POLL_MS: '50', TELEGRAM_BRIDGE_SEND_WAIT_MS: '3000',
  TELEGRAM_BRIDGE_REDELIVER_MS: '600', TELEGRAM_STATUS_STATE: BOARD,
  TELEGRAM_VOICE_INBOX_PORT: String(voicePort), TELEGRAM_VOICE_INBOX_TOKEN_FILE: voiceToken,
  ...(process.env.LD_PRELOAD ? { LD_PRELOAD: process.env.LD_PRELOAD } : {}),
  ...(process.env.NETGUARD_LOG ? { NETGUARD_LOG: process.env.NETGUARD_LOG } : {}),
}
const transport = new StdioClientTransport({ command: process.execPath, args: ['server.ts'], cwd: DIR, env, stderr: 'pipe' })
let log = ''
transport.stderr?.on('data', (d: Buffer) => { log += d.toString() })
const notes: any[] = []
const client = new Client({ name: 'telegram-plus-offline', version: '0' })
client.fallbackNotificationHandler = async n => { notes.push(n) }
const exitCode = (async () => { try { await client.connect(transport) } catch (e) { console.log(`connect failed: ${e}\n${log}`); process.exit(1) } })()
await exitCode
const pid = transport.pid!
const channel = () => notes.filter(n => n.method === 'notifications/claude/channel')
const byEvent = (id: number) => channel().filter(n => n.params.meta.event_id === String(id))

try {
  console.log('# start')
  ok('server "telegram", the channel capability, no permission relay', JSON.stringify(client.getServerVersion()) === JSON.stringify(SURFACE.server)
    && JSON.stringify(client.getServerCapabilities()) === JSON.stringify(SURFACE.capabilities), client.getServerCapabilities())
  const tools = (await client.listTools()).tools
  ok('11 tools with their 0.2.0 names, instructions unchanged', tools.length === 11 && tools.map(t => t.name).join() === SURFACE.tools.map((t: any) => t.name).join()
    && client.getInstructions() === SURFACE.instructions)
  ok(`bridge ${VERSION} up without TELEGRAM_MODE, takes inbound (lease held)`, await waitFor(() => /lease held/.test(log)) && log.includes(`telegram bridge: ${VERSION} up (bot live); eligible for inbound`), log.slice(0, 300))
  ok('voice inbox listening on 127.0.0.1', await waitFor(() => /voice inbox on 127\.0\.0\.1/.test(log)), log.slice(-200))

  console.log('# inbound, receipts, redelivery')
  const t1 = inbound('tg.message', { text: 'hello', reply_to_message_id: '7', reply_to_text: 'quoted' })
  const n1 = await waitFor(() => byEvent(t1)[0])
  ok('owner text → channel event: content, 0.1.22 meta + event_id (no update_id/bot)', n1?.params.content === 'hello' && n1.params.meta.chat_id === OWNER
    && n1.params.meta.reply_to_text === 'quoted' && n1.params.meta.event_id === String(t1) && !('update_id' in n1.params.meta) && !('bot' in n1.params.meta), n1?.params)
  ok('  deliveries row: tries 1, no receipt yet', (db.query('SELECT tries, receipt_ts FROM deliveries WHERE event_id = ?').get(t1) as any)?.tries === 1)
  db.query("UPDATE deliveries SET receipt_ts = ?, how = 'channel' WHERE event_id = ?").run(Date.now() / 1000, t1) // the agenda hook's receipt
  const t2 = inbound('tg.message', { text: 'lost on the way' })
  ok('a row without a receipt is sent once more after the idle window (redelivered="1")', await waitFor(() => byEvent(t2).length === 2, 8000)
    && byEvent(t2)[1].params.meta.redelivered === '1' && /^\(re-sent: /.test(byEvent(t2)[1].params.content))
  const und = await waitFor(() => db.query("SELECT * FROM events WHERE kind = 'bridge.undelivered'").get() as any, 8000)
  ok('  still none: one bridge.undelivered row (ids and kind only)', und && JSON.parse(und.payload).event_id === t2 && !/lost on the way/.test(und.subject + und.payload))
  ok('  the receipted row was never re-sent', byEvent(t1).length === 1)
  db.query("UPDATE deliveries SET receipt_ts = ?, how = 'channel' WHERE event_id = ?").run(Date.now() / 1000, t2)
  const b1 = inbound('tg.button', { event_type: 'button', callback_data: 'p10:yes', button_text: 'Yes', text: '(pressed button: Yes)', ack_ms: 9 })
  const r1 = inbound('tg.reaction', { event_type: 'reaction', reaction: '👍', text: 'reacted 👍' })
  const nb = await waitFor(() => byEvent(b1)[0]), nr = await waitFor(() => byEvent(r1)[0])
  ok('button press and reaction → channel events in id order (no ack_ms in meta)', nb?.params.meta.callback_data === 'p10:yes' && nr?.params.meta.reaction === '👍'
    && !('ack_ms' in nb.params.meta) && channel().findIndex(n => n === nb) < channel().findIndex(n => n === nr))
  const sp = inbound('tg.message', { text: '/sp tax letter', attachment_kind: 'document', attachment_file_id: 'BQAD-secret', attachment_name: 'letter.pdf' })
  const nsp = await waitFor(() => byEvent(sp)[0])
  ok('a private scan reaches the session only as a refusal note (no file id, caption or title)', nsp?.params.meta.handled === 'scan-private-refused'
    && !/tax letter|BQAD-secret|letter\.pdf/.test(JSON.stringify(nsp)))
  for (const id of [b1, r1, sp]) db.query("UPDATE deliveries SET receipt_ts = ?, how = 'channel' WHERE event_id = ?").run(Date.now() / 1000, id)

  console.log('# tools → outbox')
  let m = maxOut()
  let call = client.callTool({ name: 'reply', arguments: { chat_id: OWNER, text: 'hi there', format: 'rich' } })
  let row = await adapter(m, { status: 'sent', tg_message_id: 901, sent_ts: Date.now() / 1000 })
  let res: any = await call
  ok('reply → one outbox row (bot live, op send, never held or deduped, reply_to_event), answered as 0.1.22', text(res) === 'sent (id: 901)'
    && row?.bot === 'live' && row.op === 'send' && row.chat === OWNER && row.reply_to_event === sp
    && JSON.parse(row.body).hold === false && JSON.parse(row.body).dedupe === false, { answer: text(res), row })
  m = maxOut()
  call = client.callTool({ name: 'download_attachment', arguments: { file_id: 'AgAD-photo' } })
  row = await adapter(m, { status: 'sent', result: JSON.stringify({ path: '/tmp/inbox/photo.jpg' }) })
  res = await call
  ok('download_attachment → op download, the adapter\'s path', row?.op === 'download' && text(res) === '/tmp/inbox/photo.jpg', text(res))
  m = maxOut()
  call = client.callTool({ name: 'set_commands', arguments: { commands: [{ command: 'status', description: 'What I am working on' }, { command: 'night', description: 'Night log' }] } })
  row = await adapter(m, { status: 'sent' })
  res = await call
  ok('set_commands: no built-in names, /status is an ordinary entry', row?.op === 'commands' && text(res) === 'commands set: /status /night', text(res))
  const bad: any = await client.callTool({ name: 'react', arguments: { chat_id: OWNER, message_id: '5', emoji: '✅' } })
  ok('react checks the whitelist before any row', bad.isError === true && /reaction whitelist/.test(text(bad)))

  console.log('# voice inbox')
  const post = await fetch(`http://127.0.0.1:${voicePort}/voice`, { method: 'POST', headers: { authorization: 'Bearer tok-offline', 'content-type': 'application/json' },
    body: JSON.stringify({ id: 'v0ff1ce00', task: 'lights off', urgency: 'now' }) })
  const ho = await waitFor(() => db.query("SELECT * FROM events WHERE kind = 'bridge.handoff'").get() as any)
  const nho = ho ? await waitFor(() => byEvent(ho.id)[0]) : null
  ok('a hand-off → bridge.handoff row → channel event via=voice', post.status === 202 && nho?.params.meta.via === 'voice' && /lights off/.test(nho.params.content))

  console.log('# no poll path')
  ok('the mock Bot API got no request at all', botCalls.length === 0, botCalls)
  ok('the old state dir is untouched: only its .env, mode still 0644 (never read or chmod\'ed)', readdirSync(STATE).join() === '.env'
    && (statSync(join(STATE, '.env')).mode & 0o777) === 0o644, readdirSync(STATE))
  const found: string[] = []
  const walk = (d: string) => { for (const f of readdirSync(d, { withFileTypes: true })) { const p = join(d, f.name); if (f.isDirectory()) walk(p); else found.push(p.slice(HOME.length + 1)) } }
  walk(HOME)
  const pollFiles = found.filter(f => /(^|\/)(bot\.pid|health\.json|inbound-journal\.jsonl|replay-request\.json|delivery-alert\.json|reactions\.json|instant\.json|callbacks\.json|buttons\.json)$/.test(f))
  ok('no poll-path files anywhere in HOME (bot.pid, health, journal, replay, alert, instant/reaction tables)', pollFiles.length === 0, pollFiles)
  const ours = found.filter(f => !f.startsWith('.bun/')) // bun's own transpiler cache is the runtime's, not the plugin's
  ok('  HOME holds only the store, the board and the lease (besides bun\'s cache)', ours.every(f => /^(\.local\/state\/assistant\/events\.db(-wal|-shm)?|board\.json|\.local\/state\/tg-bridge\/lease)$/.test(f)), ours)

  console.log('# shutdown')
  await client.close()
  const gone = await waitFor(() => { try { process.kill(pid, 0); return false } catch { return true } }, 5000)
  ok('stdin closed → the bridge exits', gone)
  ok('  the lease is free again', existsSync(LEASE) && spawnSync('flock', ['-n', LEASE, 'true']).status === 0)
  const ng = process.env.NETGUARD_LOG && existsSync(process.env.NETGUARD_LOG) ? readFileSync(process.env.NETGUARD_LOG, 'utf8').trim() : ''
  if (process.env.LD_PRELOAD) ok('netguard: no connection outside loopback', ng === '', ng.slice(0, 200))
} catch (e) {
  failed++
  console.log(`  FAIL unexpected error: ${(e as Error).stack ?? e}\n--- bridge log ---\n${log.slice(-1500)}`)
} finally {
  await client.close().catch(() => {})
  bot.stop(true)
  db.close()
  rmSync(ROOT, { recursive: true, force: true })
}
console.log(failed ? `${failed} failed, ${passed} passed` : `all passed (${passed})`)
process.exit(failed ? 1 : 0)
