import { afterAll, expect, test } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { startVoiceInbox, voiceEvent } from '../voiceinbox.ts'

const dir = mkdtempSync(join(tmpdir(), 'voice-inbox-'))
const tokenFile = join(dir, 'tok')
writeFileSync(tokenFile, 'sekrit-token\n', { mode: 0o600 })
const stops: Array<() => void> = []
afterAll(() => { stops.forEach(s => s()); rmSync(dir, { recursive: true, force: true }) })

function start(extra: Record<string, unknown> = {}) {
  const got: Array<{ c: string; m: Record<string, string>; at: number }> = []
  const s = startVoiceInbox({ port: 0, tokenFile, deliver: (c, m) => got.push({ c, m, at: performance.now() }), ...extra })!
  stops.push(s.stop)
  return { got, url: `http://127.0.0.1:${s.port}/voice` }
}
const post = (url: string, body: unknown, token = 'sekrit-token') => fetch(url, {
  method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
  body: typeof body === 'string' ? body : JSON.stringify(body),
})

test('a valid hand-off becomes one channel event, fast', async () => {
  const { got, url } = start()
  const t0 = performance.now()
  const r = await post(url, { id: 'v0123abcd', task: 'what is on tomorrow?', urgency: 'now' })
  expect(r.status).toBe(202)
  expect(got).toHaveLength(1)
  expect(got[0]!.m).toMatchObject({ via: 'voice', voice_id: 'v0123abcd', urgency: 'now', chat_id: 'voice' })
  expect(got[0]!.c).toContain('voice-reply v0123abcd')
  expect(got[0]!.at - t0).toBeLessThan(200)
  console.log(`LATENCY post→deliver ${(got[0]!.at - t0).toFixed(2)} ms`)
})

test('wrong or missing token: 401, nothing delivered', async () => {
  const { got, url } = start()
  expect((await post(url, { id: 'v0123abcd', task: 'x' }, 'nope')).status).toBe(401)
  expect((await fetch(url, { method: 'POST', body: '{}' })).status).toBe(401)
  expect(got).toHaveLength(0)
})

test('validation: id, task, json, size, method and path', async () => {
  const { got, url } = start()
  expect((await post(url, { id: '../etc', task: 'x' })).status).toBe(400)
  expect((await post(url, { id: 'v0123abcd', task: '' })).status).toBe(400)
  expect((await post(url, { id: 'v0123abcd', task: 'x'.repeat(2001) })).status).toBe(400)
  expect((await post(url, '{not json')).status).toBe(400)
  expect([413, 400]).toContain((await post(url, { id: 'v0123abcd', task: 'y'.repeat(9000) })).status)
  expect((await fetch(url)).status).toBe(404)
  expect((await post(url.replace('/voice', '/other'), { id: 'v0123abcd', task: 'x' })).status).toBe(404)
  expect(got).toHaveLength(0)
})

test('rate limit per minute', async () => {
  const { url } = start({ maxPerMinute: 2 })
  for (let i = 0; i < 2; i++) expect((await post(url, { id: 'v0123abcd', task: 'x' + i })).status).toBe(202)
  expect((await post(url, { id: 'v0123abcd', task: 'x3' })).status).toBe(429)
})

test('off without a port or a token', () => {
  expect(startVoiceInbox({ port: undefined, tokenFile, deliver: () => {} })).toBeNull()
  expect(startVoiceInbox({ port: NaN, tokenFile, deliver: () => {} })).toBeNull()
  expect(startVoiceInbox({ port: 0, tokenFile: join(dir, 'missing'), deliver: () => {} })).toBeNull()
})

test('busy port: retries until the old instance lets go, then serves', async () => {
  const old = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: () => new Response('old') })
  const port = old.port
  const logs: string[] = []
  let late: { port: number; stop(): void } | undefined
  const got: string[] = []
  const h = startVoiceInbox({ port, tokenFile, deliver: c => got.push(c), log: m => logs.push(m), retries: 50, retryMs: 20,
    onListen: x => { late = x; stops.push(x.stop) } })
  expect(h).toBeNull()
  expect(logs.join('\n')).toContain('busy, retrying')
  old.stop(true)
  for (let i = 0; i < 100 && !late; i++) await Bun.sleep(20)
  expect(late?.port).toBe(port)
  expect((await post(`http://127.0.0.1:${port}/voice`, { id: 'v0123abcd', task: 'after restart' })).status).toBe(202)
  expect(got).toHaveLength(1)
})

test('busy port without retries: off, one log line', () => {
  const old = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: () => new Response('old') })
  const logs: string[] = []
  expect(startVoiceInbox({ port: old.port, tokenFile, deliver: () => {}, log: m => logs.push(m) })).toBeNull()
  old.stop(true)
  expect(logs).toHaveLength(1)
  expect(logs[0]).toContain('voice inbox off')
})

test('control and bidi characters are stripped, urgency defaults to now', () => {
  const ev = voiceEvent({ id: 'v0123abcd', task: 'hi\u0007 there‮', urgency: 'whenever' })
  expect(typeof ev).toBe('object')
  if (typeof ev === 'object') { expect(ev.content).toContain('hi  there'); expect(ev.meta.urgency).toBe('now') }
})

