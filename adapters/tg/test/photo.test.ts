// Unit tests for photo.ts (0.1.9): image header parsing, the photo-or-document
// decision (Telegram's limits, plus the readable aspect ratio) and the
// retry-as-document path with a mocked Bot API call.
// Run:  bun test   (offline.ts covers the same through the server, end to end)
import { describe, expect, test, afterAll } from 'bun:test'
import { mkdtempSync, writeFileSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { GrammyError } from 'grammy'
import {
  imageDims, photoProblem, photoCheck, isPhotoRejection, withDocumentFallback,
  PHOTO_MAX_BYTES, PHOTO_READABLE_RATIO,
} from '../src/photo.ts'
import { png, jpeg, gif, webp } from './images.ts'

const dir = mkdtempSync(join(tmpdir(), 'tgp-photo-'))
afterAll(() => rmSync(dir, { recursive: true, force: true }))
let n = 0
const file = (bytes: Buffer | Uint8Array, ext = '.bin') => {
  const p = join(dir, `f${n++}${ext}`)
  writeFileSync(p, bytes)
  return p
}

const tgErr = (code: number, description: string) =>
  new GrammyError(`Call to 'sendPhoto' failed!`, { ok: false, error_code: code, description }, 'sendPhoto', {})

describe('imageDims', () => {
  test('PNG', () => expect(imageDims(file(png(1905, 8710), '.png'))).toEqual({ width: 1905, height: 8710 }))
  test('JPEG: skips segments by length (EXIF thumbnail decoy ignored)', () =>
    expect(imageDims(file(jpeg(3000, 7500), '.jpg'))).toEqual({ width: 3000, height: 7500 }))
  test('JPEG: progressive SOF2 and fill bytes before the marker', () =>
    expect(imageDims(file(jpeg(4032, 3024, { sofMarker: 0xc2, fill: 5 }), '.jpg'))).toEqual({ width: 4032, height: 3024 }))
  test('JPEG: start of scan before any frame header → unknown', () =>
    expect(imageDims(file(jpeg(100, 100, { sosFirst: true }), '.jpg'))).toBeNull())
  test('JPEG: truncated → unknown', () => expect(imageDims(file(jpeg(100, 100).subarray(0, 200), '.jpg'))).toBeNull())
  test('GIF', () => expect(imageDims(file(gif(640, 480), '.gif'))).toEqual({ width: 640, height: 480 }))
  test('WebP lossy (VP8)', () => expect(imageDims(file(webp('VP8 ', 1024, 768), '.webp'))).toEqual({ width: 1024, height: 768 }))
  test('WebP lossless (VP8L)', () => expect(imageDims(file(webp('VP8L', 1905, 8710), '.webp'))).toEqual({ width: 1905, height: 8710 }))
  test('WebP extended (VP8X)', () => expect(imageDims(file(webp('VP8X', 16000, 400), '.webp'))).toEqual({ width: 16000, height: 400 }))
  test('garbage, empty, truncated PNG, missing file → null', () => {
    expect(imageDims(file(Buffer.from('x'), '.png'))).toBeNull()
    expect(imageDims(file(Buffer.alloc(0), '.png'))).toBeNull()
    expect(imageDims(file(png(10, 10).subarray(0, 20), '.png'))).toBeNull()
    expect(imageDims(join(dir, 'nope.png'))).toBeNull()
  })
  test('zero dimensions → null', () => expect(imageDims(file(png(0, 100), '.png'))).toBeNull())
})

describe('photoProblem (Telegram sendPhoto limits)', () => {
  const small = 200_000
  test('the screenshot that failed: 1905x8710 (sum 10615)', () =>
    expect(photoProblem(small, { width: 1905, height: 8710 })).toMatch(/1905x8710.*10000/))
  test('ordinary photos and screenshots pass', () => {
    expect(photoProblem(small, { width: 4032, height: 3024 })).toBeNull()
    expect(photoProblem(small, { width: 1920, height: 1080 })).toBeNull()
    expect(photoProblem(small, { width: 1080, height: 2400 })).toBeNull() // phone screenshot, ratio 2.2
    expect(photoProblem(small, { width: 2400, height: 1080 })).toBeNull()
  })
  test('too tall or wide to stay readable (ratio > PHOTO_READABLE_RATIO) → document', () => {
    expect(PHOTO_READABLE_RATIO).toBe(4)
    // A long phone screenshot: within Telegram's limits, but shrunk to ~400x2560 as a photo.
    expect(photoProblem(small, { width: 1080, height: 7000 })).toBe('1080x7000, too tall to stay readable as a photo (aspect ratio 6.5 > 4)')
    expect(photoProblem(small, { width: 7000, height: 1080 })).toMatch(/^7000x1080, too wide .*aspect ratio 6\.5 > 4/)
  })
  test('readable ratio: 4 ok, above not (either orientation)', () => {
    expect(photoProblem(small, { width: 1000, height: 4000 })).toBeNull()
    expect(photoProblem(small, { width: 4000, height: 1000 })).toBeNull()
    expect(photoProblem(small, { width: 1000, height: 4001 })).toMatch(/too tall/)
    expect(photoProblem(small, { width: 4001, height: 1000 })).toMatch(/too wide/)
  })
  test('width + height: 10000 ok, 10001 not', () => {
    expect(photoProblem(small, { width: 5000, height: 5000 })).toBeNull()
    expect(photoProblem(small, { width: 5000, height: 5001 })).not.toBeNull()
  })
  test("Telegram's aspect ratio limit 20 takes precedence in the reason (either orientation)", () => {
    expect(photoProblem(small, { width: 4000, height: 200 })).toMatch(/too wide .*aspect ratio 20\.0 > 4/)
    expect(photoProblem(small, { width: 4001, height: 200 })).toBe('4001x200, photos max aspect ratio 20')
    expect(photoProblem(small, { width: 100, height: 2100 })).toBe('100x2100, photos max aspect ratio 20')
  })
  test('size: 10 MB ok, above not, even with unknown dimensions', () => {
    expect(photoProblem(PHOTO_MAX_BYTES, { width: 100, height: 100 })).toBeNull()
    expect(photoProblem(PHOTO_MAX_BYTES + 1, null)).toMatch(/MB/)
  })
  test('unknown dimensions, small file → try as a photo', () => expect(photoProblem(small, null)).toBeNull())
})

describe('photoCheck (file on disk)', () => {
  test('tall PNG, tall JPEG, wide WebP → problem; normal ones and unparseable → none', () => {
    expect(photoCheck(file(png(1905, 8710), '.png'))).not.toBeNull()
    expect(photoCheck(file(jpeg(3000, 7500), '.jpg'))).not.toBeNull()
    expect(photoCheck(file(webp('VP8X', 16000, 400), '.webp'))).not.toBeNull()
    expect(photoCheck(file(png(1280, 720), '.png'))).toBeNull()
    expect(photoCheck(file(jpeg(4032, 3024), '.jpg'))).toBeNull()
    expect(photoCheck(file(Buffer.from('x'), '.png'))).toBeNull()
    expect(photoCheck(join(dir, 'missing.png'))).toBeNull()
  })
  test('long phone screenshots: 1080x7000 → problem (unreadable as a photo); 1080x2400 → none', () => {
    expect(photoCheck(file(png(1080, 7000), '.png'))).toMatch(/1080x7000, too tall/)
    expect(photoCheck(file(jpeg(1080, 7000), '.jpg'))).toMatch(/1080x7000, too tall/)
    expect(photoCheck(file(png(1080, 2400), '.png'))).toBeNull()
    expect(photoCheck(file(jpeg(1080, 2400), '.jpg'))).toBeNull()
  })
  test('over 10 MB → problem', () => {
    const big = Buffer.alloc(PHOTO_MAX_BYTES + 10)
    png(800, 600).copy(big)
    expect(photoCheck(file(big, '.png'))).toMatch(/MB/)
  })
})

describe('isPhotoRejection', () => {
  test('photo refusals (400/413) → retry as a document', () => {
    for (const d of ['Bad Request: PHOTO_INVALID_DIMENSIONS', 'Bad Request: PHOTO_SAVE_FILE_INVALID',
      'Bad Request: IMAGE_PROCESS_FAILED', 'Bad Request: PHOTO_EXT_INVALID']) expect(isPhotoRejection(tgErr(400, d))).toBe(true)
    expect(isPhotoRejection(tgErr(413, 'Request Entity Too Large'))).toBe(true)
  })
  test('anything else → no retry', () => {
    expect(isPhotoRejection(tgErr(403, 'Forbidden: bot was blocked by the user'))).toBe(false)
    expect(isPhotoRejection(tgErr(429, 'Too Many Requests: retry after 5'))).toBe(false)
    expect(isPhotoRejection(tgErr(400, 'Bad Request: chat not found'))).toBe(false)
    expect(isPhotoRejection(tgErr(400, 'Bad Request: message to be replied not found'))).toBe(false)
    expect(isPhotoRejection(new Error('Network request for sendPhoto failed! PHOTO_INVALID_DIMENSIONS'))).toBe(false)
    expect(isPhotoRejection(undefined)).toBe(false)
    expect(isPhotoRejection('PHOTO_INVALID_DIMENSIONS')).toBe(false)
  })
})

describe('withDocumentFallback (mocked Bot API)', () => {
  /** A fake tg(): records calls; sendPhoto/photo albums fail with `photoErr` if set. */
  function mockApi(photoErr?: unknown, docErr?: unknown) {
    const calls: string[] = []
    let id = 0
    const call = async (asPhoto: boolean) => {
      calls.push(asPhoto ? 'sendPhoto' : 'sendDocument')
      if (asPhoto && photoErr) throw photoErr
      if (!asPhoto && docErr) throw docErr
      return { message_id: ++id }
    }
    return { calls, call }
  }
  test('photo accepted → one sendPhoto', async () => {
    const api = mockApi()
    const r = await withDocumentFallback(true, api.call)
    expect(api.calls).toEqual(['sendPhoto'])
    expect(r).toEqual({ result: { message_id: 1 }, asPhoto: true })
  })
  test('decided up front as a document → one sendDocument, no photo attempt', async () => {
    const api = mockApi(tgErr(400, 'Bad Request: PHOTO_INVALID_DIMENSIONS'))
    const r = await withDocumentFallback(false, api.call)
    expect(api.calls).toEqual(['sendDocument'])
    expect(r.asPhoto).toBe(false)
    expect(r.rejection).toBeUndefined()
  })
  test('PHOTO_INVALID_DIMENSIONS → retried once as a document, with the reason', async () => {
    const api = mockApi(tgErr(400, 'Bad Request: PHOTO_INVALID_DIMENSIONS'))
    const r = await withDocumentFallback(true, api.call)
    expect(api.calls).toEqual(['sendPhoto', 'sendDocument'])
    expect(r).toEqual({ result: { message_id: 1 }, asPhoto: false, rejection: 'Bad Request: PHOTO_INVALID_DIMENSIONS' })
  })
  test('the document fails too → that error, no third attempt', async () => {
    const api = mockApi(tgErr(400, 'Bad Request: PHOTO_INVALID_DIMENSIONS'), tgErr(400, 'Bad Request: file is empty'))
    await expect(withDocumentFallback(true, api.call)).rejects.toThrow(/file is empty/)
    expect(api.calls).toEqual(['sendPhoto', 'sendDocument'])
  })
  test('other errors (blocked, rate limit, network) are thrown, not retried', async () => {
    for (const err of [tgErr(403, 'Forbidden: bot was blocked by the user'), tgErr(429, 'Too Many Requests: retry after 3'), new Error('ECONNRESET')]) {
      const api = mockApi(err)
      await expect(withDocumentFallback(true, api.call)).rejects.toBe(err)
      expect(api.calls).toEqual(['sendPhoto'])
    }
  })
})
