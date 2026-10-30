/**
 * Instant buttons (0.1.19): the owner taps an inline button whose
 * callback_data is in a table, and the plugin runs a local script at once,
 * without a Claude turn (e.g. a "📞 Talk" button on a pinned board that
 * rings the owner through a voice relay). Buttons on any message work,
 * including ones another process sent through the Bot API.
 *
 * The table is callbacks.json in the state dir (TELEGRAM_CALLBACKS_FILE;
 * "" = off; no file = no instant buttons), re-read when it changes. A JSON
 * array:
 *   { "name": "talk", "data": "call:talk", "run": "~/bin/talk-button",
 *     "toast": "📞 Calling you…", "timeout_ms": 120000, "note": false }
 * data: the exact callback_data (1-64 bytes), or an array of them; "perm:…"
 * is the permission relay's and refused, as is data an earlier entry took.
 * run: a command line (split on whitespace, leading ~/ expanded) or an argv
 * array; no shell. toast: the short popup shown at once (≤ 200 chars;
 * default "✓ <button text>"). timeout_ms: default 20 s, at most 5 min.
 * note: false (default) = a successful run is not reported to the session
 * at all; true = the session gets the button event with handled=<name>.
 * The keyboard is never touched (whatever its on_press), so the button can be
 * tapped again. One run per entry at a time: a tap while it runs only gets
 * BUSY_TOAST. The script's stdout (trimmed, ≤ REPLY_MAX chars) is sent as a
 * silent reply to the button's message; nothing printed = nothing sent.
 * Exit != 0 or a timeout: the owner gets the stdout or a short ⚠️ line, and
 * the session gets the button event with instant_error. Invalid entries are
 * skipped and logged once per change.
 */

export const CALLBACK_TIMEOUT_MS = 20_000
export const CALLBACK_TIMEOUT_MAX_MS = 300_000
export const TOAST_MAX = 200
export const REPLY_MAX = 1000
export const BUSY_TOAST = '⏳ Already running'

export type InstantButton = {
  name: string
  data: string[]
  argv: string[]
  toast: string
  timeoutMs: number
  note: boolean
}

export type CallbackDeps = {
  /** A command line → argv (whitespace split, ~/ expanded). */
  splitArgv: (s: string) => string[]
  home: string
}

const bytes = (s: string) => new TextEncoder().encode(s).length

/** Parse callbacks.json's content into valid entries plus the reasons for skipped ones. */
export function parseCallbacks(raw: unknown, deps: CallbackDeps): { list: InstantButton[]; skipped: string[] } {
  const list: InstantButton[] = []
  const skipped: string[] = []
  if (!Array.isArray(raw)) return { list, skipped: ['the whole file (not a JSON array)'] }
  const taken = new Map<string, string>()
  for (const [i, e] of raw.entries()) {
    const name = String(e?.name ?? '').trim().toLowerCase()
    const dataRaw: unknown[] = typeof e?.data === 'string' ? [e.data] : Array.isArray(e?.data) ? e.data : []
    const data = [...new Set(dataRaw.filter((x): x is string => typeof x === 'string'))]
    const argv = typeof e?.run === 'string' ? deps.splitArgv(e.run)
      : Array.isArray(e?.run) && e.run.length && e.run.every((a: unknown) => typeof a === 'string')
        ? (e.run as string[]).map(a => a.replace(/^~(?=\/)/, deps.home))
        : []
    const toast = e?.toast === undefined ? '' : typeof e.toast === 'string' ? e.toast.trim() : null
    const badData = data.filter(d => bytes(d) < 1 || bytes(d) > 64)
    // "rm:*" = every callback_data starting with "rm:" (at least 2 bytes before the *); exact entries win
    const perm = data.filter(d => d.startsWith('perm:') || (d.endsWith('*') && 'perm:'.startsWith(d.slice(0, -1))))
    const shortPrefix = data.filter(d => d.endsWith('*') && bytes(d.slice(0, -1)) < 2)
    const dup = data.filter(d => taken.has(d))
    const why = !/^[a-z0-9_-]{1,32}$/.test(name) ? `invalid name "${name.slice(0, 40)}"`
      : list.some(c => c.name === name) ? `"${name}" twice`
      : !data.length ? `"${name}": no "data"`
      : badData.length ? `"${name}": data must be 1-64 bytes`
      : perm.length ? `"${name}": "perm:" data is the permission relay's`
      : shortPrefix.length ? `"${name}": a prefix ("x*") needs at least 2 bytes before the *`
      : dup.length ? `"${name}": data "${dup[0]}" already used by "${taken.get(dup[0]!)}"`
      : !argv.length ? `"${name}": no "run"`
      : toast === null || toast.length > TOAST_MAX ? `"${name}": toast must be text up to ${TOAST_MAX} chars`
      : e.note !== undefined && typeof e.note !== 'boolean' ? `"${name}": note must be true or false`
      : null
    if (why) { skipped.push(`#${i + 1} (${why})`); continue }
    for (const d of data) taken.set(d, name)
    const t = Number(e.timeout_ms)
    list.push({
      name,
      data,
      argv,
      toast: toast ?? '',
      timeoutMs: Number.isFinite(t) && t > 0 ? Math.min(t, CALLBACK_TIMEOUT_MAX_MS) : CALLBACK_TIMEOUT_MS,
      note: e.note === true,
    })
  }
  return { list, skipped }
}

/** The instant button for a press's callback_data: an exact match, else the longest "prefix*" entry, if any. */
export function pickCallback(list: InstantButton[], data: string): InstantButton | null {
  if (data.startsWith('perm:')) return null
  const exact = list.find(c => c.data.includes(data))
  if (exact) return exact
  let best: InstantButton | null = null
  let len = 0
  for (const c of list) {
    for (const d of c.data) {
      if (d.endsWith('*') && d.length - 1 > len && data.startsWith(d.slice(0, -1))) {
        best = c
        len = d.length - 1
      }
    }
  }
  return best
}

/** The toast for a press: the entry's own, else "✓ <button text>", at most TOAST_MAX chars. */
export function toastFor(cb: InstantButton, label: string): string {
  const t = cb.toast || `✓ ${label}`
  return t.length > TOAST_MAX ? `${t.slice(0, TOAST_MAX - 1)}…` : t
}

/** What the owner gets from a script's stdout: trimmed, at most max chars ('' = send nothing). */
export function replyText(stdout: string, max = REPLY_MAX): string {
  const t = stdout.trim()
  return t.length > max ? `${t.slice(0, max - 1)}…` : t
}
