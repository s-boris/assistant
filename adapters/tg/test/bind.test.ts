// The bindings runner + tools registry v0, offline against the mock Bot API (no sockets, temp HOME):
// parity of the 0.1.22 compat tables (every entry → the same argv, runnable under the repo's registry), bound
// commands and presses answered by the adapter (text, picture, silent button replies, callback env), tg.bound rows
// and the source rows' binding receipts, on_error forward, failures, timeouts, one run per binding, the effects gate,
// the bindings-off switch, tg-bind rows (command, button, prefix, expiry), per-card binds, the /commands menu, the
// tools' environment (no token) and process groups killed at stop.
import { afterAll, afterEach, describe, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'
import { Binder, BUSY_TOAST, COMPAT_READS_KEY, type Binding } from '../src/bind.ts'
import { loadConfig } from '../src/config.ts'
import { openDb, Store } from '../src/db.ts'
import { drainOutbox } from '../src/outbox.ts'
import { defaultRegistryFile, parseRegistry, Registry, tapAllowed } from '../src/registry.ts'
import { compatText } from '../src/cli.ts'
import { STATUS_MENU_ENTRY } from '../src/instant.ts'
import { CALLBACKS_0122, INSTANT_0122 } from './compat-tables.ts'
import { cleanup, makeHome, outbox, rows, startAdapter, waitFor, type Home, type Running } from './helpers.ts'
import { png } from './images.ts'
import { MockBot, OWNER, TOKEN } from './mockbot.ts'

afterAll(cleanup)
let running: Running | null = null
afterEach(async () => { if (running) await running.stop(); running = null })

const BOT = 'testadapter_bot'
const CHAT = String(OWNER)

/** 0.1.22's splitArgv (server.ts 1430-1433): whitespace split, a leading ~/ per element expanded. */
const argv0122 = (run: string, home: string) => run.trim().split(/\s+/).filter(Boolean).map(a => a.replace(/^~(?=\/)/, home))

function script(h: Home, name: string, body: string): string {
  const dir = join(h.home, 'bin')
  mkdirSync(dir, { recursive: true })
  const p = join(dir, name)
  writeFileSync(p, `#!/bin/sh\n${body}\n`)
  chmodSync(p, 0o755)
  return p
}

const REGISTRY = `
schema = 1
[tools."say"]
argv = ["~/bin/say"]
effect = "read"
note = true
text_arg = true
menu = "Say something"
[tools."say.button"]
argv = ["~/bin/say", "button"]
effect = "read"
[tools."cbenv"]
argv = ["~/bin/cbenv"]
effect = "home"
toast = "🔔 env"
[tools."slow"]
argv = ["~/bin/slow"]
effect = "read"
[tools."fail"]
argv = ["~/bin/fail"]
effect = "read"
[tools."pic"]
argv = ["~/bin/pic"]
effect = "read"
output = "picture"
[tools."hang"]
argv = ["~/bin/hang"]
effect = "read"
[tools."ring"]
argv = ["~/bin/say", "ring"]
effect = "send_owner"
[tools."pay"]
argv = ["~/bin/say", "pay"]
effect = "spend"
[tools."status"]
argv = ["~/bin/status"]
effect = "read"
note = true
`

const INSTANT = [
  { command: 'hello', description: 'Say hello', run: '~/bin/say' },
  { command: 'echo', description: 'Echo', run: '~/bin/say', args: true, note: false },
  { command: 'oops', description: 'Fails with a fallback', run: '~/bin/fail' },
  { command: 'pic', description: 'A picture', run: '~/bin/pic' },
  { command: 'rogue', description: 'Not registered', run: '~/bin/rogue' },
  { command: 'hang', description: 'Hangs', run: '~/bin/hang', timeout_ms: 400 },
  { command: 'pay', description: 'Spends', run: '~/bin/say pay' },
]
const CALLBACKS = [
  { name: 'rm', data: 'rm:*', run: '~/bin/cbenv', toast: '', note: false },
  { name: 'slow', data: 'sl:go', run: '~/bin/slow', toast: '⏳ on it' },
  { name: 'noted', data: 'nt:x', run: '~/bin/say button', note: true },
]

/** A temp home with the test registry, compat tables and tool scripts; the adapter running against the mock. */
async function setup(config: Record<string, unknown> = {}, env: Record<string, string> = {}, mock = new MockBot()) {
  const h = makeHome({
    compat_instant_file: '~/tg/instant.json', compat_callbacks_file: '~/tg/callbacks.json', tools_registry: '~/tools.toml', ...config,
  })
  mkdirSync(join(h.home, 'tg'), { recursive: true })
  writeFileSync(join(h.home, 'tg', 'instant.json'), JSON.stringify(INSTANT))
  writeFileSync(join(h.home, 'tg', 'callbacks.json'), JSON.stringify(CALLBACKS))
  writeFileSync(join(h.home, 'tools.toml'), REGISTRY)
  script(h, 'say', 'echo "said $*"')
  script(h, 'cbenv', `echo "$TELEGRAM_CALLBACK_DATA|$TELEGRAM_CALLBACK_CHAT_ID|$TELEGRAM_CALLBACK_MESSAGE_ID|\${TELEGRAM_BOT_TOKEN:-none}|$(env | grep -c '${TOKEN}')"`)
  script(h, 'slow', 'sleep 0.6; echo slow done')
  script(h, 'fail', 'echo "fallback text"; echo "it broke" >&2; exit 2')
  const card = join(h.home, '.local', 'state', 'cardimg')
  mkdirSync(card, { recursive: true })
  writeFileSync(join(card, 'c.png'), png(400, 300))
  script(h, 'pic', `printf '%s' '{"photo": "${join(card, 'c.png')}", "caption": "the card"}'`)
  script(h, 'hang', `echo $$ > "${join(h.home, 'hang.pid')}"; sleep 30; echo late`)
  script(h, 'status', 'echo "board: idle"')
  running = await startAdapter(mock, h, { env: { TG_ADAPTER_STATUS_CMD: join(h.home, 'bin', 'status'), LEAK: `x${TOKEN}y`, ...env } })
  const r = running
  return { mock, h, r }
}

const bound = (store: Store) => rows(store, 'tg.bound')
const receipt = (store: Store, id: number) => store.db.query('SELECT * FROM deliveries WHERE event_id = ?').get(id) as any

describe('registry and compat parity', () => {
  test('the repo registry parses; effects order and the tap gate', () => {
    const r = parseRegistry(Bun.TOML.parse(readFileSync(defaultRegistryFile(), 'utf8')), '/h')
    expect(r.skipped).toEqual([])
    expect(r.tools.get('talk-button')).toMatchObject({ effect: 'send_owner', timeoutMs: 120_000, argv: ['/h/bin/talk-button'] })
    expect(tapAllowed('read') && tapAllowed('home') && tapAllowed('send_owner')).toBe(true)
    expect(tapAllowed('send_external') || tapAllowed('spend') || tapAllowed('irreversible')).toBe(false)
    const bad = parseRegistry({ tools: { 'Bad Name': { argv: ['x'], effect: 'read' }, a: { argv: [], effect: 'read' }, b: { argv: ['x'], effect: 'nuke' },
      c: { argv: ['x'], effect: 'read', output: 'gif' }, d: { argv: ['x'], effect: 'read', timeout_s: 9999 } } }, '/h')
    expect(bad.skipped.length).toBe(4)
    expect(bad.tools.get('d')!.timeoutMs).toBe(300_000)
  })

  test('every 0.1.22 instant command, /status and instant button resolves to the same argv and runs (repo registry)', () => {
    const h = makeHome({ compat_instant_file: '~/tg/instant.json', compat_callbacks_file: '~/tg/callbacks.json' })
    mkdirSync(join(h.home, 'tg'), { recursive: true })
    writeFileSync(join(h.home, 'tg', 'instant.json'), JSON.stringify(INSTANT_0122))
    writeFileSync(join(h.home, 'tg', 'callbacks.json'), JSON.stringify(CALLBACKS_0122))
    script(h, 'tg-status', 'echo board')
    const cfg = loadConfig(h.paths.config, h.home, { testRun: true })
    const store = new Store(openDb(h.paths.defaultDb))
    const binder = new Binder({ store, tg: (async () => ({})) as any, cfg, paths: h.paths, env: h.env, secret: () => false, clean: s => s,
      log: () => {}, withSend: (_c, fn) => fn(), remember: () => {} })
    const status = binder.commandBinding('status')!
    expect(status).toMatchObject({ source: 'compat', argv: [join(h.home, 'bin', 'tg-status'), 'show', '--plain'], onError: 'forward', note: true, status: true })
    expect(status.gated).toBeUndefined()
    expect(status.tool!.name).toBe('tg-status.show')
    for (const e of INSTANT_0122) {
      const b = binder.commandBinding(e.command)!
      expect([e.command, b.argv, b.gated ?? 'runs', b.takesText, b.note, b.onError, b.timeoutMs])
        .toEqual([e.command, argv0122(e.run, h.home), 'runs', e.args === true, true, 'reply', 20_000])
      expect([e.command, b.output]).toEqual([e.command, e.run.includes('TG_CMD_PICTURES=1') ? 'picture' : 'text'])
    }
    const presses: [string, string][] = [['call:talk', 'talk'], ['rm:s1h:a1b2', 'reminders'], ['rm:done:x', 'reminders'], ['vf:fixed', 'vacuum-fixed']]
    for (const [data, name] of presses) {
      const b = binder.buttonBinding(CHAT, 7, data)!
      const e = CALLBACKS_0122.find(c => c.name === name)!
      expect([data, b.name, b.argv, b.gated ?? 'runs', b.note, b.timeoutMs, b.toast]).toEqual([data, name, argv0122(e.run, h.home), 'runs', false, e.timeout_ms, e.toast])
    }
    expect(binder.buttonBinding(CHAT, 7, 'rmx')).toBeNull()
    expect(binder.buttonBinding(CHAT, 7, 'perm:allow:abcde')).toBeNull()
    expect(binder.commandBinding('nope')).toBeNull()
    expect(binder.menu().map(c => c.command)).toEqual(['status', ...INSTANT_0122.map(e => e.command)])
    expect(binder.table().filter(t => !t.runs)).toEqual([])
    store.db.close()
  })
})

/** ops/rollout/tg-bind-seed.json written into a store as `tg-bind import` does (file order = row order). */
const SEED = JSON.parse(readFileSync(join(import.meta.dir, '..', '..', '..', 'ops', 'rollout', 'tg-bind-seed.json'), 'utf8')).rows as
  { kind: string; key: string; tool: string; why: string; note?: boolean; forward?: boolean }[]
function importSeed(store: Store, seed = SEED) {
  for (const r of seed) {
    store.db.query(`INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active) VALUES (?, ?, ?, '[]', ?, ?, ?, ?, NULL, 1)
      ON CONFLICT(kind, key) DO UPDATE SET tool = excluded.tool, note = excluded.note, on_error = excluded.on_error, why = excluded.why, active = 1`)
      .run(r.kind, r.key, r.tool, r.note ? 1 : 0, r.forward ? 'forward' : 'reply', r.why, Date.now() / 1000)
  }
}
/** What a binding does, apart from where it came from. */
const behaviour = (b: Binding) => ({ argv: b.argv, timeoutMs: b.timeoutMs, note: b.note, onError: b.onError, toast: b.toast, takesText: b.takesText,
  output: b.output, status: b.status, gated: b.gated ?? null, tool: b.tool?.name ?? null })

describe('instant commands: compat tables -> tg-bind rows', () => {
  function compatBinder(count = true) {
    const h = makeHome({ compat_instant_file: '~/tg/instant.json', compat_callbacks_file: '~/tg/callbacks.json' })
    mkdirSync(join(h.home, 'tg'), { recursive: true })
    writeFileSync(join(h.home, 'tg', 'instant.json'), JSON.stringify(INSTANT_0122))
    writeFileSync(join(h.home, 'tg', 'callbacks.json'), JSON.stringify(CALLBACKS_0122))
    script(h, 'tg-status', 'echo board')
    const cfg = loadConfig(h.paths.config, h.home, { testRun: true })
    const store = new Store(openDb(h.paths.defaultDb))
    const logs: string[] = []
    const binder = new Binder({ store, tg: (async () => ({})) as any, cfg, paths: h.paths, env: h.env, secret: () => false, clean: s => s,
      log: m => logs.push(m), withSend: (_c, fn) => fn(), remember: () => {}, countCompat: count })
    return { h, cfg, store, binder, logs, reads: () => binder.compatReads()! }
  }
  const MENU_0122 = [STATUS_MENU_ENTRY, ...INSTANT_0122.map(e => ({ command: e.command, description: e.description }))]
  const PRESSES = ['call:talk', 'rm:s1h:a1b2', 'rm:done:x', 'vf:fixed']

  test('the seed covers every compat entry once, each with a why; the registry has every menu line and toast', () => {
    expect(SEED.map(r => r.kind === 'command' ? r.key : r.kind)).toEqual(['status', ...INSTANT_0122.map(e => e.command), 'button', 'prefix', 'button'])
    expect(SEED.filter(r => r.kind !== 'command').map(r => r.key)).toEqual(['call:talk', 'rm:', 'vf:fixed'])
    expect(SEED.every(r => r.why.length > 20 && typeof r.note === 'boolean')).toBe(true)
    const reg = new Registry(defaultRegistryFile(), '/h')
    for (const r of SEED.filter(r => r.kind === 'command')) expect([r.key, reg.get(r.tool)!.menu]).toEqual([r.key, MENU_0122.find(m => m.command === r.key)!.description])
    for (const c of CALLBACKS_0122) expect([c.name, reg.get(SEED.find(r => r.key === c.data.replace(/\*$/, ''))!.tool)!.toast]).toEqual([c.name, c.toast])
  })

  test('with the seed rows every command and press resolves from tg-bind to what the compat entry did; the menu is identical', () => {
    const { store, binder, reads } = compatBinder()
    const before = { cmds: [...MENU_0122.map(m => binder.commandBinding(m.command)!)], presses: PRESSES.map(d => binder.buttonBinding(CHAT, 7, d)!) }
    expect(before.cmds.every(b => b.source === 'compat') && before.presses.every(b => b.source === 'compat')).toBe(true)
    expect(binder.menu()).toEqual(MENU_0122)
    expect(reads()).toMatchObject({ instant: { n: MENU_0122.length, key: '/traces' }, callbacks: { n: 4, key: 'vacuum-fixed (vf:fixed)' }, menu: { n: 1 } })
    importSeed(store)
    const n0 = JSON.stringify(reads())
    MENU_0122.forEach((m, i) => {
      const b = binder.commandBinding(m.command)!
      expect([m.command, b.source, b.name]).toEqual([m.command, 'tg-bind', m.command])
      expect([m.command, behaviour(b)]).toEqual([m.command, behaviour(before.cmds[i]!)])
    })
    PRESSES.forEach((d, i) => {
      const b = binder.buttonBinding(CHAT, 7, d)!
      expect([d, b.source]).toEqual([d, 'tg-bind'])
      expect([d, behaviour(b)]).toEqual([d, behaviour(before.presses[i]!)])
    })
    expect(binder.menu()).toEqual(MENU_0122)
    expect(binder.commandBinding('lights')).toBeNull() // unbound: the brain's; no compat entry either, so not a use
    expect(binder.buttonBinding(CHAT, 7, 'mt:play:1')).toBeNull()
    expect(JSON.stringify(reads())).toBe(n0)
    expect(binder.table().filter(t => t.source === 'tg-bind').length).toBe(31)
    store.db.close()
  })

  test('a retired row falls back to the compat table: counted and logged; the menu keeps it last; rows lead the menu in row order', () => {
    const { store, binder, reads, logs } = compatBinder()
    importSeed(store)
    store.db.query("UPDATE bindings SET active = 0 WHERE kind = 'command' AND key = 'bills'").run()
    expect(binder.commandBinding('bills')!.source).toBe('compat')
    expect(reads().instant).toMatchObject({ n: 1, key: '/bills' })
    expect(logs.some(l => l === 'compat read: instant.json /bills (no tg-bind row covers it)')).toBe(true)
    const menu = binder.menu().map(c => c.command)
    expect(menu.at(-1)).toBe('bills')
    expect(menu.length).toBe(MENU_0122.length)
    expect(reads().menu).toMatchObject({ n: 1, key: '/bills' })
    // a row whose tool has no menu line is bound but not listed; a gated row is not listed either
    store.db.query("INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, active) VALUES ('command', 'yo', 'remind.press', '[]', 0, 'reply', 't', 0, 1)").run()
    store.db.query("INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, active) VALUES ('command', 'gone', 'no.such', '[]', 0, 'reply', 't', 0, 1)").run()
    expect(binder.menu().map(c => c.command)).toEqual(menu)
    store.db.query("UPDATE bindings SET active = 1 WHERE kind = 'command' AND key = 'bills'").run()
    expect(binder.menu()).toEqual(MENU_0122)
    store.db.close()
  })

  test('the CLI views do not count; compatText says how long the tables have gone unread', () => {
    const { store, binder, cfg } = compatBinder(false)
    binder.commandBinding('bills'); binder.menu()
    expect(store.getMeta(COMPAT_READS_KEY(cfg.bot))).toBeNull()
    expect(compatText(null)).toContain('not counted yet')
    expect(compatText({ since: 1000 }, 1000 + 49 * 3600)).toBe('compat reads since 1970-01-01 00:16Z: none\nunread for 49.0 h (since the counter started)')
    expect(compatText({ since: 1000, instant: { n: 2, last: 1000 + 3600, key: '/bills' } }, 1000 + 7200)).toContain('instant 2')
    store.db.close()
  })
})

describe('bound commands', () => {
  test('instant command: answered by the adapter, source row received by the binding, tg.bound row reaches the brain', async () => {
    const { mock, r } = await setup()
    mock.text(OWNER, '/hello')
    expect(await waitFor(() => bound(r.store).length === 1)).toBe(true)
    expect(mock.sent('sendMessage').map(c => c.params.text)).toEqual(['said'])
    expect(mock.sent('sendMessage')[0]!.params.link_preview_options).toEqual({ is_disabled: true })
    const src = rows(r.store, 'tg.message')[0]
    expect(src).toMatchObject({ status: 'acked', ack_by: 'binding', delivery: 'now' })
    expect(src.payload.handled).toBe('hello')
    expect(receipt(r.store, src.id)).toMatchObject({ how: 'binding', tries: 0 })
    expect(receipt(r.store, src.id).receipt_ts).toBeGreaterThan(0)
    const b = bound(r.store)[0]
    expect(b).toMatchObject({ delivery: 'now', status: 'new', source: 'tg-adapter' })
    expect(b.payload).toMatchObject({ handled: 'hello', ok: true, outcome: 'ok', tool: 'say', binding: 'compat', source_event: src.id, chat_id: CHAT,
      text: '/hello (answered instantly by the plugin; no reply needed). It sent:\nsaid' })
    expect(typeof b.payload.ms).toBe('number')
    expect(b.idem_key).toBe(`tg.bound:${src.idem_key}`)
  })

  test('args: "/echo <text>" passes one argument; note false = a digest row without the answer; args off = to the brain', async () => {
    const { mock, r } = await setup()
    mock.text(OWNER, '/echo some  words; rm -rf x')
    expect(await waitFor(() => bound(r.store).length === 1)).toBe(true)
    expect(mock.sent('sendMessage')[0]!.params.text).toBe('said some  words; rm -rf x')
    expect(bound(r.store)[0]).toMatchObject({ delivery: 'digest', status: 'acked', ack_by: 'binding' })
    expect(bound(r.store)[0].payload.text).toBe('/echo some words; rm -rf x (answered instantly by the plugin; no reply needed)')
    mock.text(OWNER, '/hello there')
    mock.text(OWNER, `/hello@other_bot`)
    mock.text(OWNER, `/hello@${BOT}`)
    expect(await waitFor(() => bound(r.store).length === 2 && rows(r.store, 'tg.message').length === 4)).toBe(true)
    const plain = rows(r.store, 'tg.message').filter(m => m.status === 'new')
    expect(plain.map(m => m.payload.text)).toEqual(['/hello there', '/hello@other_bot'])
  })

  test('failure: the script\'s own fallback text goes out, the brain is told (instant_error); timeout kills it', async () => {
    const { mock, r } = await setup()
    mock.text(OWNER, '/oops')
    expect(await waitFor(() => bound(r.store).length === 1)).toBe(true)
    expect(mock.sent('sendMessage').map(c => c.params.text)).toEqual(['fallback text'])
    expect(bound(r.store)[0].payload).toMatchObject({ ok: false, outcome: 'failed', instant_error: 'exit 2: it broke',
      text: "/oops (the plugin's instant answer failed: exit 2: it broke; it sent the owner the command's own fallback text; no reply needed)" })
    expect(bound(r.store)[0].delivery).toBe('now')
    mock.text(OWNER, '/hang')
    expect(await waitFor(() => bound(r.store).length === 2, 4000)).toBe(true)
    expect(mock.sent('sendMessage')[1]!.params.text).toMatch(/^⚠️ \/hang didn't work \(timed out after 0\.4 s\)/)
    expect(bound(r.store)[1].payload.instant_error).toBe('timed out after 0.4 s')
  })

  test('/status: the status command; on failure it is forwarded to the brain as the message (status_error)', async () => {
    const { mock, r, h } = await setup()
    mock.text(OWNER, '/status')
    expect(await waitFor(() => bound(r.store).length === 1)).toBe(true)
    expect(mock.sent('sendMessage')[0]!.params.text).toBe('board: idle')
    expect(bound(r.store)[0].payload.text).toBe('/status (answered instantly by the plugin with the status board; no reply needed)')
    script(h, 'status', 'exit 0') // prints nothing
    mock.text(OWNER, '/status')
    expect(await waitFor(() => bound(r.store).length === 2)).toBe(true)
    const f = bound(r.store)[1]
    expect(f.delivery).toBe('now')
    expect(f.payload).toMatchObject({ text: '/status', status_error: 'the status command printed nothing', outcome: 'forwarded', ok: false })
    expect(f.payload.handled).toBeUndefined()
    expect(mock.sent('sendMessage').length).toBe(1)
  })

  test('picture answer: sendPhoto with the caption; a picture that can\'t be sent falls back to its text', async () => {
    const { mock, r, h } = await setup()
    mock.text(OWNER, '/pic')
    expect(await waitFor(() => bound(r.store).length === 1)).toBe(true)
    expect(mock.sent('sendPhoto').length).toBe(1)
    expect(mock.sent('sendPhoto')[0]!.params.caption).toBe('the card')
    expect(bound(r.store)[0].payload).toMatchObject({ picture: 'c.png', ok: true })
    const wrong = join(h.home, '.local', 'state', 'cardimg', 'x.gif') // an allowed folder, a type it refuses
    writeFileSync(wrong, png(400, 300))
    script(h, 'pic', `printf '%s' '{"photo": "${wrong}", "caption": "cap", "text": "plain instead"}'`)
    mock.text(OWNER, '/pic')
    expect(await waitFor(() => bound(r.store).length === 2)).toBe(true)
    expect(mock.sent('sendPhoto').length).toBe(1)
    expect(mock.sent('sendMessage').map(c => c.params.text)).toEqual(['plain instead'])
    expect(bound(r.store)[1].payload.picture_error).toBe('photo is not a .png/.jpg/.webp file')
  })

  test('effects gate and unregistered entries: not run, the message goes to the brain (binding_gated)', async () => {
    const { mock, r } = await setup()
    mock.text(OWNER, '/pay')
    mock.text(OWNER, '/rogue')
    expect(await waitFor(() => rows(r.store, 'tg.message').length === 2)).toBe(true)
    await Bun.sleep(150)
    expect(mock.sent('sendMessage').length).toBe(0)
    expect(bound(r.store).length).toBe(0)
    const [pay, rogue] = rows(r.store, 'tg.message')
    expect(pay).toMatchObject({ status: 'new', delivery: 'now' })
    expect(pay.payload.binding_gated).toBe('pay: pay has effect spend, which needs an approval')
    expect(rogue.payload.binding_gated).toBe('rogue: not in the tools registry')
    expect(receipt(r.store, pay.id)).toBeNull()
  })

  test('bindings-off switch and send off: every command goes to the brain', async () => {
    const { mock, r, h } = await setup()
    writeFileSync(join(h.paths.configDir, 'bindings-off'), '')
    mock.text(OWNER, '/hello')
    expect(await waitFor(() => rows(r.store, 'tg.message').length === 1)).toBe(true)
    expect(rows(r.store, 'tg.message')[0]).toMatchObject({ status: 'new' })
    await running!.stop(); running = null
    const s = await setup({ send: false })
    s.mock.text(OWNER, '/hello')
    expect(await waitFor(() => rows(s.r.store, 'tg.message').length === 1)).toBe(true)
    expect(rows(s.r.store, 'tg.message')[0].status).toBe('new')
    expect(s.r.logs.some(l => /bindings off/.test(l))).toBe(true)
  })
})

describe('bound buttons', () => {
  test('rm:* prefix: own toast, callback env (no token anywhere), silent reply to the card, keyboard untouched, digest row', async () => {
    const { mock, r } = await setup()
    mock.press(OWNER, 'rm:s1h:abc', 'Snooze 1h', { message_id: 321 })
    expect(await waitFor(() => bound(r.store).length === 1)).toBe(true)
    expect(mock.sent('answerCallbackQuery')[0]!.params.text).toBe('✓ Snooze 1h')
    const reply = mock.sent('sendMessage')[0]!.params
    expect(reply.text).toBe(`rm:s1h:abc|${CHAT}|321|none|0`)
    expect(reply.disable_notification).toBe(true)
    expect(reply.reply_parameters).toEqual({ message_id: 321, allow_sending_without_reply: true })
    expect(mock.sent('editMessageText').length + mock.sent('editMessageReplyMarkup').length).toBe(0)
    const src = rows(r.store, 'tg.button')[0]
    expect(src).toMatchObject({ status: 'acked', ack_by: 'binding' })
    expect(typeof src.payload.ack_ms).toBe('number')
    expect(bound(r.store)[0]).toMatchObject({ delivery: 'digest', status: 'acked' })
    expect(bound(r.store)[0].payload).toMatchObject({ event_type: 'button', callback_data: 'rm:s1h:abc', button_text: 'Snooze 1h', handled: 'rm',
      instant_button: 'rm', ok: true, tool: 'cbenv' })
  })

  test('one run per binding: a second tap while it runs gets the busy toast and no row; note true reaches the brain', async () => {
    const { mock, r } = await setup()
    mock.press(OWNER, 'sl:go', 'Go')
    mock.press(OWNER, 'sl:go', 'Go')
    expect(await waitFor(() => bound(r.store).length === 1, 4000)).toBe(true)
    expect(mock.sent('answerCallbackQuery').map(c => c.params.text)).toEqual(['⏳ on it', BUSY_TOAST])
    expect(rows(r.store, 'tg.button').length).toBe(1)
    expect(r.store.getMeta('dropped:bound busy')).toBe('1')
    mock.press(OWNER, 'nt:x', 'Note it')
    expect(await waitFor(() => bound(r.store).length === 2)).toBe(true)
    expect(bound(r.store)[1]).toMatchObject({ delivery: 'now', status: 'new' })
    expect(bound(r.store)[1].payload.text).toBe('(pressed button: Note it) The plugin ran instant button "noted"; the owner got: said button. Done; no reply needed.')
  })

  test('tg-bind rows: command with args, longest prefix, exact button, expired rows ignored, gated button to the brain', async () => {
    const { mock, r } = await setup()
    const add = (kind: string, key: string, tool: string, args: string[] = [], note = 0, expires: number | null = null) =>
      r.store.db.query(`INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active) VALUES (?, ?, ?, ?, ?, 'reply', 'test', ?, ?, 1)`)
        .run(kind, key, tool, JSON.stringify(args), note, Date.now() / 1000, expires)
    add('command', 'hi', 'say', ['x'])
    add('prefix', 'zz:', 'cbenv')
    add('prefix', 'zz:a', 'say.button')
    add('button', 'old:1', 'say', [], 0, Date.now() / 1000 - 10)
    add('button', 'pay:now', 'pay')
    add('button', 'ring:me', 'ring')
    mock.text(OWNER, '/hi there')
    expect(await waitFor(() => bound(r.store).length === 1)).toBe(true)
    expect(mock.sent('sendMessage')[0]!.params.text).toBe('said x there')
    expect(bound(r.store)[0].payload).toMatchObject({ binding: 'tg-bind', tool: 'say', handled: 'hi' })
    mock.press(OWNER, 'zz:ab', 'Longest')
    mock.press(OWNER, 'ring:me', 'Ring')
    expect(await waitFor(() => bound(r.store).length === 3)).toBe(true)
    expect(bound(r.store)[1].payload.tool).toBe('say.button')
    expect(bound(r.store)[2].payload.tool).toBe('ring')
    mock.press(OWNER, 'old:1', 'Old')
    mock.press(OWNER, 'pay:now', 'Pay')
    expect(await waitFor(() => rows(r.store, 'tg.button').length === 4)).toBe(true)
    await Bun.sleep(150)
    expect(bound(r.store).length).toBe(3)
    const [old, pay] = rows(r.store, 'tg.button').slice(2)
    expect(old).toMatchObject({ status: 'new' })
    expect(pay.payload.binding_gated).toBe('pay: pay has effect spend, which needs an approval')
    // unbound presses keep 0.1.22's default: "✓ label" appended, keyboard removed
    expect(mock.sent('editMessageText').length).toBe(2)
  })
})

describe('cards and the menu', () => {
  test('buttons[].bind on a sent card: saved per message (7 days), refused binds noted, a press runs it', async () => {
    const { mock, r } = await setup()
    const id = r.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'Card', buttons: [[
      { text: 'Go', data: 'card:go', bind: { tool: 'say', args: ['card'] } },
      { text: 'Bad', data: 'card:bad', bind: { tool: 'nope' } },
      { text: 'Pay', data: 'card:pay', bind: { tool: 'pay' } },
      { text: 'Plain', data: 'card:plain' },
    ]] } })!
    await drainOutbox(r.adapter.outboxDeps())
    const res = JSON.parse(outbox(r.store, id).result)
    expect(res.notes).toEqual([
      'button "Bad": tool "nope" is not in the tools registry; a press reaches you instead',
      'button "Pay": pay has effect spend (needs an approval); a press reaches you instead',
    ])
    const mid = res.ids[0]
    const card = r.store.db.query("SELECT * FROM bindings WHERE kind = 'card'").all() as any[]
    expect(card.map(c => [c.key, c.tool, c.args])).toEqual([[`${CHAT}:${mid}:card:go`, 'say', '["card"]']])
    expect(card[0].expires - card[0].created).toBeCloseTo(7 * 86400, 0)
    mock.press(OWNER, 'card:go', 'Go', { message_id: mid, text: 'Card' })
    expect(await waitFor(() => bound(r.store).length === 1)).toBe(true)
    expect(mock.sent('sendMessage').at(-1)!.params.text).toBe('said card')
    expect(bound(r.store)[0].payload.binding).toBe('card')
    // an edit with a new keyboard replaces the message's binds
    const e = r.store.enqueue({ bot: BOT, chat: CHAT, op: 'edit', body: { message_id: mid, buttons: [[{ text: 'Done', data: 'card:done' }]] } })!
    await drainOutbox(r.adapter.outboxDeps())
    expect(outbox(r.store, e).status).toBe('sent')
    expect((r.store.db.query("SELECT COUNT(*) AS n FROM bindings WHERE kind = 'card'").get() as any).n).toBe(0)
  })

  test('set_commands: the instant commands come first (a custom one with an instant name is left out)', async () => {
    const { mock, r } = await setup()
    const id = r.store.enqueue({ bot: 'test', chat: '-', op: 'commands', body: { commands: [{ command: 'custom', description: 'C' }, { command: 'hello', description: 'dup' }] } })!
    await drainOutbox(r.adapter.outboxDeps())
    const sent = mock.sent('setMyCommands')[0]!.params.commands.map((c: any) => c.command)
    expect(sent).toEqual(['status', ...INSTANT.map(e => e.command), 'custom'])
    expect(JSON.parse(outbox(r.store, id).result).text).toBe(`commands set: ${sent.map((c: string) => '/' + c).join(' ')}`)
    expect(JSON.parse(r.store.getMeta(`commands:${BOT}`)!)).toEqual([{ command: 'custom', description: 'C' }, { command: 'hello', description: 'dup' }])
  })
})

