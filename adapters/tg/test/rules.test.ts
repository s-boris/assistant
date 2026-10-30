// Outbox rules, offline on the mock Bot API: quiet hours on the owner's clock (tripclock zone),
// sleep-in holds, urgent bypass, edit/react never held, dedupe (idem key + same text within the
// window), 429 retry_after and 5xx backoff → outbox.failed event rows, per-chat order (retry waits
// block their chat only; held and scheduled rows wait alone), and the config keys.
import { afterAll, afterEach, describe, expect, test } from 'bun:test'
import { chmodSync, mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { ConfigError, parseConfig, parseQuietHours } from '../src/config.ts'
import { drainOutbox, failInterrupted } from '../src/outbox.ts'
import { dedupeKey, holdReason, NO_RULES, SleepinGuard, sleepinArgv } from '../src/rules.ts'
import { cleanup, makeHome, outbox, rows, startAdapter, type Home, type Running } from './helpers.ts'
import { MockBot, OWNER } from './mockbot.ts'

afterAll(cleanup)
let running: Running | null = null
afterEach(async () => { if (running) await running.stop(); running = null })

const BOT = 'testadapter_bot'
const CHAT = String(OWNER)
const OTHER = '222'
// 22.10.2026 03:00 in Zurich (CEST, UTC+2) and 08:30 there; 21.10 20:00 Zurich = 22.10 03:00 Tokyo.
const NIGHT = Date.UTC(2026, 9, 22, 1, 0) / 1000
const MORNING = Date.UTC(2026, 9, 22, 6, 30) / 1000
const NOON = Date.UTC(2026, 9, 22, 10, 0) / 1000
const TOKYO_NIGHT = Date.UTC(2026, 9, 21, 18, 0) / 1000

/** A temp-HOME script (sh) for the tripclock / sleepin seams. */
function script(h: Home, name: string, body: string): string {
  const dir = join(h.home, 'fake')
  mkdirSync(dir, { recursive: true })
  const p = join(dir, name)
  writeFileSync(p, `#!/bin/sh\n${body}\n`)
  chmodSync(p, 0o755)
  return p
}

let sleeping = false
async function setup(config: Record<string, unknown> = {}, opts: { env?: Record<string, string>; rules?: Record<string, unknown> } = {}) {
  sleeping = false
  const mock = new MockBot()
  const h = makeHome({ send: false, quiet_hours: '0-8', allow_from: [CHAT, OTHER], ...config })
  running = await startAdapter(mock, h, { env: opts.env, rules: opts.rules ?? { zone: () => 'Europe/Zurich', sleeping: () => sleeping } })
  const r = running
  const q = (op: string, body: Record<string, unknown>, extra: Record<string, unknown> = {}) =>
    r.store.enqueue({ bot: BOT, chat: CHAT, op, body, ...extra })!
  const at = (t: number) => drainOutbox(r.adapter.outboxDeps(), () => t)
  return { mock, h, r, q, at }
}

const texts = (mock: MockBot) => mock.sent('sendMessage').map(c => c.params.text)
const status = (r: Running, id: number) => outbox(r.store, id).status

describe('quiet hours', () => {
  test('non-urgent send/poll wait at night; urgent, edit and react go; at 08:30 the held rows go in order', async () => {
    const { mock, r, q, at } = await setup()
    const a = q('send', { text: 'night one' })
    const p = q('poll', { question: 'Q?', options: ['a', 'b'] })
    const u = q('send', { text: 'urgent' }, { urgent: true })
    const e = q('edit', { message_id: 7, text: 'edited' })
    const re = q('react', { message_id: 7, emoji: '👍' })
    const b = q('send', { text: 'night two', silent: true })
    expect(holdReason(r.adapter.rules(), NIGHT)).toBe('quiet hours 0-8 Europe/Zurich')
    await at(NIGHT)
    expect([a, p, b].map(id => status(r, id))).toEqual(['queued', 'queued', 'queued'])
    expect([u, e, re].map(id => status(r, id))).toEqual(['sent', 'sent', 'sent'])
    expect(texts(mock)).toEqual(['urgent'])
    expect(holdReason(r.adapter.rules(), MORNING)).toBeNull()
    await at(MORNING)
    expect([a, p, b].map(id => status(r, id))).toEqual(['sent', 'sent', 'sent'])
    expect(texts(mock)).toEqual(['urgent', 'night one', 'night two'])
    const order = mock.calls.filter(c => c.method === 'sendMessage' || c.method === 'sendPoll').map(c => c.params.text ?? c.params.question)
    expect(order).toEqual(['urgent', 'night one', 'Q?', 'night two'])
  })

  test('the zone is tripclock\'s (03:00 in Tokyo holds while Zurich has 20:00); tripclock failing → home zone', async () => {
    const mock = new MockBot()
    const h = makeHome({ send: false, quiet_hours: '0-8' })
    const tc = script(h, 'tripclock', 'echo Asia/Tokyo')
    running = await startAdapter(mock, h, { env: { TELEGRAM_TRIPCLOCK_CMD: `${tc} tz`, TELEGRAM_QUIET_TZ: '' } })
    await running.adapter.quiet.refresh()
    expect(running.adapter.quiet.state()).toMatchObject({ zone: 'Asia/Tokyo', source: 'tripclock' })
    const id = running.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'trip night' } })!
    await drainOutbox(running.adapter.outboxDeps(), () => TOKYO_NIGHT)
    expect(status(running, id)).toBe('queued')
    writeFileSync(tc, '#!/bin/sh\nexit 3\n')
    await running.adapter.quiet.refresh()
    expect(running.adapter.quiet.state()).toMatchObject({ source: 'home' })
    await drainOutbox(running.adapter.outboxDeps(), () => TOKYO_NIGHT)
    expect(status(running, id)).toBe('sent') // 20:00 on the home clock
  })

  test('quiet_hours "" = off; a wrapping window (22-6)', async () => {
    const { r, q, at } = await setup({ quiet_hours: '' })
    const a = q('send', { text: 'x' })
    await at(NIGHT)
    expect(status(r, a)).toBe('sent')
    const rules = { ...NO_RULES, quietHours: '22-6', zone: () => 'Europe/Zurich' }
    expect(holdReason(rules, NIGHT)).not.toBeNull()
    expect(holdReason(rules, Date.UTC(2026, 9, 21, 20, 30) / 1000)).not.toBeNull() // 22:30
    expect(holdReason(rules, NOON)).toBeNull()
  })
})

