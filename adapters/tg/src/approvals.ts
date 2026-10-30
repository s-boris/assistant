// Approvals (v2 Phase 1 P7): one-time ✅/❌ cards. A send row's `approval` field ({action, summary, ttl_s} from the
// bridge's reply tool, or {nonce} made beforehand with `approvals new`) becomes a pending nonce in approvals.db
// (schemas/approvals.sql = src/approvals.sql; the Python side is packages/approvals) and a ✅/❌ button row with the
// callback data ap:<nonce>:y|n. His tap decides it here, at the callback answer: only his user id (the first
// allowFrom entry) in his private chat, before the expiry, once. The tap becomes an approval.decided row (delivery
// now: the bridge hands it to the brain as a channel event). A pending card past its expiry gets its keyboard swapped
// for one inert "⌛ expired" button. The action string is never stored: its sha256 and a summary of ≤ 80 chars are.
// Using an approval is `approvals consume NONCE --action …` (once, same action, before the expiry), never the adapter.
// Switch: ~/.config/tg-adapter/approvals-off (TG_ADAPTER_APPROVALS_OFF): taps on ap: buttons go to the brain as plain
// tg.button rows (the approvals CLI then reads his tap from the compat inbound journal, as in poll mode) and no
// expiry edits run.
import { Database } from 'bun:sqlite'
import { createHash, randomBytes } from 'crypto'
import { chmodSync, existsSync, mkdirSync } from 'fs'
import { dirname, join } from 'path'

// A text import: inlined by `bun build` (the hub's tg/adapter.js), read from disk when the source runs as is.
import approvalsSql from './approvals.sql' with { type: 'text' }
export const APPROVALS_SQL: string = approvalsSql
export const AP_RE = /^ap:([0-9a-f]{32}):([ynx])$/
export const NONCE_RE = /^[0-9a-f]{32}$/
export const DEFAULT_TTL_S = 900
export const MIN_TTL_S = 1
export const MAX_TTL_S = 86_400
export const SUMMARY_MAX = 80
export const ACTION_MAX = 8192
export const REF_RE = /^[\w:.@/+-]{1,80}$/
export const YES_LABEL = '✅ Approve'
export const NO_LABEL = '❌ Decline'
export const EXPIRED_LABEL = '⌛ expired'
export const REPLACED_LABEL = '⌛ replaced'
export const APPROVALS_OFF = 'approvals-off'
export const KEEP_DAYS = 30

export type ApprovalRow = {
  nonce: string; action_sha: string; summary: string; ref: string | null; chat: string | null; message_id: number | null
  created: number; expires: number; status: string; decided_ts: number | null; decided_by: string | null
  decided_via: string | null; used_ts: number | null; used_by: string | null; refused: number; card: string | null
}

/** What a tap on an ap: button came to. approved/declined also on a repeat tap (repeat: true); error: the store
 *  failed, nothing was recorded (the keyboard stays). */
export type TapOutcome = 'approved' | 'declined' | 'expired' | 'cancelled' | 'unknown' | 'not owner' | 'error'
export type Tap = { nonce: string; choice: 'y' | 'n' | 'x'; outcome: TapOutcome; repeat: boolean; row: ApprovalRow | null; toast: string }

export const sha256 = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex')

export function oneLine(s: unknown, n: number): string {
  const t = String(s ?? '').split(/\s+/).filter(Boolean).join(' ')
  return t.length <= n ? t : t.slice(0, n - 1) + '…'
}

export function parseApprovalData(data: string): { nonce: string; choice: 'y' | 'n' | 'x' } | null {
  const m = AP_RE.exec(data)
  return m ? { nonce: m[1]!, choice: m[2] as 'y' | 'n' | 'x' } : null
}

/** Every approval nonce a keyboard carries (ap:<nonce>:y|n buttons). */
export function noncesIn(buttons: unknown): string[] {
  if (!Array.isArray(buttons)) return []
  const flat = (buttons.length > 0 && !Array.isArray(buttons[0]) ? buttons : buttons.flat()) as unknown[]
  const out = new Set<string>()
  for (const b of flat) {
    if (!b || typeof b !== 'object') continue
    const s = b as Record<string, unknown>
    const p = parseApprovalData(String(s.data ?? s.callback_data ?? ''))
    if (p && p.choice !== 'x') out.add(p.nonce)
  }
  return [...out]
}