describe('the /commands menu through set_commands', () => {
  test('bound command rows with a menu line lead, then compat entries no row covers, then the custom list; a bound toast', async () => {
    const { mock, r } = await setup()
    r.store.db.query("INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, active) VALUES ('command', 'hi', 'say', '[]', 1, 'reply', 'test', 0, 1)").run()
    r.store.db.query("INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, active) VALUES ('command', 'hello', 'say', '[]', 1, 'reply', 'test', 0, 1)").run()
    r.store.db.query("INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, active) VALUES ('prefix', 'qq:', 'cbenv', '[]', 0, 'reply', 'test', 0, 1)").run()
    const id = r.store.enqueue({ bot: 'test', chat: '-', op: 'commands', body: { commands: [{ command: 'custom', description: 'C' }, { command: 'hi', description: 'dup' }] } })!
    await drainOutbox(r.adapter.outboxDeps())
    const sent = mock.sent('setMyCommands')[0]!.params.commands
    expect(sent.slice(0, 3)).toEqual([{ command: 'hi', description: 'Say something' }, { command: 'hello', description: 'Say something' }, { command: 'status', description: "What I'm working on" }])
    expect(sent.map((c: any) => c.command)).toEqual(['hi', 'hello', 'status', ...INSTANT.map(e => e.command).filter(c => c !== 'hello'), 'custom'])
    expect(JSON.parse(outbox(r.store, id).result).text).toBe(`commands set: ${sent.map((c: any) => '/' + c.command).join(' ')}`)
    expect(JSON.parse(r.store.getMeta(`commands:${BOT}`)!)).toEqual([{ command: 'custom', description: 'C' }, { command: 'hi', description: 'dup' }])
    expect(JSON.parse(r.store.getMeta(COMPAT_READS_KEY(BOT))!).menu.n).toBe(1)
    mock.press(OWNER, 'qq:1', 'Q')
    expect(await waitFor(() => mock.calls.some(c => c.method === 'answerCallbackQuery' && c.params.text === '🔔 env'))).toBe(true)
  })
})

describe('shutdown', () => {
  test('a running tool\'s process group is killed when the adapter stops', async () => {
    const { mock, r, h } = await setup()
    writeFileSync(join(h.home, 'tg', 'instant.json'), JSON.stringify([{ command: 'hang', description: 'Hangs', run: '~/bin/hang', timeout_ms: 60000 }]))
    mock.text(OWNER, '/hang')
    const pidFile = join(h.home, 'hang.pid')
    expect(await waitFor(() => existsSync(pidFile))).toBe(true)
    const pid = Number(readFileSync(pidFile, 'utf8'))
    await running!.stop(); running = null
    const gone = await waitFor(() => { try { process.kill(pid, 0); return false } catch { return true } }, 4000)
    expect(gone).toBe(true)
  })

  test('a registry file that is missing or broken binds nothing', () => {
    const h = makeHome()
    const logs: string[] = []
    const reg = new Registry(join(h.home, 'none.toml'), h.home, m => logs.push(m))
    expect(reg.tools().size).toBe(0)
    writeFileSync(join(h.home, 'bad.toml'), 'tools = [')
    expect(new Registry(join(h.home, 'bad.toml'), h.home, m => logs.push(m)).tools().size).toBe(0)
    expect(logs.length).toBe(2)
  })
})
