// Approvals with nonce + TTL, offline against the mock Bot API (no sockets, temp HOME): an `approval`
// field on a send row renders the ✅/❌ row (ap:<nonce>:y|n) and the stored body keeps the nonce, never the action;
// his tap is decided at the callback answer (toast, approval.decided row with delivery now, "✓" mark); a repeat tap
// reports the first decision; taps that aren't his decide nothing; the expiry sweep swaps the keyboard for one inert
// "⌛ expired" button and a late tap approves nothing; refused approval rows send nothing; nonces made with
// `approvals new`; explicit ap: buttons; bindings never take ap: data; the approvals-off switch; the Python CLI
// consumes exactly once (same store, other language); restart mid-wait: a waiting `approvals wait` gets the tap the
// next adapter process decides, and cards that expired while it was down are edited at start.
import { afterAll, afterEach, describe, expect, test } from 'bun:test'
import { createHash } from 'crypto'
import { existsSync, readFileSync, statSync, writeFileSync } from 'fs'
import { dirname, join } from 'path'
import { approvalsDbFor, Approvals, APPROVALS_SQL, AP_RE, noncesIn, parseApprovalData } from '../src/approvals.ts'
import { drainOutbox } from '../src/outbox.ts'
import { cleanup, makeHome, outbox, rows, startAdapter, waitFor, writeConfig, type Home, type Running } from './helpers.ts'
import { MockBot, OWNER, STRANGER } from './mockbot.ts'

afterAll(cleanup)
let running: Running[] = []
afterEach(async () => { for (const r of running.splice(0)) await r.stop() })

const BOT = 'testadapter_bot'
const CHAT = String(OWNER)
const OTHER = 222 // allowlisted, not the owner
const REPO = join(import.meta.dir, '..', '..', '..')
// the scripts' interpreter: /usr/local/bin/python3.13 on the brain and in the CI runner (python:3.13 image, no /usr/bin/python3)
const PY = existsSync('/usr/local/bin/python3.13') ? '/usr/local/bin/python3.13' : '/usr/bin/python3'
const ACTION = 'webagent:wa-test:1:pay:Pay now:#pay:https://shop.example/checkout:EUR 139.62'
const sha = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex')

type Clock = { t: number; now: () => number }
const clock = (): Clock => { const c: Clock = { t: Date.now(), now: () => c.t }; return c }

async function setup(o: { config?: Record<string, unknown>; now?: () => number; env?: Record<string, string> } = {}) {
  const mock = new MockBot()
  const h = makeHome({ allow_from: [CHAT, String(OTHER)], ...(o.config ?? {}) })
  const r = await start(mock, h, o)
  return { mock, h, r }
}

async function start(mock: MockBot, h: Home, o: { now?: () => number; env?: Record<string, string> } = {}): Promise<Running> {
  const r = await startAdapter(mock, h, { ...(o.now ? { now: o.now } : {}), ...(o.env ? { env: o.env } : {}) })
  running.push(r)
  return r
}

async function stopOne(r: Running): Promise<void> {
  running = running.filter(x => x !== r)
  await r.stop()
}

/** Enqueue an approval card and send it: { id, mid, nonce, result }. */
async function card(r: Running, approval: Record<string, unknown>, extra: Record<string, unknown> = {}, chat = CHAT) {
  const id = r.store.enqueue({ bot: BOT, chat, op: 'send', body: { text: 'Book AZ 153?', approval, ...extra } })!
  await drainOutbox(r.adapter.outboxDeps())
  const row = outbox(r.store, id)
  const body = JSON.parse(row.body)
  return { id, row, body, mid: Number(row.tg_message_id), nonce: String(body.approval?.nonce ?? ''), result: row.result ? JSON.parse(row.result) : null }
}

function ap(r: Running, nonce: string): any {
  return r.adapter.approvals.get(nonce)
}

function decided(r: Running): any[] {
  return rows(r.store, 'approval.decided')
}

function toasts(mock: MockBot): string[] {
  return mock.sent('answerCallbackQuery').map(c => c.params.text)
}

