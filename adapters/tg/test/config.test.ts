// Config and token interlocks, the test-run path guard, the schema contract, and static guards
// over the source (no listeners, no env token, no reference to the live plugin's state dir).
import { afterAll, describe, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, symlinkSync, writeFileSync } from 'fs'
import { join } from 'path'
import { parseConfig, readToken, ConfigError, loadConfig } from '../src/config.ts'
import { openDb, SCHEMA_SQL } from '../src/db.ts'
import { realHome, resolvePaths } from '../src/paths.ts'
import { cleanup, makeHome } from './helpers.ts'
import { TOKEN } from './mockbot.ts'

afterAll(cleanup)

const base = { mode: 'test', bot: 'testadapter_bot', token_file: '/x/t.env', allow_from: ['111'] }
const bad = (patch: Record<string, unknown>, re: RegExp) => {
  expect(() => parseConfig({ ...base, ...patch }, '/h')).toThrow(re)
}

describe('config', () => {
  test('a minimal config parses with safe defaults (receive-only)', () => {
    const c = parseConfig(base, '/h')
    expect(c).toMatchObject({ mode: 'test', bot: 'testadapter_bot', send: false, pollTimeoutS: 30, apiRoot: 'https://api.telegram.org', allowFrom: ['111'] })
    expect(parseConfig({ ...base, bot: '@Testadapter_Bot', token_file: '~/s/t.env' }, '/h')).toMatchObject({ bot: 'Testadapter_Bot', tokenFile: '/h/s/t.env' })
  })

  test('interlocks: mode test only, instant commands and side-effect callbacks off, one allow-from source', () => {
    bad({ mode: 'prod' }, /mode must be "test" or "live"/)
    bad({ mode: undefined }, /mode must be "test" or "live"/)
    // live: the instance must match, the main events DB, the real Bot API (outside test runs)
    expect(() => parseConfig({ ...base, mode: 'live' }, '/h', { instance: 'test' })).toThrow(/must match/)
    expect(() => parseConfig({ ...base, mode: 'test' }, '/h', { instance: 'live' })).toThrow(/must match/)
    expect(() => parseConfig({ ...base, mode: 'live', db: '/h/other.db' }, '/h', { instance: 'live', eventsDb: '/h/events.db' })).toThrow(/main events DB/)
    expect(() => parseConfig({ ...base, mode: 'live', api_root: 'http://mock.invalid' }, '/h', { instance: 'live' })).toThrow(/real Bot API/)
    expect(parseConfig({ ...base, mode: 'live', api_root: 'http://mock.invalid' }, '/h', { instance: 'live', testRun: true })).toMatchObject({ mode: 'live', scanPrivate: true })
    expect(parseConfig({ ...base }, '/h')).toMatchObject({ mode: 'test', scanPrivate: false, transcribe: true, typing: true,
      statusState: '/h/.local/state/tg-status/state.json' })
    expect(parseConfig({ ...base, compat_journal: '~/j.jsonl', compat_buttons_file: '~/b.json' }, '/h')).toMatchObject({ compatJournal: '/h/j.jsonl', compatButtonsFile: '/h/b.json' })
    bad({ transcribe: 'yes' }, /transcribe must be true or false/)
    bad({ side_effect_callbacks: 1 }, /side_effect_callbacks must be true or false/)
    bad({ bindings: 'on' }, /bindings must be true or false/)
    // compat tables: beside the compat journal unless given; "" or the flag false = none.
    expect(parseConfig({ ...base, compat_journal: '/s/j.jsonl' }, '/h')).toMatchObject({ bindings: true, compatInstant: '/s/instant.json', compatCallbacks: '/s/callbacks.json' })
    expect(parseConfig({ ...base, compat_journal: '/s/j.jsonl', instant_commands: false, compat_callbacks_file: '' }, '/h')).not.toHaveProperty('compatInstant')
    expect(parseConfig({ ...base, compat_journal: '/s/j.jsonl', side_effect_callbacks: false }, '/h')).not.toHaveProperty('compatCallbacks')
    expect(parseConfig({ ...base, compat_instant_file: '~/i.json', tools_registry: '~/t.toml', bindings: false }, '/h'))
      .toMatchObject({ compatInstant: '/h/i.json', toolsRegistry: '/h/t.toml', bindings: false })
    expect(parseConfig({ ...base }, '/h')).not.toHaveProperty('compatInstant')
    bad({ allow_from: undefined }, /exactly one allow-from source/)
    bad({ allow_from_file: '/a.json' }, /exactly one allow-from source/)
    bad({ allow_from: [] }, /non-empty list/)
    bad({ allow_from: ['abc'] }, /numeric user ids/)
    bad({ bot: 'nobotname' }, /ends in "bot"/)
    bad({ bot: '' }, /username/)
    bad({ token_file: '' }, /token_file is required/)
    bad({ api_root: 'ftp://x' }, /http\(s\) URL/)
    bad({ poll_timeout_s: 99 }, /poll_timeout_s must be 0-50/)
    bad({ surprise: 1 }, /unknown config keys: surprise/)
    expect(() => parseConfig([], '/h')).toThrow(ConfigError)
  })

  test('a missing or broken config file is refused', () => {
    const h = makeHome()
    writeFileSync(h.paths.config, '{nope')
    expect(() => loadConfig(h.paths.config, h.home)).toThrow(/not valid JSON/)
    expect(() => loadConfig(join(h.home, 'missing.json'), h.home)).toThrow(/config file required/)
  })
})

