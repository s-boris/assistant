// The companion (v2 Phase 2 H6): the brain-container half of hub mode. The adapter runs in the hub's tg container
// with the bot token but without the brain's tools, files and state; the companion runs here (`tg-adapter companion
// --instance live`, kept up by `tg-adapter ensure` while ~/.config/tg-adapter/<instance>.hub exists), reads no bot token
// and opens no port. It talks to hubd as the brain (HUB_URL, default http://claude-hub:8700; HUB_TOKEN_FILE, default
// ~/.secrets/hub-token; HUB_SCHEMA = a t_* test schema):
//   jobs      POST /tg/jobs/claim (long poll) → run → POST /tg/jobs/<id>/result:
//             bind        a registry tool by NAME (this container's schemas/tools.toml; never an argv from the hub),
//                         the Binder's rules: effect ≤ send_owner, env without secrets, ~/bin first on PATH, cwd HOME,
//                         the tool's timeout; only the TELEGRAM_CALLBACK_* extras; a picture answer's file is uploaded
//                         (POST /tg/files) and its "photo" becomes the hub ref
//             transcribe  the hub file → a 0700 temp dir → ~/bin/transcribe (TG_ADAPTER_TRANSCRIBE_CMD), 60 s
//             scan        the hub file → a 0700 temp dir under its final name → paperless upload … --tag private
//   presence  tg-status's state.json (TG_STATUS_STATE), the owner's zone (tripclock) and sleep-in → POST /tg/presence
//             on change and every 30 s; the adapter's typing and outbox rules read it.
// One process per instance (companion.pid); companion.json in the instance state dir holds counters for `status`
// (kinds, states, times, error codes: never job arguments, outputs or file names).
import { closeSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync, writeSync } from 'fs'
import { tmpdir } from 'os'
import { basename, join } from 'path'
import { runLocal, type LocalResult } from './compat.ts'
import { HubClient, oneLine, REF_RE, readHubToken, safeName, writeAtomic } from './hub.ts'
import { parsePicture, pictureProblem, pictureRoots } from './picture.ts'
import { scanArgv, SCAN_TIMEOUT_MS, transcribeArgv, TRANSCRIBE_TIMEOUT_MS } from './prep.ts'
import { QuietClock, quietZone, quietZonePinned, tripclockArgv } from './quiet.ts'
import { defaultRegistryFile, Registry, tapAllowed, TOOL_TIMEOUT_MAX_MS } from './registry.ts'
import { SleepinGuard, sleepinArgv } from './rules.ts'
import type { Paths } from './paths.ts'

export const COMPANION_MARKER = 'tg-adapter-companion'
export const KINDS = ['bind', 'transcribe', 'scan'] as const
export const PRESENCE_EVERY_MS = 30_000
const CALLBACK_ENV = new Set(['TELEGRAM_CALLBACK_DATA', 'TELEGRAM_CALLBACK_CHAT_ID', 'TELEGRAM_CALLBACK_MESSAGE_ID'])
const LIMIT: Record<string, number> = { bind: 4, transcribe: 2, scan: 1 }

export type Job = { id: string | number; kind: string; args: Record<string, unknown>; timeout_s?: number }
type Env = Record<string, string | undefined>

export type CompanionOpts = {
  hub: HubClient
  paths: Paths
  env: Env
  log: (m: string) => void
  /** tg-status's board (default ~/.local/state/tg-status/state.json). */
  boardFile?: string
  /** Tests: no tripclock / sleepin lookups. */
  noLookups?: boolean
  claimWaitS?: number
}

export function companionPaths(p: Paths) {
  return { pid: join(p.stateDir, 'companion.pid'), state: join(p.stateDir, 'companion.json') }
}

/** The hub client the companion uses (brain caller). */
export function companionHub(env: Env, home: string, fetchImpl?: (r: Request) => Promise<Response>): HubClient {
  const url = env.HUB_URL || (env.TG_ADAPTER_TEST ? '' : 'http://claude-hub:8700')
  if (!url) throw new Error('test run: set HUB_URL (tests never reach the real hub)')
  const tokenFile = env.HUB_TOKEN_FILE || join(home, '.secrets', 'hub-token')
  return new HubClient({ url, token: readHubToken(tokenFile), ...(env.HUB_SCHEMA ? { schema: env.HUB_SCHEMA } : {}), ...(fetchImpl ? { fetch: fetchImpl } : {}) })
}

