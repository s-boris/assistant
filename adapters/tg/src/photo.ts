/**
 * Photo or document? (telegram-plus 0.1.9)
 *
 * Telegram's sendPhoto (and photos in a sendMediaGroup album) refuses images
 * over 10 MB, with width + height over 10000, or with an aspect ratio over 20.
 * A 1905x8710 page screenshot, say, fails with PHOTO_INVALID_DIMENSIONS. Such
 * images go as documents instead, and so do images Telegram would accept but
 * shrink until they can't be read: an aspect ratio over 4 (a 1080x7000 phone
 * screenshot arrives as a ~400x2560 photo). Decided up front from the file
 * size and the dimensions in the file header (PNG, JPEG, GIF, WebP; a few
 * small reads, no image library), and if Telegram still refuses one as a
 * photo, it is sent once more as a document.
 *
 * Pure helpers, no Bot API or server state, so test/photo.test.ts can import
 * them without starting the server.
 */
import { openSync, readSync, closeSync, statSync } from 'fs'

/** Telegram: "The photo must be at most 10 MB in size" (decimal MB, to be safe). */
export const PHOTO_MAX_BYTES = 10_000_000
/** Telegram: "The photo's width and height must not exceed 10000 in total." */
export const PHOTO_MAX_SIDES = 10000
/** Telegram: "Width and height ratio must be at most 20." */
export const PHOTO_MAX_RATIO = 20
/** Our own limit, below Telegram's: photos are downscaled to about 2560 px on
 *  the long side, so a taller or wider image than this (long phone screenshots,
 *  1080x7000 → ~400x2560) becomes unreadable and goes as a document instead.
 *  1080x2400 (2.2) and 4:3 / 16:9 photos stay photos. */
export const PHOTO_READABLE_RATIO = 4

export type Dims = { width: number; height: number }
type Read = (pos: number, len: number) => Buffer

/** Pixel size from the file header, or null (unknown format, truncated, unreadable). */
export function imageDims(path: string): Dims | null {
  let fd: number
  try { fd = openSync(path, 'r') } catch { return null }
  try {
    const read: Read = (pos, len) => {
      const b = Buffer.alloc(len)
      return b.subarray(0, readSync(fd, b, 0, len, pos))
    }
    const d = parseDims(read)
    return d && d.width > 0 && d.height > 0 ? d : null
  } catch {
    return null
  } finally {
    try { closeSync(fd) } catch {}
  }
}

function parseDims(read: Read): Dims | null {
  const h = read(0, 32)
  // PNG: signature, then the IHDR chunk (width, height: uint32 BE).
  if (h.length >= 24 && h.readUInt32BE(0) === 0x89504e47 && h.readUInt32BE(4) === 0x0d0a1a0a
    && h.toString('latin1', 12, 16) === 'IHDR') {
    return { width: h.readUInt32BE(16), height: h.readUInt32BE(20) }
  }
  // GIF87a / GIF89a: logical screen width, height (uint16 LE).
  if (h.length >= 10 && h.toString('latin1', 0, 4) === 'GIF8') {
    return { width: h.readUInt16LE(6), height: h.readUInt16LE(8) }
  }
  // WebP: RIFF....WEBP, then a VP8 / VP8L / VP8X chunk.
  if (h.length >= 30 && h.toString('latin1', 0, 4) === 'RIFF' && h.toString('latin1', 8, 12) === 'WEBP') {
    const kind = h.toString('latin1', 12, 16)
    if (kind === 'VP8 ' && h[23] === 0x9d && h[24] === 0x01 && h[25] === 0x2a) {
      return { width: h.readUInt16LE(26) & 0x3fff, height: h.readUInt16LE(28) & 0x3fff }
    }
    if (kind === 'VP8L' && h[20] === 0x2f) {
      const bits = h.readUInt32LE(21)
      return { width: (bits & 0x3fff) + 1, height: ((bits >>> 14) & 0x3fff) + 1 }
    }
    if (kind === 'VP8X') {
      return { width: h.readUIntLE(24, 3) + 1, height: h.readUIntLE(27, 3) + 1 }
    }
    return null
  }
  // JPEG: SOI, then walk the segments (skipping each by its length, so an
  // EXIF thumbnail's frame header is never mistaken for the image's) up to
  // the first SOFn frame header.
  if (h.length >= 4 && h[0] === 0xff && h[1] === 0xd8) return jpegDims(read)
  return null
}

