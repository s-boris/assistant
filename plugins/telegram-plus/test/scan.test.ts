// Unit tests for scan.ts: the "/scan private" / "/sp" caption parser (bridgedb.ts refuses such events,
// test/bridge.test.ts covers that end to end).
// Run:  bun test
import { describe, expect, test } from 'bun:test'
import { parseScanPrivate } from '../scan.ts'

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
