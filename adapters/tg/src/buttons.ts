/**
 * Inline keyboard on_press modes that survive a restart.
 *
 * The server keeps every keyboard's on_press mode in memory. The two
 * non-default modes, 'keep' and 'remove', are also saved in buttons.json in
 * the state dir, so a press after a plugin restart still does what the
 * message asked for instead of falling back to the default 'mark'.
 *
 * File: {"<chat_id>:<message_id>": {"on_press": "keep"|"remove", "ts": <ms>}},
 * mode 0600. Only ids, the mode and a timestamp: never message text. Entries
 * older than BUTTONS_TTL_MS are dropped and at most BUTTONS_MAX (the newest)
 * are kept. Every write is read-merge-write through a temp file + rename, so a
 * reader never sees a half-written file; a missing or corrupt file reads as
 * empty and the next write replaces it.
 *
 * Pure file helpers, no Bot API or server state, so test/buttons.test.ts can
 * import them without starting the server.
 */
import { readFileSync, writeFileSync, renameSync, chmodSync, rmSync } from 'fs'

export type OnPress = 'mark' | 'remove' | 'keep'
export type SavedPress = { on_press: 'keep' | 'remove'; ts: number }
export type SavedPresses = Record<string, SavedPress>

export const BUTTONS_FILE_NAME = 'buttons.json'
export const BUTTONS_MAX = 500
export const BUTTONS_TTL_MS = 30 * 24 * 3600 * 1000

const KEY = /^-?\d+:\d+$/

export const pressKey = (chat_id: string, message_id: number) => `${chat_id}:${message_id}`

/** True for a mode worth saving: everything but the default 'mark'. */
export const isSaved = (m: unknown): m is SavedPress['on_press'] => m === 'keep' || m === 'remove'

/** Entries younger than the TTL (and not from the far future), the newest BUTTONS_MAX of them. */
export function prunePresses(entries: SavedPresses, now = Date.now(), max = BUTTONS_MAX, ttl = BUTTONS_TTL_MS): SavedPresses {
  const live = Object.entries(entries).filter(([, v]) => Math.abs(now - v.ts) < ttl)
  live.sort((a, b) => b[1].ts - a[1].ts)
  return Object.fromEntries(live.slice(0, Math.max(0, max)))
}

/** The saved entries (pruned). Missing, unreadable or malformed file → {}; malformed entries are skipped. */
export function readPresses(file: string, now = Date.now()): SavedPresses {
  let raw: unknown
  try { raw = JSON.parse(readFileSync(file, 'utf8')) } catch { return {} }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const out: SavedPresses = {}
  for (const [k, v] of Object.entries(raw as Record<string, any>)) {
    if (KEY.test(k) && v && isSaved(v.on_press) && Number.isFinite(v.ts)) out[k] = { on_press: v.on_press, ts: v.ts }
  }
  return prunePresses(out, now)
}

/** Write `entries` (pruned) atomically, 0600. */
export function writePresses(file: string, entries: SavedPresses, now = Date.now()): void {
  const tmp = `${file}.tmp-${process.pid}`
  try {
    writeFileSync(tmp, JSON.stringify(prunePresses(entries, now)) + '\n', { mode: 0o600 })
    try { chmodSync(tmp, 0o600) } catch {}
    renameSync(tmp, file)
  } catch (err) {
    try { rmSync(tmp, { force: true }) } catch {}
    throw err
  }
}

/** Saved mode for a message, if any. */
export function lookupPress(file: string, key: string, now = Date.now()): SavedPress['on_press'] | undefined {
  return readPresses(file, now)[key]?.on_press
}

/**
 * Record a message's on_press. 'keep'/'remove' are saved; anything else
 * ('mark', the default) removes a saved entry for that message, so a keyboard
 * re-sent by edit_message without on_press goes back to the default. Writes
 * only when something changes; a key that isn't "chat:message" is ignored.
 * Throws on a write error (the caller logs it).
 */
export function savePress(file: string, key: string, onPress: unknown, now = Date.now()): void {
  if (!KEY.test(key)) return
  const entries = readPresses(file, now)
  if (isSaved(onPress)) entries[key] = { on_press: onPress, ts: now }
  else if (key in entries) delete entries[key]
  else return
  writePresses(file, entries, now)
}

/** Forget a message's saved mode (after a non-'keep' press). Writes only when it was saved. */
export function forgetPress(file: string, key: string, now = Date.now()): void {
  savePress(file, key, undefined, now)
}
