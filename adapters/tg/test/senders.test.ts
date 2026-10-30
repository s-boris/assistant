// One sender: what the script senders need from the outbox, offline against the mock Bot API.
// - a voice note keeps its caption and duration (tg-voice through the outbox): first file only, voice files only
// - a row is claimed only while still queued: a writer that withdrew it (core.tg: queued -> skipped) wins, even
//   when the withdrawal lands while the same drain pass is busy with an earlier row
import { afterAll, afterEach, describe, expect, test } from 'bun:test'
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { drainOutbox } from '../src/outbox.ts'
import { cleanup, makeHome, outbox, startAdapter, type Running } from './helpers.ts'
import { png } from './images.ts'
import { MockBot, OWNER } from './mockbot.ts'

afterAll(cleanup)
let running: Running | null = null
afterEach(async () => { if (running) await running.stop(); running = null })

const BOT = 'testadapter_bot'
const CHAT = String(OWNER)

async function setup(mock = new MockBot()) {
  const h = makeHome({ send: false })
  running = await startAdapter(mock, h, {})
  const r = running
  const q = (op: string, body: Record<string, unknown>, extra: Record<string, unknown> = {}) => r.store.enqueue({ bot: BOT, chat: CHAT, op, body, ...extra })!
  return { mock, h, r, q, drain: () => drainOutbox(r.adapter.outboxDeps()) }
}

describe('voice notes through the outbox (tg-voice)', () => {
  test('caption and duration go on the first file; duration only for a voice file; a long caption is cut to 1024', async () => {
    const { mock, h, q, r, drain } = await setup()
    const dir = join(h.home, 'out')
    mkdirSync(dir, { recursive: true })
    const ogg = join(dir, 'v.ogg'), pic = join(dir, 'a.png')
    writeFileSync(ogg, 'OggS'); writeFileSync(pic, png(40, 30))
    const a = q('send', { text: '', caption: 'on my way', duration: 7, silent: true, reply_to: 41 }, { files: [ogg, pic] })
    const b = q('send', { text: '', caption: 'x'.repeat(1100), duration: 3 }, { files: [pic] })
    await drain()
    expect(outbox(r.store, a).status).toBe('sent')
    expect(outbox(r.store, b).status).toBe('sent')
    const v = mock.sent('sendVoice')
    expect(v.length).toBe(1)
    expect(v[0]!.params.caption).toBe('on my way')
    expect(v[0]!.params.duration).toBe(7)
    expect(v[0]!.params.disable_notification).toBe(true)
    expect(v[0]!.params.reply_parameters).toEqual({ message_id: 41 })
    const p = mock.sent('sendPhoto')
    expect(p.length).toBe(2)
    expect(p[0]!.params.caption).toBeUndefined()           // a's second file: no caption
    expect(String(p[1]!.params.caption).length).toBe(1024) // b's only file: cut
    expect(p[1]!.params.duration).toBeUndefined()          // not a voice file
  })
})

describe('claims', () => {
  test('a withdrawn row is never sent, also when it is withdrawn while the pass is busy with an earlier row', async () => {
    const mock = new MockBot()
    const { q, r, drain } = await setup(mock)
    const gone = q('send', { text: 'withdrawn first' })
    r.store.db.query("UPDATE outbox SET status = 'skipped', last_error = 'withdrawn: test' WHERE id = ?").run(gone)
    mock.delays.sendMessage = 300
    const slow = q('send', { text: 'slow one' })
    const late = q('send', { text: 'withdrawn mid-pass' })
    const pass = drain()
    await Bun.sleep(100) // the pass holds both rows in its list and is sending the slow one
    expect(r.store.db.query("UPDATE outbox SET status = 'skipped', last_error = 'withdrawn: test' WHERE id = ? AND status = 'queued'").run(late).changes).toBe(1)
    await pass
    expect(mock.sent('sendMessage').map(c => c.params.text)).toEqual(['slow one'])
    expect(outbox(r.store, slow).status).toBe('sent')
    expect([outbox(r.store, gone).status, outbox(r.store, late).status]).toEqual(['skipped', 'skipped'])
    expect(outbox(r.store, late).attempts).toBe(0)
  })

  test('claimOutbox: queued -> sending once; a second claim or a claim on a skipped row fails', async () => {
    const { q, r } = await setup()
    const id = q('send', { text: 'x' })
    expect(r.store.claimOutbox(id, 1)).toBe(true)
    expect(outbox(r.store, id).status).toBe('sending')
    expect(outbox(r.store, id).attempts).toBe(1)
    expect(r.store.claimOutbox(id, 2)).toBe(false)
    const s = q('send', { text: 'y' })
    r.store.db.query("UPDATE outbox SET status = 'skipped' WHERE id = ?").run(s)
    expect(r.store.claimOutbox(s, 1)).toBe(false)
  })
})
