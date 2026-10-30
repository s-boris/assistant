// Outbox rules (v2 Phase 0, WP7). The consumer applies them to each queued row before sending it. They
// hold, drop repeats and record failures; they never decide what a message means: urgency, timing and
// the idem key are data the writer (the brain) puts on the row.
//
//   quiet hours  non-urgent send/poll rows wait while the owner's wall clock is inside config.quiet_hours
//                (default "0-8"; "" = off). The zone is `tripclock tz` through quiet.ts's QuietClock
//                (background lookup every 10 min; the home zone on any error).
//   sleep-in     non-urgent send/poll rows wait while `sleepin active` exits 0 (SleepinGuard: background
//                check every minute). Exit 1 = send; any other exit, a timeout or no sleepin = no hold
//                (fail-open: quiet hours still cover the night).
//   urgent       urgent = 1 bypasses both holds. It never bypasses a retry wait or the per-chat order.
//   edit/react   never held: they don't notify.
//   dedupe       idem_key: UNIQUE at enqueue, a repeat is never inserted. Text: a send/poll whose dedupe key
//                (send: text + details + reply_to; poll: question + options) equals that of an earlier
//                queued/sending/sent live row of the same bot, chat and op enqueued at most
//                config.dedupe_window_s before it (default 600; 0 = off) → status 'skipped',
//                last_error "duplicate of #N …". body.dedupe === false opts a row out.
//   order        per chat in id order: a row waiting for a retry (429 retry_after, 5xx backoff; it has
//                attempts) holds back that chat's later rows. Held rows (quiet hours, sleep-in) and
//                scheduled rows (not_before set at enqueue, no attempt yet) wait alone.
//   failures     every row that ends 'failed' gets an event row in the same DB (kind outbox.failed,
//                idem_key outbox.failed:<bot>:<row id>; ids, op and the error only, never message text).
import { createHash } from 'crypto'
import { accessSync, constants as fsConstants, statSync } from 'fs'
import { join } from 'path'
import { inQuietHours, runCommand, type Runner } from './quiet.ts'

export const DEFAULT_QUIET_HOURS = '0-8'
export const DEFAULT_DEDUPE_WINDOW_S = 600
export const SLEEPIN_REFRESH_MS = 60_000
export const SLEEPIN_TIMEOUT_MS = 5_000
/** Ops the holds apply to (the ones that notify). */
export const HELD_OPS = new Set(['send', 'poll', 'location'])

/** Rows the holds and the text dedupe never touch (P2.5, D-i): answers to an owner event (reply_to_event set)
 *  and the bridge's rows (body.bridge = true): the brain's sends stay as in 0.1.22 until it decides otherwise. */
export function exemptFromRules(row: { reply_to_event?: number | null; body: string }): boolean {
  if (row.reply_to_event != null) return true
  try { return JSON.parse(row.body)?.bridge === true } catch { return false }
}

/** body.hold = false: quiet hours and sleep-in never hold the row (the bridge sets it on every tool call;
 *  body.dedupe = false likewise turns off the text dedupe, see dedupeKey). */
export function holdOff(row: { body: string }): boolean {
  try { return JSON.parse(row.body)?.hold === false } catch { return false }
}

export type Rules = {
  /** "0-8" style window on the owner's wall clock; "" = off. */
  quietHours: string
  /** The owner's zone right now (never waits). */
  zone: () => string
  /** Is a sleep-in morning holding non-urgent messages right now (never waits)? */
  sleeping: () => boolean
  /** Text dedupe window in seconds; 0 = off. */
  dedupeWindowS: number
}

/** WP3's v0 behaviour: no holds, no text dedupe. */
export const NO_RULES: Rules = { quietHours: '', zone: () => 'UTC', sleeping: () => false, dedupeWindowS: 0 }

/** Why non-urgent send/poll rows wait right now (null = they don't). `now` in epoch seconds. */
export function holdReason(r: Rules, now: number): string | null {
  if (r.quietHours && inQuietHours(r.quietHours, now * 1000, r.zone())) return `quiet hours ${r.quietHours} ${r.zone()}`
  if (r.sleeping()) return 'sleep-in'
  return null
}

export const sha256 = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex')

