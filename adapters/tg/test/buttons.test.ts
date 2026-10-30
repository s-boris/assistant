// Unit tests for buttons.ts: the on_press modes saved in buttons.json so 'keep'
// and 'remove' keyboards behave the same after a restart. Temp dirs only.
// Run:  bun test   (paths.test.ts covers the server side across a real restart)
import { describe, expect, test, beforeEach, afterAll } from 'bun:test'
import { mkdtempSync, writeFileSync, readFileSync, readdirSync, statSync, rmSync, existsSync, mkdirSync, chmodSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import {
  BUTTONS_FILE_NAME, BUTTONS_MAX, BUTTONS_TTL_MS, pressKey, isSaved,
  prunePresses, readPresses, writePresses, lookupPress, savePress, forgetPress, type SavedPresses,
} from '../src/buttons.ts'

const ROOT = mkdtempSync(join(tmpdir(), 'tgp-buttons-'))
let dir = ''
let file = ''
let n = 0
const NOW = 1_800_000_000_000
const DAY = 24 * 3600 * 1000

beforeEach(() => {
  dir = join(ROOT, String(++n))
  mkdirSync(dir)
  file = join(dir, BUTTONS_FILE_NAME)
})
afterAll(() => rmSync(ROOT, { recursive: true, force: true }))

const onDisk = () => JSON.parse(readFileSync(file, 'utf8'))

describe('keys and modes', () => {
  test('key is "chat:message", groups keep their minus sign', () => {
    expect(pressKey('111', 5001)).toBe('111:5001')
    expect(pressKey('-100123', 7)).toBe('-100123:7')
  })
  test('only keep and remove are saved', () => {
    expect([isSaved('keep'), isSaved('remove'), isSaved('mark'), isSaved(undefined), isSaved('KEEP')]).toEqual([true, true, false, false, false])
  })
})

describe('savePress / lookupPress / forgetPress', () => {
  test('missing file: nothing saved, lookups are undefined, a default press writes nothing', () => {
    expect(readPresses(file, NOW)).toEqual({})
    expect(lookupPress(file, '111:1', NOW)).toBeUndefined()
    savePress(file, '111:1', 'mark', NOW)
    forgetPress(file, '111:1', NOW)
    expect(existsSync(file)).toBe(false)
  })

  test('keep and remove round-trip through the file (what a restarted server reads)', () => {
    savePress(file, '111:1', 'keep', NOW)
    savePress(file, '-100123:2', 'remove', NOW)
    expect(lookupPress(file, '111:1', NOW + 1000)).toBe('keep')
    expect(lookupPress(file, '-100123:2', NOW + 1000)).toBe('remove')
    expect(lookupPress(file, '111:3', NOW)).toBeUndefined()
    expect(onDisk()).toEqual({ '111:1': { on_press: 'keep', ts: NOW }, '-100123:2': { on_press: 'remove', ts: NOW } })
  })

  test('re-sending the keyboard as "mark" (default) drops the saved mode; forgetPress drops it too', () => {
    savePress(file, '111:1', 'keep', NOW)
    savePress(file, '111:2', 'remove', NOW)
    savePress(file, '111:1', 'mark', NOW)
    expect(lookupPress(file, '111:1', NOW)).toBeUndefined()
    forgetPress(file, '111:2', NOW)
    expect(onDisk()).toEqual({})
  })

  test('a key that is not "chat:message" is ignored (nothing written)', () => {
    for (const k of ['111', '111:', ':5', 'abc:5', '111:5:6', '../x:1']) savePress(file, k, 'keep', NOW)
    expect(existsSync(file)).toBe(false)
  })

  test('a later save overwrites the mode and refreshes the time', () => {
    savePress(file, '111:1', 'remove', NOW)
    savePress(file, '111:1', 'keep', NOW + 5)
    expect(onDisk()).toEqual({ '111:1': { on_press: 'keep', ts: NOW + 5 } })
  })

  test('0600, no temp file left behind, only ids/mode/time on disk', () => {
    savePress(file, '111:1', 'keep', NOW)
    savePress(file, '111:2', 'remove', NOW)
    expect(statSync(file).mode & 0o777).toBe(0o600)
    expect(readdirSync(dir)).toEqual([BUTTONS_FILE_NAME])
    for (const v of Object.values(onDisk()) as any[]) expect(Object.keys(v).sort()).toEqual(['on_press', 'ts'])
  })

  test('a failed write throws, leaves no temp file and keeps the old file intact', () => {
    savePress(file, '111:1', 'keep', NOW)
    const before = readFileSync(file, 'utf8')
    chmodSync(dir, 0o500) // no new files in the dir
    try {
      if (process.getuid?.() === 0) return // root ignores the mode
      expect(() => savePress(file, '111:2', 'keep', NOW)).toThrow()
    } finally { chmodSync(dir, 0o700) }
    expect(readFileSync(file, 'utf8')).toBe(before)
    expect(readdirSync(dir)).toEqual([BUTTONS_FILE_NAME])
  })
})

describe('corrupt or foreign content', () => {
  test('not JSON, an array, a string: read as empty; the next save replaces the file', () => {
    for (const bad of ['{not json', '[1,2]', '"x"', 'null', '']) {
      writeFileSync(file, bad)
      expect(readPresses(file, NOW)).toEqual({})
      expect(lookupPress(file, '111:1', NOW)).toBeUndefined()
    }
    savePress(file, '111:1', 'keep', NOW)
    expect(onDisk()).toEqual({ '111:1': { on_press: 'keep', ts: NOW } })
  })

  test('malformed entries are skipped, valid ones kept', () => {
    writeFileSync(file, JSON.stringify({
      '111:1': { on_press: 'keep', ts: NOW },
      '111:2': { on_press: 'mark', ts: NOW },
      '111:3': { on_press: 'keep' },
      '111:4': { on_press: 'keep', ts: 'yesterday' },
      'abc:5': { on_press: 'keep', ts: NOW },
      '111:6': 'keep',
      '111:7': null,
      '111:8': { on_press: 'remove', ts: NOW, text: 'extra fields are dropped' },
    }))
    expect(readPresses(file, NOW)).toEqual({ '111:1': { on_press: 'keep', ts: NOW }, '111:8': { on_press: 'remove', ts: NOW } })
  })
})

describe('pruning: ~30 days, newest 500', () => {
  test('entries older than the TTL (or far in the future) are dropped on read and on write', () => {
    expect(BUTTONS_TTL_MS).toBe(30 * DAY)
    writeFileSync(file, JSON.stringify({
      '111:1': { on_press: 'keep', ts: NOW - 31 * DAY },
      '111:2': { on_press: 'keep', ts: NOW - 29 * DAY },
      '111:3': { on_press: 'keep', ts: NOW + 400 * DAY },
    }))
    expect(Object.keys(readPresses(file, NOW))).toEqual(['111:2'])
    savePress(file, '111:9', 'remove', NOW)
    expect(Object.keys(onDisk()).sort()).toEqual(['111:2', '111:9'])
  })

  test('at most BUTTONS_MAX entries, the newest kept', () => {
    expect(BUTTONS_MAX).toBe(500)
    const many: SavedPresses = {}
    for (let i = 0; i < BUTTONS_MAX + 20; i++) many[`111:${i}`] = { on_press: 'keep', ts: NOW - (BUTTONS_MAX + 20 - i) * 1000 }
    writePresses(file, many, NOW)
    const kept = onDisk()
    expect(Object.keys(kept)).toHaveLength(BUTTONS_MAX)
    expect(kept['111:0']).toBeUndefined()
    expect(kept['111:19']).toBeUndefined()
    expect(kept['111:20']).toBeDefined()
    savePress(file, '111:9999', 'keep', NOW)
    const after = onDisk()
    expect(Object.keys(after)).toHaveLength(BUTTONS_MAX)
    expect(after['111:9999']).toEqual({ on_press: 'keep', ts: NOW })
    expect(after['111:20']).toBeUndefined() // the oldest made room
  })

  test('prunePresses itself: TTL, cap, newest first', () => {
    const e: SavedPresses = {
      a: { on_press: 'keep', ts: NOW - 3 },
      b: { on_press: 'keep', ts: NOW - 1 },
      c: { on_press: 'remove', ts: NOW - 2 },
      d: { on_press: 'keep', ts: NOW - 40 * DAY },
    }
    expect(Object.keys(prunePresses(e, NOW))).toEqual(['b', 'c', 'a'])
    expect(Object.keys(prunePresses(e, NOW, 2))).toEqual(['b', 'c'])
    expect(prunePresses(e, NOW, 0)).toEqual({})
  })
})