describe('sleep-in', () => {
  test('a guarded morning holds non-urgent send/poll; urgent goes; the hold ends → sent', async () => {
    const { mock, r, q, at } = await setup()
    sleeping = true
    const a = q('send', { text: 'late morning' })
    const u = q('send', { text: 'server down' }, { urgent: true })
    await at(NOON)
    expect([status(r, a), status(r, u)]).toEqual(['queued', 'sent'])
    sleeping = false
    await at(NOON)
    expect(texts(mock)).toEqual(['server down', 'late morning'])
  })

  test('`sleepin active`: exit 0 holds, 1 does not, anything else (or a timeout) is an error that never holds', async () => {
    const h = makeHome()
    const sl = script(h, 'sleepin', 'exit "$(cat "$0.rc")"')
    const set = (rc: string) => writeFileSync(`${sl}.rc`, rc)
    const logs: string[] = []
    const g = new SleepinGuard([sl, 'active'], { log: m => logs.push(m), timeoutMs: 400 })
    set('0'); await g.refresh(); expect(g.state()).toMatchObject({ holding: true, error: null })
    set('1'); await g.refresh(); expect(g.state()).toMatchObject({ holding: false, error: null })
    set('0'); await g.refresh(); set('2'); await g.refresh()
    expect(g.state().holding).toBe(false)
    expect(g.state().error).toMatch(/^exit 2/)
    writeFileSync(sl, '#!/bin/sh\nsleep 5\n')
    await g.refresh()
    expect(g.state()).toMatchObject({ holding: false })
    expect(g.state().error).toMatch(/timed out/)
    expect(logs.some(l => /sleep-in: holding/.test(l)) && logs.some(l => /check failed/.test(l))).toBe(true)
  })

  test('the command: TG_ADAPTER_SLEEPIN_CMD, else ~/bin/sleepin active when executable; config sleepin:false = none', async () => {
    const h = makeHome()
    expect(sleepinArgv({}, h.home)).toBeNull()
    mkdirSync(join(h.home, 'bin'), { recursive: true })
    writeFileSync(join(h.home, 'bin', 'sleepin'), '#!/bin/sh\nexit 0\n', { mode: 0o755 })
    expect(sleepinArgv({}, h.home)).toEqual([join(h.home, 'bin', 'sleepin'), 'active'])
    expect(sleepinArgv({ TG_ADAPTER_SLEEPIN_CMD: '' }, h.home)).toBeNull()
    expect(sleepinArgv({ TG_ADAPTER_SLEEPIN_CMD: '~/x/sl active --at now' }, h.home)).toEqual([join(h.home, 'x/sl'), 'active', '--at', 'now'])
    // wired into the adapter: the env's command is what it runs
    const mock = new MockBot()
    const sl = script(h, 'sl', 'exit 0')
    running = await startAdapter(mock, h, { env: { TG_ADAPTER_SLEEPIN_CMD: sl } })
    await running.adapter.sleepin.refresh()
    expect(running.adapter.rules().sleeping()).toBe(true)
    await running.stop(); running = null
    const h2 = makeHome({ sleepin: false })
    running = await startAdapter(new MockBot(), h2, { env: { TG_ADAPTER_SLEEPIN_CMD: sl } })
    expect(running.adapter.sleepin.argv).toBeNull()
  })
})

