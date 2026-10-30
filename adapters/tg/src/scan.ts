/**
 * Private document intake (telegram-plus 0.1.11)
 *
 * The owner sends a photo or a document (PDF) whose caption starts with
 * "/scan private" or "/sp" (optionally followed by a title). The plugin files
 * it into paperless itself and the Claude session never sees the file, the
 * caption's title or any of the content: it gets a one-line note (meta
 * handled="scan-private", doc_id) at most. A plain "/scan" caption is a normal
 * message, as before (the session files it with the paperless skill).
 *
 * Pure helpers, no Bot API or server state, so test/scan.test.ts can import
 * them without starting the server. The server side (download to a 0700 temp
 * dir, `paperless upload`, cleanup, replies) is in server.ts ("Private scans").
 */

/** paperless-ngx's limit for a document title. */
export const SCAN_TITLE_MAX = 128

export type ScanRequest = {
  /** The command as the session may see it: never the title. */
  command: '/scan private' | '/sp'
  /** The caption after the command, whitespace collapsed ('' = none). */
  title: string
}

/** "/scan private [title]" or "/sp [title]" (any case; "/scan@<bot> private"
 *  and "/sp@<bot>" only for this bot), else null. "/scan", "/scan privately",
 *  "/spam" and "/sp@otherbot" are not private scans. */
export function parseScanPrivate(caption: string | undefined | null, botUsername: string): ScanRequest | null {
  if (!caption) return null
  const m = /^\/(?:(scan)(?:@([A-Za-z0-9_]+))?\s+private|sp(?:@([A-Za-z0-9_]+))?)(?=\s|$)([\s\S]*)$/i.exec(caption.trim())
  if (!m) return null
  const at = m[2] ?? m[3]
  if (at && at.toLowerCase() !== botUsername.toLowerCase()) return null
  return { command: m[1] ? '/scan private' : '/sp', title: cleanTitle(m[4] ?? '') }
}

function cleanTitle(s: string): string {
  return s.replace(/[\s\u0000-\u001f\u007f]+/g, ' ').trim()
}

/** At most n characters (code points, so an emoji isn't cut in half). */
function capChars(s: string, n: number): string {
  const cps = Array.from(s)
  return cps.length <= n ? s : cps.slice(0, n).join('').trimEnd()
}

const pad = (n: number) => String(n).padStart(2, '0')

/** "YYYY-MM-DD HH:MM" in the server's local time zone (the owner's). */
export function localStamp(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** The paperless title: the caption's title, else "Scan YYYY-MM-DD HH:MM";
 *  part 2, 3, … (later photos of the same album) get " (2)", " (3)", …
 *  At most SCAN_TITLE_MAX characters. */
export function scanTitle(title: string, date: Date, part = 1): string {
  const base = cleanTitle(title) || `Scan ${localStamp(date)}`
  const suffix = part > 1 ? ` (${part})` : ''
  return capChars(base, SCAN_TITLE_MAX - suffix.length) + suffix
}

/** The document id in `paperless upload`'s output ("document <id>: <url>"), else null. */
export function scanDocId(stdout: string): string | null {
  return /^document (\d+)\b/m.exec(stdout)?.[1] ?? null
}

/** A file name for the temp copy: the document's own name (paperless keeps it
 *  as the original file name) made safe, else "scan-<stamp>.<ext>". */
export function scanFileName(name: string | undefined, ext: string, date: Date): string {
  const safeExt = ext.replace(/[^A-Za-z0-9]/g, '').slice(0, 8) || 'bin'
  const base = (name ?? '').split(/[\\/]/).pop()!.replace(/[^\p{L}\p{N}._() -]/gu, '_').replace(/^[.\s-]+/, '').trim()
  if (base) {
    const capped = capChars(base, 100)
    return /\.[A-Za-z0-9]{1,8}$/.test(capped) ? capped : `${capped}.${safeExt}`
  }
  const s = localStamp(date)
  return `scan-${s.slice(0, 10).replace(/-/g, '')}-${s.slice(11).replace(':', '')}.${safeExt}`
}

/** Why `paperless upload` failed, short and for the owner only: runLocal's
 *  error ("exit 1: paperless: …", "timed out after 300 s", a spawn error)
 *  without the exit code and the tool's name. A paperless that is still
 *  consuming the file when the upload command gives up (exit 2) is said so. */
export function scanFailReason(error: string | undefined, stdout = ''): string {
  let e = (error ?? '').replace(/\s+/g, ' ').trim()
  const exit = /^exit (\d+)(?:: )?/.exec(e)
  if (exit) e = e.slice(exit[0].length)
  for (let i = 0; i < 2; i++) e = e.replace(/^(?:paperless|vault)(?: paperless)?: /i, '')
  if (exit?.[1] === '2' && /timed out .*waiting for task/i.test(e)) return 'paperless is still processing it (it may show up later)'
  if (/^timed out after/.test(e)) return `paperless upload ${e}`
  if (!e && exit) e = stdout.trim() ? 'no document id in the answer' : `exit ${exit[1]}`
  if (!e) e = 'no document id in the answer'
  return e.length <= 160 ? e : e.slice(0, 159) + '…'
}