function jpegDims(read: Read): Dims | null {
  let pos = 2
  for (let i = 0; i < 10000; i++) {
    const b = read(pos, 9)
    if (b.length < 2 || b[0] !== 0xff) return null
    const m = b[1]!
    if (m === 0xff) { pos++; continue } // fill byte
    if (m === 0x01 || m === 0xd8 || (m >= 0xd0 && m <= 0xd7)) { pos += 2; continue } // no length field
    if (m === 0xd9 || m === 0xda) return null // end of image / start of scan before any frame header
    if (b.length < 4) return null
    const len = b.readUInt16BE(2)
    if (len < 2) return null
    // SOF0-SOF15 except DHT (C4), JPG (C8), DAC (CC): precision, height, width.
    if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) {
      if (b.length < 9) return null
      return { width: b.readUInt16BE(7), height: b.readUInt16BE(5) }
    }
    pos += 2 + len
  }
  return null
}

/** Why an image of this size and these dimensions shouldn't go as a Telegram
 *  photo (Telegram would refuse it, or shrink it unreadably), or null if it
 *  (probably) can. Unknown dimensions: null. */
export function photoProblem(bytes: number, dims: Dims | null): string | null {
  if (bytes > PHOTO_MAX_BYTES) return `${(bytes / 1e6).toFixed(1)} MB, photos max 10 MB`
  if (!dims) return null
  const { width: w, height: h } = dims
  if (w + h > PHOTO_MAX_SIDES) return `${w}x${h}, photos max 10000 px width + height`
  const ratio = Math.max(w, h) / Math.min(w, h)
  if (ratio > PHOTO_MAX_RATIO) return `${w}x${h}, photos max aspect ratio 20`
  if (ratio > PHOTO_READABLE_RATIO) {
    return `${w}x${h}, too ${h > w ? 'tall' : 'wide'} to stay readable as a photo (aspect ratio ${ratio.toFixed(1)} > ${PHOTO_READABLE_RATIO})`
  }
  return null
}

/** photoProblem() for a file on disk. */
export function photoCheck(path: string): string | null {
  let bytes: number
  try { bytes = statSync(path).size } catch { return null }
  return photoProblem(bytes, imageDims(path))
}

/** Telegram refused the upload as a photo (dimensions, size, image format),
 *  so the same file will go through as a document. Bot API errors 400/413
 *  only: network errors, rate limits (429), blocked bots (403) or a bad
 *  chat/reply id would fail (or duplicate) as a document too. */
export function isPhotoRejection(err: unknown): boolean {
  const e = err as { error_code?: unknown; description?: unknown } | null
  if (!e || (e.error_code !== 400 && e.error_code !== 413)) return false
  return /\bPHOTO_[A-Z_]+|IMAGE_PROCESS_FAILED|Request Entity Too Large|too (big|large)/i.test(String(e.description ?? ''))
}

/** Run send(true) (as photos); if Telegram refuses that as a photo, run
 *  send(false) (as documents) once. asPhoto false: send(false) only. */
export async function withDocumentFallback<T>(
  asPhoto: boolean,
  send: (asPhoto: boolean) => Promise<T>,
): Promise<{ result: T; asPhoto: boolean; rejection?: string }> {
  if (!asPhoto) return { result: await send(false), asPhoto: false }
  try {
    return { result: await send(true), asPhoto: true }
  } catch (err) {
    if (!isPhotoRejection(err)) throw err
    const rejection = String((err as { description?: unknown }).description ?? err)
    return { result: await send(false), asPhoto: false, rejection }
  }
}