export class Companion {
  readonly registry: Registry
  readonly files: ReturnType<typeof companionPaths>
  private stopped = false
  private active: Record<string, number> = { bind: 0, transcribe: 0, scan: 0 }
  private inflight = new Set<Promise<unknown>>()
  private groups = new Set<number>()
  private quiet: QuietClock | null = null
  private sleepin: SleepinGuard | null = null
  private boardSig = ''
  private presenceAt = 0
  private ownsPid = false
  stats = { started: Date.now() / 1000, claims: 0, jobs: 0, by_kind: {} as Record<string, number>, failed: 0, last_claim_ts: 0, last_job_ts: 0,
    last_error: '', last_error_ts: 0, presence_posts: 0, presence_version: 0 }

  constructor(readonly o: CompanionOpts) {
    this.registry = new Registry(o.env.TG_ADAPTER_TOOLS ?? defaultRegistryFile(), o.paths.home, o.log)
    this.files = companionPaths(o.paths)
  }

  get boardFile(): string {
    return this.o.boardFile ?? this.o.env.TG_STATUS_STATE ?? join(this.o.paths.home, '.local', 'state', 'tg-status', 'state.json')
  }

  claimPid(): void {
    try {
      const [first, second] = readFileSync(this.files.pid, 'utf8').split('\n')
      const pid = parseInt(first ?? '', 10)
      if (pid > 1 && pid !== process.pid && (second ?? '').trim() === COMPANION_MARKER) {
        try { process.kill(pid, 0); throw new Error(`companion already running (pid ${pid})`) } catch (e) { if (String((e as Error).message).startsWith('companion already')) throw e }
      }
      rmSync(this.files.pid, { force: true })
    } catch (e) { if (String((e as Error).message).startsWith('companion already')) throw e }
    mkdirSync(this.o.paths.stateDir, { recursive: true, mode: 0o700 })
    const fd = openSync(this.files.pid, 'wx', 0o600)
    writeSync(fd, `${process.pid}\n${COMPANION_MARKER}\n`)
    closeSync(fd)
    this.ownsPid = true
  }

  releasePid(): void {
    if (!this.ownsPid) return
    try { if (parseInt(readFileSync(this.files.pid, 'utf8'), 10) === process.pid) rmSync(this.files.pid) } catch {}
    this.ownsPid = false
  }

  stop(): void {
    this.stopped = true
  }

  /** Wait for the jobs in hand (tests, --once). */
  async settle(maxMs = 120_000): Promise<void> {
    await Promise.race([Promise.allSettled([...this.inflight]), Bun.sleep(maxMs)])
  }

  /** Run until stop(): claim loop + presence loop. */
  async run(): Promise<number> {
    this.claimPid()
    try {
      this.startLookups()
      this.o.log(`companion up: hub ${new URL(this.o.hub.url).host}, kinds ${KINDS.join(', ')}`)
      const presence = this.presenceLoop()
      while (!this.stopped) {
        const ok = await this.claimOnce()
        if (!ok) await Bun.sleep(1_000)
      }
      await presence
      await Promise.race([Promise.allSettled([...this.inflight]), Bun.sleep(5_000)])
      return 0
    } finally {
      this.quiet?.stop()
      this.sleepin?.stop()
      this.saveStats()
      this.releasePid()
    }
  }

  /** One claim (long poll) and, when a job came, its start. False: the hub did not answer. */
  async claimOnce(waitS = this.o.claimWaitS ?? 25): Promise<boolean> {
    const kinds = KINDS.filter(k => this.active[k]! < LIMIT[k]!)
    if (!kinds.length) { await Promise.race([...this.inflight, Bun.sleep(1_000)]); return true }
    let job: Job | null
    try {
      const r = await this.o.hub.call<{ job: Job | null }>('POST', '/tg/jobs/claim', { kinds, wait: waitS }, { timeoutMs: (waitS + 10) * 1000, retries: 0 })
      job = r?.job ?? null
      this.stats.claims++
      this.stats.last_claim_ts = Date.now() / 1000
    } catch (err) {
      this.err(`claim: ${oneLine(err)}`)
      return false
    }
    if (job) {
      const p = this.handle(job)
      this.inflight.add(p)
      void p.finally(() => this.inflight.delete(p))
    }
    return true
  }

