// Quiet hours for the plugin's own owner alerts, on the owner's wall clock, never the process TZ.
// The zone: TELEGRAM_QUIET_TZ pins it. Otherwise it is the zone the owner is in right now, as the
// assistant's tripclock prints it (`tripclock tz`: his home zone, or a trip's zone while he travels;
// TELEGRAM_TRIPCLOCK_CMD, default ~/bin/tripclock tz when executable, "" off), looked up in the
// background (QuietClock: at start, then every TELEGRAM_TRIPCLOCK_REFRESH_MS, 10 min) so a lookup
// never waits on it. Without tripclock, or on any error (exit code, timeout, junk output), the home
// zone: the assistant profile's locale.tz ($ASSISTANT_PROFILE, else ~/.config/assistant/profile.json),
// else Europe/Zurich. Tests: test/quiet.test.ts.
import { spawn } from 'child_process'
import { accessSync, constants as fsConstants, readFileSync, statSync } from 'fs'
import { homedir } from 'os'
import { join } from 'path'

export const DEFAULT_QUIET_TZ = 'Europe/Zurich'
export const TRIPCLOCK_REFRESH_MS = 10 * 60_000
export const TRIPCLOCK_TIMEOUT_MS = 5_000

type Env = Record<string, string | undefined>

export const validZone = (z: unknown): z is string => {
  if (typeof z !== 'string' || !z.trim()) return false
  try { new Intl.DateTimeFormat('en-GB', { timeZone: z.trim() }); return true } catch { return false }
}

/** The fixed zone: TELEGRAM_QUIET_TZ, else the profile's locale.tz, else Europe/Zurich. */
export function quietZone(env: Env = process.env, home: string = homedir()): string {
  if (validZone(env.TELEGRAM_QUIET_TZ)) return env.TELEGRAM_QUIET_TZ.trim()
  try {
    const p = env.ASSISTANT_PROFILE || join(home, '.config/assistant/profile.json')
    const tz = JSON.parse(readFileSync(p, 'utf8'))?.locale?.tz
    if (validZone(tz)) return tz.trim()
  } catch {}
  return DEFAULT_QUIET_TZ
}

/** Does TELEGRAM_QUIET_TZ pin the zone (so tripclock isn't asked)? */
export const quietZonePinned = (env: Env = process.env): boolean => validZone(env.TELEGRAM_QUIET_TZ)

const isExecutableFile = (p: string): boolean => {
  try { accessSync(p, fsConstants.X_OK); return statSync(p).isFile() } catch { return false }
}

/** argv that prints the owner's current zone: TELEGRAM_TRIPCLOCK_CMD split on whitespace (no shell; a
 *  leading ~/ expanded; "" off), else ~/bin/tripclock tz when executable; null when TELEGRAM_QUIET_TZ
 *  pins the zone or there is no tripclock. */
export function tripclockArgv(env: Env = process.env, home: string = homedir()): string[] | null {
  if (quietZonePinned(env)) return null
  const v = env.TELEGRAM_TRIPCLOCK_CMD
  if (v === undefined) {
    const p = join(home, 'bin', 'tripclock')
    return isExecutableFile(p) ? [p, 'tz'] : null
  }
  const argv = v.trim().split(/\s+/).filter(Boolean).map(a => a.replace(/^~(?=\/)/, home))
  return argv.length ? argv : null
}

/** Is `spec` a quiet-hours window ("0-8", "22-6")? "" or junk: off. */
export function quietSpec(spec: string | undefined): [number, number] | null {
  const m = /^(\d{1,2})-(\d{1,2})$/.exec((spec ?? '').trim())
  return m ? [Number(m[1]), Number(m[2])] : null
}

/** The hour (0-23) on the wall clock of `zone` at `now` (epoch ms). */
export function zoneHour(now: number, zone: string): number {
  const h = Number(new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: 'numeric', hourCycle: 'h23' }).format(now))
  return h % 24
}

/** Is `now` (epoch ms) inside `spec` ("0-8": from 00:00 up to 08:00; "22-6" wraps; "" or junk: never) in `zone`? */
export function inQuietHours(spec: string, now: number, zone: string): boolean {
  const w = quietSpec(spec)
  if (!w) return false
  const h = zoneHour(now, zone), [a, b] = w
  return a <= b ? h >= a && h < b : h >= a || h < b
}

export type Runner = (argv: string[], timeoutMs: number) => Promise<string>

/** Runs argv (no shell) in its own process group with the environment minus the bot token; resolves
 *  with stdout on exit 0, rejects on any other exit, a spawn error or the timeout (the group is killed
 *  and the promise settles at once, even if a grandchild keeps the pipe open). */
