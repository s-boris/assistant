// Quiet hours (quiet.ts) on the owner's zone while the process runs on UTC: both DST switch nights,
// wrapping specs, how the zone is chosen (env, profile, fallback), and the tripclock zone switch
// (a fake tripclock via TELEGRAM_TRIPCLOCK_CMD: trips, errors, timeouts, never blocking).
// Run:  bun test
import { afterAll, describe, expect, test } from 'bun:test'
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import {
  DEFAULT_QUIET_TZ, inQuietHours, QuietClock, quietSpec, quietZone, quietZonePinned, runCommand, TRIPCLOCK_REFRESH_MS,
  tripclockArgv, zoneHour,
} from '../src/quiet.ts'

const savedTZ = process.env.TZ
process.env.TZ = 'UTC'
const dir = mkdtempSync(join(tmpdir(), 'quiet-test-'))
afterAll(() => {
  if (savedTZ === undefined) delete process.env.TZ; else process.env.TZ = savedTZ
  rmSync(dir, { recursive: true, force: true })
})
const utc = (iso: string) => Date.parse(iso + 'Z')
const Z = 'Europe/Zurich'

describe('process clock is UTC', () => {
  test('the TZ switch took effect', () => expect(new Date(utc('2026-07-01T12:00:00')).getTimezoneOffset()).toBe(0))
})

describe('0-8 in Europe/Zurich under TZ=UTC', () => {
  const cases: [string, string, boolean][] = [
    ['autumn Sat 23:59 CEST', '2026-10-24T21:59:00', false],
    ['autumn 00:00 CEST', '2026-10-24T22:00:00', true],
    ['autumn first 02:30 (CEST)', '2026-10-25T00:30:00', true],
    ['autumn second 02:30 (CET)', '2026-10-25T01:30:00', true],
    ['autumn 07:30 CET (UTC+2 would say 08:30)', '2026-10-25T06:30:00', true],
    ['autumn 07:59 CET', '2026-10-25T06:59:00', true],
    ['autumn 08:00 CET', '2026-10-25T07:00:00', false],
    ['spring Sat 23:59 CET', '2027-03-27T22:59:00', false],
    ['spring 01:59 CET', '2027-03-28T00:59:00', true],
    ['spring 03:00 CEST (02:xx skipped)', '2027-03-28T01:00:00', true],
    ['spring 07:59 CEST', '2027-03-28T05:59:00', true],
    ['spring 08:00 CEST (UTC+1 would say 07:00)', '2027-03-28T06:00:00', false],
    ['summer 14:00 CEST (12:00Z)', '2026-07-01T12:00:00', false],
    ['winter 07:30 CET (06:30Z)', '2026-12-01T06:30:00', true],
  ]
  for (const [label, u, want] of cases) {
    test(`${label}: ${want ? 'quiet' : 'not quiet'}`, () => expect(inQuietHours('0-8', utc(u), Z)).toBe(want))
  }
  test('zoneHour across the autumn repeated hour', () => {
    expect(zoneHour(utc('2026-10-25T00:30:00'), Z)).toBe(2)
    expect(zoneHour(utc('2026-10-25T01:30:00'), Z)).toBe(2)
    expect(zoneHour(utc('2026-10-24T22:00:00'), Z)).toBe(0)
  })
})

describe('specs', () => {
  const noonZ = utc('2026-07-01T10:00:00')   // 12:00 CEST
  test('"" and junk: never quiet', () => {
    for (const s of ['', ' ', 'off', '8', '0-8-9']) expect(inQuietHours(s, utc('2026-10-24T23:00:00'), Z)).toBe(false)
  })
  test('wrapping 22-6', () => {
    expect(inQuietHours('22-6', utc('2026-07-01T20:30:00'), Z)).toBe(true)    // 22:30 CEST
    expect(inQuietHours('22-6', utc('2026-07-01T03:59:00'), Z)).toBe(true)    // 05:59 CEST
    expect(inQuietHours('22-6', utc('2026-07-01T04:00:00'), Z)).toBe(false)   // 06:00 CEST
  })
  test('another zone shifts the window', () => {
    expect(inQuietHours('0-8', noonZ, Z)).toBe(false)
    expect(inQuietHours('0-8', noonZ, 'Pacific/Auckland')).toBe(false)      // 22:00 NZST
    expect(inQuietHours('0-8', noonZ, 'America/Los_Angeles')).toBe(true)    // 03:00 PDT
  })
})