  /** Run one job and post its result (never throws). */
  async handle(job: Job): Promise<void> {
    const kind = String(job.kind)
    this.active[kind] = (this.active[kind] ?? 0) + 1
    let result: LocalResult
    let refused = ''
    try {
      if (kind === 'bind') result = await this.bind(job.args ?? {})
      else if (kind === 'transcribe') result = await this.transcribe(job.args ?? {})
      else if (kind === 'scan') result = await this.scan(job.args ?? {})
      else { refused = `unknown job kind ${kind.slice(0, 20)}`; result = { ok: false, stdout: '', error: refused } }
    } catch (err) {
      result = { ok: false, stdout: '', error: oneLine(err) }
    } finally {
      this.active[kind] = Math.max(0, (this.active[kind] ?? 1) - 1)
    }
    this.stats.jobs++
    this.stats.by_kind[kind] = (this.stats.by_kind[kind] ?? 0) + 1
    this.stats.last_job_ts = Date.now() / 1000
    if (!result.ok) this.stats.failed++
    try {
      await this.o.hub.call('POST', `/tg/jobs/${job.id}/result`, refused
        ? { ok: false, error: refused, result: { ok: false, stdout: '', error: refused } }
        : { ok: true, result: { ok: result.ok, stdout: result.stdout, ...(result.error ? { error: result.error } : {}) } }, { retries: 4 })
    } catch (err) {
      this.err(`result of a ${kind} job: ${oneLine(err)}`)
    }
    this.saveStats()
  }

  // --- the three job kinds -----------------------------------------------------------------------------------------

  /** The Binder's run rules (bind.ts), with the tool resolved here by name. */
  async bind(a: Record<string, unknown>): Promise<LocalResult> {
    const name = typeof a.tool === 'string' ? a.tool : ''
    const tool = this.registry.get(name)
    if (!tool) return { ok: false, stdout: '', error: `${name.slice(0, 64) || 'no tool'}: not in the tools registry here` }
    if (!tapAllowed(tool.effect)) return { ok: false, stdout: '', error: `${tool.name} has effect ${tool.effect}, which needs an approval` }
    const extra = Array.isArray(a.extra) ? a.extra : []
    if (extra.length > 16 || !extra.every(x => typeof x === 'string' && x.length <= 500 && !x.includes('\0'))) return { ok: false, stdout: '', error: 'bad binding arguments' }
    let arg: string[] = []
    if (a.arg != null && a.arg !== '') {
      if (!tool.textArg || typeof a.arg !== 'string' || a.arg.length > 4096 || a.arg.includes('\0')) return { ok: false, stdout: '', error: `${tool.name} takes no text argument` }
      arg = [a.arg]
    }
    const extras: Record<string, string> = {}
    for (const [k, v] of Object.entries((a.env as Record<string, unknown>) ?? {})) {
      if (CALLBACK_ENV.has(k) && typeof v === 'string' && v.length <= 256) extras[k] = v
    }
    const timeout = Math.min(tool.timeoutMs, TOOL_TIMEOUT_MAX_MS)
    const r = await runLocal([...tool.argv, ...(extra as string[]), ...arg], timeout, s => s, { env: this.toolEnv(extras), cwd: this.o.paths.home, groups: this.groups })
    if (r.ok && tool.output === 'picture') return { ...r, stdout: await this.uploadPicture(r.stdout) }
    return r
  }

  /** A picture answer's file → a hub ref (same JSON, "photo" replaced). A file outside the picture roots stays as it
   *  is: the adapter can't open it and falls back to the text, as it does for any picture it can't send. */
  async uploadPicture(stdout: string): Promise<string> {
    const pic = parsePicture(stdout.trim())
    if (!pic || pictureProblem(pic.photo, pictureRoots(this.o.paths.home))) return stdout
    const real = realpathSync(pic.photo)
    const ref = await this.o.hub.upload(readFileSync(real), basename(real))
    const obj = JSON.parse(stdout.trim()) as Record<string, unknown>
    return JSON.stringify({ ...obj, photo: ref })
  }

  async transcribe(a: Record<string, unknown>): Promise<LocalResult> {
    const argv = transcribeArgv(this.o.env, this.o.paths.home)
    if (!argv) return { ok: false, stdout: '', error: 'no transcribe command' }
    return this.withFile(a.file, null, path => runLocal([...argv, path], TRANSCRIBE_TIMEOUT_MS, s => s))
  }

  async scan(a: Record<string, unknown>): Promise<LocalResult> {
    const argv = scanArgv(this.o.env, this.o.paths.home)
    if (!argv) return { ok: false, stdout: '', error: 'the paperless command is not available here' }
    const title = typeof a.title === 'string' ? a.title : ''
    if (!title || title.length > 200 || /[\0-\x1f]/.test(title)) return { ok: false, stdout: '', error: 'bad scan title' }
    const name = typeof a.name === 'string' && a.name ? safeName(a.name) : null
    return this.withFile(a.file, name, path =>
      runLocal([...argv, 'upload', path, `--title=${title}`, '--tag', 'Inbox', '--tag', 'private', '--create'], SCAN_TIMEOUT_MS, s => s))
  }