describe('token file', () => {
  test('read from the file only (never the env); the token never appears in errors', () => {
    const h = makeHome()
    const cfg = loadConfig(h.paths.config, h.home)
    expect(readToken(cfg, { ...h.env, TELEGRAM_BOT_TOKEN: '1:fromenv' }, h.home)).toBe(TOKEN)
    writeFileSync(h.tokenFile, 'OTHER=1\n', { mode: 0o600 })
    expect(() => readToken(cfg, { ...h.env, TELEGRAM_BOT_TOKEN: '1:fromenv' }, h.home)).toThrow(/no TELEGRAM_BOT_TOKEN line/)
    writeFileSync(h.tokenFile, 'TELEGRAM_BOT_TOKEN=not a token at all\n', { mode: 0o600 })
    try { readToken(cfg, h.env, h.home); throw new Error('no throw') } catch (e) {
      expect(String(e)).toMatch(/malformed/)
      expect(String(e)).not.toContain('not a token')
    }
  })

  test('group/other-readable token file refused', () => {
    const h = makeHome()
    chmodSync(h.tokenFile, 0o644)
    expect(() => readToken(loadConfig(h.paths.config, h.home), h.env, h.home)).toThrow(/chmod 600/)
  })

  test('a token file inside a Claude config dir (where the live plugin keeps its token) is refused', () => {
    const h = makeHome()
    for (const dir of [join(h.home, '.claude', 'x'), join(h.home, 'cc', 'y')]) {
      mkdirSync(dir, { recursive: true })
      const f = join(dir, 'bot.env')
      writeFileSync(f, `TELEGRAM_BOT_TOKEN=${TOKEN}\n`, { mode: 0o600 }) // secretscan:allow (fake token)
      const cfg = parseConfig({ ...base, token_file: f }, h.home)
      expect(() => readToken(cfg, { ...h.env, CLAUDE_CONFIG_DIR: join(h.home, 'cc') }, h.home)).toThrow(/inside the Claude config dir/)
    }
    // the real home's Claude dir too, whatever $HOME says (checked by path, never read)
    const cfg = parseConfig({ ...base, token_file: join(realHome(), '.claude', 'nonexistent-for-test.env') }, h.home)
    expect(() => readToken(cfg, h.env, h.home)).toThrow(/inside the Claude config dir/)
    // a symlink from an innocent path into a Claude dir is refused by its real path
    const target = join(h.home, '.claude', 'x', 'bot.env')
    const link = join(h.home, 'innocent.env')
    symlinkSync(target, link)
    expect(() => readToken(parseConfig({ ...base, token_file: link }, h.home), h.env, h.home)).toThrow(/inside the Claude config dir/)
  })
})

describe('paths', () => {
  test('test runs refuse paths under the real home', () => {
    expect(() => resolvePaths({ HOME: realHome(), TG_ADAPTER_TEST: '1' })).toThrow(/real home/)
    expect(() => resolvePaths({ HOME: realHome(), ASSISTANT_TEST: '1' })).toThrow(/real home/)
    expect(resolvePaths({ HOME: realHome() }).stateDir).toBe(join(realHome(), '.local', 'state', 'tg-adapter', 'test'))
    const p = resolvePaths({ HOME: '/tmp/x', TG_ADAPTER_TEST: '1' })
    expect(p).toMatchObject({ config: '/tmp/x/.config/tg-adapter/test.json', offFiles: ['/tmp/x/.config/tg-adapter/test.off', '/tmp/x/.config/tg-adapter/off'],
      pidFile: '/tmp/x/.local/state/tg-adapter/test/adapter.pid', defaultDb: '/tmp/x/.local/state/tg-adapter/test.db',
      inboxDir: '/tmp/x/.local/state/tg-adapter/test/inbox' })
    const l = resolvePaths({ HOME: '/tmp/x', TG_ADAPTER_TEST: '1' }, 'live')
    expect(l).toMatchObject({ config: '/tmp/x/.config/tg-adapter/live.json', offFiles: ['/tmp/x/.config/tg-adapter/live.off'],
      pidFile: '/tmp/x/.local/state/tg-adapter/live/adapter.pid', defaultDb: '/tmp/x/.local/state/assistant/events.db' })
  })
})

