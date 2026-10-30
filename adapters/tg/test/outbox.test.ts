// Outbox consumer v0: send (chunks, rich → plain fallback, details fold, buttons, reply_to),
// edit, react (whitelist, 2+ refused → first only), poll; the outbound gate; 429 / 5xx retries;
// network outages (retry until the window ends, one outbox.failed at the end); per-chat order;
// partial sends never retried; receive-only by default; interrupted rows.
import { afterAll, afterEach, describe, expect, test } from 'bun:test'
import { readFileSync } from 'fs'
import { join } from 'path'
import { GrammyError, HttpError } from 'grammy'
import {
  drainOutbox, failInterrupted, MAX_ATTEMPTS, MAX_BACKOFF_S, networkError, RETRY_WINDOW_S, retryable, SHORT_RETRY_WINDOW_S, type OutboxDeps,
} from '../src/outbox.ts'
import { cleanup, makeHome, outbox, rows, startAdapter, waitFor, type Running } from './helpers.ts'
import { MockBot, OWNER, STRANGER } from './mockbot.ts'

afterAll(cleanup)
let running: Running | null = null
afterEach(async () => { if (running) await running.stop(); running = null })

const BOT = 'testadapter_bot'
const CHAT = String(OWNER)

/** The adapter polls (send off), the test drives the outbox pass by hand. */
async function setup(config: Record<string, unknown> = {}) {
  const mock = new MockBot()
  const h = makeHome({ send: false, ...config })
  running = await startAdapter(mock, h)
  const r = running
  const q = (op: string, body: Record<string, unknown>, extra: Record<string, unknown> = {}) =>
    r.store.enqueue({ bot: BOT, chat: CHAT, op, body, ...extra })!
  return { mock, h, r, q, drain: () => drainOutbox(r.adapter.outboxDeps()) }
}

describe('send', () => {
  test('plain text with reply_to, silent, link preview off; long text is split at 4096', async () => {
    const { mock, r, q, drain } = await setup()
    const a = q('send', { text: 'hi', reply_to: 5, silent: true, link_preview: 'off', effect: 'party' })
    const b = q('send', { text: 'x'.repeat(5000) })
    await drain()
    const sm = mock.sent('sendMessage')
    expect(sm[0]!.params).toMatchObject({ chat_id: CHAT, text: 'hi', reply_parameters: { message_id: 5 }, disable_notification: true,
      link_preview_options: { is_disabled: true }, message_effect_id: '5046509860389126442' })
    expect(sm.slice(1).map(c => c.params.text.length)).toEqual([4096, 904])
    expect(outbox(r.store, a)).toMatchObject({ status: 'sent', attempts: 1, last_error: null })
    // tg_message_id = the last part (the one that would carry buttons)
    expect(outbox(r.store, b)).toMatchObject({ status: 'sent', tg_message_id: outbox(r.store, a).tg_message_id + 2 })
  })

  test('rich: hard breaks added; a rejected rich message goes again as plain text with a note', async () => {
    const { mock, r, q, drain } = await setup()
    const a = q('send', { text: 'line one\nline two', format: 'rich' })
    const b = q('send', { text: 'BADRICH **x**', format: 'rich', buttons: [{ text: 'Go', data: 'go' }] })
    await drain()
    expect(mock.sent('sendRichMessage')[0]!.params.rich_message.markdown).toBe('line one<br>\nline two')
    const plain = mock.sent('sendMessage')
    expect(plain.length).toBe(1)
    expect(plain[0]!.params).toMatchObject({ text: 'BADRICH **x**', reply_markup: { inline_keyboard: [[{ text: 'Go', callback_data: 'go' }]] } })
    expect(outbox(r.store, a).status).toBe('sent')
    expect(outbox(r.store, b)).toMatchObject({ status: 'sent' })
    expect(outbox(r.store, b).last_error).toMatch(/^note: rich rejected/)
  })

  test('details fold into an expandable block; buttons validated (64 bytes, no perm:)', async () => {
    const { mock, r, q, drain } = await setup()
    const a = q('send', { text: 'Summary', details: 'the long part', details_title: 'More' })
    const bad = q('send', { text: 'x', buttons: [{ text: 'P', data: 'perm:allow:abcde' }] })
    await drain()
    const sm = mock.sent('sendMessage')
    expect(sm.length).toBe(1)
    expect(sm[0]!.params.text).toContain('the long part')
    expect(sm[0]!.params.entities?.some((e: any) => e.type === 'expandable_blockquote')).toBe(true)
    expect(outbox(r.store, a).status).toBe('sent')
    expect(outbox(r.store, bad)).toMatchObject({ status: 'failed', last_error: 'button data may not start with "perm:"' })
  })

  test('on_press is saved in buttons.json for keep/remove', async () => {
    const { r, h, q, drain } = await setup()
    const a = q('send', { text: 'x', buttons: [{ text: 'A', data: 'a' }], on_press: 'remove' })
    await drain()
    const saved = JSON.parse(readFileSync(join(h.paths.stateDir, 'buttons.json'), 'utf8'))
    expect(saved[`${CHAT}:${outbox(r.store, a).tg_message_id}`].on_press).toBe('remove')
  })

  test('the outbound gate: a chat the inbound gate would not deliver from is refused', async () => {
    const { mock, r, drain } = await setup()
    const id = r.store.enqueue({ bot: BOT, chat: String(STRANGER), op: 'send', body: { text: 'hi' } })!
    await drain()
    expect(outbox(r.store, id)).toMatchObject({ status: 'failed', last_error: `chat ${STRANGER} is not allowlisted` })
    expect(mock.sent('sendMessage')).toEqual([])
  })

  test('other bots\' rows, shadow rows and unknown ops', async () => {
    const { mock, r, q, drain } = await setup()
    const other = r.store.enqueue({ bot: 'live_bot', chat: CHAT, op: 'send', body: { text: 'not mine' } })!
    const shadow = q('send', { text: 'shadow' }, { mode: 'shadow' })
    const unk = q('nope', { message_id: 1 })
    await drain()
    expect(outbox(r.store, other).status).toBe('queued')
    expect(outbox(r.store, shadow).status).toBe('queued')
    expect(outbox(r.store, unk)).toMatchObject({ status: 'failed', last_error: 'unknown op: nope' })
    expect(mock.sent('sendMessage')).toEqual([])
  })
})

