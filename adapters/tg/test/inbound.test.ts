// Inbound: message / button / reaction / poll answer → event rows, the access gate, callback acks
// at once (< 200 ms on the mock), the on_press marking, and attachments as file ids.
// The adapter runs in process against MockBot (injected fetch: no network, no sockets).
import { afterAll, afterEach, describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { join } from 'path'
import { cleanup, makeHome, rows, startAdapter, waitFor, writeConfig, type Running } from './helpers.ts'
import { GROUP, MockBot, OWNER, STRANGER } from './mockbot.ts'

afterAll(cleanup)
let running: Running | null = null
afterEach(async () => { if (running) await running.stop(); running = null })

async function setup(config: Record<string, unknown> = {}) {
  const mock = new MockBot()
  const h = makeHome(config)
  running = await startAdapter(mock, h)
  return { mock, h, r: running }
}

describe('messages', () => {
  test('an owner message becomes one tg.message row (idem_key = update id), offset advances', async () => {
    const { mock, r } = await setup()
    const uid = mock.text(OWNER, 'hello there', { reply_to_message: { message_id: 7, date: 0, chat: { id: OWNER, type: 'private' }, text: 'earlier' } })
    expect(await waitFor(() => rows(r.store).length === 1)).toBe(true)
    const [row] = rows(r.store)
    expect(row.kind).toBe('tg.message')
    expect(row.source).toBe('tg-adapter')
    expect(row.idem_key).toBe(`tg:4242:${uid}`)
    expect(row.status).toBe('new')
    expect(row.delivery).toBe('now')
    expect(row.labels).toEqual(['tg', 'test'])
    expect(row.subject).toBe('owner: hello there')
    expect(row.payload).toMatchObject({ chat_id: String(OWNER), user: 'owner', user_id: String(OWNER), event_type: 'message', text: 'hello there',
      reply_to_message_id: '7', reply_to_text: 'earlier', update_id: uid, bot: 'testadapter_bot' })
    expect(await waitFor(() => r.adapter.offset === uid + 1)).toBe(true)
  })

  test('a stranger is dropped without a reply (no pairing); the offset still moves on', async () => {
    const { mock, r } = await setup()
    const uid = mock.text(STRANGER, 'let me in')
    expect(await waitFor(() => r.adapter.offset === uid + 1)).toBe(true)
    expect(rows(r.store)).toEqual([])
    expect(mock.sent('sendMessage')).toEqual([])
    expect(r.store.getMeta('dropped:not allowed')).toBe('1')
  })

  test('groups: allowlisted group needs a mention (access file read live)', async () => {
    const mock = new MockBot()
    const h = makeHome()
    const accessFile = join(h.home, 'access.json')
    writeFileSync(accessFile, JSON.stringify({ dmPolicy: 'pairing', allowFrom: [String(OWNER)], groups: { [String(GROUP)]: { requireMention: true, allowFrom: [] } }, pending: {} }))
    writeConfig(h, { allow_from: undefined, allow_from_file: accessFile })
    running = await startAdapter(mock, h)
    const r = running
    mock.text(OWNER, 'no mention here', {}, GROUP)
    const u2 = mock.text(OWNER, 'hey @testadapter_bot look', { entities: [{ type: 'mention', offset: 4, length: 16 }] }, GROUP)
    expect(await waitFor(() => r.adapter.offset === u2 + 1)).toBe(true)
    expect(rows(r.store).map(x => x.payload.text)).toEqual(['hey @testadapter_bot look'])
    // dmPolicy "pairing" counts as allowlist: a stranger's DM is dropped, the file is never written
    const u3 = mock.text(STRANGER, 'hi')
    expect(await waitFor(() => r.adapter.offset === u3 + 1)).toBe(true)
    expect(rows(r.store).length).toBe(1)
    expect(JSON.parse(require('fs').readFileSync(accessFile, 'utf8')).pending).toEqual({})
  })

  test('attachments become file ids (photos are also downloaded); venue before location; contact', async () => {
    const { mock, r } = await setup()
    const base = (x: any) => ({ message: { message_id: 900 + Math.floor(Math.random() * 99), date: 1, chat: { id: OWNER, type: 'private' }, from: mock.user(OWNER), ...x } })
    mock.push(base({ photo: [{ file_id: 'ph-small', file_unique_id: 'a', width: 1, height: 1 }, { file_id: 'ph-big', file_unique_id: 'b', width: 9, height: 9, file_size: 99 }], caption: 'look' }))
    mock.push(base({ document: { file_id: 'doc1', file_unique_id: 'd', file_name: 'a<b>.pdf', mime_type: 'application/pdf', file_size: 5 } }))
    mock.push(base({ voice: { file_id: 'v1', file_unique_id: 'v', duration: 3, mime_type: 'audio/ogg' } }))
    mock.push(base({ venue: { location: { latitude: 1.5, longitude: 2.5 }, title: 'Club', address: 'Street 1' }, location: { latitude: 1.5, longitude: 2.5 } }))
    mock.push(base({ contact: { phone_number: '+10000000000', first_name: 'Ann' } }))
    const last = mock.push(base({ dice: { emoji: '🎲', value: 3 } }))
    expect(await waitFor(() => r.adapter.offset === last + 1)).toBe(true)
    const p = rows(r.store).map(x => x.payload)
    expect(p.length).toBe(5)
    expect(p[0]).toMatchObject({ text: 'look', attachment_kind: 'photo', attachment_file_id: 'ph-big', attachment_size: '99' })
    expect(p[1]).toMatchObject({ text: '(document: a_b_.pdf)', attachment_kind: 'document', attachment_name: 'a_b_.pdf', attachment_mime: 'application/pdf' })
    expect(p[2]).toMatchObject({ text: '(voice message)', attachment_kind: 'voice', attachment_duration: '3' })
    expect(p[3]).toMatchObject({ text: '(venue: Club, Street 1)', venue_title: 'Club' })
    expect(p[4]).toMatchObject({ text: '(contact: Ann, +10000000000)', contact_name: 'Ann' })
    expect(r.store.getMeta('dropped:unsupported message type')).toBe('1')
    // parity: the photo is downloaded eagerly (the mock has no such file: no image_path, the row still shows)
    expect(mock.sent('getFile').map(c => c.params.file_id)).toEqual(['ph-big'])
    expect(await waitFor(() => rows(r.store).every(x => x.status === 'new'))).toBe(true)
  })
})

describe('buttons', () => {
  test('a press is answered at once (< 200 ms on the mock), becomes a tg.button row with ack_ms, and is marked ✓', async () => {
    const { mock, r } = await setup()
    mock.delays.editMessageText = 50 // the mark edit must not hold up the ack
    const uid = mock.press(OWNER, 'yes', 'Yes')
    expect(await waitFor(() => rows(r.store, 'tg.button').length === 1)).toBe(true)
    const ans = mock.sent('answerCallbackQuery')
    expect(ans.length).toBe(1)
    expect(ans[0]!.params).toMatchObject({ callback_query_id: `cq${uid}`, text: '✓ Yes' })
    const batch = mock.getUpdatesCalls.find(c => c.returned.includes(uid))!
    const tapToAck = ans[0]!.at - batch.doneAt!
    expect(tapToAck).toBeLessThan(200)
    const [row] = rows(r.store, 'tg.button')
    expect(row.subject).toBe('button "Yes"')
    expect(row.payload).toMatchObject({ event_type: 'button', callback_data: 'yes', button_text: 'Yes', message_text: 'Pick one', ack_ok: true })
    expect(row.payload.ack_ms).toBeLessThan(200)
    expect(await waitFor(() => mock.sent('editMessageText').length === 1)).toBe(true)
    expect(mock.sent('editMessageText')[0]!.params).toMatchObject({ text: 'Pick one\n\n✓ Yes', reply_markup: { inline_keyboard: [] } })
  })

  test('several presses in one batch are all answered before any row is written', async () => {
    const { mock, r } = await setup()
    mock.delays.answerCallbackQuery = 80
    mock.maxBatch = 10
    const ids = [mock.press(OWNER, 'a', 'A'), mock.press(OWNER, 'b', 'B'), mock.press(OWNER, 'c', 'C')]
    expect(await waitFor(() => rows(r.store, 'tg.button').length === 3)).toBe(true)
    const ans = mock.sent('answerCallbackQuery')
    const batch = mock.getUpdatesCalls.find(c => ids.every(i => c.returned.includes(i)))
    expect(batch).toBeDefined()
    // concurrent: all three started right after the batch; one after the other they would be >= 80 ms apart
    for (const a of ans) expect(a.at - batch!.doneAt!).toBeLessThan(60)
  })

  test('a stranger\'s press gets "Not authorized." and no row; perm: buttons are not relayed', async () => {
    const { mock, r } = await setup()
    mock.press(STRANGER, 'yes', 'Yes')
    const u2 = mock.press(OWNER, 'perm:allow:abcde', 'Allow')
    expect(await waitFor(() => r.adapter.offset === u2 + 1)).toBe(true)
    expect(rows(r.store)).toEqual([])
    expect(mock.sent('answerCallbackQuery').map(c => c.params.text)).toEqual(['Not authorized.', undefined])
    expect(mock.sent('editMessageText')).toEqual([])
  })

  test('on_press keep (saved by the outbox in buttons.json) leaves the message alone; rich text gets the ✓ edit as rich', async () => {
    const { mock, r } = await setup()
    const keep = r.store.enqueue({ bot: 'testadapter_bot', chat: String(OWNER), op: 'send', body: { text: 'Keep me', buttons: [{ text: 'K', data: 'k' }], on_press: 'keep' } })!
    const rich = r.store.enqueue({ bot: 'testadapter_bot', chat: String(OWNER), op: 'send', body: { text: '**Rich** pick', format: 'rich', buttons: [{ text: 'R', data: 'r' }] } })!
    expect(await waitFor(() => r.store.db.query("SELECT COUNT(*) AS n FROM outbox WHERE status = 'sent'").get() as any && (r.store.db.query("SELECT COUNT(*) AS n FROM outbox WHERE status = 'sent'").get() as any).n === 2)).toBe(true)
    const keepId = (r.store.db.query('SELECT tg_message_id AS m FROM outbox WHERE id = ?').get(keep) as any).m
    const richId = (r.store.db.query('SELECT tg_message_id AS m FROM outbox WHERE id = ?').get(rich) as any).m
    mock.press(OWNER, 'k', 'K', { message_id: keepId, text: 'Keep me' })
    const u2 = mock.press(OWNER, 'r', 'R', { message_id: richId, text: null })
    expect(await waitFor(() => r.adapter.offset === u2 + 1 && mock.sent('editMessageText').length === 1)).toBe(true)
    const edits = mock.sent('editMessageText')
    expect(edits[0]!.params.message_id).toBe(richId)
    expect(edits[0]!.params.rich_message.markdown).toContain('✓ R')
  })
})

describe('reactions and polls', () => {
  test('a reaction on a message the outbox sent says which message (from the DB after a restart too)', async () => {
    const { mock, h, r } = await setup()
    const id = r.store.enqueue({ bot: 'testadapter_bot', chat: String(OWNER), op: 'send', body: { text: 'Shall I?' } })!
    expect(await waitFor(() => (r.store.db.query('SELECT status FROM outbox WHERE id = ?').get(id) as any).status === 'sent')).toBe(true)
    const mid = (r.store.db.query('SELECT tg_message_id AS m FROM outbox WHERE id = ?').get(id) as any).m
    mock.react(OWNER, mid, ['👍'])
    expect(await waitFor(() => rows(r.store, 'tg.reaction').length === 1)).toBe(true)
    expect(rows(r.store, 'tg.reaction')[0].payload).toMatchObject({ event_type: 'reaction', reaction: '👍', reacted_to_message_id: String(mid),
      reacted_to_from: 'bot', reacted_to_text: 'Shall I?', text: 'Reacted 👍 to your message: "Shall I?"' })
    // a fresh process (empty memory) still knows it from the outbox row
    await r.stop()
    running = await startAdapter(mock, h)
    mock.react(OWNER, mid, ['🔥'], ['👍'])
    expect(await waitFor(() => rows(running!.store, 'tg.reaction').length === 2)).toBe(true)
    expect(rows(running!.store, 'tg.reaction')[1].payload).toMatchObject({ reaction: '🔥', removed_reaction: '👍', reacted_to_from: 'bot' })
  })

  test('pure removals are dropped (forwardReactions default true); unknown messages are named by id', async () => {
    const { mock, r } = await setup()
    mock.react(OWNER, 77, [], ['👍'])
    const u = mock.react(OWNER, 78, ['👀'])
    expect(await waitFor(() => r.adapter.offset === u + 1)).toBe(true)
    const rs = rows(r.store, 'tg.reaction')
    expect(rs.length).toBe(1)
    expect(rs[0].payload.text).toBe('Reacted 👀 to message 78 (not in the recent-message cache)')
  })

  test('a poll sent via the outbox maps answers back to option text; unknown polls are dropped', async () => {
    const { mock, r } = await setup()
    const id = r.store.enqueue({ bot: 'testadapter_bot', chat: String(OWNER), op: 'poll', body: { question: 'Which?', options: ['Red', 'Blue', 'Green'], multiple: true } })!
    expect(await waitFor(() => (r.store.db.query('SELECT status FROM outbox WHERE id = ?').get(id) as any).status === 'sent')).toBe(true)
    expect(mock.sent('sendPoll')[0]!.params).toMatchObject({ question: 'Which?', allows_multiple_answers: true, is_anonymous: false })
    mock.pollAnswer(OWNER, 'poll-1', [0, 2])
    mock.pollAnswer(OWNER, 'poll-1', [])
    const u = mock.pollAnswer(OWNER, 'poll-unknown', [1])
    expect(await waitFor(() => r.adapter.offset === u + 1)).toBe(true)
    const rs = rows(r.store, 'tg.poll_answer')
    expect(rs.map(x => x.payload.text)).toEqual(['(poll answer to "Which?": Red, Green)', '(retracted vote on poll "Which?")'])
    expect(rs[0].payload).toMatchObject({ poll_options: 'Red | Green', option_ids: [0, 2], chat_id: String(OWNER) })
    expect(r.store.getMeta('dropped:unknown poll')).toBe('1')
  })
})
