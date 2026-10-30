// Tools registry v0 (Phase 1 P3): schemas/tools.toml, read live (re-parsed when its mtime or size changes).
// A tool: name, argv (no shell; a leading ~/ per element = $HOME), effect, timeout, output text|picture, note,
// text_arg, description, menu (P9: the /commands line of a command bound to it), toast (a bound tap's toast). The
// effects gate (tapAllowed): a tap or command never runs a tool above send_owner.
// The same rules in Python: packages/tgadapter/bind.py (tg-bind).
import { readFileSync, statSync } from 'fs'
import { join } from 'path'

export const EFFECTS = ['read', 'home', 'send_owner', 'send_external', 'spend', 'irreversible'] as const
export type Effect = (typeof EFFECTS)[number]
/** The highest effect a tap or command may run without an approval (P7 adds approvals). */
export const TAP_MAX_EFFECT: Effect = 'send_owner'
export const TOOL_TIMEOUT_MS = 20_000
export const TOOL_TIMEOUT_MAX_MS = 300_000

export type Tool = {
  name: string
  argv: string[]
  effect: Effect
  timeoutMs: number
  output: 'text' | 'picture'
  note: boolean
  textArg: boolean
  description: string
  /** The /commands menu line of a command bound to this tool ('' = not in the menu). */
  menu: string
  /** A bound tap's toast ('' = "✓ <button text>"). */
  toast: string
}

export const effectRank = (e: string) => (EFFECTS as readonly string[]).indexOf(e)
export const tapAllowed = (e: Effect) => effectRank(e) <= effectRank(TAP_MAX_EFFECT)

/** The repo's registry next to this checkout (adapters/tg/src → schemas/tools.toml). */
export const defaultRegistryFile = () => join(import.meta.dir, '..', '..', '..', 'schemas', 'tools.toml')

const NAME = /^[a-z0-9][a-z0-9_.-]{0,63}$/

/** The parsed TOML → valid tools plus the reasons for skipped ones. */
export function parseRegistry(raw: unknown, home: string): { tools: Map<string, Tool>; skipped: string[] } {
  const tools = new Map<string, Tool>()
  const skipped: string[] = []
  const table = (raw as { tools?: unknown } | null)?.tools
  if (!table || typeof table !== 'object' || Array.isArray(table)) return { tools, skipped: ['no [tools] table'] }
  for (const [name, t] of Object.entries(table as Record<string, any>)) {
    const argv = Array.isArray(t?.argv) && t.argv.length && t.argv.every((a: unknown) => typeof a === 'string' && a !== '')
      ? (t.argv as string[]).map(a => a.replace(/^~(?=\/)/, home)) : []
    const timeout = t?.timeout_s == null ? TOOL_TIMEOUT_MS / 1000 : Number(t.timeout_s)
    const why = !NAME.test(name) ? `invalid name "${name.slice(0, 40)}"`
      : !argv.length ? `${name}: argv must be a non-empty list of strings`
      : effectRank(String(t?.effect)) < 0 ? `${name}: effect must be one of ${EFFECTS.join('|')}`
      : !Number.isFinite(timeout) || timeout <= 0 ? `${name}: timeout_s must be a positive number`
      : t?.output != null && t.output !== 'text' && t.output !== 'picture' ? `${name}: output must be text or picture`
      : t?.note != null && typeof t.note !== 'boolean' ? `${name}: note must be true or false`
      : t?.text_arg != null && typeof t.text_arg !== 'boolean' ? `${name}: text_arg must be true or false`
      : t?.menu != null && (typeof t.menu !== 'string' || t.menu.length > 256) ? `${name}: menu must be a string of at most 256 chars`
      : t?.toast != null && typeof t.toast !== 'string' ? `${name}: toast must be a string`
      : null
    if (why) { skipped.push(why); continue }
    tools.set(name, {
      name,
      argv,
      effect: t.effect as Effect,
      timeoutMs: Math.min(timeout * 1000, TOOL_TIMEOUT_MAX_MS),
      output: t.output === 'picture' ? 'picture' : 'text',
      note: t.note === true,
      textArg: t.text_arg === true,
      description: typeof t.description === 'string' ? t.description : '',
      menu: typeof t.menu === 'string' ? t.menu.trim() : '',
      toast: typeof t.toast === 'string' ? t.toast : '',
    })
  }
  return { tools, skipped }
}

const sameArgv = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i])

export class Registry {
  private cache: { key: string; tools: Map<string, Tool>; error: string | null } | null = null

  constructor(readonly file: string, readonly home: string, private readonly log: (m: string) => void = () => {}) {}

  /** The tools now (an unreadable or invalid file = none, logged once per change). */
  tools(): Map<string, Tool> {
    let key: string
    try {
      const st = statSync(this.file)
      key = `${st.mtimeMs}:${st.size}`
    } catch {
      if (this.cache?.key !== 'missing') this.log(`tools registry missing: ${this.file} (nothing is bound)`)
      this.cache = { key: 'missing', tools: new Map(), error: 'missing' }
      return this.cache.tools
    }
    if (this.cache?.key === key) return this.cache.tools
    let tools = new Map<string, Tool>()
    let error: string | null = null
    try {
      const r = parseRegistry(Bun.TOML.parse(readFileSync(this.file, 'utf8')), this.home)
      tools = r.tools
      if (r.skipped.length) this.log(`tools registry: skipped ${r.skipped.join('; ')}`)
    } catch (err) {
      error = String((err as Error)?.message ?? err)
      this.log(`tools registry unreadable (${error}): nothing is bound`)
    }
    this.cache = { key, tools, error }
    return tools
  }

  get(name: string): Tool | null {
    return this.tools().get(name) ?? null
  }

  /** The registered tool with exactly this argv (the compat tables' entries are matched this way). */
  byArgv(argv: string[]): Tool | null {
    for (const t of this.tools().values()) if (sameArgv(t.argv, argv)) return t
    return null
  }
}
