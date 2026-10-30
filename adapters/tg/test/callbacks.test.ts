import { describe, expect, test } from 'bun:test'
import { parseCallbacks, pickCallback, toastFor, replyText, CALLBACK_TIMEOUT_MS, CALLBACK_TIMEOUT_MAX_MS, TOAST_MAX, REPLY_MAX } from '../src/callbacks.ts'

const deps = {
  splitArgv: (s: string) => s.trim().split(/\s+/).map(a => a.replace(/^~(?=\/)/, '/home/x')),
  home: '/home/x',
}

describe('parseCallbacks', () => {
  test('valid table: run as string or argv, data as string or list, toast, timeouts, note', () => {
    const { list, skipped } = parseCallbacks([
      { name: 'Talk', data: 'call:talk', run: '~/bin/talk-button', toast: '  📞 Calling you…  ', timeout_ms: 120000 },
      { name: 'lights', data: ['lt:off', 'lt:on', 'lt:off'], run: ['~/bin/ha', 'x'], note: true },
      { name: 'slow', data: 'slow', run: 'x', timeout_ms: 10 ** 9 },
    ], deps)
    expect(skipped).toEqual([])
    expect(list[0]).toEqual({ name: 'talk', data: ['call:talk'], argv: ['/home/x/bin/talk-button'], toast: '📞 Calling you…', timeoutMs: 120000, note: false })
    expect(list[1]).toEqual({ name: 'lights', data: ['lt:off', 'lt:on'], argv: ['/home/x/bin/ha', 'x'], toast: '', timeoutMs: CALLBACK_TIMEOUT_MS, note: true })
    expect(list[2]!.timeoutMs).toBe(CALLBACK_TIMEOUT_MAX_MS)
  })

  test('bad entries are skipped with a reason; the rest still load', () => {
    const { list, skipped } = parseCallbacks([
      { name: 'ok', data: 'a', run: 'x' },
      { name: 'bad name!', data: 'b', run: 'x' },
      { name: 'ok', data: 'c', run: 'x' },
      { name: 'nodata', run: 'x' },
      { name: 'long', data: 'x'.repeat(65), run: 'x' },
      { name: 'perm', data: 'perm:allow:abcde', run: 'x' },
      { name: 'dup', data: ['z', 'a'], run: 'x' },
      { name: 'norun', data: 'n' },
      { name: 'toast', data: 't', run: 'x', toast: 'y'.repeat(TOAST_MAX + 1) },
      { name: 'toast2', data: 't2', run: 'x', toast: 5 },
      { name: 'note', data: 'nn', run: 'x', note: 'yes' },
      { name: 'emptyargv', data: 'e', run: [] },
    ], deps)
    expect(list.map(c => c.name)).toEqual(['ok'])
    expect(skipped).toEqual([
      '#2 (invalid name "bad name!")',
      '#3 ("ok" twice)',
      '#4 ("nodata": no "data")',
      '#5 ("long": data must be 1-64 bytes)',
      '#6 ("perm": "perm:" data is the permission relay\'s)',
      '#7 ("dup": data "a" already used by "ok")',
      '#8 ("norun": no "run")',
      `#9 ("toast": toast must be text up to ${TOAST_MAX} chars)`,
      `#10 ("toast2": toast must be text up to ${TOAST_MAX} chars)`,
      '#11 ("note": note must be true or false)',
      '#12 ("emptyargv": no "run")',
    ])
  })

  test('64 bytes counts UTF-8 bytes, not characters', () => {
    const { list, skipped } = parseCallbacks([
      { name: 'a', data: '📞'.repeat(16), run: 'x' },
      { name: 'b', data: '📞'.repeat(17), run: 'x' },
    ], deps)
    expect(list.map(c => c.name)).toEqual(['a'])
    expect(skipped).toEqual(['#2 ("b": data must be 1-64 bytes)'])
  })

  test('not an array → nothing', () => {
    expect(parseCallbacks({ name: 'x' }, deps)).toEqual({ list: [], skipped: ['the whole file (not a JSON array)'] })
  })
})

describe('pickCallback / toastFor / replyText', () => {
  const { list } = parseCallbacks([
    { name: 'talk', data: 'call:talk', run: 'x', toast: '📞 Calling you…' },
    { name: 'multi', data: ['m1', 'm2'], run: 'x' },
  ], deps)

  test('exact callback_data only', () => {
    expect(pickCallback(list, 'call:talk')?.name).toBe('talk')
    expect(pickCallback(list, 'm2')?.name).toBe('multi')
    expect(pickCallback(list, 'call:talk2')).toBeNull()
    expect(pickCallback(list, 'call:')).toBeNull()
    expect(pickCallback([], 'call:talk')).toBeNull()
  })

  test('toast: own text, else ✓ label, capped', () => {
    expect(toastFor(list[0]!, '📞 Talk')).toBe('📞 Calling you…')
    expect(toastFor(list[1]!, 'Go')).toBe('✓ Go')
    const t = toastFor(list[1]!, 'L'.repeat(300))
    expect(t.length).toBe(TOAST_MAX)
    expect(t.endsWith('…')).toBe(true)
  })

  test('reply text: trimmed, capped, empty stays empty', () => {
    expect(replyText('\n  hi there \n')).toBe('hi there')
    expect(replyText(' \n ')).toBe('')
    expect(replyText('a'.repeat(REPLY_MAX + 50)).length).toBe(REPLY_MAX)
  })
})

describe('prefix entries (0.1.21)', () => {
  const deps = { splitArgv: (x: string) => x.split(/\s+/), home: '/h' }
  test('"rm:*" matches every rm: press; exact entries win; longest prefix wins', () => {
    const { list, skipped } = parseCallbacks([
      { name: 'rem', data: 'rm:*', run: '/bin/true' },
      { name: 'remdone', data: 'rm:done:*', run: '/bin/true' },
      { name: 'exact', data: 'rm:stom:abcd1234', run: '/bin/true' },
    ], deps)
    expect(skipped).toEqual([])
    expect(pickCallback(list, 'rm:s1h:0123abcd')?.name).toBe('rem')
    expect(pickCallback(list, 'rm:done:0123abcd')?.name).toBe('remdone')
    expect(pickCallback(list, 'rm:stom:abcd1234')?.name).toBe('exact')
    expect(pickCallback(list, 'rx:1')).toBeNull()
    expect(pickCallback(list, 'rm')).toBeNull()
  })
  test('prefixes that could cover perm: or are too short are skipped', () => {
    const { list, skipped } = parseCallbacks([
      { name: 'p1', data: 'pe*', run: '/bin/true' },
      { name: 'p2', data: 'perm:*', run: '/bin/true' },
      { name: 'short', data: 'r*', run: '/bin/true' },
      { name: 'star', data: '*', run: '/bin/true' },
    ], deps)
    expect(list).toEqual([])
    expect(skipped.length).toBe(4)
  })
  test('perm: presses never reach an instant button', () => {
    const { list } = parseCallbacks([{ name: 'any', data: 'pa*', run: '/bin/true' }], deps)
    expect(pickCallback(list, 'perm:allow:x')).toBeNull()
  })
})
