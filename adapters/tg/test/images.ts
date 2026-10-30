// Minimal image files for the photo tests: just the headers photo.ts reads
// (PNG IHDR, JPEG segments, GIF screen size, WebP VP8/VP8L/VP8X), plus padding.

const u16be = (v: number) => { const b = Buffer.alloc(2); b.writeUInt16BE(v); return b }
const u32be = (v: number) => { const b = Buffer.alloc(4); b.writeUInt32BE(v); return b }

export function png(w: number, h: number): Buffer {
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    u32be(13), Buffer.from('IHDR'), u32be(w), u32be(h), Buffer.from([8, 6, 0, 0, 0]), u32be(0),
    Buffer.alloc(64),
  ])
}
const seg = (marker: number, payload: Buffer) => Buffer.concat([Buffer.from([0xff, marker]), u16be(payload.length + 2), payload])
const sof = (marker: number, w: number, h: number) => seg(marker, Buffer.concat([Buffer.from([8]), u16be(h), u16be(w), Buffer.from([3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])]))
/** JPEG with JFIF APP0 and an EXIF APP1 whose payload holds a decoy SOF0
 *  (a thumbnail's 160x120 frame header), then optional fill bytes, the real frame. */
export function jpeg(w: number, h: number, opts: { sofMarker?: number; fill?: number; sosFirst?: boolean } = {}): Buffer {
  const app0 = seg(0xe0, Buffer.concat([Buffer.from('JFIF\0'), Buffer.from([1, 1, 0, 0, 1, 0, 1, 0, 0])]))
  const thumb = Buffer.concat([Buffer.from([0xff, 0xd8]), sof(0xc0, 160, 120), Buffer.from([0xff, 0xd9])])
  const app1 = seg(0xe1, Buffer.concat([Buffer.from('Exif\0\0'), Buffer.alloc(40), thumb, Buffer.alloc(3000)]))
  const dqt = seg(0xdb, Buffer.alloc(65))
  const sos = seg(0xda, Buffer.alloc(10))
  const frame = sof(opts.sofMarker ?? 0xc0, w, h)
  return Buffer.concat([
    Buffer.from([0xff, 0xd8]), app0, app1, dqt,
    Buffer.alloc(opts.fill ?? 0, 0xff),
    ...(opts.sosFirst ? [sos, frame] : [frame, sos]),
    Buffer.alloc(100), Buffer.from([0xff, 0xd9]),
  ])
}
export function gif(w: number, h: number): Buffer {
  const b = Buffer.alloc(32); b.write('GIF89a', 0, 'latin1'); b.writeUInt16LE(w, 6); b.writeUInt16LE(h, 8); return b
}
export function webp(kind: 'VP8 ' | 'VP8L' | 'VP8X', w: number, h: number): Buffer {
  const b = Buffer.alloc(64)
  b.write('RIFF', 0, 'latin1'); b.writeUInt32LE(56, 4); b.write('WEBP', 8, 'latin1'); b.write(kind, 12, 'latin1')
  if (kind === 'VP8 ') { b[23] = 0x9d; b[24] = 0x01; b[25] = 0x2a; b.writeUInt16LE(w, 26); b.writeUInt16LE(h, 28) }
  if (kind === 'VP8L') { b[20] = 0x2f; b.writeUInt32LE(((w - 1) & 0x3fff) | (((h - 1) & 0x3fff) << 14), 21) }
  if (kind === 'VP8X') { b.writeUIntLE(w - 1, 24, 3); b.writeUIntLE(h - 1, 27, 3) }
  return b
}