describe('dedupe', () => {
  test('idem key: a repeat is never inserted', async () => {
    const { r } = await setup()
    expect(r.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'a' }, idem_key: 'k1' })).not.toBeNull()
    expect(r.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'b' }, idem_key: 'k1' })).toBeNull()
  })

  test('the same text to the same chat within 10 min is skipped; outside it, with another reply_to, opted out, after a failure: sent', async () => {
    const { mock, r, q, at } = await setup()
    const a = q('send', { text: 'backup failed' })
    const dup = q('send', { text: 'backup failed' })
    const otherReply = q('send', { text: 'backup failed', reply_to: 5 })
    const optOut = q('send', { text: 'backup failed', dedupe: false })
    const otherChat = r.store.enqueue({ bot: BOT, chat: OTHER, op: 'send', body: { text: 'backup failed' } })!
    const withDetails = q('send', { text: 'backup failed', details: 'disk 3' })
    await at(NOON)
    expect(status(r, a)).toBe('sent')
    expect(outbox(r.store, dup)).toMatchObject({ status: 'skipped', last_error: `duplicate of #${a} (same text within 600s)` })
    expect([otherReply, optOut, otherChat, withDetails].map(id => status(r, id))).toEqual(['sent', 'sent', 'sent', 'sent'])
    // outside the window
    r.store.db.query('UPDATE outbox SET ts = ts - 700').run()
    const later = q('send', { text: 'backup failed' })
    // an earlier failed row is not a duplicate (a retry by the writer)
    r.store.setOutbox(a, { status: 'failed' })
    await at(NOON)
    expect(status(r, later)).toBe('sent')
    expect(texts(mock).filter(t => t === 'backup failed').length).toBe(5) // a, other reply_to, opted out, other chat, later
  })

  test('polls by question + options; held rows count (the night repeat is dropped while both wait); window 0 = off', async () => {
    const { r, q, at } = await setup()
    const p1 = q('poll', { question: 'Q?', options: ['a', 'b'] })
    const p2 = q('poll', { question: 'Q?', options: ['a', 'b'] })
    const p3 = q('poll', { question: 'Q?', options: ['a', 'c'] })
    const n1 = q('send', { text: 'night' })
    const n2 = q('send', { text: 'night' })
    await at(NIGHT)
    expect([p1, p2, p3, n1, n2].map(id => status(r, id))).toEqual(['queued', 'skipped', 'queued', 'queued', 'skipped'])
    await at(MORNING)
    expect([p1, p3, n1].map(id => status(r, id))).toEqual(['sent', 'sent', 'sent'])
    expect(dedupeKey('send', { text: 'x' })).toBe(dedupeKey('send', { text: 'x', buttons: [{ text: 'A', data: 'a' }] }))
    expect(dedupeKey('edit', { text: 'x' })).toBeNull()
    await running!.stop(); running = null
    const s = await setup({ dedupe_window_s: 0 })
    const x = s.q('send', { text: 'same' })
    const y = s.q('send', { text: 'same' })
    await s.at(NOON)
    expect([status(s.r, x), status(s.r, y)]).toEqual(['sent', 'sent'])
  })
})