describe('edit, react, poll', () => {
  test('edit text (plain fallback on bad formatting) and buttons only', async () => {
    const { mock, r, q, drain } = await setup()
    const a = q('edit', { message_id: 9, text: 'BADPARSE *x*', format: 'markdownv2' })
    const b = q('edit', { message_id: 9, buttons: [{ text: 'B', data: 'b' }] })
    await drain()
    const et = mock.sent('editMessageText')
    expect(et.map(c => c.params.parse_mode)).toEqual(['MarkdownV2', undefined])
    expect(outbox(r.store, a).last_error).toMatch(/markdownv2 rejected/)
    expect(mock.sent('editMessageReplyMarkup')[0]!.params).toMatchObject({ message_id: 9, reply_markup: { inline_keyboard: [[{ text: 'B', callback_data: 'b' }]] } })
    expect(outbox(r.store, b).status).toBe('sent')
  })

  test('react: whitelist; two refused → the first only', async () => {
    const { mock, r, q, drain } = await setup()
    const a = q('react', { message_id: 3, emojis: ['👍', '🔥'] })
    const b = q('react', { message_id: 3, emoji: '🦖' })
    const c = q('react', { message_id: 3, remove: true })
    await drain()
    const sr = mock.sent('setMessageReaction')
    expect(sr.map(x => x.params.reaction.length)).toEqual([2, 1, 0])
    expect(outbox(r.store, a)).toMatchObject({ status: 'sent' })
    expect(outbox(r.store, a).last_error).toMatch(/set 👍 only/)
    expect(outbox(r.store, b).status).toBe('failed')
    expect(outbox(r.store, c).status).toBe('sent')
  })

  test('poll: 2-12 options; the poll id is kept for answers', async () => {
    const { r, q, drain } = await setup()
    const bad = q('poll', { question: 'Q', options: ['only one'] })
    const ok = q('poll', { question: 'Q', options: ['a', 'b'] })
    await drain()
    expect(outbox(r.store, bad)).toMatchObject({ status: 'failed', last_error: '2-12 options required' })
    expect(outbox(r.store, ok).status).toBe('sent')
    expect(JSON.parse(r.store.getMeta('poll:poll-1')!)).toMatchObject({ chat_id: CHAT, question: 'Q', options: ['a', 'b'] })
  })
})