/** The approvals CLI (Python, packages/approvals) on the adapter's store. */
function cli(h: Home, args: string[], extra: Record<string, string> = {}): { code: number; out: string; err: string } {
  const p = Bun.spawnSync([PY, '-I', join(REPO, 'scripts', 'approvals'), ...args], {
    env: { HOME: h.home, PATH: '/usr/bin:/bin', ASSISTANT_TEST: '1', APPROVALS_DB: h.paths.approvalsDb, ...extra },
  })
  return { code: p.exitCode ?? -1, out: p.stdout.toString(), err: p.stderr.toString() }
}

const STD = { action: ACTION, summary: 'Book AZ 153 · EUR 139.62', ttl_s: 900, ref: 'webagent:wa-test' }

describe('cards', () => {
  test('an approval field: ✅/❌ under the card, the body keeps the nonce (never the action), a note tells the nonce', async () => {
    const { mock, r, h } = await setup()
    const c = await card(r, STD, { buttons: [[{ text: '🖥 Open', url: 'https://example.com/' }]] })
    expect(c.row.status).toBe('sent')
    expect(c.nonce).toMatch(/^[0-9a-f]{32}$/)
    expect(c.row.body.includes(ACTION)).toBe(false)
    expect(c.body.approval).toEqual({ nonce: c.nonce, summary: STD.summary, expires: expect.any(Number), ref: STD.ref })
    const kb = mock.sent('sendMessage').at(-1)!.params.reply_markup.inline_keyboard
    expect(kb).toEqual([[{ text: '🖥 Open', url: 'https://example.com/' }],
      [{ text: '✅ Approve', callback_data: `ap:${c.nonce}:y` }, { text: '❌ Decline', callback_data: `ap:${c.nonce}:n` }]])
    expect(c.result.notes.join(' ')).toContain(`approval ${c.nonce} pending until `)
    const a = ap(r, c.nonce)
    expect([a.status, a.chat, a.message_id, a.action_sha, a.summary, a.ref]).toEqual(['pending', CHAT, c.mid, sha(ACTION), STD.summary, STD.ref])
    expect(a.expires - a.created).toBeCloseTo(900, 3)
    expect(statSync(h.paths.approvalsDb).mode & 0o777).toBe(0o600)
    expect(h.paths.approvalsDb).toBe(join(h.paths.stateDir, 'approvals.db')) // the test instance keeps its own
    // custom labels; the default TTL is 15 min
    const d = await card(r, { action: 'x', summary: 'y', yes: '✅ Do it', no: '❌ Stop' })
    expect(mock.sent('sendMessage').at(-1)!.params.reply_markup.inline_keyboard).toEqual([[
      { text: '✅ Do it', callback_data: `ap:${d.nonce}:y` }, { text: '❌ Stop', callback_data: `ap:${d.nonce}:n` }]])
    expect(ap(r, d.nonce).expires - ap(r, d.nonce).created).toBeCloseTo(900, 3)
  })

  test('refused approvals send nothing: another chat, a reply keyboard, an album, a missing summary, a bad TTL', async () => {
    const { mock, r } = await setup()
    const before = mock.sent('sendMessage').length
    const bad = [
      [await card(r, STD, {}, String(OTHER)), "owner's private chat only"],
      [await card(r, STD, { keyboard: [['a']] }), 'needs inline buttons'],
      [await card(r, STD, { album: true }), 'album'],
      [await card(r, { action: ACTION }), 'summary is required'],
      [await card(r, { ...STD, ttl_s: 0 }), 'ttl_s must be'],
      [await card(r, 'yes please' as any), 'approval must be an object'],
    ] as const
    for (const [c, why] of bad) {
      expect(c.row.status).toBe('failed')
      expect(c.row.last_error).toContain(why)
      expect(c.row.body.includes(ACTION)).toBe(false) // the hash stays, the action does not
    }
    expect(JSON.parse(bad[3][0].row.body).approval).toEqual({ action_sha: sha(ACTION) })
    expect(mock.sent('sendMessage').length).toBe(before)
    expect(r.adapter.approvals.db().query('SELECT COUNT(*) AS n FROM approvals').get()).toEqual({ n: 0 })
    expect(rows(r.store, 'outbox.failed').length).toBe(bad.length)
  })

  test('approval cards are never deduped (each one is its own)', async () => {
    const { mock, r } = await setup({ config: { dedupe_window_s: 600 } })
    const a = await card(r, STD)
    const b = await card(r, STD)
    expect([a.row.status, b.row.status]).toEqual(['sent', 'sent'])
    expect(a.nonce).not.toBe(b.nonce)
    expect(mock.sent('sendMessage').length).toBe(2)
  })
})

