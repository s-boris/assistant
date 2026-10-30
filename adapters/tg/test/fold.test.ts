// Unit tests for fold.ts: details folded behind a tap (rich details block,
// HTML/MarkdownV2 expandable quote, plain-text entity), escaping, and splitting
// that never cuts a block across messages. Pure functions, no I/O.
// Run:  bun test   (offline.ts covers reply/edit_message end to end)
import { describe, expect, test } from 'bun:test'
import {
  escapeHtml, escapeMarkdownV2, foldFromArgs, foldBlock, foldBlocks, withFold, plainWithFold,
  type FoldFormat, type Piece,
} from '../src/fold.ts'

// Newline-preferring splitter like the server's chunk(..., 'newline').
function split(s: string, n: number): string[] {
  const out: string[] = []
  let r = s
  while (r.length > n) {
    let c = r.lastIndexOf('\n', n)
    if (c <= 0) c = n
    out.push(r.slice(0, c))
    r = r.slice(c).replace(/^\n/, '')
  }
  if (r) out.push(r)
  return out
}
const FORMATS: FoldFormat[] = ['rich', 'html', 'markdownv2', 'text']
const lines = (n: number, w = 60) => Array.from({ length: n }, (_, i) => `line ${i} `.padEnd(w, 'x')).join('\n')

/** The block is complete: opened and closed exactly once in this message. */
function completeBlock(p: Piece, f: FoldFormat): boolean {
  const count = (s: string, sub: string) => s.split(sub).length - 1
  switch (f) {
    case 'rich': return count(p.text, '<details>') === 1 && count(p.text, '</details>') === 1 && p.text.endsWith('</details>')
    case 'html': return count(p.text, '<blockquote expandable>') === 1 && count(p.text, '</blockquote>') === 1 && p.text.endsWith('</blockquote>')
    case 'markdownv2': {
      const q = p.text.slice(p.text.indexOf('**>'))
      return p.text.includes('**>') && q.endsWith('||') && q.split('\n').every((l, i) => (i === 0 ? l.startsWith('**>') : l.startsWith('>')))
    }
    case 'text': {
      const e = p.entities?.find(x => x.type === 'expandable_blockquote')
      return !!e && e.offset + e.length === p.text.length
    }
  }
}

describe('foldFromArgs', () => {
  test('no details or blank details: no fold', () => {
    expect(foldFromArgs(undefined, 'T')).toBeUndefined()
    expect(foldFromArgs('  \n\n ', 'T')).toBeUndefined()
  })
  test('trims blank lines and trailing space, CRLF → LF, title on one line', () => {
    expect(foldFromArgs('\n\n a\r\nb  \n\n', '  Full\n plan ')).toEqual({ body: ' a\nb', title: 'Full plan' })
    expect(foldFromArgs('x', '   ')).toEqual({ body: 'x' })
  })
  test('rejects non-strings', () => {
    expect(() => foldFromArgs(5, undefined)).toThrow(/details must be a string/)
    expect(() => foldFromArgs('x', 5)).toThrow(/details_title/)
  })
})

describe('escaping', () => {
  test('HTML: & < > only', () => {
    expect(escapeHtml('a<b>&"c\'')).toBe('a&lt;b&gt;&amp;"c\'')
  })
  test('MarkdownV2: every special character (Bot API list) and the backslash', () => {
    const special = '_*[]()~`>#+-=|{}.!\\'
    expect(escapeMarkdownV2(special)).toBe(special.split('').map(c => '\\' + c).join(''))
    expect(escapeMarkdownV2('Plan v2.1 (Bern)')).toBe('Plan v2\\.1 \\(Bern\\)')
  })
})

describe('foldBlock', () => {
  test('rich: details block, default title "Details", Markdown body kept', () => {
    expect(foldBlock('- **a**\n- b', 'rich').text).toBe('<details><summary>Details</summary>\n- **a**\n- b\n</details>')
  })
  test('rich: title HTML-escaped; a literal </details> in the body cannot close it early', () => {
    expect(foldBlock('x </details> y', 'rich', 'A<b>&').text)
      .toBe('<details><summary>A&lt;b&gt;&amp;</summary>\nx &lt;/details> y\n</details>')
  })
  test('html: expandable blockquote, bold escaped title, body as given', () => {
    expect(foldBlock('<i>a</i>\nb', 'html', 'Q&A').text).toBe('<blockquote expandable><b>Q&amp;A</b>\n<i>a</i>\nb</blockquote>')
    expect(foldBlock('a', 'html').text).toBe('<blockquote expandable>a</blockquote>')
  })
  test('markdownv2: **> first line, > on every line, || expandability mark, escaped bold title', () => {
    expect(foldBlock('a\n\nb\\.', 'markdownv2', 'Plan (v2)').text).toBe('**>*Plan \\(v2\\)*\n>a\n>\n>b\\.||')
    expect(foldBlock('only', 'markdownv2').text).toBe('**>only||')
  })
  test('text: no markup, expandable_blockquote + bold entities in UTF-16 units', () => {
    const b = foldBlock('body ✓', 'text', '📅 Week')
    expect(b.text).toBe('📅 Week\nbody ✓')
    expect(b.entities).toEqual([
      { type: 'expandable_blockquote', offset: 0, length: b.text.length },
      { type: 'bold', offset: 0, length: '📅 Week'.length },
    ])
    expect('📅 Week'.length).toBe(7) // the emoji is 2 UTF-16 units, as Telegram counts
    expect(foldBlock('x', 'text').entities).toEqual([{ type: 'expandable_blockquote', offset: 0, length: 1 }])
  })
})

