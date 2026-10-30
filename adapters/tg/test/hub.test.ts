// H6 hub mode: the adapter against a fake hubd (test/hubfake.ts) and the mock Bot API, the companion in process.
// No sockets, temp HOME, fake tokens only.
import { afterEach, describe, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'
import { Adapter } from '../src/adapter.ts'
import { status as statusOf } from '../src/cli.ts'
import { Companion } from '../src/companion.ts'
import { ConfigError, loadConfig, parseConfig, readToken } from '../src/config.ts'
import { openDb, Store } from '../src/db.ts'
import { HubClient } from '../src/hub.ts'
import { FWD_ID } from '../src/hubsync.ts'
import { drainOutbox, MAX_ATTEMPTS } from '../src/outbox.ts'
import { cleanup, makeHome, waitFor, type Home } from './helpers.ts'
import { BRAIN_TOKEN, FakeHub, TG_TOKEN } from './hubfake.ts'
import { png } from './images.ts'
import { MockBot, OWNER } from './mockbot.ts'

const HUBCFG = { url: 'http://hub.invalid:8700', token_file: '~/.secrets/hub-tg-token', files_dir: '~/tgfiles' }
const REGISTRY = (stubs: string) => `
schema = 1
[tools."say"]
argv = ["${stubs}/say"]
effect = "read"
note = true
text_arg = true
[tools."slow.button"]
argv = ["${stubs}/slow"]
effect = "read"
[tools."pic"]
argv = ["${stubs}/pic"]
effect = "read"
output = "picture"
[tools."spender"]
argv = ["${stubs}/say"]
effect = "spend"
`

type Run = {
  h: Home; hub: FakeHub; mock: MockBot; adapter: Adapter; store: Store; logs: string[]; done: Promise<number>
  companion: Companion | null; stop: () => Promise<void>
}
const runs: Run[] = []
afterEach(async () => {
  for (const r of runs.splice(0)) await r.stop().catch(() => {})
  cleanup()
})

function home(config: Record<string, unknown> = {}): Home {
  const h = makeHome({ hub: HUBCFG, tools_registry: '~/tools.toml', ...config })
  const tok = join(h.home, '.secrets', 'hub-tg-token')
  writeFileSync(tok, TG_TOKEN + '\n', { mode: 0o600 })
  chmodSync(tok, 0o600)
  const stubs = join(h.home, 'stubs')
  mkdirSync(stubs, { recursive: true })
  const sh = (name: string, body: string) => { const p = join(stubs, name); writeFileSync(p, `#!/bin/sh\n${body}\n`); chmodSync(p, 0o755) }
  sh('say', 'echo "said: $*"')
  sh('slow', 'sleep 1; echo "slow done $TELEGRAM_CALLBACK_DATA"')
  sh('transcribe', 'echo "hello from the voice note"')
  mkdirSync(join(h.home, '.local', 'state', 'cardimg'), { recursive: true })
  writeFileSync(join(h.home, '.local', 'state', 'cardimg', 'card.png'), png(60, 40))
  sh('pic', `printf '{"photo": "%s", "caption": "a card"}' "${join(h.home, '.local', 'state', 'cardimg', 'card.png')}"`)
  writeFileSync(join(h.home, 'tools.toml'), REGISTRY(stubs))
  return h
}

async function start(o: { h?: Home; hub?: FakeHub; mock?: MockBot; companion?: boolean; compEnv?: Record<string, string>; rules?: Record<string, unknown> } = {}): Promise<Run> {
  const h = o.h ?? home()
  const hub = o.hub ?? new FakeHub(join(h.home, 'tgfiles'))
  const mock = o.mock ?? new MockBot()
  const cfg = loadConfig(h.paths.config, h.home, { testRun: true })
  const token = readToken(cfg, h.env, h.home)
  mkdirSync(h.paths.stateDir, { recursive: true })
  const store = new Store(openDb(join(h.paths.stateDir, 'journal.db')))
  const logs: string[] = []
  let companion: Companion | null = null
  let compDone: Promise<number> = Promise.resolve(0)
  if (o.companion) {
    companion = new Companion({
      hub: new HubClient({ url: HUBCFG.url, token: BRAIN_TOKEN, fetch: hub.fetch }), paths: h.paths,
      env: { ...h.env, PATH: process.env.PATH ?? '/usr/bin:/bin', TG_ADAPTER_TOOLS: join(h.home, 'tools.toml'),
        TG_ADAPTER_TRANSCRIBE_CMD: join(h.home, 'stubs', 'transcribe'), ...(o.compEnv ?? {}) },
      log: m => logs.push(`companion: ${m}`), noLookups: true, claimWaitS: 1, boardFile: join(h.home, 'board.json'),
    })
    compDone = companion.run()
    await waitFor(() => hub.lastClaimAt > 0, 2000)
  }
  const adapter = new Adapter({ cfg, paths: h.paths, token, store, apiOptions: { fetch: mock.fetch }, log: m => logs.push(m), env: { ...h.env },
    hubFetch: hub.fetch, ...(o.rules ? { rules: o.rules as any } : {}) })
  const done = adapter.run()
  await waitFor(() => logs.some(l => l.startsWith('polling as')), 3000)
  const run: Run = {
    h, hub, mock, adapter, store, logs, done, companion,
    stop: async () => { companion?.stop(); adapter.stop(); await done; await compDone; try { store.db.close() } catch {} },
  }
  runs.push(run)
  return run
}

const texts = (hub: FakeHub, kind = 'tg.message') => hub.kinds(kind).map(e => JSON.parse(e.payload).text)
const base = (extra: any) => ({ message_id: Math.floor(Math.random() * 1e6), date: Math.floor(Date.now() / 1000), chat: { id: OWNER, type: 'private' },
  from: { id: OWNER, is_bot: false, first_name: 'O', username: 'owner' }, ...extra })

describe('config', () => {
  test('hub needs url, token_file and files_dir; no compat files; schema only for tests and the test instance', () => {
    const c = { mode: 'test', bot: 'testadapter_bot', token_file: '~/t.env', allow_from: ['1'] }
    expect(parseConfig({ ...c, hub: HUBCFG }, '/h').hub).toEqual({ url: 'http://hub.invalid:8700', tokenFile: '/h/.secrets/hub-tg-token', filesDir: '/h/tgfiles' })
    expect(() => parseConfig({ ...c, hub: { url: 'ftp://x' } }, '/h')).toThrow(ConfigError)
    expect(() => parseConfig({ ...c, hub: { ...HUBCFG, token_file: '~/.claude/x' } }, '/h')).toThrow(/Claude config dir/)
    expect(() => parseConfig({ ...c, hub: HUBCFG, compat_journal: '~/j.jsonl' }, '/h')).toThrow(/compat files/)
    expect(() => parseConfig({ ...c, mode: 'live', hub: { ...HUBCFG, schema: 't_x' } }, '/h', { instance: 'live' })).toThrow(/test runs/)
    expect(parseConfig({ ...c, hub: { ...HUBCFG, schema: 't_rehearsal' } }, '/h').hub!.schema).toBe('t_rehearsal')
    const live = parseConfig({ ...c, mode: 'live', hub: HUBCFG, db: '/srv/tg/journal.db' }, '/h', { instance: 'live', eventsDb: '/h/events.db' })
    expect(live.db).toBe('/srv/tg/journal.db') // the journal is the instance's own in hub mode
    expect(live.compatInstant).toBeUndefined()
  })
})

describe('inbound rows → hub', () => {
  test('once each, in id order; a lost watermark or a restart duplicates nothing', async () => {
    const r = await start()
    r.mock.text(OWNER, 'one'); r.mock.text(OWNER, 'two')
    expect(await waitFor(() => r.hub.kinds('tg.message').length === 2)).toBe(true)
    expect(texts(r.hub)).toEqual(['one', 'two'])
    r.store.setMeta(FWD_ID, '0')
    await r.adapter.forwarder!.pass()
    expect(r.hub.kinds('tg.message').length).toBe(2)
    await r.stop()
    runs.splice(runs.indexOf(r), 1)
    const r2 = await start({ h: r.h, hub: r.hub, mock: r.mock })
    r2.mock.text(OWNER, 'three')
    expect(await waitFor(() => r2.hub.kinds('tg.message').length === 3)).toBe(true)
    expect(texts(r2.hub)).toEqual(['one', 'two', 'three'])
  })

  test('hub down: rows wait in the journal (polling goes on) and arrive in order when it answers', async () => {
    const r = await start()
    r.hub.down = true
    for (const t of ['a', 'b', 'c']) r.mock.text(OWNER, t)
    expect(await waitFor(() => (r.store.db.query("SELECT COUNT(*) AS n FROM events WHERE kind = 'tg.message'").get() as any).n === 3)).toBe(true)
    await Bun.sleep(1500)
    expect(r.hub.kinds('tg.message').length).toBe(0)
    r.hub.down = false
    expect(await waitFor(() => r.hub.kinds('tg.message').length === 3, 5000)).toBe(true)
    expect(texts(r.hub)).toEqual(['a', 'b', 'c'])
    expect(r.logs.some(l => /hub forwarder: .*unreachable/.test(l))).toBe(true)
  })

  test('a lost answer (crash mid-call): the rows go again and the hub keeps one of each', async () => {
    const r = await start()
    r.hub.lose = { path: '/tg/rows', n: 3 } // one whole call: the first try and both retries
    r.mock.text(OWNER, 'p'); r.mock.text(OWNER, 'q')
    expect(await waitFor(() => r.hub.lose!.n === 0 && r.hub.kinds('tg.message').length === 2, 5000)).toBe(true)
    await r.adapter.forwarder!.pass()
    expect(texts(r.hub)).toEqual(['p', 'q'])
    expect(r.hub.calls.filter(c => c.path === '/tg/rows').length).toBeGreaterThanOrEqual(4)
    expect(r.adapter.forwarder!.backlog()).toBe(0)
  })

  test('hub down 30 s: polling and taps go on, nothing floods the log; afterwards every row arrives once, in order', async () => {
    const r = await start()
    const before = r.logs.length
    r.hub.down = true
    const t0 = Date.now()
    let n = 0
    while (Date.now() - t0 < 30_000) {
      r.mock.text(OWNER, `m${n++}`)
      if (n === 3) r.mock.press(OWNER, 'free', 'Free')
      await Bun.sleep(2_000)
    }
    expect(r.mock.sent('answerCallbackQuery').length).toBe(1)
    expect((r.store.db.query("SELECT COUNT(*) AS n FROM events WHERE kind = 'tg.message'").get() as any).n).toBe(n)
    expect(r.hub.kinds('tg.message').length).toBe(0)
    const noisy = r.logs.slice(before)
    expect(noisy.length, noisy.map(l => l.slice(0, 40)).join(' | ')).toBeLessThan(12)
    r.hub.down = false
    expect(await waitFor(() => r.hub.kinds('tg.message').length === n, 8000)).toBe(true)
    expect(texts(r.hub)).toEqual(Array.from({ length: n }, (_, k) => `m${k}`))
    expect(r.hub.kinds('tg.button').length).toBe(1)
  }, 60_000)

  test('payload patches and meta counters follow', async () => {
    const r = await start()
    r.mock.text(OWNER, 'x')
    await waitFor(() => r.hub.kinds('tg.message').length === 1)
    const idem = r.hub.kinds('tg.message')[0]!.idem_key
    r.store.patchPayload(idem, { extra_key: 'v' })
    await r.adapter.forwarder!.pass()
    expect(JSON.parse(r.hub.kinds('tg.message')[0]!.payload).extra_key).toBe('v')
    await r.adapter.forwarder!.flushMeta()
    expect([...r.hub.meta.keys()].some(k => k.startsWith('offset:'))).toBe(true)
    expect(r.hub.meta.get('tg.last_poll_ts')).toMatch(/^\d+(\.\d+)?$/)
  })

  test('reaction, poll_answer and stop rows', async () => {
    const r = await start()
    const { hub, mock } = r
    const poll = hub.enqueue('poll', { question: 'Which?', options: ['Red', 'Blue'] })
    const draft = hub.enqueue('draft', { text: 'Checking' })
    expect(await waitFor(() => hub.row(poll).status === 'sent' && hub.row(draft).status === 'sent')).toBe(true)
    mock.pollAnswer(OWNER, 'poll-1', [1])
    mock.react(OWNER, hub.row(poll).tg_message_id, ['👍'])
    mock.push({ stopped_message_generation: { chat: { id: OWNER, type: 'private' }, draft_id: mock.sent('sendMessageDraft')[0]!.params.draft_id } })
    expect(await waitFor(() => ['tg.poll_answer', 'tg.reaction', 'tg.stop'].every(k => hub.kinds(k).length === 1), 4000)).toBe(true)
    expect(hub.kinds('tg.poll_answer')[0]!.text ?? JSON.parse(hub.kinds('tg.poll_answer')[0]!.payload).text).toMatch(/Blue/)
    expect(JSON.parse(hub.kinds('tg.stop')[0]!.payload).event_type).toBe('stop')
  })

  test('tap → ack never waits on the hub (hub down)', async () => {
    const r = await start()
    r.hub.down = true
    const t0 = Date.now()
    r.mock.press(OWNER, 'free', 'Free')
    expect(await waitFor(() => r.mock.sent('answerCallbackQuery').length === 1)).toBe(true)
    expect(Date.now() - t0).toBeLessThan(1500)
    r.hub.down = false
    expect(await waitFor(() => r.hub.kinds('tg.button').length === 1, 5000)).toBe(true)
  })
})

describe('outbox over the hub', () => {
  test('interrupted rows fail at start; send; dedupe; local mirror for reaction context', async () => {
    const h = home({ dedupe_window_s: 600 })
    const hub = new FakeHub(join(h.home, 'tgfiles'))
    const stale = hub.enqueue('send', { text: 'half' }, { status: 'sending' })
    const r = await start({ h, hub })
    expect(hub.row(stale).status).toBe('failed')
    expect(hub.kinds('outbox.failed').length).toBe(1)
    const a = hub.enqueue('send', { text: 'hello there' })
    const b = hub.enqueue('send', { text: 'hello there' })
    expect(await waitFor(() => hub.row(a).status === 'sent' && hub.row(b).status === 'skipped')).toBe(true)
    expect(hub.row(b).last_error).toMatch(/duplicate of #/)
    const mid = hub.row(a).tg_message_id
    expect(mid).toBeGreaterThan(0)
    const mirror = r.store.db.query("SELECT * FROM outbox WHERE id = ? AND mode = 'mirror'").get(a) as any
    expect(mirror?.status).toBe('sent')
    expect(r.store.messageText(String(OWNER), mid)?.text).toContain('hello there')
  })

  test('a Telegram outage: the retry window lives in the hub row (result) across passes; one send once the link is back', async () => {
    const r = await start({ h: home({ send: false }) })
    r.mock.down = true // Telegram unreachable; the hub (local) still answers
    const a = r.hub.enqueue('send', { text: 'Disk 3 at 55 °C' })
    const t0 = Date.now() / 1000
    const deps = { ...r.adapter.outboxDeps(), lastOk: () => t0 - 5 }
    let t = t0
    for (let i = 0; i < MAX_ATTEMPTS + 3; i++) { await drainOutbox(deps, () => t); t = Math.max(t + 1, Number(r.hub.row(a).not_before)) }
    expect(r.hub.row(a)).toMatchObject({ status: 'queued', attempts: MAX_ATTEMPTS + 3 })
    expect(JSON.parse(String(r.hub.row(a).result)).retry).toMatchObject({ since: t0, strikes: 1 })
    expect(r.hub.kinds('outbox.failed')).toEqual([])
    r.mock.down = false
    await drainOutbox(deps, () => t)
    expect(r.hub.row(a).status).toBe('sent')
    expect(r.mock.sent('sendMessage').map(c => c.params.text)).toEqual(['Disk 3 at 55 °C'])
  })

  test('ops parity: send (files, album, voice, document), edit, react, poll, draft, chat_action, delete, pin, location, commands, download', async () => {
    const r = await start()
    const { hub, mock } = r
    const out = join(hub.filesDir, 'out')
    writeFileSync(join(out, 'a.png'), png(40, 30)); writeFileSync(join(out, 'b.png'), png(30, 40))
    writeFileSync(join(out, 'v.ogg'), 'OggS'); writeFileSync(join(out, 'x.pdf'), '%PDF-1.4')
    mock.addFile('doc1', 'hello file', 'pdf')
    const ids = [
      hub.enqueue('send', { text: 'look' }, { files: ['hubfile:out/a.png', 'hubfile:out/x.pdf'] }),
      hub.enqueue('send', { text: '', album: true }, { files: ['hubfile:out/a.png', 'hubfile:out/b.png'] }),
      hub.enqueue('send', { text: '', files: ['hubfile:out/v.ogg'] }),
      hub.enqueue('edit', { message_id: 5, text: 'edited' }),
      hub.enqueue('react', { message_id: 5, emoji: '👍' }),
      hub.enqueue('poll', { question: 'Q?', options: ['a', 'b'] }),
      hub.enqueue('draft', { text: 'thinking' }),
      hub.enqueue('chat_action', { action: 'upload_photo' }),
      hub.enqueue('delete', { message_id: 7 }),
      hub.enqueue('pin', { message_id: 8 }),
      hub.enqueue('location', { latitude: 1.5, longitude: 2.5 }),
      hub.enqueue('commands', { commands: [{ command: 'space', description: 'Disk space' }] }, { chat: '-' }),
    ]
    const dl = hub.enqueue('download', { file_id: 'doc1' }, { chat: '-' })
    const outside = join(r.h.home, 'secret.txt'); writeFileSync(outside, 's')
    const refused = hub.enqueue('send', { text: '', files: [outside] })
    expect(await waitFor(() => [...ids, dl, refused].every(i => ['sent', 'failed'].includes(hub.row(i).status)), 8000)).toBe(true)
    for (const i of ids) expect({ op: hub.row(i).op, status: hub.row(i).status, err: hub.row(i).last_error ?? '' }).toMatchObject({ status: 'sent' })
    expect(mock.sent('sendPhoto').length).toBeGreaterThanOrEqual(1)
    expect(mock.sent('sendMediaGroup').length).toBe(1)
    expect(mock.sent('sendDocument').length).toBe(1)
    expect(mock.sent('sendVoice').length).toBe(1)
    const path = JSON.parse(hub.row(dl).result).path
    expect(path).toMatch(/^hubfile:in\//)
    expect(existsSync(join(hub.filesDir, 'in', path.slice('hubfile:in/'.length)))).toBe(true)
    expect(hub.row(refused).status).toBe('failed')
    expect(hub.row(refused).last_error).toMatch(/refusing to send/)
  })
})

describe('inbound prep and bindings through the companion', () => {
  test('a photo shows as a hub ref; a voice note is transcribed by the companion', async () => {
    const r = await start({ companion: true })
    r.mock.addFile('ph1', png(50, 50), 'jpg')
    r.mock.push({ message: base({ photo: [{ file_id: 'ph1', file_unique_id: 'uph1', width: 50, height: 50, file_size: 300 }], caption: 'pic' }) })
    r.mock.addFile('vo1', 'OggS-voice', 'oga')
    r.mock.push({ message: base({ voice: { file_id: 'vo1', file_unique_id: 'uvo1', duration: 3, mime_type: 'audio/ogg', file_size: 10 } }) })
    expect(await waitFor(() => r.hub.kinds('tg.message').length === 2, 8000)).toBe(true)
    const [photo, voice] = r.hub.kinds('tg.message').map(e => JSON.parse(e.payload))
    expect(photo.image_path).toMatch(/^hubfile:in\//)
    expect(voice.text).toContain('hello from the voice note')
    expect(voice.transcript).toBe('auto')
    expect(r.companion!.stats.by_kind.transcribe).toBe(1)
  })

  test('companion offline: transcribe_error, and a bound command goes to the brain', async () => {
    const h = home()
    const hub = new FakeHub(join(h.home, 'tgfiles'))
    hub.bindings = [{ id: 1, kind: 'command', key: 'say', tool: 'say', args: '[]', note: 1, on_error: 'reply', why: 't', created: 0, expires: null, active: 1 }]
    const r = await start({ h, hub })
    r.mock.addFile('vo2', 'OggS', 'oga')
    r.mock.push({ message: base({ voice: { file_id: 'vo2', file_unique_id: 'uvo2', duration: 2, mime_type: 'audio/ogg', file_size: 4 } }) })
    expect(await waitFor(() => r.hub.kinds('tg.message').length === 1, 5000)).toBe(true)
    expect(JSON.parse(r.hub.kinds('tg.message')[0]!.payload).transcribe_error).toMatch(/companion is offline/)
    r.mock.text(OWNER, '/say hi')
    expect(await waitFor(() => r.hub.kinds('tg.bound').length === 1, 5000)).toBe(true)
    const b = r.hub.kinds('tg.bound')[0]!
    expect(b.delivery).toBe('now')
    expect(JSON.parse(b.payload).instant_error ?? JSON.parse(b.payload).status_error).toMatch(/companion is offline/)
  })

  test('bound command and button run in the companion; the ack does not wait for the job; unbound press is a row', async () => {
    const h = home()
    const hub = new FakeHub(join(h.home, 'tgfiles'))
    hub.bindings = [
      { id: 1, kind: 'command', key: 'say', tool: 'say', args: '[]', note: 1, on_error: 'reply', why: 't', created: 0, expires: null, active: 1 },
      { id: 2, kind: 'button', key: 'slow', tool: 'slow.button', args: '[]', note: 0, on_error: 'reply', why: 't', created: 0, expires: null, active: 1 },
      { id: 3, kind: 'command', key: 'pic', tool: 'pic', args: '[]', note: 0, on_error: 'reply', why: 't', created: 0, expires: null, active: 1 },
    ]
    const r = await start({ h, hub, companion: true })
    r.mock.text(OWNER, '/say hi there')
    expect(await waitFor(() => r.mock.sent('sendMessage').some(c => /said: hi there/.test(c.params.text)), 6000)).toBe(true)
    const t0 = Date.now()
    r.mock.press(OWNER, 'slow', 'Slow')
    expect(await waitFor(() => r.mock.sent('answerCallbackQuery').length === 1)).toBe(true)
    expect(Date.now() - t0).toBeLessThan(800) // the tool sleeps 1 s
    expect(await waitFor(() => r.mock.sent('sendMessage').some(c => /slow done slow/.test(c.params.text)), 6000)).toBe(true)
    r.mock.text(OWNER, '/pic')
    expect(await waitFor(() => r.mock.sent('sendPhoto').length === 1, 6000)).toBe(true)
    expect(r.mock.uploads.some(u => u.method === 'sendPhoto')).toBe(true)
    r.mock.press(OWNER, 'nobind', 'X')
    expect(await waitFor(() => r.hub.kinds('tg.button').length === 1, 4000)).toBe(true)
    const said = r.hub.kinds('tg.message').find(e => JSON.parse(e.payload).text === '/say hi there')!
    expect(said.status).toBe('acked')
    expect(r.hub.deliveries.get(said.id)?.how).toBe('binding')
    expect(r.companion!.stats.by_kind.bind).toBe(3)
  })
})

describe('companion rules', () => {
  test('tools by name only, effect ≤ send_owner, callback extras only; a picture is uploaded as a ref', async () => {
    const h = home()
    const hub = new FakeHub(join(h.home, 'tgfiles'))
    const c = new Companion({ hub: new HubClient({ url: HUBCFG.url, token: BRAIN_TOKEN, fetch: hub.fetch }), paths: h.paths,
      env: { ...h.env, PATH: process.env.PATH ?? '/usr/bin:/bin', TG_ADAPTER_TOOLS: join(h.home, 'tools.toml') }, log: () => {}, noLookups: true })
    expect((await c.bind({ tool: 'nope' })).error).toMatch(/not in the tools registry/)
    expect((await c.bind({ tool: 'spender' })).error).toMatch(/needs an approval/)
    expect((await c.bind({ tool: 'say', extra: ['a'.repeat(600)] })).error).toMatch(/bad binding arguments/)
    expect((await c.bind({ tool: 'say', arg: 'x', env: { TELEGRAM_CALLBACK_DATA: 'd', PATH: '/evil', LD_PRELOAD: 'x' } })).stdout).toContain('said: x')
    const env = c.toolEnv({ TELEGRAM_CALLBACK_DATA: 'd' })
    expect(env.PATH!.split(':')[0]).toBe(join(h.home, 'bin'))
    const pic = await c.bind({ tool: 'pic' })
    const ref = JSON.parse(pic.stdout).photo
    expect(ref).toMatch(/^hubfile:out\//)
    expect(existsSync(join(hub.filesDir, 'out', ref.slice('hubfile:out/'.length)))).toBe(true)
    expect((await c.transcribe({ file: 'hubfile:in/missing.oga' })).ok).toBe(false)
  })
})

describe('approvals over the hub', () => {
  test('a card gets a hub nonce; his tap decides there; one approval.decided row; the expiry sweep edits the card', async () => {
    const r = await start()
    const { hub, mock } = r
    const nonce = hub.request({ action: 'book flight X', summary: 'Book X', ttl: 600, chat: String(OWNER) }) // the brain mints
    const id = hub.enqueue('send', { text: 'Book it?', approval: { nonce, action: 'book flight X' } })
    expect(await waitFor(() => hub.row(id).status === 'sent')).toBe(true)
    expect(hub.approvals.get(nonce)!.message_id).toBe(hub.row(id).tg_message_id)
    const sent = mock.sent('sendMessage').find(c => c.params.text?.includes('Book it?'))!
    expect(JSON.stringify(sent.params.reply_markup)).toContain(`ap:${nonce}:y`)
    mock.press(OWNER, `ap:${nonce}:y`, '✅ Approve', { message_id: hub.row(id).tg_message_id, text: 'Book it?' })
    expect(await waitFor(() => hub.kinds('approval.decided').length === 1, 4000)).toBe(true)
    expect(hub.approvals.get(nonce)!.status).toBe('approved')
    expect(hub.approvals.get(nonce)!.decided_by).toBe(String(OWNER))
    expect(JSON.parse(hub.kinds('approval.decided')[0]!.payload).outcome).toBe('approved')
    await Bun.sleep(400)
    expect(hub.kinds('approval.decided').length).toBe(1)
    const n2 = hub.request({ action: 'x', summary: 'S', ttl: 1, chat: String(OWNER) })
    hub.approvals.get(n2)!.message_id = 77
    await Bun.sleep(1100)
    await r.adapter.sweepApprovals(true)
    expect(hub.approvals.get(n2)!.status).toBe('expired')
    expect(hub.approvals.get(n2)!.card).toBe('edited')
    expect(JSON.stringify(mock.sent('editMessageReplyMarkup').at(-1)!.params)).toContain('expired')
  })

  test('a brain-minted nonce is used as is; a wrong action is refused', async () => {
    const r = await start()
    const n = r.hub.request({ action: 'pay 10', summary: 'Pay', chat: String(OWNER) })
    const ok = r.hub.enqueue('send', { text: 'Pay?', approval: { nonce: n } })
    const bad = r.hub.enqueue('send', { text: 'Pay??', approval: { nonce: n, action: 'pay 1000' } })
    expect(await waitFor(() => r.hub.row(ok).status === 'sent' && r.hub.row(bad).status === 'failed')).toBe(true)
    expect(r.hub.row(bad).last_error).toMatch(/does not match/)
    const mint = r.hub.enqueue('send', { text: 'Mint?', approval: { action: 'a', summary: 'S' } })
    expect(await waitFor(() => r.hub.row(mint).status === 'failed')).toBe(true)
    expect(r.hub.row(mint).last_error).toBe('hub mode: the brain mints approvals; send approval:{nonce}')
    expect(r.hub.kinds('outbox.failed').some(e => JSON.parse(e.payload).outbox_id === mint)).toBe(true)
  })

  test('the hub tg never calls POST /approvals, even where hubd would let it', async () => {
    const r = await start()
    r.hub.tgMayRequest = true // a hubd that allowed it changes nothing: minting is the brain's
    const a = r.hub.enqueue('send', { text: 'Mint?', approval: { action: 'pay 5', summary: 'Pay 5', ttl_s: 600 } })
    const b = r.hub.enqueue('send', { text: 'Mint too?', approval: { action_sha: 'f'.repeat(64), summary: 'Pay 6' } })
    expect(await waitFor(() => r.hub.row(a).status === 'failed' && r.hub.row(b).status === 'failed')).toBe(true)
    expect(r.hub.row(b).last_error).toBe('hub mode: the brain mints approvals; send approval:{nonce}')
    expect(r.hub.calls.filter(c => c.method === 'POST' && c.path === '/approvals')).toEqual([])
    expect(r.hub.approvals.size).toBe(0)
    expect(r.mock.sent('sendMessage').some(c => c.params.text?.includes('Mint?'))).toBe(false)
  })
})

describe('mirrors', () => {
  test('bindings: every kind but card from the hub; presence: board file, zone and sleep-in', async () => {
    const r = await start({ rules: undefined })
    r.store.db.query("INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active) VALUES ('card', '111:5:x', 'say', '[]', 0, 'reply', 'card', 0, NULL, 1)").run()
    r.hub.bindings = [{ id: 9, kind: 'command', key: 'say', tool: 'say', args: '[]', note: 0, on_error: 'reply', why: 'hub', created: 0, expires: null, active: 1 }]
    r.hub.bindingsVersion++
    await r.adapter.bindMirror!.sync()
    const local = r.store.db.query('SELECT kind, key FROM bindings ORDER BY kind').all() as any[]
    expect(local).toEqual([{ kind: 'card', key: '111:5:x' }, { kind: 'command', key: 'say' }])
    r.hub.presence = { version: 1, state: { board: { working: true, idle_at: 0 }, zone: 'Asia/Tokyo', sleeping: true } }
    const board = join(r.h.paths.stateDir, 'presence-board.json')
    expect(await waitFor(() => existsSync(board), 3000)).toBe(true)
    expect(JSON.parse(readFileSync(board, 'utf8')).working).toBe(true)
    expect(r.adapter.rules().zone()).toBe('Asia/Tokyo')
    expect(r.adapter.rules().sleeping()).toBe(true)
    const st = statusOf(r.store, r.h.paths, loadConfig(r.h.paths.config, r.h.home, { testRun: true }))
    expect(st.hub).toMatchObject({ host: 'hub.invalid:8700', backlog: 0, last_error: '' })
    expect(st.hub.companion_seen_age_s).toBeLessThan(5)
  })
})

describe('companion CLI', () => {
  test('--once claims a job and posts presence; status reads its stats; the off switch keeps it down (exit 7)', async () => {
    const h = home()
    const hub = new FakeHub(join(h.home, 'tgfiles'))
    const srv = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: hub.fetch })
    try {
      writeFileSync(join(h.home, '.secrets', 'hub-token'), BRAIN_TOKEN + '\n', { mode: 0o600 })
      chmodSync(join(h.home, '.secrets', 'hub-token'), 0o600)
      const env = { ...h.env, PATH: process.env.PATH ?? '/usr/bin:/bin', HUB_URL: `http://127.0.0.1:${srv.port}`, TG_ADAPTER_TOOLS: join(h.home, 'tools.toml') }
      hub.lastClaimAt = Date.now()
      const job = await (await hub.fetch(new Request('http://hub.invalid/tg/jobs', { method: 'POST', headers: { authorization: `Bearer ${TG_TOKEN}` },
        body: JSON.stringify({ rid: 'r1', kind: 'bind', args: { tool: 'say', arg: 'from the cli' }, timeout_s: 30 }) }))).json() as any
      const cli = ['bun', join(import.meta.dir, '..', 'src', 'cli.ts'), 'companion', '--instance', 'test']
      // async spawn: the fake hub answers from this process's event loop
      const sh = async (args: string[]) => {
        const c = Bun.spawn(args, { env, cwd: h.home, stdout: 'pipe', stderr: 'pipe' })
        const code = await c.exited
        return { code, out: await new Response(c.stdout).text(), err: await new Response(c.stderr).text() }
      }
      const p = await sh([...cli, '--once', '--wait', '1'])
      expect(p.code, p.err.slice(0, 300)).toBe(0)
      const j = hub.jobs.find(x => x.id === job.id)!
      expect(j.state).toBe('done')
      expect(JSON.stringify(j.result)).toContain('said: from the cli')
      expect(hub.presence.version).toBeGreaterThan(0)
      expect(JSON.parse((await sh([...cli, 'status', '--json'])).out)).toMatchObject({ running: false, jobs: 1 })
      writeFileSync(join(h.paths.configDir, 'test.companion-off'), '')
      expect((await sh([...cli, '--once'])).code).toBe(7)
    } finally { srv.stop(true) }
  }, 30_000)
})

describe('hub file names', () => {
  test('names follow hubd FILE_NAME: a leading dot or dash becomes _', async () => {
    const { safeName, NAME_RE, REF_RE } = await import('../src/hub.ts')
    expect(safeName('-AgADx.jpg')).toBe('_AgADx.jpg')
    expect(safeName('..secret')).toBe('_secret')
    expect(safeName('a b/c?.ogg')).toBe('c_.ogg')
    for (const n of ['-AgADx.jpg', '.x', '']) expect(NAME_RE.test(n)).toBe(false)
    expect(NAME_RE.test('_AgADx.jpg')).toBe(true)
    expect(REF_RE.test('hubfile:in/-x.jpg')).toBe(false)
    expect(REF_RE.test('hubfile:out/0a1b-photo.png')).toBe(true)
  })
})
