// Unit tests for scan.ts (0.1.11): the "/scan private" / "/sp" caption parser,
// titles, the document id in paperless's output, temp file names and the
// owner's short failure reason.
// Run:  bun test   (offline.ts covers the server side end to end)
import { describe, expect, test } from 'bun:test'
import { parseScanPrivate, scanTitle, scanDocId, scanFileName, scanFailReason, localStamp, SCAN_TITLE_MAX } from '../src/scan.ts'

describe('parseScanPrivate', () => {
  const p = (c: string | undefined) => parseScanPrivate(c, 'testbot')
  test('both spellings, with and without a title', () => {
    expect(p('/scan private')).toEqual({ command: '/scan private', title: '' })
    expect(p('/sp')).toEqual({ command: '/sp', title: '' })
    expect(p('/scan private Arztrechnung Oktober')).toEqual({ command: '/scan private', title: 'Arztrechnung Oktober' })
    expect(p('/sp Befund')).toEqual({ command: '/sp', title: 'Befund' })
  })
  test('case, extra spaces, newlines and control characters', () => {
    expect(p('/SCAN   Private  a   b ')).toEqual({ command: '/scan private', title: 'a b' })
    expect(p('  /Sp\nLine one\n\tline two')).toEqual({ command: '/sp', title: 'Line one line two' })
    expect(p('/sp a\u0000b\u0007c')).toEqual({ command: '/sp', title: 'a b c' })
  })
  test('"@this bot" only', () => {
    expect(p('/sp@testbot x')).toEqual({ command: '/sp', title: 'x' })
    expect(p('/scan@TestBot private')).toEqual({ command: '/scan private', title: '' })
    expect(p('/sp@otherbot x')).toBeNull()
    expect(p('/scan@otherbot private')).toBeNull()
  })
  test('not a private scan: plain /scan, look-alikes, no caption', () => {
    for (const c of ['/scan', '/scan Rechnung', '/scan privately', '/scanprivate', '/spam', '/sp-x', 'sp', 'scan private', '/scan\nprivate notes?', 'hello /sp', '', undefined]) {
      // "/scan\nprivate notes?" is "/scan private" + a title: newlines count as spaces
      if (c === '/scan\nprivate notes?') { expect(p(c)).toEqual({ command: '/scan private', title: 'notes?' }); continue }
      expect(p(c)).toBeNull()
    }
    expect(parseScanPrivate(null, 'testbot')).toBeNull()
  })
})

describe('scanTitle', () => {
  const d = new Date(2026, 9, 12, 9, 5) // local time
  test('default "Scan YYYY-MM-DD HH:MM" in local time', () => {
    expect(localStamp(d)).toBe('2026-10-12 09:05')
    expect(scanTitle('', d)).toBe('Scan 2026-10-12 09:05')
    expect(scanTitle('   ', d)).toBe('Scan 2026-10-12 09:05')
  })
  test('own title, album parts', () => {
    expect(scanTitle('Befund', d)).toBe('Befund')
    expect(scanTitle('Befund', d, 2)).toBe('Befund (2)')
    expect(scanTitle('', d, 3)).toBe('Scan 2026-10-12 09:05 (3)')
  })
  test(`at most ${SCAN_TITLE_MAX} characters, suffix kept, emoji not cut in half`, () => {
    const long = 'x'.repeat(300)
    expect(scanTitle(long, d)).toHaveLength(SCAN_TITLE_MAX)
    const t = scanTitle(long, d, 12)
    expect(t).toHaveLength(SCAN_TITLE_MAX)
    expect(t.endsWith(' (12)')).toBe(true)
    const emo = scanTitle('x' + '😀'.repeat(200), d)
    expect(Array.from(emo)).toHaveLength(SCAN_TITLE_MAX)
    expect(emo.endsWith('😀')).toBe(true)
  })
})

describe('scanDocId', () => {
  test('the id from "document <id>: <url>"', () => {
    expect(scanDocId('document 123: http://198.51.100.5:8000/documents/123/details\n')).toBe('123')
    expect(scanDocId('task abc queued\n')).toBeNull()
    expect(scanDocId('')).toBeNull()
    expect(scanDocId('the document 5 is a duplicate')).toBeNull()
  })
})

describe('scanFileName', () => {
  const d = new Date(2026, 9, 12, 22, 7)
  test('photos: scan-<stamp>.<ext>', () => {
    expect(scanFileName(undefined, 'jpg', d)).toBe('scan-20261012-2207.jpg')
    expect(scanFileName('', 'j/p\\g', d)).toBe('scan-20261012-2207.jpg')
  })
  test("documents keep their own (safe) name", () => {
    expect(scanFileName('Befund Mai.pdf', 'pdf', d)).toBe('Befund Mai.pdf')
    expect(scanFileName('../../etc/passwd', 'bin', d)).toBe('passwd.bin')
    expect(scanFileName('-rf $(id).pdf', 'pdf', d)).toBe('rf _(id).pdf')
    expect(scanFileName('Rechnung', 'pdf', d)).toBe('Rechnung.pdf')
    expect(scanFileName('...', 'pdf', d)).toBe('scan-20261012-2207.pdf')
  })
})

describe('scanFailReason', () => {
  test('exit code and tool name dropped', () => {
    expect(scanFailReason('exit 1: paperless: consumption failed: boom')).toBe('consumption failed: boom')
    expect(scanFailReason("exit 1: vault: no tag named 'x'")).toBe("no tag named 'x'")
    expect(scanFailReason('exit 3: vault: cannot reach the vault at http://x (refused). Check: vault admin ps')).toBe('cannot reach the vault at http://x (refused). Check: vault admin ps')
  })
  test('paperless still consuming (exit 2) / our timeout / no id', () => {
    expect(scanFailReason('exit 2: vault: timed out after 180s waiting for task t1 (status started; check later with `paperless task t1`)'))
      .toBe('paperless is still processing it (it may show up later)')
    expect(scanFailReason('timed out after 300 s')).toBe('paperless upload timed out after 300 s')
    expect(scanFailReason(undefined, 'task x queued')).toBe('no document id in the answer')
    expect(scanFailReason('exit 1', '')).toBe('exit 1')
  })
  test('short', () => {
    expect(scanFailReason('exit 1: ' + 'y'.repeat(500)).length).toBe(160)
  })
})