/** The text a reader would see twice, hashed; null = the row is never text-deduped. */
export function dedupeKey(op: string, body: Record<string, unknown>): string | null {
  if (body.dedupe === false || body.approval != null) return null // P7: every approval card is its own
  if (op === 'send') {
    const text = typeof body.text === 'string' ? body.text : ''
    const details = typeof body.details === 'string' ? body.details : ''
    if (!text && !details) return null
    return sha256(JSON.stringify(['send', text, details, body.reply_to ?? null]))
  }
  if (op === 'poll') {
    if (typeof body.question !== 'string') return null
    return sha256(JSON.stringify(['poll', body.question, Array.isArray(body.options) ? body.options : []]))
  }
  return null
}

const isExecutableFile = (p: string): boolean => {
  try { accessSync(p, fsConstants.X_OK); return statSync(p).isFile() } catch { return false }
}

/** argv for the sleep-in check: TG_ADAPTER_SLEEPIN_CMD split on whitespace (no shell; a leading ~/
 *  expanded; "" off), else ~/bin/sleepin active when executable; null = no sleep-in holds. */
export function sleepinArgv(env: Record<string, string | undefined>, home: string): string[] | null {
  const v = env.TG_ADAPTER_SLEEPIN_CMD
  if (v === undefined) {
    const p = join(home, 'bin', 'sleepin')
    return isExecutableFile(p) ? [p, 'active'] : null
  }
  const argv = v.trim().split(/\s+/).filter(Boolean).map(a => a.replace(/^~(?=\/)/, home))
  return argv.length ? argv : null
}

export type SleepinState = { holding: boolean; checked_at: string | null; error: string | null }

/** `sleepin active` kept fresh in the background, like QuietClock: active() never waits. It reuses
 *  quiet.ts's runner (own process group, timeout, environment without the bot token), which resolves on
 *  exit 0 (= hold) and rejects with "exit N…" otherwise: exit 1 = send now, anything else = an error,
 *  which never holds. */
export class SleepinGuard {
  private holding = false
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

  constructor(readonly argv: string[] | null, opts: {
    refreshMs?: number; timeoutMs?: number; run?: Runner; now?: () => number; log?: (m: string) => void
  } = {}) {
    this.refreshMs = opts.refreshMs && opts.refreshMs > 0 ? opts.refreshMs : SLEEPIN_REFRESH_MS
    this.timeoutMs = opts.timeoutMs && opts.timeoutMs > 0 ? opts.timeoutMs : SLEEPIN_TIMEOUT_MS
    this.run = opts.run ?? runCommand
    this.now = opts.now ?? Date.now
    this.log = opts.log ?? (() => {})
  }

  active(): boolean {
    if (this.argv && !this.inflight && this.now() - this.lastTry >= this.refreshMs) void this.refresh()
    return this.holding
  }

  refresh(): Promise<void> {
    if (!this.argv) return Promise.resolve()
    if (this.inflight) return this.inflight
    this.lastTry = this.now()
    let p: Promise<string>
    try { p = this.run(this.argv, this.timeoutMs) } catch (e) { p = Promise.reject(e) }
    this.inflight = p.then(() => {
      this.holding = true
      this.error = null
    }).catch(e => {
      const msg = (e instanceof Error ? e.message : String(e)).slice(0, 200)
      this.holding = false
      this.error = /^exit 1\b/.test(msg) ? null : msg
    }).finally(() => {
      this.checkedAt = this.now()
      this.inflight = null
      this.settledOnce = true
      this.report()
    })
    return this.inflight
  }

  /** Waits (at most maxMs) for the first check, so the first outbox pass is an informed one. */
  async ready(maxMs = 2_000): Promise<void> {
    if (this.settledOnce || !this.argv) return
    const first = this.inflight ?? this.refresh()
    let t: ReturnType<typeof setTimeout> | undefined
    await Promise.race([first, new Promise<void>(r => { t = setTimeout(r, maxMs) })])
    clearTimeout(t)
  }

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

  state(): SleepinState {
    return { holding: this.holding, checked_at: this.checkedAt === null ? null : new Date(this.checkedAt).toISOString(), error: this.error }
  }

  private report(): void {
    const key = `${this.holding}|${this.error ?? ''}`
    if (key === this.lastLogged) return
    this.lastLogged = key
    this.log(this.error ? `sleep-in check failed (${this.error}): not holding` : `sleep-in: ${this.holding ? 'holding non-urgent messages' : 'not holding'}`)
  }
}