describe('quietZone', () => {
  const prof = (tz: unknown) => {
    const p = join(dir, `profile-${Math.random().toString(36).slice(2)}.json`)
    writeFileSync(p, JSON.stringify({ locale: { tz } }))
    return p
  }
  test('TELEGRAM_QUIET_TZ wins', () => {
    expect(quietZone({ TELEGRAM_QUIET_TZ: 'Asia/Tokyo', ASSISTANT_PROFILE: prof('Europe/Berlin') }, dir)).toBe('Asia/Tokyo')
  })
  test('else the profile locale.tz', () => {
    expect(quietZone({ ASSISTANT_PROFILE: prof('Europe/Berlin') }, dir)).toBe('Europe/Berlin')
  })
  test('else ~/.config/assistant/profile.json under home', () => {
    expect(quietZone({}, dir)).toBe(DEFAULT_QUIET_TZ)   // none there yet
  })
  test('bad values fall back', () => {
    expect(quietZone({ TELEGRAM_QUIET_TZ: 'Not/AZone', ASSISTANT_PROFILE: prof('Europe/Berlin') }, dir)).toBe('Europe/Berlin')
    expect(quietZone({ ASSISTANT_PROFILE: prof('Mars/Olympus') }, dir)).toBe(DEFAULT_QUIET_TZ)
    expect(quietZone({ ASSISTANT_PROFILE: prof(42) }, dir)).toBe(DEFAULT_QUIET_TZ)
    expect(quietZone({ ASSISTANT_PROFILE: join(dir, 'missing.json') }, dir)).toBe(DEFAULT_QUIET_TZ)
    expect(DEFAULT_QUIET_TZ).toBe('Europe/Zurich')
  })
  test('the process TZ is never used', () => {
    expect(quietZone({ TZ: 'UTC' }, dir)).toBe(DEFAULT_QUIET_TZ)
  })
})

// --- tripclock: the zone he is in now ------------------------------------------------------------

const script = (name: string, body: string) => {
  const p = join(dir, name)
  writeFileSync(p, `#!/bin/sh\n${body}\n`)
  chmodSync(p, 0o755)
  return p
}
// The fake tripclock: prints the zone in $FAKE_TZ_FILE (as `tripclock tz` would), exit 3 when the file says "fail".
const zoneFile = join(dir, 'fake-zone')
const fake = script('fake-tripclock', `[ "$1" = tz ] || exit 2\nz=$(cat "${zoneFile}") || exit 4\n[ "$z" = fail ] && { echo 'tripclock: broken config' >&2; exit 3; }\necho "$z"`)
const setZone = (z: string) => writeFileSync(zoneFile, z + '\n')
const NIGHT_IN_TOKYO = utc('2027-02-10T18:00:00')   // 03:00 in Tokyo, 19:00 in Zurich (CET)
const quietIn = (clock: QuietClock) => inQuietHours('0-8', NIGHT_IN_TOKYO, clock.zone())

