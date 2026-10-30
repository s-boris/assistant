/**
 * Picture answers for instant commands (telegram-plus 0.1.18)
 *
 * An instant command may answer with an image instead of text: its whole
 * stdout is one JSON object
 *   {"photo": "/abs/path.png", "caption": "short text", "text": "longer text"}
 * "photo" (required): an absolute path to the image. "caption" (optional):
 * sent with the photo (Telegram allows up to 1024 chars; a longer caption
 * goes as a text message after the photo). "text" (optional): the fallback
 * the owner gets instead of the picture when it can't be sent; without it the
 * caption is the fallback, and without both a short "⚠️" line.
 *
 * Only images under the picture roots are sent: ~/.local/state/cardimg and
 * /tmp, checked after resolving symlinks and "..", so a script can't send
 * just any file of the user's (only the owner ever gets the answer, but a
 * stray path shouldn't leak a file into the chat either). It must be a
 * regular, non-empty file with a photo extension that Telegram takes as a
 * photo (photo.ts's size and aspect-ratio checks); anything else falls back
 * to the text.
 *
 * Pure helpers, no Bot API or server state, so test/picture.test.ts can
 * import them without starting the server.
 */
import { realpathSync, statSync } from 'fs'
import { isAbsolute, join, sep, extname } from 'path'
import { photoCheck } from './photo.ts'

/** Telegram: photo captions are at most 1024 characters. */
export const CAPTION_MAX = 1024
export const PICTURE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.webp'])

export type Picture = { photo: string; caption: string; text: string }

/** The picture an instant command's stdout asks for, or null (plain text, as
 *  before). Only a stdout that is exactly one JSON object with a non-empty
 *  string "photo" counts; "caption" and "text" must be strings if present. */
export function parsePicture(stdout: string): Picture | null {
  const s = stdout.trim()
  if (!s.startsWith('{') || !s.endsWith('}')) return null
  let o: any
  try { o = JSON.parse(s) } catch { return null }
  if (!o || typeof o !== 'object' || Array.isArray(o)) return null
  if (typeof o.photo !== 'string' || !o.photo.trim()) return null
  if (o.caption !== undefined && typeof o.caption !== 'string') return null
  if (o.text !== undefined && typeof o.text !== 'string') return null
  return { photo: o.photo.trim(), caption: (o.caption ?? '').trim(), text: (o.text ?? '').trim() }
}

/** The directories picture answers may come from (symlinks resolved; a root
 *  that doesn't exist yet is kept as given, so it simply matches nothing). */
export function pictureRoots(home: string): string[] {
  return [join(home, '.local', 'state', 'cardimg'), '/tmp'].map(r => {
    try { return realpathSync(r) } catch { return r }
  })
}

/** Why this path can't be sent as the picture, or null if it can.
 *  Resolves symlinks and ".." first, then requires a root as its prefix. */
export function pictureProblem(path: string, roots: string[]): string | null {
  if (!isAbsolute(path)) return 'photo path is not absolute'
  let real: string
  try { real = realpathSync(path) } catch { return 'photo file is missing' }
  if (!roots.some(r => real.startsWith(r.endsWith(sep) ? r : r + sep))) return 'photo path is outside the allowed folders'
  if (!PICTURE_EXTS.has(extname(real).toLowerCase())) return 'photo is not a .png/.jpg/.webp file'
  let st
  try { st = statSync(real) } catch { return 'photo file is missing' }
  if (!st.isFile()) return 'photo is not a regular file'
  if (st.size === 0) return 'photo file is empty'
  return photoCheck(real)
}

/** The text the owner gets when the picture can't be sent. */
export function pictureFallback(p: Picture, command: string, why: string): string {
  return p.text || p.caption || `⚠️ ${command}: the picture couldn't be sent (${why}).`
}
