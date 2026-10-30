// tgloader: runs the Telegram adapter of the brain-built hub release that RELEASES/CURRENT names (v2 Phase 2 H6).
// Bun, stdlib only, one file. The owner's tg paste copies it into the hub's appdata (HUB_ROOT/loader, root-owned) and
// claude-hub-tg mounts it read-only: the brain changes releases, never this loader (a new one comes with a paste).
//   bun tgloader.ts --releases /srv/releases --state /srv/tg/state --config /srv/tg/config/live.json
// CURRENT holds "<release id> <attempt>". Every --poll seconds the loader reads it; when it names another release
// than the running one it verifies RELEASES/<id>/release.json (format 1, its own id, entry.tg = tg/adapter.js among
// its files, every listed tg/ file a regular file inside the release with the listed sha256, no symlinks) and only
// then stops the running adapter and starts `bun <release>/tg/adapter.js run --instance live` (adapters/tg as one
// `bun build` bundle: grammy and the DDL inlined, no node_modules in a release), stdin closed, with
// TG_ADAPTER_CONFIG, TG_ADAPTER_OFF_FILE (<config dir>/live.off), TG_ADAPTER_STATE_DIR (<state>/adapter),
// ASSISTANT_EVENTS_DB (<state>/adapter/journal.db: the adapter's local journal), APPROVALS_DB, TG_ADAPTER_TOOLS
// (<release>/tg/tools.toml), HUB_RELEASE and TG_HUB_LOADER=1 in its environment.
// Healthy = still running --healthy-after seconds after the start: that release becomes last_good. A release that
// fails to verify or exits before it is healthy is marked bad for that CURRENT line (a new deploy writes a new
// attempt) and the last good release runs again. A running adapter that exits is restarted with backoff (2 s .. 60 s);
// 3 exits within 10 min mark a release bad, except the last good one, which is retried every 60 s instead.
// Exit codes that mean "starting again won't help" stop the restarts until CURRENT changes or the container restarts:
// 2 config/usage · 3 409 (another poller has the bot: one poller per token) · 4 401 · 5 wrong bot · 6 already running
// · 7 the switch file. The loader itself keeps running (the container stays up, unhealthy, saying why).
// The brain's hold (`hub tg stop`): before every step the loader posts its state to the hub (POST /tg/hold/seen with
// the config's hub.url and hub.token_file, 3 s timeout) and gets the hold flag back. Held = it stops the adapter
// (SIGTERM, SIGKILL after 7 s) and starts none, also the last good one. It keeps the last answer in tgloader.json, so
// a restart while the hub is away honours a hold the hub gave before; released (`hub tg start`) = it starts again,
// the 409 stop and the restart backoff forgotten. No hub block in the config, or no answer yet: no hold.
// State: <state>/tgloader.json (last_good, bad attempts with reasons, hold). Status (the healthcheck reads it;
// rewritten on every change and every 30 s): <state>/tgloader-status.json {state: starting|running|down|conflict|
// idle|held, release, pid, since, why, ts}. Log: one JSON line per event on stdout (ids, reasons, codes; never config
// or token values).
// SIGTERM/SIGINT: the adapter gets SIGTERM, then SIGKILL after 7 s.
import { spawn, type ChildProcess } from 'child_process'
import { createHash } from 'crypto'
import { existsSync, lstatSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs'
import { dirname, join, resolve, sep } from 'path'

const FORMAT = 1
const RID = /^[0-9a-f]{7,40}$/
const ATTEMPT = /^[A-Za-z0-9._:-]{1,64}$/
const SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/ // the hub loader's rule
export const ENTRY = 'tg/adapter.js'
export const STOP_CODES: Record<number, string> = {
  2: 'config or usage error', 3: '409: another poller has this bot (one poller per token)', 4: '401: token refused',
  5: 'wrong bot for this token', 6: 'already running', 7: 'switch file present (live.off)',
}
const RESTART_WINDOW_MS = 600_000
const RESTARTS_BAD = 3
const LAST_GOOD_RETRY_MS = 60_000
const STOP_WAIT_MS = 7_000
const BAD_KEEP = 20
const HOLD_ASK_MS = 3_000

type Args = { releases: string; state: string; config: string; instance: string; pollMs: number; healthyMs: number }

function parseArgs(argv: string[]): Args & { check?: string } {
  const a: Args & { check?: string } = { releases: '/srv/releases', state: '/srv/tg/state', config: '/srv/tg/config/live.json',
    instance: 'live', pollMs: 5_000, healthyMs: 20_000 }
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i]!, v = argv[i + 1]
    if (v === undefined) throw new Error(`${k}: a value is missing`)
    if (k === '--releases') a.releases = v
    else if (k === '--state') a.state = v
    else if (k === '--config') a.config = v
    else if (k === '--instance') a.instance = v
    else if (k === '--poll') a.pollMs = Number(v) * 1000
    else if (k === '--healthy-after') a.healthyMs = Number(v) * 1000
    else if (k === '--check-release') a.check = v
    else throw new Error(`unknown option ${k}`)
    i++
  }
  return a
}

