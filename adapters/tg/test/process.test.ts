// The CLI as a real process against the mock on a unix socket (no TCP port anywhere): SIGKILL
// mid-stream and restart (no loss, no duplicates, stale PID file replaced), no listening sockets,
// SIGTERM, wrong username, missing config, and the check / status / enqueue commands.
import { afterAll, afterEach, describe, expect, test } from 'bun:test'
import { Database } from 'bun:sqlite'
import { existsSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { EXIT } from '../src/adapter.ts'
import { cleanup, makeHome, waitFor, writeConfig, type Home } from './helpers.ts'
import { MockBot, OWNER } from './mockbot.ts'

const CLI = join(import.meta.dir, '..', 'src', 'cli.ts')
const sockDir = mkdtempSync(join(tmpdir(), 'tga-sock-'))
afterAll(() => { cleanup(); rmSync(sockDir, { recursive: true, force: true }) })

type Proc = { p: ReturnType<typeof Bun.spawn>; err: () => string }
const procs: Proc[] = []
afterEach(() => { for (const x of procs.splice(0)) { try { x.p.kill('SIGKILL') } catch {} } })

let n = 0
function setup(config: Record<string, unknown> = {}): { mock: MockBot; h: Home } {
  const mock = new MockBot()
  const sock = join(sockDir, `m${++n}.sock`)
  mock.serveUnix(sock)
  const h = makeHome(config, { socket: sock })
  return { mock, h }
}

/** A minimal env: never the parent's (no Telegram variables leak in). */
const envOf = (h: Home) => ({ HOME: h.home, TG_ADAPTER_TEST: '1', PATH: process.env.PATH ?? '/usr/bin:/bin' })

function spawn(h: Home, args: string[]): Proc {
  const p = Bun.spawn([process.execPath, CLI, ...args], { env: envOf(h), stdout: 'pipe', stderr: 'pipe' })
  let err = ''
  void (async () => { for await (const c of p.stderr as any) err += new TextDecoder().decode(c) })()
  const x = { p, err: () => err }
  procs.push(x)
  return x
}

async function runCli(h: Home, args: string[]): Promise<{ code: number; out: string; err: string }> {
  const p = Bun.spawn([process.execPath, CLI, ...args], { env: envOf(h), stdout: 'pipe', stderr: 'pipe' })
  const [out, err] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text()])
  return { code: await p.exited, out, err }
}

function dbRows(h: Home): any[] {
  const path = h.paths.defaultDb
  if (!existsSync(path)) return []
  const db = new Database(path, { readonly: true })
  try { return db.query('SELECT idem_key, payload FROM events ORDER BY id').all() as any[] } catch { return [] } finally { db.close() }
}

/** Inodes of TCP/UDP sockets in LISTEN state that belong to pid. */
function listeningInodes(pid: number): string[] {
  const mine = new Set<string>()
  for (const fd of readdirSync(`/proc/${pid}/fd`)) {
    try { const m = /^socket:\[(\d+)\]$/.exec(readlinkSync(`/proc/${pid}/fd/${fd}`)); if (m) mine.add(m[1]!) } catch {}
  }
  const hits: string[] = []
  for (const f of ['tcp', 'tcp6']) {
    let text = ''
    try { text = readFileSync(`/proc/${pid}/net/${f}`, 'utf8') } catch { continue }
    for (const line of text.split('\n').slice(1)) {
      const c = line.trim().split(/\s+/)
      if (c.length > 9 && c[3] === '0A' && mine.has(c[9]!)) hits.push(`${f}:${c[1]}`)
    }
  }
  return hits
}