describe('errors → outbox.failed events', () => {
  const e500 = { status: 500, body: { ok: false, error_code: 500, description: 'Internal Server Error' } }

  test('5xx: backoff (2, 4, 8, 16 s), then failed after 5 attempts with one outbox.failed row (no text)', async () => {
    const { mock, r, q } = await setup()
    mock.failures.sendMessage = Array(5).fill(e500)
    const a = q('send', { text: 'secret words' }, { urgent: true, trace_id: 'tr1' })
    let t = NOON
    const waits: number[] = []
    for (let i = 0; i < 5; i++) {
      await drainOutbox(r.adapter.outboxDeps(), () => t)
      const row = outbox(r.store, a)
      if (row.status === 'queued') { waits.push(Math.round(row.not_before - t)); t = row.not_before + 0.01 }
    }
    expect(waits).toEqual([2, 4, 8, 16])
    expect(outbox(r.store, a)).toMatchObject({ status: 'failed', attempts: 5 })
    const ev = rows(r.store, 'outbox.failed')
    expect(ev.length).toBe(1)
    expect(ev[0]).toMatchObject({ source: 'tg-adapter', idem_key: `outbox.failed:${BOT}:${a}`, delivery: 'now', status: 'new', trace_id: 'tr1' })
    expect(ev[0].payload).toMatchObject({ outbox_id: a, op: 'send', chat: CHAT, attempts: 5, urgent: 1, partial: false })
    expect(ev[0].payload.error).toMatch(/500: Internal Server Error/)
    expect(JSON.stringify(ev[0])).not.toContain('secret words')
  })

  test('429 waits retry_after and blocks only that chat; 400, bad body and interrupted rows fail with events (delivery beat)', async () => {
    const { mock, r, q, at } = await setup()
    mock.failures.sendMessage = [{ status: 429, body: { ok: false, error_code: 429, description: 'Too Many Requests: retry after 7', parameters: { retry_after: 7 } } }]
    const a = q('send', { text: 'first' })
    const b = q('send', { text: 'second' })
    const c = r.store.enqueue({ bot: BOT, chat: OTHER, op: 'send', body: { text: 'other chat' } })!
    await at(NOON)
    expect(outbox(r.store, a)).toMatchObject({ status: 'queued', attempts: 1, not_before: NOON + 7 })
    expect([status(r, b), status(r, c)]).toEqual(['queued', 'sent'])
    await at(NOON + 3)
    expect(status(r, b)).toBe('queued')
    await at(NOON + 8)
    expect([status(r, a), status(r, b)]).toEqual(['sent', 'sent'])
    expect(rows(r.store, 'outbox.failed')).toEqual([])
    const bad = q('react', { message_id: 3, emoji: '🦖' })
    r.store.db.query("INSERT INTO outbox(ts, bot, chat, op, body) VALUES (?, ?, ?, 'send', '[1]')").run(NOON, BOT, CHAT)
    const badBody = Number((r.store.db.query('SELECT max(id) AS m FROM outbox').get() as any).m)
    await at(NOON + 9)
    const stuck = q('send', { text: 'was sending' })
    r.store.setOutbox(stuck, { status: 'sending', attempts: 1 })
    expect(failInterrupted(r.store, BOT)).toBe(1)
    const ev = rows(r.store, 'outbox.failed')
    expect(ev.map(e => e.payload.outbox_id)).toEqual([bad, badBody, stuck])
    expect(ev.map(e => e.delivery)).toEqual(['beat', 'beat', 'beat'])
    expect(ev[1].payload.error).toMatch(/^bad body/)
    expect(ev[2].payload.error).toBe('interrupted while sending (may have been delivered)')
    // a second pass never duplicates an event
    expect(failInterrupted(r.store, BOT)).toBe(0)
    expect(rows(r.store, 'outbox.failed').length).toBe(3)
  })

  test('a partial send fails with partial: true in the event', async () => {
    const { mock, r, q, at } = await setup()
    const orig = mock.handle.bind(mock)
    let n = 0
    mock.handle = async (req: Request) => {
      if (req.url.endsWith('/sendMessage') && ++n === 2) return Response.json({ ok: false, error_code: 502, description: 'Bad Gateway' }, { status: 502 })
      return orig(req)
    }
    const a = q('send', { text: 'y'.repeat(5000) })
    await at(NOON)
    expect(status(r, a)).toBe('failed')
    expect(rows(r.store, 'outbox.failed')[0].payload).toMatchObject({ outbox_id: a, partial: true })
  })
})