const log = (event: string, kw: Record<string, unknown> = {}) =>
  console.log(JSON.stringify({ ts: Math.round(Date.now() / 1000), event, ...kw }))

export function parseCurrent(text: string): { rid: string; line: string } | null {
  const parts = text.trim().split(/\s+/).filter(Boolean)
  if (!parts.length || parts.length > 2 || !RID.test(parts[0]!)) return null
  if (parts.length === 2 && !ATTEMPT.test(parts[1]!)) return null
  return { rid: parts[0]!, line: parts.join(' ') }
}

const sha256 = (p: string) => createHash('sha256').update(readFileSync(p)).digest('hex')

/** null when RELEASES/<rid> carries a complete, untouched tg tree; else why not. */
export function verifyRelease(releases: string, rid: string): string | null {
  if (!RID.test(rid)) return 'bad id'
  const root = resolve(releases)
  const dir = join(root, rid)
  let meta: any
  try { meta = JSON.parse(readFileSync(join(dir, 'release.json'), 'utf8')) } catch { return 'no readable release.json' }
  if (!meta || meta.format !== FORMAT || meta.rid !== rid) return 'release.json: wrong format or id'
  const files = meta.files && typeof meta.files === 'object' ? meta.files as Record<string, { sha256?: string }> : {}
  if (meta.entry?.tg !== ENTRY || !files[ENTRY]) return 'release.json: no tg entry (a release without the adapter)'
  let n = 0
  for (const [rel, info] of Object.entries(files)) {
    if (!rel.startsWith('tg/')) continue
    const segs = rel.split('/')
    if (!segs.every(s => SEGMENT.test(s) && s !== '.' && s !== '..')) return `release.json: bad path ${rel.slice(0, 80)}`
    const p = join(dir, ...segs)
    if (!resolve(p).startsWith(dir + sep)) return `release.json: path outside the release`
    try {
      for (let d = dir, i = 0; i < segs.length; i++) {
        d = join(d, segs[i]!)
        if (lstatSync(d).isSymbolicLink()) return `${rel}: a symlink`
      }
      if (!lstatSync(p).isFile()) return `${rel}: not a file`
    } catch { return `${rel}: missing` }
    if (sha256(p) !== info?.sha256) return `${rel}: sha256 mismatch`
    n++
  }
  return n ? null : 'no tg files'
}

type Hold = { why: string; ts: number | null }
type St = { last_good: string | null; bad: Record<string, { why: string; ts: number }>; hold: Hold | null }