describe('errors and order', () => {
  test('429 → queued again after retry_after, blocking that chat\'s later rows; then sent in order', async () => {
    const { mock, r, q, drain } = await setup()
    mock.failures.sendMessage = [{ status: 429, body: { ok: false, error_code: 429, description: 'Too Many Requests: retry after 1', parameters: { retry_after: 1 } } }]
    const a = q('send', { text: 'first' })
    const b = q('send', { text: 'second' })
    await drain()
    expect(outbox(r.store, a)).toMatchObject({ status: 'queued', attempts: 1 })
    expect(outbox(r.store, a).not_before).toBeGreaterThan(Date.now() / 1000)
    expect(outbox(r.store, b)).toMatchObject({ status: 'queued', attempts: 0 })
    let t = Date.now() / 1000
    await drainOutbox(r.adapter.outboxDeps(), () => t)
    expect(outbox(r.store, b).attempts).toBe(0) // still blocked behind a
    t += 2
    await drainOutbox(r.adapter.outboxDeps(), () => t)
    expect([outbox(r.store, a).status, outbox(r.store, b).status]).toEqual(['sent', 'sent'])
    expect(mock.sent('sendMessage').map(c => c.params.text)).toEqual(['first', 'first', 'second'])
  })

  test('5xx while Telegram answers other calls (message-specific) → failed after 5 strikes; during an outage it keeps going', async () => {
    const { mock, r, q } = await setup()
    const e500 = { status: 500, body: { ok: false, error_code: 500, description: 'Internal Server Error' } }
    mock.failures.sendMessage = Array(13).fill(e500)
    const a = q('send', { text: 'x' })
    let t = Date.now() / 1000
    const answering = { ...r.adapter.outboxDeps(), lastOk: () => t } // getUpdates etc. answer between the retries
    for (let i = 0; i < 6; i++) { await drainOutbox(answering, () => t); t += 100 }
    expect(outbox(r.store, a)).toMatchObject({ status: 'failed', attempts: 5, last_error: 'Call to \'sendMessage\' failed! (500: Internal Server Error)' })
    expect(rows(r.store, 'outbox.failed').map(e => e.payload.outbox_id)).toEqual([a])
    // The same 5xx while nothing else gets an answer either (Telegram's side is down): not a strike.
    const b = q('send', { text: 'y' })
    const lastAnswer = t - 1
    const silent = { ...r.adapter.outboxDeps(), lastOk: () => lastAnswer }
    for (let i = 0; i < 8; i++) { await drainOutbox(silent, () => t); t += 100 }
    expect(outbox(r.store, b)).toMatchObject({ status: 'queued', attempts: 8 })
    expect(JSON.parse(outbox(r.store, b).result).retry.strikes).toBe(1) // its first failure only
    expect(rows(r.store, 'outbox.failed')).toHaveLength(1)
  })

  test('a partial send (part 2 of 2 failed) is never retried', async () => {
    const { mock, r, q, drain } = await setup()
    mock.failures.sendMessage = []
    const a = q('send', { text: 'y'.repeat(5000) })
    // the second part fails with a 5xx
    const orig = mock.handle.bind(mock)
    let n = 0
    mock.handle = async (req: Request) => {
      if (req.url.endsWith('/sendMessage') && ++n === 2) return Response.json({ ok: false, error_code: 502, description: 'Bad Gateway' }, { status: 502 })
      return orig(req)
    }
    await drain()
    const row = outbox(r.store, a)
    expect(row.status).toBe('failed')
    expect(row.last_error).toMatch(/after 1 of 2 part/)
    expect(row.tg_message_id).not.toBeNull()
  })

  test('rows left "sending" by a killed process are marked failed, never re-sent blind', async () => {
    const { mock, r, q } = await setup()
    const a = q('send', { text: 'x' })
    r.store.setOutbox(a, { status: 'sending', attempts: 1 })
    expect(failInterrupted(r.store, BOT)).toBe(1)
    expect(outbox(r.store, a)).toMatchObject({ status: 'failed', last_error: 'interrupted while sending (may have been delivered)' })
    expect(mock.sent('sendMessage')).toEqual([])
  })

  test('receive-only by default: with send off the adapter never drains the outbox', async () => {
    const { mock, r, q } = await setup()
    const a = q('send', { text: 'x' })
    await Bun.sleep(150)
    expect(outbox(r.store, a).status).toBe('queued')
    expect(mock.sent('sendMessage')).toEqual([])
  })

  test('send on: the consumer drains on its own', async () => {
    const mock = new MockBot()
    const h = makeHome({ send: true })
    running = await startAdapter(mock, h)
    const id = running.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'auto' } })!
    expect(await waitFor(() => outbox(running!.store, id).status === 'sent')).toBe(true)
  })
})