describe('schema contract', () => {
  test('the DB gets the §3 tables, columns and pragmas; files 0600', () => {
    const h = makeHome()
    const path = h.paths.defaultDb
    const db = openDb(path)
    const cols = (t: string) => (db.query(`PRAGMA table_info(${t})`).all() as { name: string }[]).map(c => c.name)
    expect(cols('events')).toEqual(['id', 'ts', 'source', 'kind', 'subject', 'next', 'payload', 'labels', 'delivery', 'trace_id', 'idem_key',
      'note_path', 'status', 'updated_ts', 'first_beat', 'first_listed_ts', 'acked_ts', 'ack_by', 'ack_note'])
    expect(cols('outbox')).toEqual(['id', 'ts', 'bot', 'chat', 'op', 'body', 'idem_key', 'urgent', 'not_before', 'mode', 'status', 'attempts',
      'last_error', 'sent_ts', 'tg_message_id', 'trace_id', 'reply_to_event', 'files', 'result'])
    expect(cols('deliveries')).toEqual(['event_id', 'sent_ts', 'tries', 'receipt_ts', 'how'])
    expect(cols('bindings').length).toBe(11)
    expect((db.query("SELECT v FROM meta WHERE k = 'schema'").get() as any).v).toBe('2')
    expect(cols('beats').length).toBe(7)
    expect(cols('wake_rules').length).toBe(7)
    expect(cols('meta')).toEqual(['k', 'v'])
    expect((db.query('PRAGMA journal_mode').get() as any).journal_mode).toBe('wal')
    expect((db.query('PRAGMA synchronous').get() as any).synchronous).toBe(2) // FULL
    expect((db.query('PRAGMA busy_timeout').get() as any).timeout).toBe(5000)
    expect(statSync(path).mode & 0o777).toBe(0o600)
    expect(statSync(h.paths.stateRoot).mode & 0o777).toBe(0o700)
    db.close()
  })

  test('same DDL as the repo\'s schemas/events.sql (once it exists)', () => {
    const repo = join(import.meta.dir, '..', '..', '..', 'schemas', 'events.sql')
    if (!existsSync(repo)) return
    const norm = (s: string) => s.replace(/--[^\n]*/g, '').replace(/\s+/g, ' ').trim()
    expect(norm(SCHEMA_SQL)).toBe(norm(readFileSync(repo, 'utf8')))
  })

  test('DDL v2 is the repo\'s schemas/events-v2.sql and applies twice without harm', () => {
    const repo = join(import.meta.dir, '..', '..', '..', 'schemas', 'events-v2.sql')
    expect(readFileSync(join(import.meta.dir, '..', 'src', 'schema_v2.sql'), 'utf8')).toBe(readFileSync(repo, 'utf8'))
    const h = makeHome()
    openDb(h.paths.defaultDb).close()
    const db = openDb(h.paths.defaultDb)
    expect((db.query('PRAGMA table_info(outbox)').all() as any[]).filter(c => c.name === 'result').length).toBe(1)
    db.close()
  })
})

describe('static guards over src/', () => {
  const src = join(import.meta.dir, '..', 'src')
  const files = readdirSync(src).filter(f => f.endsWith('.ts')).map(f => [f, readFileSync(join(src, f), 'utf8')] as const)
  const code = (s: string) => s.split('\n').filter(l => !/^\s*(\/\/|\*|\/\*)/.test(l)).join('\n')

  test('no listeners: the adapter binds no ports', () => {
    for (const [f, s] of files) expect([f, /Bun\.serve|createServer|\.listen\(|node:net|from 'net'|from 'http'/.test(code(s))]).toEqual([f, false])
  })

  test('never reads the token from the environment', () => {
    for (const [f, s] of files) expect([f, /process\.env\.TELEGRAM_BOT_TOKEN|env\.TELEGRAM_BOT_TOKEN/.test(code(s))]).toEqual([f, false])
  })

  test('never names the live plugin\'s state dir', () => {
    const live = ['channels', 'telegram'].join('/')
    for (const [f, s] of files) expect([f, s.includes(live)]).toEqual([f, false])
  })

  test('no MCP SDK, no claude-process poll veto; local commands only in quiet.ts (tripclock) and compat.ts (runLocal)', () => {
    for (const [f, s] of files) {
      expect([f, /modelcontextprotocol|registersUsAsChannel|claudeAncestorArgs|pollVeto/.test(code(s))]).toEqual([f, false])
      if (f !== 'quiet.ts' && f !== 'compat.ts') expect([f, /child_process/.test(code(s))]).toEqual([f, false])
    }
  })
})

describe('lifted files', () => {
  test('verbatim copies still match the recorded telegram-plus commit (lifted.json)', () => {
    const root = join(import.meta.dir, '..')
    const m = JSON.parse(readFileSync(join(root, 'lifted.json'), 'utf8'))
    expect(m.commit).toBe('974bed5')
    for (const [f, sha] of Object.entries(m.verbatim as Record<string, string>)) {
      const h = new Bun.CryptoHasher('sha256').update(readFileSync(join(root, f))).digest('hex')
      expect([f, h]).toEqual([f, sha])
    }
  })
})
