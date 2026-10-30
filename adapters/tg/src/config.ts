// tg-adapter's config file and its start interlocks. The file is required; nothing has a default
// that could point the adapter at the live bot:
//   mode        "test" or "live"; it must equal the instance (--instance test|live, paths.ts). live also
//               requires rows in the main events DB (db absent or that path) and the real Bot API
//               (api_root / api_unix_socket only in test runs)
//   bot         the bot's username (no @); getMe must return exactly this name before polling
//   token_file  KEY=VALUE env file holding TELEGRAM_BOT_TOKEN, mode 0600, never inside the Claude
//               config dir (where the live channel plugin keeps its token). The process env's
//               TELEGRAM_BOT_TOKEN is never read.
//   allow_from  inline list of user ids  — or —  allow_from_file: an access.json-shaped file
//               (allowFrom, groups, mentionPatterns, forwardReactions; read live, never written;
//               dmPolicy "pairing" counts as "allowlist": the adapter never pairs anyone)
//   api_root    Bot API base URL (default https://api.telegram.org)
//   send        outbox consumer on (default false: receive-only)
//   poll_timeout_s  getUpdates long-poll timeout (default 30)
//   quiet_hours     outbox rule (rules.ts): non-urgent send/poll rows wait inside this window on the
//                   owner's wall clock ("0-8" default, "22-6" wraps, "" or "off" = no quiet hours)
//   sleepin         outbox rule: hold non-urgent send/poll rows while `sleepin active` says so (default true)
//   dedupe_window_s outbox rule: skip a repeat of the same text to the same chat within this many seconds
//                   (default 600, 0 = off)
//   bindings        P3 bindings runner (default true; needs send): commands and buttons bound to registry tools run
//                   here without a brain turn (bind.ts); ~/.config/tg-adapter/bindings-off sends everything to the brain
//   instant_commands, side_effect_callbacks  the 0.1.22 compat tables (default true): instant.json (+ /status) and
//                   callbacks.json, read live until P9
//   compat_instant_file, compat_callbacks_file  those tables (default: beside compat_journal; "" = none)
//   tools_registry  the tools registry (default: the repo's schemas/tools.toml)
//   transcribe      voice notes, audio and video notes are transcribed before their row shows (default true;
//                   ~/bin/transcribe, TG_ADAPTER_TRANSCRIBE_CMD)
//   scan_private    "/scan private" | "/sp" photos/documents are filed into paperless and never reach the brain
//                   (default: live true, test false; when off such a message still never shows its content)
//   typing          "typing…" while tg-status says the session works on the chat's event (default true)
//   status_state    tg-status's state.json (default ~/.local/state/tg-status/state.json)
//   compat_journal       0.1.22 inbound-journal.jsonl to keep writing until P10 (absent = none)
//   compat_buttons_file  the on_press store to use instead of the instance's buttons.json (0.1.22 readers)
//   hub             H6 hub mode (the adapter in the hub's tg container): {url, token_file, files_dir, schema?}: hubd's
//                   URL, the tg caller's token file (one line, 0600), the shared files dir (in/ = downloads, out/ = brain
//                   uploads) and, for tests and rehearsals only, a t_* schema. The journal is the instance's own
//                   (<state>/journal.db unless db is given); no compat files; bind runs, transcripts and private scans
//                   go to the brain-side companion (README "Hub mode").
// Tests only: api_unix_socket (Bot API over a unix socket, so the mock binds no port).
import { readFileSync, realpathSync, statSync } from 'fs'
import { dirname, join, resolve } from 'path'
import { expandHome, isInside, realHome } from './paths.ts'
import { DEFAULT_DEDUPE_WINDOW_S, DEFAULT_QUIET_HOURS } from './rules.ts'

export type Config = {
  mode: 'test' | 'live'
  bot: string
  tokenFile: string
  allowFrom?: string[]
  allowFromFile?: string
  apiRoot: string
  apiUnixSocket?: string
  send: boolean
  pollTimeoutS: number
  outboxIntervalMs: number
  /** "" = off */
  quietHours: string
  sleepin: boolean
  dedupeWindowS: number
  db?: string
  transcribe: boolean
  scanPrivate: boolean
  typing: boolean
  statusState: string
  compatJournal?: string
  compatButtonsFile?: string
  bindings: boolean
  compatInstant?: string
  compatCallbacks?: string
  toolsRegistry?: string
  /** H6 hub mode. */
  hub?: HubConfig
}