describe('tripclockArgv', () => {
  test('TELEGRAM_TRIPCLOCK_CMD: split on whitespace, ~/ expanded, no shell', () => {
    expect(tripclockArgv({ TELEGRAM_TRIPCLOCK_CMD: `${fake} tz` }, dir)).toEqual([fake, 'tz'])
    expect(tripclockArgv({ TELEGRAM_TRIPCLOCK_CMD: '~/bin/tripclock  tz --at  x' }, '/h')).toEqual(['/h/bin/tripclock', 'tz', '--at', 'x'])
  })
  test('"" turns it off', () => expect(tripclockArgv({ TELEGRAM_TRIPCLOCK_CMD: '  ' }, dir)).toBeNull())
  test('unset: ~/bin/tripclock tz when it is executable, else none', () => {
    expect(tripclockArgv({}, dir)).toBeNull()
    const home = join(dir, 'home-with-tripclock')
    mkdirSync(join(home, 'bin'), { recursive: true })
    writeFileSync(join(home, 'bin', 'tripclock'), '#!/bin/sh\necho UTC\n')
    expect(tripclockArgv({}, home)).toBeNull()              // not executable
    chmodSync(join(home, 'bin', 'tripclock'), 0o755)
    expect(tripclockArgv({}, home)).toEqual([join(home, 'bin', 'tripclock'), 'tz'])
  })
  test('TELEGRAM_QUIET_TZ pins the zone: tripclock is not asked', () => {
    expect(quietZonePinned({ TELEGRAM_QUIET_TZ: 'Asia/Tokyo' })).toBe(true)
    expect(tripclockArgv({ TELEGRAM_QUIET_TZ: 'Asia/Tokyo', TELEGRAM_TRIPCLOCK_CMD: fake }, dir)).toBeNull()
    expect(quietZonePinned({ TELEGRAM_QUIET_TZ: 'Not/AZone' })).toBe(false)
    expect(tripclockArgv({ TELEGRAM_QUIET_TZ: 'Not/AZone', TELEGRAM_TRIPCLOCK_CMD: fake }, dir)).toEqual([fake])
  })
  test('quietSpec', () => {
    expect(quietSpec('0-8')).toEqual([0, 8])
    expect(quietSpec(' 22-6 ')).toEqual([22, 6])
    for (const s of ['', 'off', '8', undefined]) expect(quietSpec(s)).toBeNull()
  })
})

