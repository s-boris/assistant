// Live mode + parity, offline against the mock Bot API (no sockets, temp HOME):
// the bridge's ops (send with files/photos/albums/voice, draft, chat_action, delete, pin, location, commands,
// download), results for the bridge, the file guard, inbound prep (eager photos, transcripts in per-chat order,
// private scans), tg.stop, typing, the ack reaction, the compat journal and buttons file, rule exemptions
// (reply_to_event, bridge rows), pending-row recovery and the live instance on the main events DB.
import { afterAll, afterEach, describe, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'fs'
import { join } from 'path'
import { journalEntry } from '../src/compat.ts'
import { openDb, Store } from '../src/db.ts'
import { drainOutbox } from '../src/outbox.ts'
import { resolvePaths } from '../src/paths.ts'
import { cleanup, makeHome, outbox, rows, startAdapter, waitFor, writeConfig, type Home, type Running } from './helpers.ts'
import { png } from './images.ts'
import { MockBot, OWNER } from './mockbot.ts'

afterAll(cleanup)
let running: Running | null = null
afterEach(async () => { if (running) await running.stop(); running = null })

const BOT = 'testadapter_bot'
const CHAT = String(OWNER)

/** An executable stub in the temp home: prints `out` (or fails with exit 3 when out is null). */
function stub(h: Home, name: string, out: string | null): string {
  const dir = join(h.home, 'stubs')
  mkdirSync(dir, { recursive: true })
  const p = join(dir, name)
  writeFileSync(p, out == null ? '#!/bin/sh\necho "boom" >&2\nexit 3\n' : `#!/bin/sh\necho "$@" >> "${p}.args"\nprintf '%s\\n' '${out}'\n`)
  chmodSync(p, 0o755)
  return p
}

async function setup(config: Record<string, unknown> = {}, env: Record<string, string> = {}, mock = new MockBot()) {
  const h = makeHome({ send: false, ...config })
  running = await startAdapter(mock, h, { env })
  const r = running
  const q = (op: string, body: Record<string, unknown>, extra: Record<string, unknown> = {}) => r.store.enqueue({ bot: BOT, chat: CHAT, op, body, ...extra })!
  const result = (id: number) => JSON.parse(outbox(r.store, id).result ?? 'null')
  return { mock, h, r, q, result, drain: () => drainOutbox(r.adapter.outboxDeps()) }
}

const base = (extra: any) => ({ message_id: Math.floor(Math.random() * 1e6), date: Math.floor(Date.now() / 1000), chat: { id: OWNER, type: 'private' },
  from: { id: OWNER, is_bot: false, first_name: 'O', username: 'owner' }, ...extra })

describe('send with files', () => {
  test('photo, voice (sendAudio fallback), document, album; files column; ids and notes in result', async () => {
    const { mock, h, q, result, drain } = await setup()
    const dir = join(h.home, 'out')
    mkdirSync(dir, { recursive: true })
    const pic = join(dir, 'a.png'), pic2 = join(dir, 'b.png'), ogg = join(dir, 'v.ogg'), pdf = join(dir, 'x.pdf')
    writeFileSync(pic, png(40, 30)); writeFileSync(pic2, png(30, 40)); writeFileSync(ogg, 'OggS'); writeFileSync(pdf, '%PDF-1.4')
    const a = q('send', { text: 'look', buttons: [[{ text: 'Ok', data: 'ok' }]] }, { files: [pic, pdf] })
    mock.failures.sendVoice = [{ status: 400, body: { ok: false, error_code: 400, description: 'Bad Request: VOICE_MESSAGES_FORBIDDEN' } }]
    const b = q('send', { text: '', files: [ogg] })
    const c = q('send', { text: '', album: true }, { files: [pic, pic2] })
    await drain()
    expect(outbox(running!.store, a).status).toBe('sent')
    expect(result(a).ids.length).toBe(3)
    expect(mock.sent('sendPhoto').length).toBe(1)
    expect(mock.sent('sendDocument').length).toBe(1)
    expect(mock.uploads.find(u => u.method === 'sendPhoto')!.files[0]!.name).toBe('a.png')
    expect(mock.sent('sendVoice').length).toBe(1)
    expect(mock.sent('sendAudio').length).toBe(1)
    expect(result(b).notes.join(' ')).toMatch(/sent as audio/)
    expect(mock.sent('sendMediaGroup').length).toBe(1)
    expect(result(c).ids.length).toBe(2)
  })

  test('the file guard: secrets, Claude dir, adapter config/state and the token file refused; the instance inbox allowed', async () => {
    const { h, q, drain, r } = await setup()
    const secret = join(h.home, '.secrets', 'x.txt'); writeFileSync(secret, 's')
    const claude = join(h.home, '.claude', 'y.txt'); mkdirSync(join(h.home, '.claude'), { recursive: true }); writeFileSync(claude, 'c')
    const state = join(h.paths.stateDir, 'z.txt'); writeFileSync(state, 'z')
    mkdirSync(h.paths.inboxDir, { recursive: true }); const inbox = join(h.paths.inboxDir, 'ok.txt'); writeFileSync(inbox, 'ok')
    const ids = [secret, claude, state, h.tokenFile, h.paths.config, 'relative.txt'].map(f => q('send', { text: '', files: [f] }))
    const ok = q('send', { text: '', files: [inbox] })
    await drain()
    for (const id of ids) expect(outbox(r.store, id)).toMatchObject({ status: 'failed' })
    expect(outbox(r.store, ids[0]!).last_error).toMatch(/refusing to send/)
    expect(outbox(r.store, ids[5]!).last_error).toMatch(/absolute/)
    expect(outbox(r.store, ok).status).toBe('sent')
  })
})

describe('the other ops', () => {
  test('delete, pin/unpin, location/venue, commands, chat_action, download (+ result path)', async () => {
    const { mock, q, result, drain, r } = await setup()
    mock.addFile('doc1', 'hello file', 'pdf')
    const ids = {
      del: q('delete', { message_id: 7 }),
      pin: q('pin', { message_id: 8 }), unpin: q('pin', { message_id: 8, unpin: true }),
      loc: q('location', { latitude: 1.5, longitude: 2.5 }), venue: q('location', { latitude: 1, longitude: 2, title: 'T', address: 'A' }),
      cmds: r.store.enqueue({ bot: BOT, chat: '-', op: 'commands', body: { commands: [{ command: '/Space', description: 'Disk space' }] } })!,
      clear: r.store.enqueue({ bot: BOT, chat: '-', op: 'commands', body: { commands: [] } })!,
      act: q('chat_action', { action: 'upload_photo' }),
      dl: r.store.enqueue({ bot: BOT, chat: '-', op: 'download', body: { file_id: 'doc1' } })!,
      gone: r.store.enqueue({ bot: BOT, chat: '-', op: 'download', body: { file_id: 'nope' } })!,
    }
    await drain()
    expect(await waitFor(() => ['sent', 'failed'].includes(outbox(r.store, ids.gone).status) && outbox(r.store, ids.dl).status === 'sent')).toBe(true)
    expect(mock.sent('deleteMessage')[0]!.params).toMatchObject({ chat_id: CHAT, message_id: 7 })
    expect(mock.sent('pinChatMessage')[0]!.params).toMatchObject({ message_id: 8, disable_notification: true })
    expect(mock.sent('unpinChatMessage').length).toBe(1)
    expect(mock.sent('sendLocation')[0]!.params).toMatchObject({ latitude: 1.5, longitude: 2.5 })
    expect(mock.sent('sendVenue')[0]!.params).toMatchObject({ title: 'T', address: 'A' })
    expect(mock.sent('setMyCommands')[0]!.params).toMatchObject({ commands: [{ command: 'space', description: 'Disk space' }], scope: { type: 'all_private_chats' } })
    expect(mock.sent('deleteMyCommands').length).toBe(1)
    expect(mock.sent('sendChatAction')[0]!.params).toMatchObject({ chat_id: CHAT, action: 'upload_photo' })
    const path = result(ids.dl).path as string
    expect(path.startsWith(running!.adapter.o.paths.inboxDir)).toBe(true)
    expect(readFileSync(path, 'utf8')).toBe('hello file')
    expect(statSync(path).mode & 0o777).toBe(0o600)
    expect(outbox(r.store, ids.gone)).toMatchObject({ status: 'failed' })
    expect(rows(r.store, 'outbox.failed').length).toBe(1)
    // the URL with the token never reaches a row
    expect(JSON.stringify(r.store.db.query('SELECT * FROM outbox').all())).not.toContain('TEST-not-a-real-token')
  })

  test('draft: first update at once, a burst coalesced (≥ 1.2 s apart), a reply ends it; Stop → tg.stop row', async () => {
    const { mock, q, drain, r } = await setup()
    q('draft', { text: 'Checking' })
    await drain()
    q('draft', { text: '…a', append: true }); q('draft', { text: '…b', append: true })
    await drain()
    expect(mock.sent('sendMessageDraft').length).toBe(1)
    expect(await waitFor(() => mock.sent('sendMessageDraft').length === 2, 2500)).toBe(true)
    const calls = mock.sent('sendMessageDraft')
    expect(calls[1]!.params.text).toBe('Checking…a…b')
    expect(calls[1]!.at - calls[0]!.at).toBeGreaterThanOrEqual(1100)
    const draftId = calls[0]!.params.draft_id
    mock.push({ stopped_message_generation: { chat: { id: OWNER, type: 'private' }, draft_id: draftId } })
    expect(await waitFor(() => rows(r.store, 'tg.stop').length === 1)).toBe(true)
    expect(rows(r.store, 'tg.stop')[0].payload).toMatchObject({ event_type: 'stop', draft_id: String(draftId), chat_id: CHAT })
    q('send', { text: 'done' })
    await drain()
    expect(r.adapter.drafts.has(CHAT)).toBe(false)
  })
})

describe('inbound parity', () => {
  test('voice: transcript before the row shows, later rows of the chat wait behind it; the audio is kept for download', async () => {
    const mock = new MockBot()
    mock.addFile('vo1', 'OggS-audio', 'oga')
    const h0 = makeHome()
    const tx = stub(h0, 'transcribe', 'hello world')
    const { r, result, drain } = await setup({}, { TG_ADAPTER_TRANSCRIBE_CMD: tx }, mock)
    mock.push({ message: base({ voice: { file_id: 'vo1', file_unique_id: 'v1', duration: 3, mime_type: 'audio/ogg', file_size: 10 } }) })
    mock.text(OWNER, 'right after')
    expect(await waitFor(() => rows(r.store).length === 2 && rows(r.store).every(x => x.status === 'new'), 4000)).toBe(true)
    const [v, t] = rows(r.store)
    expect(v.payload).toMatchObject({ text: '🎙️ (voice, 3 s) hello world', transcript: 'auto', attachment_kind: 'voice' })
    expect(v.id).toBeLessThan(t.id)
    expect(t.payload.text).toBe('right after')
    const dl = r.store.enqueue({ bot: BOT, chat: '-', op: 'download', body: { file_id: 'vo1' } })!
    await drain()
    expect(await waitFor(() => outbox(r.store, dl).status === 'sent')).toBe(true)
    expect(readFileSync(result(dl).path, 'utf8')).toBe('OggS-audio')
    expect(mock.sent('getFile').length).toBe(1) // the kept copy, not a second download
  })

  test('a failing transcriber: transcribe_error, the row still shows', async () => {
    const mock = new MockBot()
    mock.addFile('vo2', 'OggS', 'oga')
    const h0 = makeHome()
    const { r } = await setup({}, { TG_ADAPTER_TRANSCRIBE_CMD: stub(h0, 'transcribe', null) }, mock)
    mock.push({ message: base({ voice: { file_id: 'vo2', file_unique_id: 'v2', duration: 2 } }) })
    expect(await waitFor(() => rows(r.store).length === 1 && rows(r.store)[0].status === 'new', 4000)).toBe(true)
    expect(rows(r.store)[0].payload).toMatchObject({ text: '(voice message)', transcribe_error: 'exit 3: boom' })
  })

  test('photos are downloaded eagerly: image_path in the instance inbox (0600)', async () => {
    const mock = new MockBot()
    mock.addFile('ph1', png(4, 4), 'jpg')
    const { r } = await setup({}, {}, mock)
    mock.push({ message: base({ photo: [{ file_id: 'ph1', file_unique_id: 'p1', width: 4, height: 4 }], caption: 'see' }) })
    expect(await waitFor(() => rows(r.store).length === 1 && rows(r.store)[0].status === 'new')).toBe(true)
    const p = rows(r.store)[0].payload
    expect(p.text).toBe('see')
    expect(existsSync(p.image_path)).toBe(true)
    expect(p.image_path.startsWith(running!.adapter.o.paths.inboxDir)).toBe(true)
    expect(statSync(p.image_path).mode & 0o777).toBe(0o600)
  })

  test('/scan private: filed by the paperless stub, the owner told; the row carries no caption, title or file id', async () => {
    const mock = new MockBot()
    mock.addFile('sc1', '%PDF-1.4 secret', 'pdf')
    const h0 = makeHome()
    const pl = stub(h0, 'paperless', 'document 77: http://paperless.invalid/77')
    const { r } = await setup({ send: true, scan_private: true }, { TG_ADAPTER_SCAN_CMD: pl }, mock)
    mock.push({ message: base({ document: { file_id: 'sc1', file_unique_id: 's1', file_name: 'tax.pdf', file_size: 15 }, caption: '/scan private Tax letter' }) })
    expect(await waitFor(() => rows(r.store).length === 1 && rows(r.store)[0].status === 'new', 4000)).toBe(true)
    const row = rows(r.store)[0]
    expect(row.payload).toMatchObject({ handled: 'scan-private', doc_id: '77' })
    expect(row.payload.text).toMatch(/^\/scan private \(filed privately by the plugin → paperless document 77; the owner was told/)
    const all = JSON.stringify(row)
    for (const leak of ['Tax letter', 'sc1', 'tax.pdf']) expect(all).not.toContain(leak)
    expect(readFileSync(`${pl}.args`, 'utf8')).toMatch(/upload .* --title=Tax letter --tag Inbox --tag private --create/)
    expect(await waitFor(() => mock.sent('sendMessage').some(c => /Filed privately → document 77/.test(c.params.text)))).toBe(true)
  })

  test('/sp with private scans off: refused, the content never shows', async () => {
    const mock = new MockBot()
    const { r } = await setup({ scan_private: false }, {}, mock)
    mock.push({ message: base({ photo: [{ file_id: 'ph9', file_unique_id: 'p9', width: 1, height: 1 }], caption: '/sp Passport' }) })
    expect(await waitFor(() => rows(r.store).length === 1 && rows(r.store)[0].status === 'new')).toBe(true)
    const row = rows(r.store)[0]
    expect(row.payload).toMatchObject({ handled: 'scan-private', scan_error: 'off' })
    expect(JSON.stringify(row)).not.toContain('Passport')
    expect(row.payload.image_path).toBeUndefined()
    expect(mock.sent('getFile')).toEqual([])
  })

  test('a row a killed process left pending is released at start as interrupted', async () => {
    const h = makeHome({ send: false })
    const store = new Store(openDb(h.paths.defaultDb))
    store.commitUpdate({ ts: 1, source: 'tg-adapter', kind: 'tg.message', subject: 's', next: '', delivery: 'now', trace_id: null, idem_key: 'tg:4242:1',
      labels: ['tg', 'test'], payload: { chat_id: CHAT, user_id: CHAT, attachment_kind: 'voice', text: '(voice message)', bot: BOT } }, 'offset:4242', 2, 'pending')
    store.db.close()
    running = await startAdapter(new MockBot(), h)
    const row = rows(running.store)[0]
    expect(row.status).toBe('new')
    expect(row.payload.transcribe_error).toBe('interrupted (the adapter restarted)')
  })
})

describe('typing, ack reaction, compat writers', () => {
  test('typing while the board says working; a reply ends it; ackReaction; compat journal + buttons file', async () => {
    const mock = new MockBot()
    const h = makeHome()
    const board = join(h.home, 'board', 'state.json')
    mkdirSync(join(h.home, 'board'), { recursive: true })
    writeFileSync(board, JSON.stringify({ mode: 'working', since: Date.now() / 1000 - 5 }))
    const access = join(h.home, 'access.json')
    writeFileSync(access, JSON.stringify({ dmPolicy: 'allowlist', allowFrom: [CHAT], groups: {}, ackReaction: '👀' }))
    const journal = join(h.home, 'compat', 'inbound-journal.jsonl')
    const buttons = join(h.home, 'compat', 'buttons.json')
    writeConfig(h, { send: true, status_state: board, allow_from: undefined, allow_from_file: access, compat_journal: journal, compat_buttons_file: buttons })
    running = await startAdapter(mock, h)
    const r = running
    mock.text(OWNER, 'hi there')
    expect(await waitFor(() => mock.sent('sendChatAction').length >= 1)).toBe(true)
    expect(mock.sent('setMessageReaction')[0]!.params.reaction).toEqual([{ type: 'emoji', emoji: '👀' }])
    const id = r.store.enqueue({ bot: BOT, chat: CHAT, op: 'send', body: { text: 'answer', buttons: [[{ text: 'A', data: 'a' }]], on_press: 'keep' } })!
    expect(await waitFor(() => outbox(r.store, id).status === 'sent')).toBe(true)
    expect(r.adapter.typing.shown(CHAT)).toBe(null)
    const j = readFileSync(journal, 'utf8').trim().split('\n').map(l => JSON.parse(l))
    expect(j.length).toBe(1)
    expect(j[0]).toMatchObject({ kind: 'message', owner: true, chat_id: CHAT, content: 'hi there', status: 'unverified' })
    expect(j[0].meta).toMatchObject({ chat_id: CHAT, user_id: CHAT, event_type: 'message' })
    expect(Object.keys(JSON.parse(readFileSync(buttons, 'utf8')))).toEqual([`${CHAT}:${outbox(r.store, id).tg_message_id}`])
    expect(statSync(journal).mode & 0o777).toBe(0o600)
  })

  test('journal entry shape matches 0.1.22 (keys), scan rows journal as instant', () => {
    const e = journalEntry('tg.button', { chat_id: '1', message_id: '2', user_id: '1', ts: 't', event_type: 'button', callback_data: 'x', text: '(pressed button: X)', bot: 'b', update_id: 5, ack_ms: 12 }, true)
    expect(Object.keys(e)).toEqual(['id', 'at', 'kind', 'owner', 'chat_id', 'message_id', 'ts', 'content', 'meta', 'status', 'tries', 'last_sent_at', 'pid'])
    expect(e.meta).toEqual({ chat_id: '1', message_id: '2', user_id: '1', ts: 't', event_type: 'button', callback_data: 'x', ack_ms: '12' })
    expect(journalEntry('tg.message', { chat_id: '1', handled: 'scan-private', text: '/sp' }, true).kind).toBe('instant')
  })
})

describe('rules: answers and bridge rows are never held or deduped', () => {
  test('quiet hours hold a script row but not a reply_to_event or bridge row; bridge repeats are sent', async () => {
    const { mock, q, drain, r } = await setup({ quiet_hours: '0-24' }, {})
    void r
    const script = q('send', { text: 'script' })
    const answer = q('send', { text: 'answer' }, { reply_to_event: 12 })
    const b1 = q('send', { text: 'same', bridge: true }), b2 = q('send', { text: 'same', bridge: true })
    await drain()
    expect(outbox(running!.store, script).status).toBe('queued')
    for (const id of [answer, b1, b2]) expect(outbox(running!.store, id).status).toBe('sent')
    expect(mock.sent('sendMessage').map(c => c.params.text)).toEqual(['answer', 'same', 'same'])
  })
})

describe('the bridge contract (telegram-plus 0.2.0 bridge mode)', () => {
  test('rows under the instance key with hold:false/dedupe:false: sent in quiet hours, repeats sent, files once, 0.1.22 texts', async () => {
    const { mock, h, r, result, drain } = await setup({ quiet_hours: '0-24' }, {})
    const dir = join(h.home, 'out')
    mkdirSync(dir, { recursive: true })
    const pdf = join(dir, 'x.pdf')
    writeFileSync(pdf, '%PDF-1.4')
    const b = (chat: string, op: string, body: Record<string, unknown>, extra: Record<string, unknown> = {}) =>
      r.store.enqueue({ bot: 'test', chat, op, body: { ...body, hold: false, dedupe: false }, ...extra })!
    const s1 = b(CHAT, 'send', { text: 'same', files: [pdf] }, { files: [pdf] })
    const s2 = b(CHAT, 'send', { text: 'same' })
    const re = b(CHAT, 'react', { message_id: 5, emoji: '👍' })
    const ca = b(CHAT, 'chat_action', { action: 'upload_photo' })
    const dr = b(CHAT, 'draft', { text: 'Working' })
    const clr = b(CHAT, 'draft', { clear: true })
    const none = b(CHAT, 'draft', { clear: true })
    await drain()
    for (const id of [s1, s2, re, ca, dr, clr, none]) expect(outbox(r.store, id).status).toBe('sent')
    expect(mock.sent('sendDocument').length).toBe(1) // the files column and body.files name the same file: sent once
    expect(mock.sent('sendMessage').filter(c => c.params.text === 'same').length).toBe(2)
    expect(result(s1).ids.length).toBe(2)
    expect(result(re).text).toBe('reacted 👍')
    expect(result(ca).text).toBe('showing upload_photo')
    expect(result(dr).text).toBe('draft updated (streaming draft, 7 chars)')
    expect(result(clr).text).toBe('draft cleared')
    expect(result(none).text).toBe('no active draft')
    mock.addFile('doc1', 'payload', 'pdf')
    const dl = b('', 'download', { file_id: 'doc1' })
    await drain()
    expect(await waitFor(() => outbox(r.store, dl).status === 'sent')).toBe(true)
    expect(readFileSync(result(dl).path, 'utf8')).toBe('payload')
  })

  test('a pending row is invisible to delivery-now readers and is released under a fresh id', async () => {
    const mock = new MockBot()
    mock.addFile('vo2', 'OggS-audio', 'oga')
    const h0 = makeHome()
    const dir = join(h0.home, 'stubs')
    mkdirSync(dir, { recursive: true })
    const tx = join(dir, 'slow-transcribe')
    writeFileSync(tx, '#!/bin/sh\nsleep 1\necho "later words"\n')
    chmodSync(tx, 0o755)
    const { r } = await setup({}, { TG_ADAPTER_TRANSCRIBE_CMD: tx }, mock)
    mock.push({ message: base({ voice: { file_id: 'vo2', file_unique_id: 'v2', duration: 2, mime_type: 'audio/ogg', file_size: 10 } }) })
    expect(await waitFor(() => !!r.store.db.query("SELECT 1 FROM events WHERE status = 'pending'").get(), 3000)).toBe(true)
    const held = r.store.db.query("SELECT id, delivery FROM events WHERE status = 'pending'").get() as { id: number; delivery: string }
    expect(held.delivery).toBe('held:now')
    expect(r.store.db.query("SELECT COUNT(*) AS n FROM events WHERE delivery = 'now'").get()).toEqual({ n: 0 })
    expect(await waitFor(() => rows(r.store).length === 1 && rows(r.store)[0].status === 'new', 5000)).toBe(true)
    const row = r.store.db.query('SELECT id, delivery, idem_key FROM events').get() as { id: number; delivery: string; idem_key: string }
    expect(row.delivery).toBe('now')
    expect(row.id).toBeGreaterThan(held.id)
    expect(row.idem_key).toMatch(/^tg:/)
  })
})

describe('instances', () => {
  test('live mode writes rows into the main events DB; its own state dir; test.json beside the older config.json', async () => {
    const mock = new MockBot()
    const h = makeHome()
    const env = { ...h.env, ASSISTANT_EVENTS_DB: join(h.home, 'main', 'events.db') }
    const live = resolvePaths(env, 'live')
    writeFileSync(live.config, readFileSync(h.paths.config, 'utf8').replace('"mode":"test"', '"mode":"live"'))
    const lh: Home = { ...h, env, paths: live }
    running = await startAdapter(mock, lh, { dbPath: live.defaultDb })
    expect(running.logs.some(l => /mode live/.test(l))).toBe(true)
    mock.text(OWNER, 'to live')
    expect(await waitFor(() => rows(running!.store).length === 1)).toBe(true)
    expect(running.adapter.o.paths.pidFile).toBe(join(h.home, '.local', 'state', 'tg-adapter', 'live', 'adapter.pid'))
    expect(existsSync(join(h.home, 'main', 'events.db'))).toBe(true)
    // the older config.json still serves the test instance while test.json is absent
    writeFileSync(join(h.home, '.config', 'tg-adapter', 'config.json'), '{}')
    const t = resolvePaths(h.env, 'test')
    expect(t.config).toBe(join(h.home, '.config', 'tg-adapter', 'test.json')) // test.json exists here
  })
})