describe('taps', () => {
  test('his ✅: decided at the answer, approval.decided (delivery now), "✓" mark; the CLI consumes it exactly once', async () => {
    const { mock, r, h } = await setup()
    const c = await card(r, STD)
    mock.press(OWNER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => decided(r).length === 1)).toBe(true)
    expect(toasts(mock)).toEqual(['✅ Approved'])
    const ev = decided(r)[0]
    expect(ev.delivery).toBe('now')
    expect(ev.subject).toBe(`approval approved: ${STD.summary}`)
    expect(ev.next).toContain('approvals consume')
    expect(ev.payload).toMatchObject({ event_type: 'approval', nonce: c.nonce, decision: 'yes', outcome: 'approved', summary: STD.summary,
      ref: STD.ref, callback_data: `ap:${c.nonce}:y`, chat_id: CHAT, user_id: CHAT })
    expect(ev.payload.text).toBe(`(approval ✅ approved: "${STD.summary}" · ref ${STD.ref} · nonce ${c.nonce})`)
    expect(rows(r.store, 'tg.button')).toEqual([])
    expect(ap(r, c.nonce)).toMatchObject({ status: 'approved', decided_by: CHAT, decided_via: 'adapter' })
    expect(await waitFor(() => mock.sent('editMessageText').length === 1)).toBe(true)
    expect(mock.sent('editMessageText')[0]!.params).toMatchObject({ chat_id: CHAT, message_id: c.mid, text: 'Book AZ 153?\n\n✓ ✅ Approve' })
    // the other side: the approvals CLI on the same store
    expect(cli(h, ['consume', c.nonce, '--action', 'something else']).code).toBe(5)
    const first = cli(h, ['consume', c.nonce, '--action', ACTION, '--by', 'test', '--json'])
    expect(first.code).toBe(0)
    expect(JSON.parse(first.out)).toMatchObject({ code: 0, state: 'used', used_by: 'test' })
    expect(cli(h, ['consume', c.nonce, '--action', ACTION]).code).toBe(4)
    expect(cli(h, ['consume', `ap:${c.nonce}:y`, '--action', ACTION]).code).toBe(4)
    expect(cli(h, ['consume', 'f'.repeat(32), '--action', ACTION]).code).toBe(5)
    expect(ap(r, c.nonce).refused).toBe(1)
    const audit = cli(h, ['audit', '--json'])
    expect([audit.code, JSON.parse(audit.out).violations]).toEqual([0, 0])
    // a second tap reports the first decision and decides nothing
    mock.press(OWNER, `ap:${c.nonce}:n`, '❌ Decline', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => decided(r).length === 2)).toBe(true)
    expect(toasts(mock).at(-1)).toBe('✓ Already approved')
    expect(decided(r)[1].payload).toMatchObject({ outcome: 'approved', repeat: '1', decision: 'no' })
    expect(ap(r, c.nonce).status).toBe('approved')
  })

  test('his ❌: declined, decision no; consuming it is refused (5)', async () => {
    const { mock, r, h } = await setup()
    const c = await card(r, STD)
    mock.press(OWNER, `ap:${c.nonce}:n`, '❌ Decline', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => decided(r).length === 1)).toBe(true)
    expect(toasts(mock)).toEqual(['❌ Declined'])
    expect(decided(r)[0].payload).toMatchObject({ decision: 'no', outcome: 'declined' })
    expect(decided(r)[0].next).toBe('he declined: stop that action')
    expect(ap(r, c.nonce).status).toBe('denied')
    const res = cli(h, ['consume', c.nonce, '--action', ACTION, '--json'])
    expect([res.code, JSON.parse(res.out).why]).toEqual([5, 'he declined'])
  })

  test('taps that are not his decide nothing: another allowlisted user, a stranger', async () => {
    const { mock, r } = await setup()
    const c = await card(r, STD)
    mock.press(OTHER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    mock.press(STRANGER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => toasts(mock).length === 2)).toBe(true)
    expect(toasts(mock)[0]).toBe('Not authorized.')
    expect(toasts(mock)[1]).toContain('Not authorized')
    await Bun.sleep(50)
    expect(decided(r)).toEqual([])
    expect(rows(r.store, 'tg.button')).toEqual([])
    expect(ap(r, c.nonce).status).toBe('pending')
    expect(mock.sent('editMessageText')).toEqual([])
  })

  test('a store error records nothing: "tap again" toast, the keyboard stays', async () => {
    const { mock, r } = await setup()
    const c = await card(r, STD)
    const real = r.adapter.approvals.tap.bind(r.adapter.approvals)
    r.adapter.approvals.tap = () => { throw new Error('disk I/O error') }
    mock.press(OWNER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => decided(r).length === 1)).toBe(true)
    expect(toasts(mock)).toEqual(['⚠️ Not recorded: tap again'])
    expect(decided(r)[0].payload.outcome).toBe('error')
    expect(ap(r, c.nonce).status).toBe('pending')
    expect(mock.sent('editMessageText').length + mock.sent('editMessageReplyMarkup').length).toBe(0)
    r.adapter.approvals.tap = real
    mock.press(OWNER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => decided(r).length === 2)).toBe(true)
    expect(ap(r, c.nonce).status).toBe('approved')
  })
})