describe('QuietClock with the fake tripclock (TELEGRAM_TRIPCLOCK_CMD)', () => {
  const clockFor = (opts: ConstructorParameters<typeof QuietClock>[2] = {}) =>
    new QuietClock(Z, tripclockArgv({ TELEGRAM_TRIPCLOCK_CMD: `${fake} tz` }, dir), { timeoutMs: 3000, ...opts })

  test('home → trip → home: the zone and the quiet verdict switch with tripclock', async () => {
    const logs: string[] = []
    const c = clockFor({ log: m => logs.push(m) })
    setZone(Z)
    expect(c.zone()).toBe(Z)                        // before the first answer: the home zone, at once
    await c.ready()
    expect(c.state()).toMatchObject({ zone: Z, source: 'tripclock' })
    expect(quietIn(c)).toBe(false)                  // 19:00 at home
    setZone('Asia/Tokyo'); await c.refresh()
    expect(c.zone()).toBe('Asia/Tokyo')
    expect(quietIn(c)).toBe(true)                   // 03:00 where he is
    expect(c.state()).toMatchObject({ zone: 'Asia/Tokyo', source: 'tripclock', error: null })
    setZone(Z); await c.refresh()
    expect(c.zone()).toBe(Z)
    expect(quietIn(c)).toBe(false)
    expect(logs).toEqual([`quiet hours: zone ${Z} (tripclock)`, 'quiet hours: zone Asia/Tokyo (tripclock)', `quiet hours: zone ${Z} (tripclock)`])
  })

  test('any error falls back to the home zone (exit code, junk, missing binary, empty output)', async () => {
    const c = clockFor()
    setZone('Asia/Tokyo'); await c.refresh()
    expect(c.zone()).toBe('Asia/Tokyo')
    setZone('fail'); await c.refresh()
    expect(c.zone()).toBe(Z)
    expect(c.state()).toMatchObject({ zone: Z, source: 'home' })
    expect(c.state().error).toContain('exit 3: tripclock: broken config')
    setZone('Mars/Olympus'); await c.refresh()
    expect(c.zone()).toBe(Z)
    expect(c.state().error).toContain('not a time zone')
    setZone(''); await c.refresh()
    expect(c.zone()).toBe(Z)
    const gone = new QuietClock(Z, [join(dir, 'no-such-tripclock'), 'tz'])
    await gone.refresh()
    expect(gone.zone()).toBe(Z)
    expect(gone.state().error).toBeTruthy()
  })

  test('a hanging tripclock: zone() never waits, the lookup is killed at the timeout', async () => {
    const hang = script('hang-tripclock', 'exec sleep 30')
    const c = new QuietClock(Z, [hang, 'tz'], { timeoutMs: 300 })
    const t0 = performance.now()
    expect(c.zone()).toBe(Z)                        // kicks the lookup, answers at once
    expect(performance.now() - t0).toBeLessThan(50)
    await c.ready(5000)
    expect(performance.now() - t0).toBeLessThan(2000)
    expect(c.zone()).toBe(Z)
    expect(c.state().error).toContain('timed out')
  })

  test('cached between lookups: one run per refresh period, a stale cache refreshes in the background', async () => {
    let now = 1_000_000, runs = 0
    let answer = 'Asia/Tokyo'
    const c = new QuietClock(Z, ['fake'], { refreshMs: 600_000, now: () => now, run: async () => { runs++; return answer + '\n' } })
    expect(c.zone()).toBe(Z)                        // first call starts the lookup, doesn't wait for it
    await c.ready()
    expect(runs).toBe(1)
    for (let i = 0; i < 50; i++) expect(c.zone()).toBe('Asia/Tokyo')
    expect(runs).toBe(1)
    answer = Z
    now += 600_000                                  // 10 min later: served from the cache, refreshed behind it
    expect(c.zone()).toBe('Asia/Tokyo')
    await c.ready(); await Bun.sleep(0)
    expect(runs).toBe(2)
    expect(c.zone()).toBe(Z)
  })

  test('one lookup at a time; ready() bounds the wait for the first answer', async () => {
    let release: (s: string) => void = () => {}
    let runs = 0
    const c = new QuietClock(Z, ['fake'], { run: () => { runs++; return new Promise<string>(r => { release = r }) } })
    c.zone(); c.zone(); void c.refresh(); void c.refresh()
    expect(runs).toBe(1)
    const t0 = performance.now()
    await c.ready(100)                              // still running: gives up after ~100 ms
    expect(performance.now() - t0).toBeLessThan(1000)
    expect(c.zone()).toBe(Z)
    release('Asia/Tokyo')
    await c.ready()
    expect(c.zone()).toBe('Asia/Tokyo')
  })

  test('a throwing runner is an error, not a crash', async () => {
    const c = new QuietClock(Z, ['fake'], { run: () => { throw new Error('spawn exploded') } })
    expect(c.zone()).toBe(Z)
    await c.ready()
    expect(c.state().error).toBe('spawn exploded')
  })

  test('no tripclock (off, pinned): the fixed zone, nothing runs', async () => {
    let runs = 0
    const c = new QuietClock('Asia/Tokyo', null, { pinned: true, run: async () => { runs++; return 'UTC' } }).start()
    await c.ready()
    expect(c.zone()).toBe('Asia/Tokyo')
    expect(c.state()).toEqual({ zone: 'Asia/Tokyo', source: 'pinned', checked_at: null, error: null })
    expect(runs).toBe(0)
    c.stop()
  })

  test('start(): first lookup at once, then on the timer', async () => {
    let runs = 0
    const c = new QuietClock(Z, ['fake'], { refreshMs: 40, run: async () => { runs++; return 'Asia/Tokyo' } }).start()
    await c.ready()
    expect(c.zone()).toBe('Asia/Tokyo')
    await Bun.sleep(150)
    c.stop()
    expect(runs).toBeGreaterThanOrEqual(3)
    expect(TRIPCLOCK_REFRESH_MS).toBe(600_000)
  })

  test('the lookup gets the environment minus the bot token', async () => {
    const env = script('env-tripclock', '[ -n "$TELEGRAM_BOT_TOKEN" ] && exit 9\necho "${FAKE_SEEN:-none}"')
    process.env.TELEGRAM_BOT_TOKEN = '123:fake'
    process.env.FAKE_SEEN = 'UTC'
    try {
      expect((await runCommand([env], 3000)).trim()).toBe('UTC')
    } finally {
      delete process.env.TELEGRAM_BOT_TOKEN
      delete process.env.FAKE_SEEN
    }
  })
})
