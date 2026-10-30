// telegram-plus is a bridge only: static checks that no poll path is left, that the bridge reaches no token and
// no network, and that the manifests agree. Behaviour: test/bridge.test.ts and test/offline.ts.
import { describe, expect, test } from 'bun:test'
import { readdirSync, readFileSync } from 'fs'
import { join } from 'path'

const DIR = join(import.meta.dir, '..')
const read = (f: string) => readFileSync(join(DIR, f), 'utf8')
/** Source without comments, so the checks look at code only. */
const code = (f: string) => read(f).replace(/\/\*[\s\S]*?\*\//g, '').split('\n').filter(l => !/^\s*\/\//.test(l)).join('\n')

/** server.ts and every local module it imports, transitively. */
function modules(): string[] {
  const seen = new Set<string>()
  const walk = (f: string) => {
    if (seen.has(f)) return
    seen.add(f)
    for (const m of code(f).matchAll(/(?:from |import )'\.\/([\w-]+\.ts)'/g)) walk(m[1]!)
  }
  walk('server.ts')
  return [...seen].sort()
}

describe('entry point', () => {
  test('server.ts only imports the bridge: no TELEGRAM_MODE switch, nothing else runs', () => {
    expect(code('server.ts').trim().split('\n').filter(l => l.trim() !== '' && !l.startsWith('#!'))).toEqual(["import './bridge.ts'"])
  })

  test('every plugin module is the bridge\'s: none is left over from the poll path', () => {
    expect(modules()).toEqual(['bridge.ts', 'bridgedb.ts', 'bridgehub.ts', 'bridgelease.ts', 'scan.ts', 'server.ts', 'tools.ts', 'voiceinbox.ts'])
    const top = readdirSync(DIR).filter(f => f.endsWith('.ts')).sort()
    expect(top).toEqual(modules())
  })

  test('nothing reads TELEGRAM_MODE or any poll-path setting', () => {
    for (const f of modules()) {
      expect(`${f}: ${/TELEGRAM_(MODE|NO_POLL|API_ROOT|STATE_DIR|ACCESS_MODE|INSTANT_FILE|CALLBACKS_FILE|REACTIONS_FILE|MCP_LOG_DIR)\b/.test(code(f))}`).toBe(`${f}: false`)
    }
  })

  test('no poll-path state files: bot.pid, health/journal/replay/alert files, instant tables, reaction shortcuts', () => {
    // tools.ts is left out: it holds 0.1.22's texts verbatim (TOOLS_0122, the instructions); what it lists is checked below
    for (const f of modules().filter(f => f !== 'tools.ts')) {
      const s = code(f)
      expect(`${f}: ${/bot\.pid|health\.json|inbound-journal|replay-request|delivery-alert|instant\.json|callbacks\.json|reactions\.json|buttons\.json|commands\.json|access\.json/.test(s)}`).toBe(`${f}: false`)
    }
  })

  test('the listed tools name no instant tables, shortcuts, pairing or built-in commands', async () => {
    const { bridgeTools } = await import('../tools.ts')
    const listed = JSON.stringify(bridgeTools())
    expect(listed).not.toMatch(/instant|shortcut|pairing|built in|\.json/i)
  })
})

describe('the bridge reads no token and opens no network connection', () => {
  test('no grammY, no Bot API, no outbound sockets (Bun.serve\'s fetch handler is inbound); Bun.serve only on 127.0.0.1', () => {
    for (const f of modules()) {
      const s = read(f)
      // bridgehub.ts (hub mode) is the one network client: checked below
      const out = f === 'bridgehub.ts' ? /from ['"]grammy|api\.telegram\.org|from ['"](node:)?(net|http|https|tls|dgram)['"]|Bun\.connect|new WebSocket/
        : /from ['"]grammy|api\.telegram\.org|(?<!async )\bfetch\(|from ['"](node:)?(net|http|https|tls|dgram)['"]|Bun\.connect|new WebSocket/
      expect(`${f}: ${out.test(s)}`).toBe(`${f}: false`)
      if (/Bun\.serve\(/.test(s)) expect(`${f}: ${/hostname: '127\.0\.0\.1'/.test(s)}`).toBe(`${f}: true`)
    }
  })

  test('hub mode: bridgehub.ts has one fetch, to the hub URL, with the hub token; nothing else reaches out', () => {
    const s = read('bridgehub.ts')
    const calls = [...s.matchAll(/(?<!async )\bfetch\(([^,]+),/g)].map(m => m[1]!.trim())
    expect(calls).toEqual(['this.url + path'])
    expect(s).toContain("export const HUB_DEFAULT_URL = 'http://claude-hub:8700'")
    expect(s).toMatch(/authorization: `Bearer \$\{this\.bearer\(\)\}`/)
    // the hub token is never logged: no log line names it
    expect(s.split('\n').filter(l => /log\(/.test(l) && /token/i.test(l) && !/^\s*(\/\/|\*)/.test(l))).toEqual([])
    // bridge.ts reaches the hub only through the store (no URL of its own)
    expect(read('bridge.ts')).not.toMatch(/https?:\/\//)
  })

  test('the token variable is only ever deleted, and no env file is loaded', () => {
    for (const f of modules()) {
      const lines = read(f).split('\n').filter(l => l.includes('TELEGRAM_BOT_TOKEN') && !/^\s*(\/\/|\*)/.test(l))
      for (const l of lines) expect(l.trim()).toBe('delete process.env.TELEGRAM_BOT_TOKEN')
      expect(`${f}: ${/'\.env'|"\.env"|ENV_FILE/.test(read(f))}`).toBe(`${f}: false`)
    }
  })
})

describe('manifests', () => {
  const pkg = JSON.parse(read('package.json'))
  const plugin = JSON.parse(read('.claude-plugin/plugin.json'))
  test('package.json and plugin.json carry the same version', () => {
    expect(plugin.version).toBe(pkg.version)
    expect(plugin.name).toBe('telegram-plus')
  })

  test('the MCP server is still "telegram", started by package.json\'s start script on server.ts', () => {
    const mcp = JSON.parse(read('.mcp.json'))
    expect(Object.keys(mcp.mcpServers)).toEqual(['telegram'])
    expect(mcp.mcpServers.telegram.args).toContain('start')
    expect(pkg.scripts.start).toMatch(/ bun server\.ts$/)
  })

  test('no grammY dependency: the bridge never talks to Telegram', () => {
    expect(Object.keys(pkg.dependencies ?? {})).not.toContain('grammy')
    expect(read('bun.lock').includes('"grammy"')).toBe(false)
  })
})