describe('expiry', () => {
  test('past its expiry the sweep swaps the keyboard for "⌛ expired"; a late tap approves nothing', async () => {
    const ck = clock()
    const { mock, r, h } = await setup({ now: ck.now })
    const c = await card(r, { ...STD, ttl_s: 60 })
    ck.t += 59_000
    await r.adapter.sweepApprovals(true)
    expect(mock.sent('editMessageReplyMarkup')).toEqual([])
    ck.t += 1_000
    await r.adapter.sweepApprovals(true)
    expect(mock.sent('editMessageReplyMarkup').map(x => x.params)).toEqual([{ chat_id: CHAT, message_id: c.mid,
      reply_markup: { inline_keyboard: [[{ text: '⌛ expired', callback_data: `ap:${c.nonce}:x` }]] } }])
    expect(ap(r, c.nonce)).toMatchObject({ status: 'expired', card: 'edited' })
    await r.adapter.sweepApprovals(true) // once per card
    expect(mock.sent('editMessageReplyMarkup').length).toBe(1)
    // a tap on the old keyboard (sent before the edit reached him)
    mock.press(OWNER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => decided(r).length === 1)).toBe(true)
    expect(toasts(mock)).toEqual(['⌛ Expired: nothing was approved'])
    expect(decided(r)[0].payload).toMatchObject({ outcome: 'expired' })
    expect(decided(r)[0].next).toBe('nothing approved by this tap')
    // the inert button only answers
    mock.press(OWNER, `ap:${c.nonce}:x`, '⌛ expired', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => toasts(mock).length === 2)).toBe(true)
    await Bun.sleep(50)
    expect(decided(r).length).toBe(1)
    expect(ap(r, c.nonce).status).toBe('expired')
    expect(cli(h, ['consume', c.nonce, '--action', ACTION]).code).toBe(3)
  })

  test('a tap after the expiry but before the sweep: expired at the tap, the card edited then', async () => {
    const ck = clock()
    const { mock, r } = await setup({ now: ck.now })
    const c = await card(r, { ...STD, ttl_s: 30 })
    ck.t += 30_000
    mock.press(OWNER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => decided(r).length === 1)).toBe(true)
    expect(toasts(mock)).toEqual(['⌛ Expired: nothing was approved'])
    expect(await waitFor(() => mock.sent('editMessageReplyMarkup').length === 1)).toBe(true)
    expect(ap(r, c.nonce)).toMatchObject({ status: 'expired', card: 'edited', decided_ts: null })
    await r.adapter.sweepApprovals(true)
    expect(mock.sent('editMessageReplyMarkup').length).toBe(1)
  })
})

