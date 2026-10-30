// Shared test setup: a temp HOME with config + token file (fake token, 0600), the adapter in
// process against the MockBot (injected fetch: no sockets at all), and small wait helpers.
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Adapter } from '../src/adapter.ts'
import { loadConfig, readToken } from '../src/config.ts'
import { openDb, Store } from '../src/db.ts'
import { resolvePaths, type Paths } from '../src/paths.ts'
import { MockBot, OWNER, TOKEN } from './mockbot.ts'

export const roots: string[] = []
export function cleanup(): void {
  for (const r of roots.splice(0)) rmSync(r, { recursive: true, force: true })
}

export type Home = { home: string; env: Record<string, string>; paths: Paths; tokenFile: string }

/** A temp HOME with ~/.config/tg-adapter/test.json and a 0600 token file. */
export function makeHome(config: Record<string, unknown> = {}, opts: { socket?: string } = {}): Home {
  const home = mkdtempSync(join(tmpdir(), 'tga-home-'))
  roots.push(home)
  const env = { HOME: home, TG_ADAPTER_TEST: '1' }
  const paths = resolvePaths(env)
  mkdirSync(join(home, '.config', 'tg-adapter'), { recursive: true })
  mkdirSync(join(home, '.secrets'), { recursive: true, mode: 0o700 })
  const tokenFile = join(home, '.secrets', 'tg-test-bot.env')
  writeFileSync(tokenFile, `TELEGRAM_BOT_TOKEN=${TOKEN}\n`, { mode: 0o600 }) // secretscan:allow (fake token)
  chmodSync(tokenFile, 0o600)
  writeConfig({ home, env, paths, tokenFile }, config, opts)
  return { home, env, paths, tokenFile }
}

export function writeConfig(h: Home, config: Record<string, unknown> = {}, opts: { socket?: string } = {}): void {
  writeFileSync(h.paths.config, JSON.stringify({
    mode: 'test',
    bot: 'testadapter_bot',
    token_file: '~/.secrets/tg-test-bot.env',
    allow_from: [String(OWNER)],
    api_root: 'http://mock.invalid',
    send: true,
    poll_timeout_s: 1,
    outbox_interval_ms: 25,
    quiet_hours: '', // wall-clock independent by default; rules.test.ts sets the window and the clock
    ...(opts.socket ? { api_unix_socket: opts.socket } : {}),
    ...config,
  }))
}

export type Running = {
  adapter: Adapter
  store: Store
  logs: string[]
  done: Promise<number>
  stop: () => Promise<number>
}

/** Start the adapter in process against the mock; resolves once it polls (or has exited). */
export async function startAdapter(mock: MockBot, h: Home, opts: { dbPath?: string; wait?: boolean; env?: Record<string, string>; rules?: Record<string, unknown>; now?: () => number } = {}): Promise<Running> {
  const cfg = loadConfig(h.paths.config, h.home, { testRun: true })
  const token = readToken(cfg, h.env, h.home)
  const store = new Store(openDb(opts.dbPath ?? h.paths.defaultDb))
  const logs: string[] = []
  const adapter = new Adapter({ cfg, paths: h.paths, token, store, apiOptions: { fetch: mock.fetch }, log: m => logs.push(m),
    env: { ...h.env, ...(opts.env ?? {}) }, ...(opts.rules ? { rules: opts.rules as any } : {}), ...(opts.now ? { now: opts.now } : {}) })
  const done = adapter.run()
  if (opts.wait !== false) await waitFor(() => logs.some(l => l.startsWith('polling as')) || settled(done), 3000)
  return {
    adapter, store, logs, done,
    stop: async () => { adapter.stop(); const c = await done; return c },
  }
}

const settledSet = new WeakSet<Promise<unknown>>()
function settled(p: Promise<unknown>): boolean {
  if (!settledSet.has(p)) { settledSet.add(p); void p.then(() => { (p as any).__done = true }, () => { (p as any).__done = true }) }
  return (p as any).__done === true
}

export async function waitFor(pred: () => boolean, ms = 3000): Promise<boolean> {
  const t = Date.now()
  while (!pred() && Date.now() - t < ms) await Bun.sleep(10)
  return pred()
}

export function rows(store: Store, kind?: string): any[] {
  const r = (kind
    ? store.db.query('SELECT * FROM events WHERE kind = ? ORDER BY id').all(kind)
    : store.db.query('SELECT * FROM events ORDER BY id').all()) as any[]
  return r.map(x => ({ ...x, payload: JSON.parse(x.payload), labels: JSON.parse(x.labels) }))
}

export function outbox(store: Store, id: number): any {
  return store.db.query('SELECT * FROM outbox WHERE id = ?').get(id)
}