  /** The hub file in a private temp dir for the length of fn. */
  private async withFile(ref: unknown, name: string | null, fn: (path: string) => Promise<LocalResult>): Promise<LocalResult> {
    if (typeof ref !== 'string' || !REF_RE.test(ref)) return { ok: false, stdout: '', error: 'no hub file in the job' }
    const dir = mkdtempSync(join(tmpdir(), 'tga-companion-'))
    try {
      const bytes = await this.o.hub.download(ref)
      const path = join(dir, name ?? REF_RE.exec(ref)![2]!)
      writeFileSync(path, bytes, { mode: 0o600 })
      return await fn(path)
    } catch (err) {
      return { ok: false, stdout: '', error: `download failed: ${oneLine(err)}` }
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  }

  /** bind.ts Binder.env(): the process env minus the bot token, ~/bin and ~/.local/bin first on PATH, + extras. */
  toolEnv(extra: Record<string, string> = {}): Record<string, string | undefined> {
    const base: Record<string, string | undefined> = {}
    for (const [k, v] of Object.entries(this.o.env)) {
      if (k === 'TELEGRAM_BOT_TOKEN' || k === 'HUB_TOKEN' || v == null) continue
      base[k] = v
    }
    const path = (base.PATH || '/usr/local/bin:/usr/bin:/bin').split(':')
    const bins = [join(this.o.paths.home, 'bin'), join(this.o.paths.home, '.local', 'bin')].filter(b => !path.includes(b))
    base.PATH = [...bins, ...path].join(':')
    return { ...base, ...extra }
  }

  // --- presence ----------------------------------------------------------------------------------------------------

  private startLookups(): void {
    if (this.o.noLookups) return
    const env = this.o.env, home = this.o.paths.home
    this.quiet = new QuietClock(quietZone(env, home), tripclockArgv(env, home), { log: this.o.log, pinned: quietZonePinned(env) })
    this.sleepin = new SleepinGuard(sleepinArgv(env, home), { log: this.o.log })
    this.quiet.start()
    this.sleepin.start()
  }

  /** The presence state now (board parsed, or null when unreadable). */
  presence(): { board: unknown; zone?: string; sleeping?: boolean; ts: number } {
    let board: unknown = null
    try { board = JSON.parse(readFileSync(this.boardFile, 'utf8')) } catch {}
    return {
      board,
      ...(this.quiet ? { zone: this.quiet.zone() } : {}),
      ...(this.sleepin ? { sleeping: this.sleepin.active() } : {}),
      ts: Date.now() / 1000,
    }
  }

  /** Post presence when the board file changed (mtime/size) or every 30 s. */
  async presenceTick(force = false): Promise<boolean> {
    let sig = ''
    try { const st = statSync(this.boardFile); sig = `${st.mtimeMs}:${st.size}` } catch {}
    const z = this.quiet ? this.quiet.zone() : ''
    const s = this.sleepin ? String(this.sleepin.active()) : ''
    const full = `${sig}|${z}|${s}`
    if (!force && full === this.boardSig && Date.now() - this.presenceAt < PRESENCE_EVERY_MS) return false
    try {
      const r = await this.o.hub.call<{ version: number }>('POST', '/tg/presence', { state: this.presence() }, { retries: 0, timeoutMs: 5_000 })
      this.boardSig = full
      this.presenceAt = Date.now()
      this.stats.presence_posts++
      this.stats.presence_version = Number(r?.version ?? 0)
      return true
    } catch (err) {
      this.err(`presence: ${oneLine(err)}`)
      return false
    }
  }

  private async presenceLoop(): Promise<void> {
    while (!this.stopped) {
      await this.presenceTick()
      await Bun.sleep(1_000)
    }
  }

  private err(m: string): void {
    const first = !this.stats.last_error || Date.now() / 1000 - this.stats.last_error_ts > 300
    this.stats.last_error = m.slice(0, 200)
    this.stats.last_error_ts = Date.now() / 1000
    if (first) this.o.log(m)
  }

  saveStats(): void {
    try { writeAtomic(this.files.state, JSON.stringify({ pid: process.pid, ...this.stats })) } catch {}
  }
}

/** `companion status`: pid, running, the counters (no job content). */
export function companionStatus(p: Paths): Record<string, unknown> {
  const f = companionPaths(p)
  let pid: number | null = null
  let running = false
  try {
    const [first, second] = readFileSync(f.pid, 'utf8').split('\n')
    pid = parseInt(first ?? '', 10) || null
    if (pid && (second ?? '').trim() === COMPANION_MARKER) { try { process.kill(pid, 0); running = true } catch {} }
  } catch {}
  let stats: Record<string, unknown> = {}
  try { if (existsSync(f.state)) stats = JSON.parse(readFileSync(f.state, 'utf8')) } catch {}
  return { pid, running, ...stats }
}