describe('nonces made beforehand, explicit buttons, bindings, the switch', () => {
  test('{nonce} from `approvals new`: the card carries it; a wrong action is refused; explicit ap: buttons attach', async () => {
    const { mock, r, h } = await setup()
    const made = cli(h, ['new', '--action', ACTION, '--summary', 'Pay EUR 139.62', '--ref', 'webagent:wa-test', '--json'])
    expect(made.code).toBe(0)
    const n = JSON.parse(made.out)
    expect(n.buttons).toEqual([[{ text: '✅ Approve', data: `ap:${n.nonce}:y` }, { text: '❌ Decline', data: `ap:${n.nonce}:n` }]])
    const bad = await card(r, { nonce: n.nonce, action: 'another action' })
    expect([bad.row.status, bad.row.last_error]).toEqual(['failed', expect.stringContaining('does not match its nonce')])
    const ok = await card(r, { nonce: n.nonce, action: ACTION })
    expect(ok.row.status).toBe('sent')
    expect(ap(r, n.nonce)).toMatchObject({ chat: CHAT, message_id: ok.mid })
    // the same nonce on explicit buttons (poll-mode style): the newest card wins
    const id = r.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'Pay?', buttons: n.buttons } })!
    await drainOutbox(r.adapter.outboxDeps())
    const row = outbox(r.store, id)
    expect(JSON.parse(row.result).notes.join(' ')).toContain(`approval ${n.nonce} pending until`)
    expect(ap(r, n.nonce).message_id).toBe(Number(row.tg_message_id))
    // an unknown nonce on explicit buttons: noted
    const u = r.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'Old', buttons: [[{ text: 'ok', data: `ap:${'a'.repeat(32)}:y` }]] } })!
    await drainOutbox(r.adapter.outboxDeps())
    expect(JSON.parse(outbox(r.store, u).result).notes).toEqual([`approval ${'a'.repeat(32)}: not pending: a tap on this card approves nothing`])
    mock.press(OWNER, `ap:${n.nonce}:y`, '✅ Approve', { message_id: Number(row.tg_message_id), text: 'Pay?' })
    expect(await waitFor(() => decided(r).length === 1)).toBe(true)
    expect(cli(h, ['consume', n.nonce, '--action', ACTION]).code).toBe(0)
    // cancel: a pending one only
    const m2 = JSON.parse(cli(h, ['new', '--action', 'a2', '--summary', 's2', '--json']).out)
    expect(cli(h, ['cancel', m2.nonce]).code).toBe(0)
    expect(cli(h, ['cancel', m2.nonce]).code).toBe(5)
    const gone = await card(r, { nonce: m2.nonce })
    expect(gone.row.status).toBe('failed')
  })

  test('a tap on a replaced (cancelled) card: "⌛ replaced", nothing approved', async () => {
    const { mock, r, h } = await setup()
    const c = await card(r, STD)
    expect(cli(h, ['cancel', c.nonce]).code).toBe(0)
    mock.press(OWNER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => decided(r).length === 1)).toBe(true)
    expect(toasts(mock)).toEqual(['⌛ Replaced by a newer card: nothing was approved'])
    expect(await waitFor(() => mock.sent('editMessageReplyMarkup').length === 1)).toBe(true)
    expect(mock.sent('editMessageReplyMarkup')[0]!.params.reply_markup.inline_keyboard[0][0].text).toBe('⌛ replaced')
    expect(ap(r, c.nonce).status).toBe('cancelled')
  })

  test('bindings never take ap: data; approvals-off hands the tap to the brain as tg.button', async () => {
    const { mock, r, h } = await setup()
    const c = await card(r, STD)
    r.store.db.query(`INSERT INTO bindings(kind, key, tool, why, created) VALUES ('prefix', 'ap:', 'say', 'x', 0)`).run()
    expect(r.adapter.binder.forButton(CHAT, c.mid, `ap:${c.nonce}:y`)).toBeNull()
    const id = r.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'x', buttons: [[{ text: 'Go', data: `ap:${c.nonce}:y`, bind: { tool: 'say' } }]] } })!
    await drainOutbox(r.adapter.outboxDeps())
    expect(JSON.parse(outbox(r.store, id).result).notes[0]).toBe(`button "Go": approval buttons (ap:) can't be bound`)
    writeFileSync(join(h.paths.configDir, 'approvals-off'), '')
    mock.press(OWNER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => rows(r.store, 'tg.button').length === 1)).toBe(true)
    expect(rows(r.store, 'tg.button')[0].payload.callback_data).toBe(`ap:${c.nonce}:y`)
    expect(decided(r)).toEqual([])
    expect(ap(r, c.nonce).status).toBe('pending')
  })
})