describe('network outage (23.10: alerts lost after 5 attempts in ~30 s)', () => {
  const failed = (r: Running) => rows(r.store, 'outbox.failed').map(e => e.payload.outbox_id)
  /** Drain at each due retry (fake clock) while `go(t)`; returns the clock after the last pass. */
  async function passes(r: Running, deps: () => OutboxDeps, t: number, go: (t: number) => boolean): Promise<number> {
    for (let i = 0; i < 2000 && go(t); i++) {
      await drainOutbox(deps(), () => t)
      const nb = (r.store.db.query("SELECT MIN(not_before) AS nb FROM outbox WHERE status = 'queued'").get() as { nb: number | null }).nb
      t = Math.max(t + 1, nb ?? t + 1)
    }
    return t
  }

  test('the link heals after 10 min: rows wait in order with capped backoff, then go out; nothing fails', async () => {
    const { mock, r, q } = await setup()
    const t0 = Date.now() / 1000
    const lastAnswer = t0 - 5 // Telegram's last answer came before the outage
    const deps = () => ({ ...r.adapter.outboxDeps(), lastOk: () => lastAnswer })
    mock.down = true
    const a = q('send', { text: 'Disk 3 at 55 °C' })
    const b = q('send', { text: 'second alert' })
    const waits: number[] = []
    let t = t0
    while (t < t0 + 600) {
      await drainOutbox(deps(), () => t)
      const nb = outbox(r.store, a).not_before as number
      waits.push(Math.round(nb - t))
      t = nb
    }
    expect(outbox(r.store, a).status).toBe('queued')
    expect(outbox(r.store, a).attempts).toBeGreaterThan(MAX_ATTEMPTS)
    expect(outbox(r.store, a).last_error).toMatch(/Network request for 'sendMessage' failed/)
    expect(waits.slice(0, 6)).toEqual([2, 4, 8, 16, 32, MAX_BACKOFF_S])
    expect(Math.max(...waits)).toBe(MAX_BACKOFF_S)
    expect(JSON.parse(outbox(r.store, a).result).retry).toMatchObject({ since: t0, strikes: 1 })
    expect(outbox(r.store, b)).toMatchObject({ status: 'queued', attempts: 0 }) // behind a: the chat's order holds
    expect(failed(r)).toEqual([])
    mock.down = false
    await drainOutbox(deps(), () => t)
    expect([outbox(r.store, a).status, outbox(r.store, b).status]).toEqual(['sent', 'sent'])
    expect(mock.sent('sendMessage').map(c => c.params.text)).toEqual(['Disk 3 at 55 °C', 'second alert'])
    expect(JSON.parse(outbox(r.store, a).result).ids).toHaveLength(1) // the sent result replaced the retry window
    expect(failed(r)).toEqual([])
  })

  test('a permanent 400 fails at once, with one outbox.failed event', async () => {
    const { mock, r, q, drain } = await setup()
    mock.failures.sendMessage = [{ status: 400, body: { ok: false, error_code: 400, description: 'Bad Request: chat not found' } }]
    const a = q('send', { text: 'x' })
    await drain()
    expect(outbox(r.store, a)).toMatchObject({ status: 'failed', attempts: 1, last_error: 'Call to \'sendMessage\' failed! (400: Bad Request: chat not found)' })
    expect(failed(r)).toEqual([a])
  })

  test('the deadline: an edit gives up after 10 min, the send behind it after 6 h of retries; one outbox.failed each', async () => {
    const { mock, r, q } = await setup()
    const t0 = Date.now() / 1000
    const deps = () => ({ ...r.adapter.outboxDeps(), lastOk: () => t0 - 5 })
    mock.down = true
    const e = q('edit', { message_id: 7, text: 'progress: done' })
    const s = q('send', { text: 'never arrives' })
    let t = await passes(r, deps, t0, () => outbox(r.store, e).status === 'queued')
    const eRow = outbox(r.store, e)
    expect(eRow.status).toBe('failed')
    expect(eRow.last_error).toMatch(/failed!.*\(gave up after 10 min of retries\)$/)
    expect(t - t0).toBeGreaterThanOrEqual(SHORT_RETRY_WINDOW_S)
    expect(t - t0).toBeLessThan(SHORT_RETRY_WINDOW_S + 2 * MAX_BACKOFF_S)
    expect(failed(r)).toEqual([e])
    const sStart = t - 1
    t = await passes(r, deps, t, () => outbox(r.store, s).status === 'queued')
    const sRow = outbox(r.store, s)
    expect(sRow.status).toBe('failed')
    expect(sRow.last_error).toMatch(/\(gave up after 6 h of retries\)$/)
    expect(t - sStart).toBeGreaterThanOrEqual(RETRY_WINDOW_S)
    expect(t - sStart).toBeLessThan(RETRY_WINDOW_S + 2 * MAX_BACKOFF_S)
    expect(sRow.attempts).toBeGreaterThan(300) // about one try a minute
    expect(failed(r)).toEqual([e, s]) // at the end only, never per retry
    expect(rows(r.store, 'outbox.failed')[1].payload).toMatchObject({ outbox_id: s, op: 'send', attempts: sRow.attempts })
  })

  test('a gap in retrying (adapter stopped) does not use up the window', async () => {
    const { mock, r, q } = await setup()
    const t0 = Date.now() / 1000
    const deps = () => ({ ...r.adapter.outboxDeps(), lastOk: () => t0 - 5 })
    mock.down = true
    const a = q('send', { text: 'x' })
    let t = await passes(r, deps, t0, t => t < t0 + 3600)
    t += 8 * 3600 // nothing ran for 8 h
    t = await passes(r, deps, t, t => t < t0 + 9 * 3600 + 600)
    expect(outbox(r.store, a).status).toBe('queued') // 1 h + 10 min of retrying so far
    expect(JSON.parse(outbox(r.store, a).result).retry.since).toBeGreaterThan(t0 + 8 * 3600 - MAX_BACKOFF_S)
  })

  test('the adapter tells an outage from a message-specific error: lastOk moves only while Telegram answers', async () => {
    const { mock, r } = await setup()
    const lastOk = r.adapter.outboxDeps().lastOk!
    expect(await waitFor(() => lastOk() > 0)).toBe(true) // getMe / getUpdates answered
    mock.down = true
    await Bun.sleep(400)
    const frozen = lastOk()
    await Bun.sleep(700)
    expect(lastOk()).toBe(frozen)
    mock.down = false
    expect(await waitFor(() => lastOk() > frozen, 4000)).toBe(true)
  })

  test('what retries: no answer (grammY HttpError, Bun fetch codes, timeouts), 5xx, 429; not 4xx, aborts or plain errors', () => {
    const tg = (code: number, extra: Record<string, unknown> = {}) =>
      new GrammyError('x', { ok: false, error_code: code, description: 'd', ...extra } as any, 'sendMessage', {})
    expect(retryable(new HttpError('Network request for \'sendMessage\' failed!', new TypeError('fetch failed')))).toEqual({ retry: true })
    expect(retryable(tg(502))).toEqual({ retry: true })
    expect(retryable(tg(429, { parameters: { retry_after: 7 } }))).toEqual({ retry: true, after: 7 })
    expect(retryable(tg(400))).toEqual({ retry: false })
    expect(retryable(tg(403))).toEqual({ retry: false })
    for (const code of ['ECONNREFUSED', 'ENOTFOUND', 'ECONNRESET', 'EAI_AGAIN', 'ETIMEDOUT']) {
      expect(networkError(Object.assign(new TypeError(`${code}: example.org`), { code }))).toBe(true)
    }
    expect(networkError(new DOMException('The operation timed out.', 'TimeoutError'))).toBe(true)
    expect(networkError(new Error('download failed', { cause: Object.assign(new Error('x'), { code: 'ECONNRESET' }) }))).toBe(true)
    expect(retryable(new DOMException('aborted', 'AbortError'))).toEqual({ retry: false })
    expect(retryable(new Error('message_id is required'))).toEqual({ retry: false })
  })
})