/** The hub block of the adapter's config (url + token file), null when it has none; undefined when unreadable. */
export function hubOf(config: string): { url: string; tokenFile: string } | null | undefined {
  let c: any
  try { c = JSON.parse(readFileSync(config, 'utf8')) } catch { return undefined }
  const u = c?.hub?.url, t = c?.hub?.token_file
  if (typeof u !== 'string' || !/^https?:\/\/[^\s/?#]+/.test(u) || typeof t !== 'string' || !t) return null
  return { url: u.replace(/\/+$/, ''), tokenFile: t }
}

export class Loader {
  st: St
  hub: { url: string; tokenFile: string } | null | undefined = undefined
  child: ChildProcess | null = null
  running: { rid: string; line: string; started: number; healthy: boolean } | null = null
  exits: number[] = []
  stoppedFor: string | null = null // a CURRENT line whose adapter stopped with a STOP_CODE
  retryAt = 0
  backoffMs = 2_000
  status: Record<string, unknown> = { state: 'idle' }
  stopping = false

  constructor(readonly a: Args) {
    mkdirSync(a.state, { recursive: true, mode: 0o700 })
    this.st = this.loadState()
  }

  loadState(): St {
    try {
      const d = JSON.parse(readFileSync(join(this.a.state, 'tgloader.json'), 'utf8'))
      const h = d.hold && typeof d.hold === 'object' ? d.hold : null
      return { last_good: typeof d.last_good === 'string' ? d.last_good : null, bad: d.bad && typeof d.bad === 'object' ? d.bad : {},
        hold: h ? { why: typeof h.why === 'string' ? h.why : '', ts: typeof h.ts === 'number' ? h.ts : null } : null }
    } catch { return { last_good: null, bad: {}, hold: null } }
  }

  /** Report our state to the hub and get its hold flag: {held, why, ts}, or null (no hub block, no answer). */
  async askHub(): Promise<{ held: boolean; why: string; ts: number | null } | null> {
    if (this.hub === undefined) this.hub = hubOf(this.a.config) // unreadable (undefined): read it again next time
    const h = this.hub
    if (!h) return null
    let tok = ''
    try { tok = readFileSync(h.tokenFile, 'utf8').trim() } catch { return null }
    if (!tok) return null
    const st = this.status
    try {
      const r = await fetch(`${h.url}/tg/hold/seen`, {
        method: 'POST', signal: AbortSignal.timeout(HOLD_ASK_MS),
        headers: { authorization: `Bearer ${tok}`, 'content-type': 'application/json' },
        body: JSON.stringify({ state: st.state ?? 'idle', release: st.release ?? null, why: String(st.why ?? '').slice(0, 200) }),
      })
      if (!r.ok) return null
      const d: any = await r.json()
      if (typeof d?.held !== 'boolean') return null
      return { held: d.held, why: typeof d.why === 'string' ? d.why.slice(0, 200) : '', ts: typeof d.ts === 'number' ? d.ts : null }
    } catch { return null }
  }

  /** The hold in force: the hub's answer, else the last one it gave (kept in tgloader.json). */
  async holdNow(): Promise<Hold | null> {
    const got = await this.askHub()
    if (got?.held) {
      const h = { why: got.why, ts: got.ts }
      if (!this.st.hold || this.st.hold.ts !== h.ts || this.st.hold.why !== h.why) {
        this.st.hold = h
        this.save()
        log('hold', { ts: h.ts })
      }
    } else if (got && this.st.hold) {
      this.st.hold = null
      this.save()
      this.stoppedFor = null
      this.retryAt = 0
      this.exits = []
      this.backoffMs = 2_000
      log('hold_released', {})
    }
    return this.st.hold
  }

  async holdDown(h: Hold): Promise<void> {
    const c = this.child
    if (c) {
      log('hold_stop', { release: this.running?.rid ?? null, pid: c.pid })
      this.child = null
      this.running = null
      c.removeAllListeners('exit')
      if (c.exitCode === null && c.signalCode === null) {
        await new Promise<void>(res => {
          const t = setTimeout(() => { try { c.kill('SIGKILL') } catch {} }, STOP_WAIT_MS)
          c.once('exit', () => { clearTimeout(t); res() })
          c.kill('SIGTERM')
        })
      }
    }
    this.setStatus('held', `held by the brain${h.why ? `: ${h.why}` : ''}`.slice(0, 200))
    if (c) await this.askHub() // report "held" at once (the brain's `hub tg stop` waits for it)
  }

  save(): void {
    const keys = Object.keys(this.st.bad).sort((x, y) => this.st.bad[x]!.ts - this.st.bad[y]!.ts)
    for (const k of keys.slice(0, Math.max(0, keys.length - BAD_KEEP))) delete this.st.bad[k]
    atomic(join(this.a.state, 'tgloader.json'), JSON.stringify(this.st))
  }

  setStatus(state: string, why = ''): void {
    const prev = this.status
    const rid = this.running?.rid ?? null
    if (prev.state !== state || prev.release !== rid || prev.why !== why) {
      this.status = { state, release: rid, pid: this.child?.pid ?? null, since: Math.round(Date.now() / 1000), why }
      log('status', { state, release: rid, why })
    }
    atomic(join(this.a.state, 'tgloader-status.json'), JSON.stringify({ ...this.status, pid: this.child?.pid ?? null,
      ts: Math.round(Date.now() / 1000) }))
  }

  markBad(line: string, why: string): void {
    this.st.bad[line] = { why: why.slice(0, 120), ts: Math.round(Date.now() / 1000) }
    this.save()
    log('bad', { attempt: line, why: why.slice(0, 120) })
  }

  readCurrent(): { rid: string; line: string } | null {
    try { return parseCurrent(readFileSync(join(this.a.releases, 'CURRENT'), 'utf8')) } catch { return null }
  }

  start(rid: string, line: string): boolean {
    const why = verifyRelease(this.a.releases, rid)
    if (why) {
      if (rid !== this.st.last_good) this.markBad(line, `verify: ${why}`)
      log('verify_failed', { release: rid, why })
      return false
    }
    const rdir = join(resolve(this.a.releases), rid)
    const sdir = join(this.a.state, 'adapter')
    mkdirSync(sdir, { recursive: true, mode: 0o700 })
    const env: Record<string, string> = {
      PATH: process.env.PATH || '/usr/local/bin:/usr/bin:/bin', TZ: process.env.TZ || 'UTC', HOME: process.env.HOME || '/tmp',
      TG_ADAPTER_CONFIG: this.a.config, TG_ADAPTER_OFF_FILE: join(dirname(this.a.config), `${this.a.instance}.off`),
      TG_ADAPTER_STATE_DIR: sdir, ASSISTANT_EVENTS_DB: join(sdir, 'journal.db'), APPROVALS_DB: join(sdir, 'approvals.db'),
      TG_ADAPTER_TOOLS: join(rdir, 'tg', 'tools.toml'), HUB_RELEASE: rid, TG_HUB_LOADER: '1',
    }
    const child = spawn(process.execPath, [join(rdir, ENTRY), 'run', '--instance', this.a.instance],
      { cwd: join(rdir, 'tg'), env, stdio: ['ignore', 'inherit', 'inherit'] })
    this.child = child
    this.running = { rid, line, started: Date.now(), healthy: false }
    log('start', { release: rid, pid: child.pid })
    this.setStatus('starting')
    child.on('exit', (code, signal) => this.onExit(child, code, signal))
    return true
  }

  onExit(child: ChildProcess, code: number | null, signal: string | null): void {
    if (child !== this.child) return
    const r = this.running
    this.child = null
    this.running = null
    log('exit', { release: r?.rid, code, signal })
    if (this.stopping || !r) return
    if (code !== null && STOP_CODES[code]) {
      this.stoppedFor = r.line
      this.running = { ...r }
      this.setStatus(code === 3 ? 'conflict' : 'down', `exit ${code}: ${STOP_CODES[code]}`)
      this.running = null
      return
    }
    const now = Date.now()
    this.exits = [...this.exits.filter(t => now - t < RESTART_WINDOW_MS), now]
    if (!r.healthy && r.rid !== this.st.last_good) {
      this.markBad(r.line, `exited before healthy (code ${code ?? signal})`)
    } else if (this.exits.length >= RESTARTS_BAD && r.rid !== this.st.last_good) {
      this.markBad(r.line, `${RESTARTS_BAD} exits in ${RESTART_WINDOW_MS / 60_000} min`)
    }
    const wait = r.rid === this.st.last_good && this.exits.length >= RESTARTS_BAD ? LAST_GOOD_RETRY_MS : this.backoffMs
    this.backoffMs = Math.min(this.backoffMs * 2, 60_000)
    this.retryAt = now + wait
    this.setStatus('down', `exit ${code ?? signal}; retry in ${Math.round(wait / 1000)} s`)
  }

  async stopChild(): Promise<void> {
    const c = this.child
    if (!c || c.exitCode !== null) return
    const done = new Promise<void>(res => c.once('exit', () => res()))
    c.kill('SIGTERM')
    const t = setTimeout(() => { try { c.kill('SIGKILL') } catch {} }, STOP_WAIT_MS)
    await done
    clearTimeout(t)
  }

  async step(): Promise<void> {
    const hold = await this.holdNow()
    if (hold) { await this.holdDown(hold); return }
    const cur = this.readCurrent()
    const want = cur && !this.st.bad[cur.line] ? cur : null
    if (this.running && !this.running.healthy && this.child && Date.now() - this.running.started >= this.a.healthyMs) {
      this.running.healthy = true
      this.backoffMs = 2_000
      if (this.st.last_good !== this.running.rid) { this.st.last_good = this.running.rid; this.save() }
      log('healthy', { release: this.running.rid })
    }
    if (this.child && this.running) {
      if (want && want.rid !== this.running.rid && verifyRelease(this.a.releases, want.rid) === null) {
        log('switch', { from: this.running.rid, to: want.rid })
        this.stoppedFor = null
        const old = this.child
        this.child = null
        this.running = null
        old.removeAllListeners('exit')
        await new Promise<void>(res => { old.once('exit', () => res()); old.kill('SIGTERM'); setTimeout(() => { try { old.kill('SIGKILL') } catch {} }, STOP_WAIT_MS) })
        this.start(want.rid, want.line)
      } else if (want && want.rid !== this.running.rid) {
        const why = verifyRelease(this.a.releases, want.rid)
        if (why && !this.st.bad[want.line]) this.markBad(want.line, `verify: ${why}`)
      }
      this.setStatus(this.running?.healthy ? 'running' : 'starting')
      return
    }
    if (this.stoppedFor) {
      if (cur && cur.line !== this.stoppedFor) { log('retry_after_stop', { attempt: cur.line }); this.stoppedFor = null }
      else { this.setStatus(String(this.status.state ?? 'down'), String(this.status.why ?? '')); return }
    }
    if (Date.now() < this.retryAt) { this.setStatus('down', String(this.status.why ?? '')); return }
    if (want && this.start(want.rid, want.line)) return
    const lg = this.st.last_good
    if (lg && (!want || want.rid !== lg) && this.start(lg, `${lg} last_good`)) return
    this.setStatus('idle', cur ? 'no runnable release (CURRENT bad, no last good)' : 'no CURRENT yet')
  }

  async run(): Promise<number> {
    log('loader_start', { last_good: this.st.last_good })
    const stop = async () => {
      if (this.stopping) return
      this.stopping = true
      log('loader_stop', {})
      await this.stopChild()
      this.setStatus('idle', 'stopped')
      process.exit(0)
    }
    process.on('SIGTERM', () => void stop())
    process.on('SIGINT', () => void stop())
    let beat = 0
    while (!this.stopping) {
      try { await this.step() } catch (e) { log('step_error', { error: String((e as Error)?.name ?? e) }) }
      await new Promise(r => setTimeout(r, this.a.pollMs))
      if (++beat * this.a.pollMs >= 30_000) { beat = 0; this.setStatus(String(this.status.state ?? 'idle'), String(this.status.why ?? '')) }
    }
    return 0
  }
}

function atomic(path: string, text: string): void {
  const tmp = `${path}.${process.pid}.tmp`
  writeFileSync(tmp, text + '\n', { mode: 0o600 })
  renameSync(tmp, path)
}

if (import.meta.main) {
  let a: ReturnType<typeof parseArgs>
  try { a = parseArgs(process.argv.slice(2)) } catch (e) { console.error(`tgloader: ${(e as Error).message}`); process.exit(2) }
  if (a.check) {
    const why = verifyRelease(a.releases, a.check)
    console.log(why ?? 'ok')
    process.exit(why ? 1 : 0)
  }
  if (!existsSync(a.config)) log('config_missing', { config: a.config })
  process.exit(await new Loader(a).run())
}