describe('compat journal (0.1.22 shape)', () => {
  test('approval.decided is journaled as a button press; with approvals-off the CLI decides from the journal', async () => {
    const { mock, h } = await setup()
    await stopOne(running[0]!)
    const journal = join(h.home, 'journal.jsonl')
    writeConfig(h, { allow_from: [CHAT, String(OTHER)], compat_journal: journal })
    const r = await start(mock, h)
    const c = await card(r, STD)
    mock.press(OWNER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => decided(r).length === 1)).toBe(true)
    const lines = () => readFileSync(journal, 'utf8').trim().split('\n').map(l => JSON.parse(l))
    expect(lines().at(-1)).toMatchObject({ kind: 'button', owner: true, meta: { callback_data: `ap:${c.nonce}:y`, outcome: 'approved', event_type: 'approval' } })
    // switch off: the press reaches the brain as tg.button, the CLI reads his tap from the journal (poll-mode path)
    writeFileSync(join(h.paths.configDir, 'approvals-off'), '')
    const d = await card(r, STD)
    mock.press(OWNER, `ap:${d.nonce}:y`, '✅ Approve', { message_id: d.mid, text: 'Book AZ 153?' })
    expect(await waitFor(() => rows(r.store, 'tg.button').length === 1)).toBe(true)
    expect(lines().at(-1)).toMatchObject({ kind: 'button', owner: true, meta: { callback_data: `ap:${d.nonce}:y`, user_id: CHAT, chat_id: CHAT } })
    const access = join(h.home, 'access.json')
    writeFileSync(access, JSON.stringify({ allowFrom: [CHAT, String(OTHER)] }))
    const env = { APPROVALS_JOURNAL: journal, APPROVALS_ACCESS: access }
    expect(cli(h, ['consume', d.nonce, '--action', 'wrong'], env).code).toBe(5)
    expect(JSON.parse(cli(h, ['status', d.nonce, '--json'], env).out)).toMatchObject({ state: 'approved', decided_via: 'journal' })
    expect(cli(h, ['consume', d.nonce, '--action', ACTION], env).code).toBe(0)
    expect(cli(h, ['consume', d.nonce, '--action', ACTION], env).code).toBe(4)
  })
})