export const hm = (ts: number) => new Date(ts * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false })

export const TOAST: Record<TapOutcome, string> = {
  approved: '✅ Approved', declined: '❌ Declined', expired: '⌛ Expired: nothing was approved',
  cancelled: '⌛ Replaced by a newer card: nothing was approved', unknown: '⌛ No longer valid: nothing was approved',
  'not owner': 'Not authorized.', error: '⚠️ Not recorded: tap again',
}

export class Approvals {
  private dbh: Database | null = null

  constructor(readonly path: string, private readonly clock: () => number = () => Date.now() / 1000) {}

  /** The store exists (opened here, or created by the CLI). Readers never create it. */
  exists(): boolean {
    return this.dbh != null || existsSync(this.path)
  }

  db(): Database {
    if (this.dbh) return this.dbh
    const old = process.umask(0o077)
    try {
      mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 })
      const db = new Database(this.path, { create: true })
      db.exec('PRAGMA journal_mode=WAL')
      db.exec('PRAGMA synchronous=FULL')
      db.exec('PRAGMA busy_timeout=5000')
      db.exec(APPROVALS_SQL)
      for (const f of [this.path, `${this.path}-wal`, `${this.path}-shm`]) {
        try { if (existsSync(f)) chmodSync(f, 0o600) } catch {}
      }
      this.dbh = db
      return db
    } finally {
      process.umask(old)
    }
  }

  close(): void {
    try { this.dbh?.close() } catch {}
    this.dbh = null
  }

  now(): number {
    return this.clock()
  }

  /** A pending approval for this exact action (the string is hashed, never kept). Throws on bad input. */
  create(o: { action: unknown; summary: unknown; ttlS?: unknown; ref?: unknown; chat?: string }): ApprovalRow {
    if (typeof o.action !== 'string' || !o.action.trim()) throw new Error('approval.action is required (the canonical action string)')
    if (o.action.length > ACTION_MAX) throw new Error(`approval.action is longer than ${ACTION_MAX} characters`)
    const summary = oneLine(o.summary, SUMMARY_MAX)
    if (!summary) throw new Error('approval.summary is required (≤ 80 chars)')
    const ttl = o.ttlS == null ? DEFAULT_TTL_S : Number(o.ttlS)
    if (!Number.isInteger(ttl) || ttl < MIN_TTL_S || ttl > MAX_TTL_S) throw new Error(`approval.ttl_s must be ${MIN_TTL_S}-${MAX_TTL_S} seconds`)
    const ref = o.ref == null || o.ref === '' ? null : String(o.ref)
    if (ref != null && !REF_RE.test(ref)) throw new Error('approval.ref: 1-80 of letters, digits and :.@/+-_')
    const now = this.clock()
    const nonce = randomBytes(16).toString('hex')
    const db = this.db()
    db.transaction(() => {
      db.query(`INSERT INTO approvals(nonce, action_sha, summary, ref, chat, created, expires) VALUES (?, ?, ?, ?, ?, ?, ?)`)
        .run(nonce, sha256(o.action as string), summary, ref, o.chat ?? null, now, now + ttl)
      db.query('DELETE FROM approvals WHERE created < ?').run(now - KEEP_DAYS * 86_400)
    })()
    return this.get(nonce)!
  }

  get(nonce: string): ApprovalRow | null {
    if (!NONCE_RE.test(nonce) || !this.exists()) return null
    return this.db().query('SELECT * FROM approvals WHERE nonce = ?').get(nonce) as ApprovalRow | null
  }

  /** The card carrying a pending approval (the newest wins: the expiry edit uses it). False: unknown, decided, or
   *  already bound to another chat. */
  attach(nonce: string, chat: string, message_id: number): boolean {
    if (!this.get(nonce)) return false
    return this.db().query(`UPDATE approvals SET chat = ?, message_id = ? WHERE nonce = ? AND status = 'pending'
      AND (chat IS NULL OR chat = ?)`).run(chat, message_id, nonce, chat).changes === 1
  }

  /** His tap on ap:<nonce>:<choice>. Decides a pending approval (his user id in his own private chat, before the
   *  expiry, once); a later tap reports the first decision (repeat). Nothing else ever decides here. */
  tap(nonce: string, choice: 'y' | 'n' | 'x', who: { userId: string; chatId: string; owner: string }): Tap {
    const out = (outcome: TapOutcome, row: ApprovalRow | null, repeat = false): Tap =>
      ({ nonce, choice, outcome, repeat, row, toast: repeat ? `✓ Already ${outcome}` : TOAST[outcome] })
    if (!who.owner || who.userId !== who.owner || who.chatId !== who.owner) return out('not owner', null)
    let r = this.get(nonce)
    if (!r) return out('unknown', null)
    if (r.chat && r.chat !== who.chatId) return out('not owner', r)
    const now = this.clock()
    if (r.status === 'pending' && choice !== 'x') {
      const status = choice === 'y' ? 'approved' : 'denied'
      const changed = this.db().query(`UPDATE approvals SET status = ?, decided_ts = ?, decided_by = ?, decided_via = 'adapter'
        WHERE nonce = ? AND status = 'pending' AND expires > ?`).run(status, now, who.userId, nonce, now).changes === 1
      r = this.get(nonce)!
      if (changed) return out(status === 'approved' ? 'approved' : 'declined', r)
    }
    if (r.status === 'pending' && now >= r.expires) {
      this.expire(nonce)
      r = this.get(nonce)!
    }
    if (r.status === 'approved') return out('approved', r, true)
    if (r.status === 'denied') return out('declined', r, true)
    if (r.status === 'cancelled') return out('cancelled', r)
    return out('expired', r)
  }

  /** pending → expired, only once it is past its expiry. */
  expire(nonce: string): boolean {
    return this.db().query("UPDATE approvals SET status = 'expired' WHERE nonce = ? AND status = 'pending' AND expires <= ?")
      .run(nonce, this.clock()).changes === 1
  }

  /** Pending approvals past their expiry whose card was not edited yet (oldest first). */
  due(limit = 20): ApprovalRow[] {
    if (!this.exists()) return []
    return this.db().query(`SELECT * FROM approvals WHERE status = 'pending' AND expires <= ? AND message_id IS NOT NULL
      AND chat IS NOT NULL AND card IS NULL ORDER BY expires LIMIT ?`).all(this.clock(), limit) as ApprovalRow[]
  }

  markCard(nonce: string, state: 'edited' | 'failed'): void {
    this.db().query('UPDATE approvals SET card = ? WHERE nonce = ?').run(state, nonce)
  }

  /** Housekeeping: pending rows past expiry without a card → expired; rows older than KEEP_DAYS go. */
  sweepStale(): { expired: number; pruned: number } {
    if (!this.exists()) return { expired: 0, pruned: 0 }
    const now = this.clock()
    const db = this.db()
    const expired = db.query("UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND expires <= ? AND message_id IS NULL").run(now).changes
    const pruned = db.query('DELETE FROM approvals WHERE created < ?').run(now - KEEP_DAYS * 86_400).changes
    return { expired, pruned }
  }
}

/** The ✅/❌ row a card gets for its approval. */
export function approvalRow(nonce: string, yes?: unknown, no?: unknown): { text: string; data: string }[] {
  const label = (v: unknown, d: string) => (typeof v === 'string' && v.trim() ? oneLine(v, 40) : d)
  return [{ text: label(yes, YES_LABEL), data: `ap:${nonce}:y` }, { text: label(no, NO_LABEL), data: `ap:${nonce}:n` }]
}

/** The approvals store of an instance: APPROVALS_DB (tests), else live → beside the main events DB
 *  (~/.local/state/assistant/approvals.db), test → its own state dir. */
export function approvalsDbFor(env: Record<string, string | undefined>, instance: string, eventsDb: string, stateDir: string, resolveP: (p: string) => string): string {
  if (env.APPROVALS_DB) return resolveP(env.APPROVALS_DB)
  return instance === 'live' ? join(dirname(eventsDb), 'approvals.db') : join(stateDir, 'approvals.db')
}