describe('the CLI as a process (unix-socket mock)', () => {
  test('SIGKILL mid-stream, restart: every update exactly once; stale PID file replaced', async () => {
    const { mock, h } = setup()
    mock.maxBatch = 2
    mock.delays.answerCallbackQuery = 25
    const a = spawn(h, ['run'])
    expect(await waitFor(() => /polling as @testadapter_bot/.test(a.err()), 8000)).toBe(true)
    const N = 40
    for (let i = 0; i < N; i++) i % 4 === 0 ? mock.press(OWNER, `d${i}`, `L${i}`) : mock.text(OWNER, `m${i}`)
    expect(await waitFor(() => dbRows(h).length >= 10, 8000)).toBe(true)
    a.p.kill('SIGKILL')
    await a.p.exited
    const before = dbRows(h).length
    expect(before).toBeLessThan(N)
    expect(existsSync(h.paths.pidFile)).toBe(true) // SIGKILL: no cleanup
    const b = spawn(h, ['run'])
    expect(await waitFor(() => dbRows(h).length === N, 10000)).toBe(true)
    await Bun.sleep(200)
    const rs = dbRows(h)
    expect(rs.length).toBe(N)
    expect(new Set(rs.map(r => r.idem_key)).size).toBe(N)
    expect(rs.map(r => JSON.parse(r.payload).update_id)).toEqual(Array.from({ length: N }, (_, i) => i + 1))
    expect(parseInt(readFileSync(h.paths.pidFile, 'utf8'), 10)).toBe(b.p.pid)
    b.p.kill('SIGTERM')
    expect(await b.p.exited).toBe(0)
    expect(existsSync(h.paths.pidFile)).toBe(false)
    mock.stop()
  }, 30000)

  test('binds no ports; the DB is 0600', async () => {
    const { mock, h } = setup()
    const a = spawn(h, ['run'])
    expect(await waitFor(() => /polling as/.test(a.err()), 8000)).toBe(true)
    expect(listeningInodes(a.p.pid)).toEqual([])
    const st = require('fs').statSync(h.paths.defaultDb)
    expect(st.mode & 0o777).toBe(0o600)
    a.p.kill('SIGTERM')
    expect(await a.p.exited).toBe(0)
    mock.stop()
  }, 15000)

  test('wrong username: exit 5 before polling', async () => {
    const { mock, h } = setup()
    mock.username = 'not_the_test_bot'
    const r = await runCli(h, ['run'])
    expect(r.code).toBe(EXIT.wrongBot)
    expect(r.err).toMatch(/refusing to start/)
    expect(mock.getUpdatesCalls.length).toBe(0)
    mock.stop()
  }, 15000)

  test('config required: exit 2', async () => {
    const { mock, h } = setup()
    rmSync(h.paths.config)
    const r = await runCli(h, ['run'])
    expect(r.code).toBe(EXIT.config)
    expect(r.err).toMatch(/config file required/)
    expect(mock.calls.length).toBe(0)
    mock.stop()
  }, 15000)

  test('check, enqueue, status', async () => {
    const { mock, h } = setup({ send: false })
    const c = await runCli(h, ['check'])
    expect(c.code).toBe(0)
    expect(c.out).toMatch(/config ok: @testadapter_bot, instance test, mode test/)
    expect(c.out + c.err).not.toContain('TEST-not-a-real')
    const e = await runCli(h, ['enqueue', 'send', '--chat', String(OWNER), '--body', '{"text":"hi"}', '--idem', 'k1'])
    expect(e.out.trim()).toBe('queued #1')
    const e2 = await runCli(h, ['enqueue', 'send', '--chat', String(OWNER), '--body', '{"text":"hi"}', '--idem', 'k1'])
    expect(e2.out.trim()).toBe('duplicate (idem key already queued)')
    const bad = await runCli(h, ['enqueue', 'nope', '--chat', '1', '--body', '{}'])
    expect(bad.code).toBe(EXIT.config)
    const s = await runCli(h, ['status', '--json'])
    expect(JSON.parse(s.out)).toMatchObject({ running: false, outbox: { queued: 1 }, events: {} })
    writeConfig(h, { mode: 'live' })
    const live = await runCli(h, ['check'])
    expect(live.code).toBe(EXIT.config)
    expect(live.err).toMatch(/must match/)
    const noLive = await runCli(h, ['--instance', 'live', 'check'])
    expect(noLive.code).toBe(EXIT.config)
    expect(noLive.err).toMatch(/config file required: .*live\.json/)
    expect((await runCli(h, ['--instance', 'prod', 'check'])).code).toBe(EXIT.config)
    mock.stop()
  }, 20000)
})
