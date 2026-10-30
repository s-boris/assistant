// Restarts and interlocks, in process: stop mid-stream and restart (no loss, no duplicates, the
// stored offset is used), Telegram re-sending confirmed updates, idem_key dedupe with a lagging
// offset; wrong bot username, 409, 401, switch file (at start and mid-run), PID file.
import { afterAll, afterEach, describe, expect, test } from 'bun:test'
import { existsSync, mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { EXIT } from '../src/adapter.ts'
import { cleanup, makeHome, rows, startAdapter, waitFor, type Running } from './helpers.ts'
import { MockBot, OWNER } from './mockbot.ts'

afterAll(cleanup)
let running: Running | null = null
afterEach(async () => { if (running) await running.stop(); running = null })

const uniqueIds = (rs: any[]) => new Set(rs.map(r => r.idem_key)).size

describe('restart mid-stream', () => {
  test('stop after some updates, restart on the same DB: every update exactly once; the stored offset is used', async () => {
    const mock = new MockBot()
    mock.maxBatch = 3
    mock.delays.answerCallbackQuery = 20
    const h = makeHome()
    running = await startAdapter(mock, h)
    const N = 30
    const ids: number[] = []
    for (let i = 0; i < N; i++) ids.push(i % 3 === 0 ? mock.press(OWNER, `d${i}`, `L${i}`) : mock.text(OWNER, `m${i}`))
    expect(await waitFor(() => rows(running!.store).length >= 8)).toBe(true)
    await running.stop()
    const mid = rows(running.store).length
    expect(mid).toBeLessThan(N)
    const stored = running.adapter.offset
    running.store.db.close()
    running = await startAdapter(mock, h)
    expect(await waitFor(() => rows(running!.store).length === N, 5000)).toBe(true)
    await Bun.sleep(100)
    const rs = rows(running.store)
    expect(rs.length).toBe(N)
    expect(uniqueIds(rs)).toBe(N)
    expect(rs.map(r => r.payload.update_id)).toEqual(ids)
    // the restarted adapter asked for exactly the stored offset first
    const firstAfter = mock.getUpdatesCalls.find(c => c.offset === stored)
    expect(firstAfter).toBeDefined()
  })

  test('Telegram re-sends already confirmed updates: no duplicate rows', async () => {
    const mock = new MockBot()
    const h = makeHome()
    running = await startAdapter(mock, h)
    const a = mock.text(OWNER, 'one')
    expect(await waitFor(() => rows(running!.store).length === 1)).toBe(true)
    // put the confirmed update back in front and ignore the next offset once
    mock.queue.unshift({ update_id: a, message: { message_id: 1, date: 1, chat: { id: OWNER, type: 'private' }, from: mock.user(OWNER), text: 'one' } })
    mock.redeliverOnce = true
    const b = mock.text(OWNER, 'two')
    expect(await waitFor(() => running!.adapter.offset === b + 1)).toBe(true)
    expect(rows(running.store).map(r => r.payload.text)).toEqual(['one', 'two'])
  })

  test('a lagging offset (rows kept, offset lost) is caught by the UNIQUE idem_key', async () => {
    const mock = new MockBot()
    const h = makeHome()
    running = await startAdapter(mock, h)
    const a = mock.text(OWNER, 'one')
    expect(await waitFor(() => rows(running!.store).length === 1)).toBe(true)
    await running.stop()
    running.store.setMeta(running.adapter.offsetKey, '0')
    running.store.db.close()
    mock.queue.unshift({ update_id: a, message: { message_id: 1, date: 1, chat: { id: OWNER, type: 'private' }, from: mock.user(OWNER), text: 'one' } })
    running = await startAdapter(mock, h)
    expect(await waitFor(() => running!.adapter.offset === a + 1)).toBe(true)
    expect(rows(running.store).length).toBe(1)
    expect(running.store.getMeta('duplicates')).toBe('1')
  })
})

describe('interlocks', () => {
  test('getMe names another bot → refuses to start: exit 5, no getUpdates, no PID file', async () => {
    const mock = new MockBot()
    mock.username = 'some_other_bot'
    const h = makeHome()
    running = await startAdapter(mock, h)
    expect(await running.done).toBe(EXIT.wrongBot)
    expect(running.logs.join('\n')).toMatch(/getMe says @some_other_bot, config says @testadapter_bot: refusing to start/)
    expect(mock.getUpdatesCalls.length).toBe(0)
    expect(existsSync(h.paths.pidFile)).toBe(false)
    running = null
  })

  test('409 Conflict → stops at once (exit 3), never retries', async () => {
    const mock = new MockBot()
    mock.conflict = true
    const h = makeHome()
    running = await startAdapter(mock, h)
    expect(await running.done).toBe(EXIT.conflict)
    expect(mock.getUpdatesCalls.length).toBe(1)
    expect(running.logs.join('\n')).toMatch(/409 Conflict.*not fighting for it/)
    expect(existsSync(h.paths.pidFile)).toBe(false)
    running = null
  })

  test('401 on getMe → exit 4; the token never appears in logs', async () => {
    const mock = new MockBot()
    mock.failures.getMe = [{ status: 401, body: { ok: false, error_code: 401, description: 'Unauthorized' } }]
    const h = makeHome()
    running = await startAdapter(mock, h)
    expect(await running.done).toBe(EXIT.unauthorized)
    expect(running.logs.join('\n')).not.toContain('TEST-not-a-real')
    running = null
  })

  test('switch file present → stays down before any Bot API call (exit 7)', async () => {
    const mock = new MockBot()
    const h = makeHome()
    writeFileSync(h.paths.offFiles[0]!, '')
    running = await startAdapter(mock, h)
    expect(await running.done).toBe(EXIT.off)
    expect(mock.calls.length + mock.getUpdatesCalls.length).toBe(0)
    running = null
  })

  test('switch file appearing mid-run → clean stop (exit 0), PID file removed', async () => {
    const mock = new MockBot()
    const h = makeHome()
    running = await startAdapter(mock, h)
    expect(existsSync(h.paths.pidFile)).toBe(true)
    writeFileSync(h.paths.offFiles[0]!, '')
    expect(await running.done).toBe(EXIT.ok)
    expect(existsSync(h.paths.pidFile)).toBe(false)
    running = null
  })

  test('a live PID file holder → refuses (exit 6); a stale one is replaced', async () => {
    const mock = new MockBot()
    const h = makeHome()
    mkdirSync(h.paths.stateDir, { recursive: true })
    // a live process whose cmdline names tg-adapter
    const holder = Bun.spawn(['bash', '-c', 'exec -a tg-adapter-holder sleep 30'])
    await Bun.sleep(100)
    writeFileSync(h.paths.pidFile, `${holder.pid}\ntg-adapter\n`)
    running = await startAdapter(mock, h)
    expect(await running.done).toBe(EXIT.running)
    expect(mock.getUpdatesCalls.length).toBe(0)
    holder.kill()
    await holder.exited
    running.store.db.close()
    // now stale: replaced
    running = await startAdapter(mock, h)
    expect(running.logs.some(l => l.startsWith('polling as'))).toBe(true)
  })
})