describe('restart', () => {
  test('restart mid-wait: `approvals wait` keeps waiting; the next adapter process decides his tap', async () => {
    const { mock, h, r } = await setup()
    const c = await card(r, STD)
    const waiter = Bun.spawn([PY, '-I', join(REPO, 'scripts', 'approvals'), 'wait', c.nonce, '--timeout', '20', '--json'], {
      env: { HOME: h.home, PATH: '/usr/bin:/bin', ASSISTANT_TEST: '1', APPROVALS_DB: h.paths.approvalsDb }, stdout: 'pipe',
    })
    await Bun.sleep(300)
    await stopOne(r)
    const r2 = await start(mock, h)
    mock.press(OWNER, `ap:${c.nonce}:y`, '✅ Approve', { message_id: c.mid, text: 'Book AZ 153?' })
    const code = await waiter.exited
    const out = JSON.parse(await new Response(waiter.stdout).text())
    expect([code, out.state, out.decided_via]).toEqual([0, 'approved', 'adapter'])
    expect(decided(r2).length).toBe(1)
    expect(cli(h, ['consume', c.nonce, '--action', ACTION]).code).toBe(0)
  })

  test('cards that expired while the adapter was down are edited at start', async () => {
    const ck = clock()
    const { mock, h, r } = await setup({ now: ck.now })
    const c = await card(r, { ...STD, ttl_s: 60 })
    await stopOne(r)
    ck.t += 61_000
    await start(mock, h, { now: ck.now })
    expect(await waitFor(() => mock.sent('editMessageReplyMarkup').length === 1)).toBe(true)
    expect(mock.sent('editMessageReplyMarkup')[0]!.params.reply_markup.inline_keyboard).toEqual([[{ text: '⌛ expired', callback_data: `ap:${c.nonce}:x` }]])
    expect(new Approvals(h.paths.approvalsDb).get(c.nonce)).toMatchObject({ status: 'expired', card: 'edited' })
  })

  test('send:false never edits cards', async () => {
    const ck = clock()
    const { mock, h, r } = await setup({ now: ck.now })
    const c = await card(r, { ...STD, ttl_s: 60 })
    await stopOne(r)
    writeConfig(h, { allow_from: [CHAT], send: false })
    ck.t += 61_000
    const r2 = await start(mock, h, { now: ck.now })
    await r2.adapter.sweepApprovals(true)
    expect(mock.sent('editMessageReplyMarkup')).toEqual([])
    expect(new Approvals(h.paths.approvalsDb).get(c.nonce)!.status).toBe('pending')
  })
})

describe('store', () => {
  test('one DDL: src/approvals.sql = schemas/approvals.sql; paths per instance; ap: data', () => {
    expect(APPROVALS_SQL).toBe(readFileSync(join(REPO, 'schemas', 'approvals.sql'), 'utf8'))
    const r = (p: string) => p.replace(/^~/, '/h')
    expect(approvalsDbFor({}, 'live', '/h/.local/state/assistant/events.db', '/h/.local/state/tg-adapter/live', r)).toBe('/h/.local/state/assistant/approvals.db')
    expect(approvalsDbFor({}, 'test', '/h/.local/state/tg-adapter/test.db', '/h/.local/state/tg-adapter/test', r)).toBe('/h/.local/state/tg-adapter/test/approvals.db')
    expect(approvalsDbFor({ APPROVALS_DB: '~/x.db' }, 'live', '/a', '/b', r)).toBe('/h/x.db')
    const n = 'ab'.repeat(16)
    expect(parseApprovalData(`ap:${n}:y`)).toEqual({ nonce: n, choice: 'y' })
    expect(parseApprovalData(`ap:${n}:q`)).toBeNull()
    expect(parseApprovalData(`ap:${n.toUpperCase()}:y`)).toBeNull()
    expect(AP_RE.test(`xap:${n}:y`)).toBe(false)
    expect(noncesIn([[{ text: 'a', data: `ap:${n}:y` }, { text: 'b', callback_data: `ap:${n}:n` }], [{ text: 'c', data: `ap:${n}:x` }]])).toEqual([n])
    expect(noncesIn([{ text: 'a', data: `ap:${n}:y` }])).toEqual([n])
  })

  test('the reader never creates the store; its file is 0600 in a 0700 directory', () => {
    const h = makeHome()
    const a = new Approvals(join(h.home, 'deep', 'dir', 'approvals.db'))
    expect(a.get('ab'.repeat(16))).toBeNull()
    expect(a.due()).toEqual([])
    expect(existsSync(join(h.home, 'deep'))).toBe(false)
    const row = a.create({ action: 'x', summary: '  a   b  ' })
    expect(row.summary).toBe('a b')
    expect(statSync(dirname(a.path)).mode & 0o777).toBe(0o700)
    expect(statSync(a.path).mode & 0o777).toBe(0o600)
    expect(() => a.create({ action: '', summary: 's' })).toThrow('approval.action is required')
    expect(() => a.create({ action: 'x', summary: 's', ref: 'bad ref' })).toThrow('approval.ref')
    expect(a.create({ action: 'x', summary: 'y'.repeat(100) }).summary.length).toBe(80)
    a.close()
  })
})