export const runCommand: Runner = (argv, timeoutMs) => new Promise((resolve, reject) => {
  const { TELEGRAM_BOT_TOKEN: _token, ...env } = process.env
  let out = '', err = '', done = false
  const finish = (e: Error | null) => {
    if (done) return
    done = true
    clearTimeout(timer)
    if (e) reject(e); else resolve(out)
  }
  let child: ReturnType<typeof spawn>
  try {
    child = spawn(argv[0]!, argv.slice(1), { detached: true, stdio: ['ignore', 'pipe', 'pipe'], env })
  } catch (e) {
    return reject(e instanceof Error ? e : new Error(String(e)))
  }
  const timer = setTimeout(() => {
    try { if (child.pid) process.kill(-child.pid, 'SIGKILL') } catch {}
    finish(new Error(`timed out after ${timeoutMs} ms`))
  }, timeoutMs)
  child.stdout!.setEncoding('utf8')
  child.stderr!.setEncoding('utf8')
  child.stdout!.on('data', (d: string) => { if (out.length < 4096) out += d })
  child.stderr!.on('data', (d: string) => { if (err.length < 4096) err += d })
  child.on('error', e => finish(e))
  child.on('close', (code, sig) => {
    if (code === 0) return finish(null)
    const why = (err.trim().split('\n')[0] ?? '').slice(0, 160)
    finish(new Error(`${sig ? `killed by ${sig}` : `exit ${code}`}${why ? `: ${why}` : ''}`))
  })
})

export type QuietSource = 'tripclock' | 'home' | 'pinned'
export type QuietState = { zone: string; source: QuietSource; checked_at: string | null; error: string | null }

/** The owner's zone for quiet hours, kept fresh in the background. zone() never waits: it returns the
 *  last tripclock answer, or the home zone before the first one and after any failed lookup. */
export class QuietClock {
  private trip: string | null = null
  private error: string | null = null
  private lastTry = -Infinity
  private checkedAt: number | null = null
  private inflight: Promise<void> | null = null
  private settledOnce = false
  private timer: ReturnType<typeof setInterval> | null = null
  private lastLogged = ''
  readonly refreshMs: number
  readonly timeoutMs: number
  private readonly run: Runner
  private readonly now: () => number
  private readonly log: (m: string) => void
  private readonly pinned: boolean

  constructor(readonly home: string, readonly argv: string[] | null, opts: {
    refreshMs?: number; timeoutMs?: number; run?: Runner; now?: () => number; log?: (m: string) => void; pinned?: boolean
  } = {}) {
    this.refreshMs = opts.refreshMs && opts.refreshMs > 0 ? opts.refreshMs : TRIPCLOCK_REFRESH_MS
    this.timeoutMs = opts.timeoutMs && opts.timeoutMs > 0 ? opts.timeoutMs : TRIPCLOCK_TIMEOUT_MS
    this.run = opts.run ?? runCommand
    this.now = opts.now ?? Date.now
    this.log = opts.log ?? (() => {})
    this.pinned = opts.pinned ?? false
  }

  /** The zone to use now; starts a background lookup when the last one is older than refreshMs. */
  zone(): string {
    if (this.argv && !this.inflight && this.now() - this.lastTry >= this.refreshMs) void this.refresh()
    return this.trip ?? this.home
  }

  /** One tripclock lookup (at most one at a time); never rejects. */
  refresh(): Promise<void> {
    if (!this.argv) return Promise.resolve()
    if (this.inflight) return this.inflight
    this.lastTry = this.now()
    let p: Promise<string>
    try { p = this.run(this.argv, this.timeoutMs) } catch (e) { p = Promise.reject(e) }
    this.inflight = p.then(out => {
      const z = (out ?? '').trim().split('\n')[0]!.trim()
      if (!validZone(z)) throw new Error(`not a time zone: ${JSON.stringify(z.slice(0, 40))}`)
      this.trip = z
      this.error = null
    }).catch(e => {
      this.trip = null
      this.error = (e instanceof Error ? e.message : String(e)).slice(0, 200)
    }).finally(() => {
      this.checkedAt = this.now()
      this.inflight = null
      this.settledOnce = true
      this.report()
    })
    return this.inflight
  }

  /** Waits (at most maxMs) for the first lookup when it hasn't finished yet; for an alert sent right at start. */
  async ready(maxMs = 2_000): Promise<void> {
    if (this.settledOnce || !this.argv) return
    const first = this.inflight ?? this.refresh()
    let t: ReturnType<typeof setTimeout> | undefined
    await Promise.race([first, new Promise<void>(r => { t = setTimeout(r, maxMs) })])
    clearTimeout(t)
  }

  /** First lookup now, then every refreshMs (the timer doesn't keep the process alive). */
  start(): this {
    if (!this.argv || this.timer) return this
    void this.refresh()
    this.timer = setInterval(() => void this.refresh(), this.refreshMs)
    ;(this.timer as { unref?: () => void }).unref?.()
    return this
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
  }

  state(): QuietState {
    return {
      zone: this.trip ?? this.home,
      source: this.trip ? 'tripclock' : this.pinned ? 'pinned' : 'home',
      checked_at: this.checkedAt === null ? null : new Date(this.checkedAt).toISOString(),
      error: this.error,
    }
  }

  // One stderr line when the zone or the failure changes, not on every lookup.
  private report(): void {
    const s = this.state()
    const key = `${s.zone}|${s.source}|${s.error ?? ''}`
    if (key === this.lastLogged) return
    this.lastLogged = key
    this.log(s.error
      ? `quiet hours: tripclock failed (${s.error}); using the home zone ${s.zone}`
      : `quiet hours: zone ${s.zone} (${s.source})`)
  }
}
