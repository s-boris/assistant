/**
 * The "/scan private" / "/sp" caption parser. Private documents go to the vault bot; when the owner sends one
 * here anyway, bridgedb.ts turns the event into a refusal note (meta handled="scan-private-refused"), so the
 * session never sees the file id, the caption or the title. A plain "/scan" caption is a normal message.
 * Pure: test/scan.test.ts imports it directly.
 */

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