export type HubConfig = { url: string; tokenFile: string; filesDir: string; schema?: string }

export type ParseOpts = { instance?: 'test' | 'live'; eventsDb?: string; testRun?: boolean }

export class ConfigError extends Error {}

const KNOWN = new Set(['mode', 'bot', 'token_file', 'allow_from', 'allow_from_file', 'api_root', 'api_unix_socket', 'send',
  'poll_timeout_s', 'outbox_interval_ms', 'db', 'instant_commands', 'side_effect_callbacks', 'comment',
  'quiet_hours', 'sleepin', 'dedupe_window_s', 'transcribe', 'scan_private', 'typing', 'status_state', 'compat_journal',
  'compat_buttons_file', 'bindings', 'compat_instant_file', 'compat_callbacks_file', 'tools_registry', 'hub'])

export function parseConfig(raw: unknown, home: string, opts: ParseOpts = {}): Config {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new ConfigError('config must be a JSON object')
  const c = raw as Record<string, unknown>
  const unknown = Object.keys(c).filter(k => !KNOWN.has(k))
  if (unknown.length) throw new ConfigError(`unknown config keys: ${unknown.join(', ')}`)
  if (c.mode !== 'test' && c.mode !== 'live') throw new ConfigError('mode must be "test" or "live"')
  const mode = c.mode
  if (opts.instance && mode !== opts.instance) throw new ConfigError(`mode "${mode}" in the ${opts.instance} instance's config: they must match`)
  const bot = typeof c.bot === 'string' ? c.bot.trim().replace(/^@/, '') : ''
  if (!/^[A-Za-z][A-Za-z0-9_]{3,31}$/.test(bot) || !/bot$/i.test(bot)) throw new ConfigError('bot must be the bot\'s username (5-32 chars, ends in "bot")')
  if (typeof c.token_file !== 'string' || !c.token_file.trim()) throw new ConfigError('token_file is required')
  const hasList = c.allow_from != null, hasFile = c.allow_from_file != null
  if (hasList === hasFile) throw new ConfigError('give exactly one allow-from source: allow_from (ids) or allow_from_file')
  let allowFrom: string[] | undefined
  if (hasList) {
    if (!Array.isArray(c.allow_from) || !c.allow_from.length || !c.allow_from.every(x => /^-?\d+$/.test(String(x)))) {
      throw new ConfigError('allow_from must be a non-empty list of numeric user ids')
    }
    allowFrom = c.allow_from.map(x => String(x))
  }
  const num = (k: string, d: number, min: number, max: number) => {
    if (c[k] == null) return d
    const n = Number(c[k])
    if (!Number.isFinite(n) || n < min || n > max) throw new ConfigError(`${k} must be ${min}-${max}`)
    return n
  }
  const apiRoot = (typeof c.api_root === 'string' && c.api_root.trim() ? c.api_root.trim() : 'https://api.telegram.org').replace(/\/+$/, '')
  if (!/^https?:\/\//.test(apiRoot)) throw new ConfigError('api_root must be an http(s) URL')
  const quietHours = parseQuietHours(c.quiet_hours)
  for (const k of ['sleepin', 'transcribe', 'scan_private', 'typing', 'bindings', 'instant_commands', 'side_effect_callbacks']) {
    if (c[k] != null && typeof c[k] !== 'boolean') throw new ConfigError(`${k} must be true or false`)
  }
  const path = (k: string) => {
    if (c[k] == null) return undefined
    if (typeof c[k] !== 'string' || !(c[k] as string).trim()) throw new ConfigError(`${k} must be a path`)
    return resolve(expandHome((c[k] as string).trim(), home))
  }
  const db = path('db')
  // The compat tables: given, else beside the compat journal (the plugin's state dir); "" or the flag false = none.
  const compatTable = (k: string, flag: string, name: string): string | undefined => {
    if (c[flag] === false || c[k] === '') return undefined
    const p = path(k)
    if (p) return p
    const j = path('compat_journal')
    return j ? join(dirname(j), name) : undefined
  }
  let hub: HubConfig | undefined
  if (c.hub != null) {
    const h = c.hub as Record<string, unknown>
    if (!h || typeof h !== 'object' || Array.isArray(h)) throw new ConfigError('hub must be an object: {url, token_file, files_dir}')
    const extra = Object.keys(h).filter(k => !['url', 'token_file', 'files_dir', 'schema'].includes(k))
    if (extra.length) throw new ConfigError(`unknown hub keys: ${extra.join(', ')}`)
    const url = typeof h.url === 'string' ? h.url.trim().replace(/\/+$/, '') : ''
    if (!/^https?:\/\/[^\s/]+/.test(url)) throw new ConfigError('hub.url must be an http(s) URL')
    for (const k of ['token_file', 'files_dir']) if (typeof h[k] !== 'string' || !(h[k] as string).trim()) throw new ConfigError(`hub.${k} is required`)
    const tokenFile = resolve(expandHome((h.token_file as string).trim(), home))
    if (/(^|\/)\.claude(\/|$)/.test(tokenFile)) throw new ConfigError('hub.token_file must not be inside a Claude config dir')
    const filesDir = resolve(expandHome((h.files_dir as string).trim(), home))
    let schema: string | undefined
    if (h.schema != null) {
      if (typeof h.schema !== 'string' || !/^t_[a-z0-9_]{1,40}$/.test(h.schema)) throw new ConfigError('hub.schema must be a t_* test schema')
      if (mode === 'live' && !opts.testRun) throw new ConfigError('hub.schema is for test runs and the test instance only')
      schema = h.schema
    }
    for (const k of ['compat_journal', 'compat_buttons_file', 'compat_instant_file', 'compat_callbacks_file']) {
      if (c[k] != null && c[k] !== '') throw new ConfigError(`${k}: hub mode writes no compat files (the brain's files are not here)`)
    }
    hub = { url, tokenFile, filesDir, ...(schema ? { schema } : {}) }
  }
  const compatInstant = hub ? undefined : compatTable('compat_instant_file', 'instant_commands', 'instant.json')
  const compatCallbacks = hub ? undefined : compatTable('compat_callbacks_file', 'side_effect_callbacks', 'callbacks.json')
  if (mode === 'live') {
    if (!hub && db && opts.eventsDb && resolve(db) !== resolve(opts.eventsDb)) throw new ConfigError('live mode writes the main events DB: db must be absent or that path')
    if (!opts.testRun && (c.api_unix_socket != null || (c.api_root != null && apiRoot !== 'https://api.telegram.org'))) {
      throw new ConfigError('live mode talks to the real Bot API: api_root / api_unix_socket are for tests only')
    }
  }
  return {
    mode,
    bot,
    tokenFile: resolve(expandHome(c.token_file.trim(), home)),
    ...(allowFrom ? { allowFrom } : { allowFromFile: resolve(expandHome(String(c.allow_from_file), home)) }),
    apiRoot,
    ...(typeof c.api_unix_socket === 'string' && c.api_unix_socket ? { apiUnixSocket: resolve(expandHome(c.api_unix_socket, home)) } : {}),
    send: c.send === true,
    pollTimeoutS: num('poll_timeout_s', 30, 0, 50),
    outboxIntervalMs: num('outbox_interval_ms', 500, 20, 60_000),
    quietHours,
    sleepin: c.sleepin !== false,
    dedupeWindowS: num('dedupe_window_s', DEFAULT_DEDUPE_WINDOW_S, 0, 86_400),
    ...(db ? { db } : {}),
    transcribe: c.transcribe !== false,
    scanPrivate: c.scan_private != null ? c.scan_private === true : mode === 'live',
    typing: c.typing !== false,
    statusState: path('status_state') ?? join(home, '.local', 'state', 'tg-status', 'state.json'),
    ...(path('compat_journal') ? { compatJournal: path('compat_journal') } : {}),
    ...(path('compat_buttons_file') ? { compatButtonsFile: path('compat_buttons_file') } : {}),
    bindings: c.bindings !== false,
    ...(compatInstant ? { compatInstant } : {}),
    ...(compatCallbacks ? { compatCallbacks } : {}),
    ...(path('tools_registry') ? { toolsRegistry: path('tools_registry') } : {}),
    ...(hub ? { hub } : {}),
  }
}

/** quiet_hours: absent → the default window; "" / "off" → none; else "A-B" with hours 0-24, A ≠ B. */
export function parseQuietHours(v: unknown): string {
  if (v == null) return DEFAULT_QUIET_HOURS
  if (typeof v !== 'string') throw new ConfigError('quiet_hours must be a string like "0-8" (or "" for none)')
  const s = v.trim()
  if (s === '' || s === 'off') return ''
  const m = /^(\d{1,2})-(\d{1,2})$/.exec(s)
  if (!m || Number(m[1]) > 24 || Number(m[2]) > 24 || Number(m[1]) === Number(m[2])) {
    throw new ConfigError('quiet_hours must be "A-B" with hours 0-24 and A != B (e.g. "0-8", "22-6"), or "" for none')
  }
  return `${Number(m[1])}-${Number(m[2])}`
}

export function loadConfig(file: string, home: string, opts: ParseOpts = {}): Config {
  let text: string
  try { text = readFileSync(file, 'utf8') } catch {
    throw new ConfigError(`config file required: ${file} (see adapters/tg/README.md)`)
  }
  let raw: unknown
  try { raw = JSON.parse(text) } catch (e) { throw new ConfigError(`config is not valid JSON: ${(e as Error).message}`) }
  return parseConfig(raw, home, opts)
}

/** Directories a token file may never come from: the Claude config dir(s), where the live
 *  channel plugin keeps its state and token. */
export function forbiddenTokenDirs(env: Record<string, string | undefined>, home: string): string[] {
  const dirs = [join(home, '.claude')]
  const real = realHome()
  if (real) dirs.push(join(real, '.claude'))
  if (env.CLAUDE_CONFIG_DIR) dirs.push(resolve(env.CLAUDE_CONFIG_DIR))
  return [...new Set(dirs)]
}

/** The bot token from config.token_file. Refuses a file inside a Claude config dir or one that
 *  group/other can read. Never logs the token; errors never include it. */
export function readToken(cfg: Config, env: Record<string, string | undefined>, home: string): string {
  const refuse = () => new ConfigError('token_file is inside the Claude config dir (the live plugin\'s token): refused')
  const dirs = forbiddenTokenDirs(env, home)
  // By the configured path first (before touching the file), then by its real path (symlinks).
  if (dirs.some(d => isInside(cfg.tokenFile, d))) throw refuse()
  let real: string
  try { real = realpathSync(cfg.tokenFile) } catch { throw new ConfigError('token_file not found') }
  for (const d of dirs) {
    let dr = d
    try { dr = realpathSync(d) } catch {}
    if (isInside(real, dr)) throw refuse()
  }
  const st = statSync(real)
  if (!st.isFile()) throw new ConfigError('token_file is not a file')
  if ((st.mode & 0o077) !== 0) throw new ConfigError('token_file must not be readable by group/other (chmod 600)')
  let token = ''
  for (const line of readFileSync(real, 'utf8').split('\n')) {
    const m = /^\s*(?:export\s+)?TELEGRAM_BOT_TOKEN\s*=\s*(.*?)\s*$/.exec(line)
    if (m) token = m[1]!.replace(/^(['"])(.*)\1$/, '$2')
  }
  if (!token) throw new ConfigError('token_file has no TELEGRAM_BOT_TOKEN line')
  if (!/^\d{1,20}:[A-Za-z0-9_-]{1,100}$/.test(token)) throw new ConfigError('TELEGRAM_BOT_TOKEN in token_file is malformed')
  return token
}

/** The bot id is the token's numeric prefix (public: it is also getMe's id). */
export const botIdOf = (token: string) => token.slice(0, token.indexOf(':'))