describe('per-chat order', () => {
  test('a scheduled row waits alone; a held row does not block an urgent one; a retry wait blocks its chat only', async () => {
    const { mock, r, q, at } = await setup()
    const sched = q('send', { text: 'at 13:00' }, { not_before: NOON + 3600 })
    const now1 = q('send', { text: 'now' })
    await at(NOON)
    expect([status(r, sched), status(r, now1)]).toEqual(['queued', 'sent'])
    await at(NOON + 3601)
    expect(status(r, sched)).toBe('sent')
    expect(texts(mock)).toEqual(['now', 'at 13:00'])
  })

  test('send on: the consumer runs the rules by itself (quiet hours all day: only urgent goes)', async () => {
    const mock = new MockBot()
    const h = makeHome({ send: true, quiet_hours: '0-24' })
    running = await startAdapter(mock, h)
    const a = running.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'held' } })!
    const u = running.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'urgent' }, urgent: true })!
    const ok = await (async () => { for (let i = 0; i < 100 && status(running!, u) !== 'sent'; i++) await Bun.sleep(20); return status(running!, u) === 'sent' })()
    expect(ok).toBe(true)
    await Bun.sleep(100)
    expect(status(running, a)).toBe('queued')
    expect(texts(mock)).toEqual(['urgent'])
  })
})

describe('config', () => {
  const base = { mode: 'test', bot: 'testadapter_bot', token_file: '/x/t.env', allow_from: ['111'] }
  test('defaults: quiet 0-8, sleep-in on, dedupe 600 s; validation', () => {
    expect(parseConfig(base, '/h')).toMatchObject({ quietHours: '0-8', sleepin: true, dedupeWindowS: 600 })
    expect(parseConfig({ ...base, quiet_hours: 'off', sleepin: false, dedupe_window_s: 0 }, '/h')).toMatchObject({ quietHours: '', sleepin: false, dedupeWindowS: 0 })
    expect(parseQuietHours(' 22-06 ')).toBe('22-6')
    for (const bad of ['8', '0-25', '5-5', 'night', 3]) expect(() => parseQuietHours(bad)).toThrow(ConfigError)
    expect(() => parseConfig({ ...base, sleepin: 'yes' }, '/h')).toThrow(/sleepin must be true or false/)
    expect(() => parseConfig({ ...base, dedupe_window_s: -1 }, '/h')).toThrow(/dedupe_window_s/)
  })
})

describe('network trouble and the gate', () => {
  test('a soft send failing at 23:50 stays held when the link heals at night and goes after 08:00; held hours are not retry time', async () => {
    const { mock, r, q } = await setup()
    const EVENING = Date.UTC(2026, 9, 21, 21, 50) / 1000 // 21.10 23:50 Zurich
    const deps = { ...r.adapter.outboxDeps(), lastOk: () => EVENING - 60 }
    const at = (t: number) => drainOutbox(deps, () => t)
    mock.down = true
    const a = q('send', { text: 'Disk 3 at 55 °C' })
    await at(EVENING)
    await at(EVENING + 120)
    expect(outbox(r.store, a)).toMatchObject({ status: 'queued', attempts: 2 })
    mock.down = false
    await at(EVENING + 1800) // 00:20: the link is back, but quiet hours hold the row
    expect(outbox(r.store, a)).toMatchObject({ status: 'queued', attempts: 2 })
    expect(texts(mock)).toEqual([])
    mock.down = true
    await at(MORNING) // 08:30, down again: 8 h 40 min after the first failure, yet it retries
    expect(outbox(r.store, a)).toMatchObject({ status: 'queued', attempts: 3 })
    mock.down = false
    await at(MORNING + 60)
    expect(outbox(r.store, a).status).toBe('sent')
    expect(texts(mock)).toEqual(['Disk 3 at 55 °C'])
    expect(rows(r.store, 'outbox.failed')).toEqual([])
  })
})