describe('withFold', () => {
  test('without details the chunks go out unchanged', () => {
    expect(withFold(['a', 'b'], undefined, 'rich', 100, split)).toEqual([{ text: 'a' }, { text: 'b' }])
  })
  test('rich: joins the last chunk after a blank line; plain fallback carries a quote entity', () => {
    const p = withFold(['Short answer.  \n'], { body: 'more', title: 'Why' }, 'rich', 32000, split)
    expect(p).toHaveLength(1)
    expect(p[0]!.text).toBe('Short answer.\n\n<details><summary>Why</summary>\nmore\n</details>')
    expect(p[0]!.plain).toEqual({
      text: 'Short answer.\nWhy\nmore',
      entities: [{ type: 'expandable_blockquote', offset: 14, length: 8 }, { type: 'bold', offset: 14, length: 3 }],
    })
  })
  test('text: entity offsets shifted past the text (UTF-16, emoji in the text)', () => {
    const p = withFold(['🎬 Two films'], { body: 'a\nb' }, 'text', 4096, split)
    expect(p).toEqual([{ text: '🎬 Two films\na\nb', entities: [{ type: 'expandable_blockquote', offset: 13, length: 3 }] }])
    expect(p[0]!.text.slice(13)).toBe('a\nb')
  })
  test('html / markdownv2: one newline before the block', () => {
    expect(withFold(['Hi'], { body: 'x' }, 'html', 4096, split)[0]!.text).toBe('Hi\n<blockquote expandable>x</blockquote>')
    expect(withFold(['Hi'], { body: 'x' }, 'markdownv2', 4096, split)[0]!.text).toBe('Hi\n**>x||')
  })
  test('a block that does not fit the last chunk becomes its own message (never split)', () => {
    for (const f of FORMATS) {
      const p = withFold(['a'.repeat(4000)], { body: lines(5) }, f, 4096, split)
      expect(p).toHaveLength(2)
      expect(p[0]!.text).toBe('a'.repeat(4000))
      expect(completeBlock(p[1]!, f)).toBe(true)
    }
  })
  test('details only (no text): just the block', () => {
    const p = withFold([], { body: 'x', title: 'T' }, 'html', 4096, split)
    expect(p).toEqual([{ text: '<blockquote expandable><b>T</b>\nx</blockquote>', plain: { text: 'T\nx', entities: [{ type: 'expandable_blockquote', offset: 0, length: 3 }, { type: 'bold', offset: 0, length: 1 }] } }])
  })
  test('long details: several complete blocks, each message ≤ limit, numbered titles, nothing lost', () => {
    const body = lines(200) // ~12k chars
    for (const f of FORMATS) {
      const p = withFold(['Intro'], { body, title: 'Log' }, f, 4096, split)
      expect(p.length).toBeGreaterThan(3)
      for (const x of p) expect(x.text.length).toBeLessThanOrEqual(4096)
      for (const x of p.slice(1)) expect(completeBlock(x, f)).toBe(true)
      expect(completeBlock(p[0]!, f)).toBe(true) // the first part joined "Intro"
      const n = p.length
      expect(p[0]!.text).toContain(f === 'markdownv2' ? `Log \\(1/${n}\\)` : `Log (1/${n})`)
      expect(p[n - 1]!.text).toContain(f === 'markdownv2' ? `Log \\(${n}/${n}\\)` : `Log (${n}/${n})`)
      for (let i = 0; i < 200; i++) expect(p.some(x => x.text.includes(`line ${i} x`))).toBe(true)
    }
  })
  test('rich long details get default numbered titles; legacy formats stay untitled', () => {
    const r = withFold([], { body: lines(200) }, 'rich', 4096, split)
    expect(r[0]!.text.startsWith(`<details><summary>Details (1/${r.length})</summary>`)).toBe(true)
    const h = withFold([], { body: lines(200) }, 'html', 4096, split)
    expect(h.every(x => x.text.startsWith('<blockquote expandable>line'))).toBe(true)
  })
  test('markdownv2 with many short lines (a ">" per line): shrinks until every message fits', () => {
    const body = Array.from({ length: 3000 }, () => 'x').join('\n')
    const p = withFold([], { body }, 'markdownv2', 4096, split)
    for (const x of p) {
      expect(x.text.length).toBeLessThanOrEqual(4096)
      expect(completeBlock(x, 'markdownv2')).toBe(true)
    }
    expect(p.reduce((a, x) => a + (x.text.match(/^(\*\*)?>x/gm)?.length ?? 0), 0)).toBe(3000)
  })
  test('a small chunk limit (access.json textChunkLimit) is respected too', () => {
    const p = withFold(['hello'], { body: lines(10, 30) }, 'text', 100, split)
    for (const x of p) {
      expect(x.text.length).toBeLessThanOrEqual(100)
      expect(completeBlock(x, 'text')).toBe(true)
    }
  })
})

describe('plainWithFold', () => {
  test('raw text, then the details as an expandable quote with a bold title', () => {
    expect(plainWithFold('<b>Hi</b>\n', { body: 'x', title: 'T' })).toEqual({
      text: '<b>Hi</b>\nT\nx',
      entities: [{ type: 'expandable_blockquote', offset: 10, length: 3 }, { type: 'bold', offset: 10, length: 1 }],
    })
  })
})

test('foldBlocks: raw parts and titles kept for the plain fallback', () => {
  const b = foldBlocks({ body: 'a\nb', title: 'T' }, 'rich', 1000, split)
  expect(b).toEqual([{ text: '<details><summary>T</summary>\na\nb\n</details>', raw: 'a\nb', title: 'T' }])
})