test('app chat (source "app"): one channel event with meta via="app", answered with phone chat', async () => {
  const { got, url } = start()
  const r = await post(url, { source: 'app', id: 'm_01JAB3K6Q2W8Z7M4D5N0P9R1ST', text: 'is the server ok?', reply_to: 'm_01JAB00000' })
  expect(r.status).toBe(202)
  expect(got).toHaveLength(1)
  expect(got[0]!.m).toMatchObject({ via: 'app', chat_id: 'app', app_msg_id: 'm_01JAB3K6Q2W8Z7M4D5N0P9R1ST', reply_to: 'm_01JAB00000', user: 'owner (app)' })
  expect(got[0]!.m.voice_id).toBeUndefined()
  expect(got[0]!.c.startsWith('📱 (app) is the server ok?\n')).toBe(true)
  expect(got[0]!.c).toContain('phone chat "…" --reply-to m_01JAB3K6Q2W8Z7M4D5N0P9R1ST')
})

test('app chat validation: id, text, size, source, token; task works as text', async () => {
  const { got, url } = start()
  expect((await post(url, { source: 'app', id: '../etc', text: 'x' })).status).toBe(400)
  expect((await post(url, { source: 'app', id: 'm_1', text: 'x' })).status).toBe(400)
  expect((await post(url, { source: 'app', id: 'm_0123', text: '  ' })).status).toBe(400)
  expect((await post(url, { source: 'app', id: 'm_0123', text: 'z'.repeat(4001) })).status).toBe(400)
  expect((await post(url, { source: 'app', id: 'm_0123', text: 'x' }, 'nope')).status).toBe(401)
  expect((await post(url, { source: 'web', id: 'v0123abcd', task: 'x' })).status).toBe(400)
  expect(got).toHaveLength(0)
  expect((await post(url, { source: 'app', id: 'm_0123', task: 'from task', reply_to: '../x' })).status).toBe(202)
  expect(got[0]!.c.startsWith('📱 (app) from task')).toBe(true)
  expect(got[0]!.m.reply_to).toBeUndefined()
  expect((await post(url, { source: 'voice', id: 'v0123abcd', task: 'still voice' })).status).toBe(202)
  expect(got[1]!.m.via).toBe('voice')
})

test('app chat: control and bidi characters are stripped', () => {
  const ev = voiceEvent({ source: 'app', id: 'm_0123', text: 'hi\u0007 there‮!' })
  expect(typeof ev).toBe('object')
  expect((ev as { content: string }).content.split('\n')[0]).toBe('📱 (app) hi  there !')
})

test('app chat as phonegw sends it: hand-off id + msg_id + task; the reply hint names msg_id', async () => {
  const { got, url } = start()
  const r = await post(url, { id: 'a0123abcd', task: '[button y] Yes', urgency: 'later', source: 'app', msg_id: 'm_01JAB3K6Q2W8Z7M4D5N0P9R1ST', device_id: 'd_7Q2M' })
  expect(r.status).toBe(202)
  expect(got[0]!.m).toMatchObject({ via: 'app', app_msg_id: 'm_01JAB3K6Q2W8Z7M4D5N0P9R1ST', inbox_id: 'a0123abcd', device_id: 'd_7Q2M' })
  expect(got[0]!.c).toContain('--reply-to m_01JAB3K6Q2W8Z7M4D5N0P9R1ST')
  expect((await post(url, { id: 'a0123abcd', task: 'x', source: 'app', msg_id: '../bad' })).status).toBe(202)
  expect(got[1]!.m.app_msg_id).toBeUndefined()
  expect(got[1]!.m.inbox_id).toBe('a0123abcd')
  expect(got[1]!.c).not.toContain('--reply-to a0123abcd')
})

test('app share / card pick (hand-off id, no msg_id): the hint leaves --reply-to out, no app_msg_id', async () => {
  const { got, url } = start()
  const share = '[share s_cap1] image share from his phone app (21.10 10:00, intent what): handle it like his Telegram message'
  expect((await post(url, { id: 'a89abcdef', task: share, urgency: 'later', source: 'app', device_id: 'd_7Q2M' })).status).toBe(202)
  expect(got[0]!.m).toMatchObject({ via: 'app', chat_id: 'app', inbox_id: 'a89abcdef', device_id: 'd_7Q2M' })
  expect(got[0]!.m.app_msg_id).toBeUndefined()
  expect(got[0]!.c.startsWith('📱 (app) [share s_cap1] image share')).toBe(true)
  expect(got[0]!.c).toContain('Answer with: phone chat "…" (no --reply-to: not a chat message)')
  expect(got[0]!.c).not.toContain('--reply-to a89abcdef')
  // a direct app message id as the hand-off id (no msg_id) still gets the reply hint
  expect((await post(url, { id: 'm_01JAB3K6Q2W8Z7M4D5N0P9R1ST', task: 'hi', source: 'app' })).status).toBe(202)
  expect(got[1]!.m.app_msg_id).toBe('m_01JAB3K6Q2W8Z7M4D5N0P9R1ST')
  expect(got[1]!.m.inbox_id).toBeUndefined()
  expect(got[1]!.c).toContain('phone chat "…" --reply-to m_01JAB3K6Q2W8Z7M4D5N0P9R1ST')
})
