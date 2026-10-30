// Clock edge for the buttons.json TTL (epoch ms, so DST must not shift it), lifted from
// telegram-plus test/clock.test.ts (its scan.ts half is dropped with scan.ts).
// Run:  bun test
import { afterAll, describe, expect, test } from 'bun:test'
import { BUTTONS_TTL_MS, prunePresses, type SavedPresses } from '../src/buttons.ts'

const savedTZ = process.env.TZ
process.env.TZ = 'Europe/Zurich'
afterAll(() => { if (savedTZ === undefined) delete process.env.TZ; else process.env.TZ = savedTZ })
const utc = (iso: string) => new Date(iso + 'Z')

describe('buttons TTL is elapsed time, not wall time', () => {
  test('a press survives (or expires) by epoch ms across both DST switches', () => {
    for (const at of ['2027-03-28T00:30:00', '2026-10-25T00:30:00']) {
      const ts = utc(at).getTime()
      const entries: SavedPresses = { 'c:1': { on_press: 'keep', ts } }
      expect(Object.keys(prunePresses(entries, ts + BUTTONS_TTL_MS - 1))).toEqual(['c:1'])
      expect(Object.keys(prunePresses(entries, ts + BUTTONS_TTL_MS))).toEqual([])
    }
  })
})
