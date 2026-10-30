// Instant commands (telegram-plus 0.1.8-0.1.10, 0.1.18 pictures): the 0.1.22 table format and command match, as
// pure helpers. Adapted from server.ts (commit 974bed5) lines 1430-1433 (splitArgv), 1452-1567 (the table, its
// parse), 1759-1786 (the /status entry, instantFor). The adapter reads the plugin's instant.json through its compat
// path (config.ts) until P9 moves every entry to a tg-bind row.
//
// instant.json is a JSON array:
//   { "command": "bills", "description": "Open bills", "run": "~/bin/tg-cmd bills",
//     "timeout_ms": 20000, "on_error": "reply" | "forward", "note": true, "args": false }
// run: a command line (split on whitespace, leading ~/ expanded) or an argv array. note defaults to true, on_error
// to "reply", args to false. Invalid entries and the built-in names start/help/status are skipped (reasons returned).
// The owner's exact "/name" (or "/name@<this bot>") in a DM matches; "/name <text>" only for an entry with args true
// (text trimmed, at most INSTANT_ARG_MAX chars, passed as ONE extra argv element).

export const INSTANT_TIMEOUT_MS = 20_000
export const INSTANT_TIMEOUT_MAX_MS = 300_000
export const INSTANT_ARG_MAX = 512
export const INSTANT_NOTE_MAX = 1500
export const STATUS_TIMEOUT_MS = 10_000
export const BUILTIN_COMMANDS = ['start', 'help', 'status']
export const STATUS_MENU_ENTRY = { command: 'status', description: "What I'm working on" }

export type Instant = {
  command: string
  description: string
  argv: string[]
  timeoutMs: number
  onError: 'reply' | 'forward'
  note: boolean
  args: boolean
}

/** A command line split on whitespace (no shell, no quoting); a leading ~/ is expanded. */
export function splitArgv(v: string, home: string): string[] {
  return v.trim().split(/\s+/).filter(Boolean).map(a => a.replace(/^~(?=\/)/, home))
}

const cut = (s: string, n: number) => s.length <= n ? s : s.slice(0, n - 1) + '…'

/** instant.json's content → valid entries plus the reasons for skipped ones. */
export function parseInstant(raw: unknown, home: string): { list: Instant[]; skipped: string[] } {
  const list: Instant[] = []
  const skipped: string[] = []
  if (!Array.isArray(raw)) return { list, skipped: ['the whole file (not a JSON array)'] }
  const builtin = new Set(BUILTIN_COMMANDS)
  for (const [i, e] of raw.entries()) {
    const name = String(e?.command ?? '').replace(/^\//, '').toLowerCase()
    const description = String(e?.description ?? '').trim()
    const argv = typeof e?.run === 'string' ? splitArgv(e.run, home)
      : Array.isArray(e?.run) && e.run.length && e.run.every((a: unknown) => typeof a === 'string') ? (e.run as string[]).map(a => a.replace(/^~(?=\/)/, home))
      : []
    const why = !/^[a-z0-9_]{1,32}$/.test(name) ? `invalid command name "${cut(name, 40)}"`
      : builtin.has(name) ? `/${name} is built in`
      : list.some(c => c.command === name) ? `/${name} twice`
      : description.length < 1 || description.length > 256 ? `/${name}: description must be 1-256 chars`
      : !argv.length ? `/${name}: no "run"`
      : e.on_error !== undefined && e.on_error !== 'reply' && e.on_error !== 'forward' ? `/${name}: on_error must be "reply" or "forward"`
      : e.args !== undefined && typeof e.args !== 'boolean' ? `/${name}: args must be true or false`
      : null
    if (why) { skipped.push(`#${i + 1} (${why})`); continue }
    const t = Number(e.timeout_ms)
    list.push({
      command: name,
      description,
      argv,
      timeoutMs: Number.isFinite(t) && t > 0 ? Math.min(t, INSTANT_TIMEOUT_MAX_MS) : INSTANT_TIMEOUT_MS,
      onError: e.on_error === 'forward' ? 'forward' : 'reply',
      note: e.note !== false,
      args: e.args === true,
    })
  }
  return { list, skipped }
}

/** The built-in /status entry for a status command argv (0.1.6: on_error forward, note true, no args). */
export function statusEntry(argv: string[]): Instant {
  return { ...STATUS_MENU_ENTRY, argv, timeoutMs: STATUS_TIMEOUT_MS, onError: 'forward', note: true, args: false }
}

/** "/name", "/name@bot" or "/name <text>": the command name (lowercase), the bot it names (or ''), the text. */
export function parseCommand(text: string): { name: string; at: string; arg: string } | null {
  const m = /^\/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]+))?(?:\s+([\s\S]*))?$/.exec(text.trim())
  if (!m) return null
  return { name: m[1]!.toLowerCase(), at: m[2] ?? '', arg: (m[3] ?? '').trim() }
}
