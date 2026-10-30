import { describe, expect, test } from 'bun:test'
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, realpathSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { parsePicture, pictureRoots, pictureProblem, pictureFallback, CAPTION_MAX } from '../src/picture.ts'
import { png } from './images.ts'

describe('parsePicture', () => {
  test('one JSON object with a photo path → picture; caption and text optional, trimmed', () => {
    expect(parsePicture('{"photo": "/tmp/a.png", "caption": " Space "}\n')).toEqual({ photo: '/tmp/a.png', caption: 'Space', text: '' })
    expect(parsePicture('  {"photo":"/tmp/a.png","text":"full card"}  ')).toEqual({ photo: '/tmp/a.png', caption: '', text: 'full card' })
  })
  test('plain text, other JSON, bad types → null (stays a text answer)', () => {
    for (const s of ['💳 3 bills open', '', '{}', '[{"photo":"/tmp/a.png"}]', '{"photo": ""}', '{"photo": 5}',
      '{"photo":"/tmp/a.png","caption":3}', '{"photo":"/tmp/a.png","text":null}', '{"photo":"/tmp/a.png"} trailing',
      'Disk: {"photo":"/tmp/a.png"}', '{not json}', 'null']) {
      expect(parsePicture(s)).toBeNull()
    }
  })
})

describe('pictureProblem', () => {
  const home = mkdtempSync(join(tmpdir(), 'tgp-pic-home-'))
  const cardDir = join(home, '.local', 'state', 'cardimg')
  mkdirSync(cardDir, { recursive: true })
  const good = join(cardDir, 'space.png')
  writeFileSync(good, png(1080, 1350))
  const tmpGood = join(home, 'tmp-card.png') // home itself lives under /tmp in the test
  writeFileSync(tmpGood, png(1080, 1350))
  const roots = pictureRoots(home)

  test('roots: the cardimg dir and /tmp, symlinks resolved', () => {
    expect(roots).toEqual([realpathSync(cardDir), realpathSync('/tmp')])
  })
  test('a 1080x1350 PNG in the cardimg dir or under /tmp is fine', () => {
    expect(pictureProblem(good, roots)).toBeNull()
    if (realpathSync(home).startsWith(realpathSync('/tmp') + '/')) expect(pictureProblem(tmpGood, roots)).toBeNull()
  })
  test('outside the roots, also via "..", a symlink or a prefix lookalike → refused', () => {
    expect(pictureProblem('/etc/passwd', roots)).toBe('photo path is outside the allowed folders')
    expect(pictureProblem('/tmp/../etc/passwd', roots)).toBe('photo path is outside the allowed folders')
    const link = join(cardDir, 'sneaky.png')
    symlinkSync('/etc/passwd', link)
    expect(pictureProblem(link, roots)).toBe('photo path is outside the allowed folders')
    const lookalike = mkdtempSync(join(tmpdir(), 'tgp-pic-look-'))
    mkdirSync(join(lookalike, 'cardimg-evil'))
    writeFileSync(join(lookalike, 'cardimg-evil', 'x.png'), png(10, 10))
    expect(pictureProblem(join(lookalike, 'cardimg-evil', 'x.png'), [join(lookalike, 'cardimg')])).toBe('photo path is outside the allowed folders')
  })
  test('missing, relative, wrong type, empty, directory, unreadable shape → a reason', () => {
    expect(pictureProblem(join(cardDir, 'gone.png'), roots)).toBe('photo file is missing')
    expect(pictureProblem('card.png', roots)).toBe('photo path is not absolute')
    writeFileSync(join(cardDir, 'notes.txt'), 'x')
    expect(pictureProblem(join(cardDir, 'notes.txt'), roots)).toBe('photo is not a .png/.jpg/.webp file')
    writeFileSync(join(cardDir, 'empty.png'), '')
    expect(pictureProblem(join(cardDir, 'empty.png'), roots)).toBe('photo file is empty')
    mkdirSync(join(cardDir, 'dir.png'))
    expect(pictureProblem(join(cardDir, 'dir.png'), roots)).toBe('photo is not a regular file')
    writeFileSync(join(cardDir, 'tall.png'), png(1080, 7000))
    expect(pictureProblem(join(cardDir, 'tall.png'), roots)).toMatch(/too tall/)
  })
})

describe('pictureFallback', () => {
  test('text, else caption, else a ⚠️ line naming the command and reason', () => {
    expect(pictureFallback({ photo: '/x', caption: 'cap', text: 'full' }, '/space', 'gone')).toBe('full')
    expect(pictureFallback({ photo: '/x', caption: 'cap', text: '' }, '/space', 'gone')).toBe('cap')
    expect(pictureFallback({ photo: '/x', caption: '', text: '' }, '/space', 'photo file is missing'))
      .toBe("⚠️ /space: the picture couldn't be sent (photo file is missing).")
    expect(CAPTION_MAX).toBe(1024)
  })
})
