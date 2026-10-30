// Guard: the MCP SDK must stay on a v1 line that does not speak protocol revision 2026-07-28.
// With that revision (the v2 SDK) Claude Code silently drops channel notifications, so the
// owner's inbound Telegram messages would vanish without any error. This test fails if the
// declared range, the lockfile's resolved version or the installed package would bring it in.
// Run:  bun test   (or: bun test test/sdk-guard.test.ts)
import { describe, expect, test } from 'bun:test'
import { readFileSync } from 'fs'
import { join } from 'path'
import { LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS } from '@modelcontextprotocol/sdk/types.js'

const ROOT = join(import.meta.dir, '..')
const SDK = '@modelcontextprotocol/sdk'
const BAD_REVISION = '2026-07-28'
// v2 ships as split packages; none of them may appear as a dependency.
const V2_PACKAGES = ['@modelcontextprotocol/server', '@modelcontextprotocol/client', '@modelcontextprotocol/core']

const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'))
const allDeps: Record<string, string> = {
  ...pkg.dependencies, ...pkg.devDependencies, ...pkg.optionalDependencies, ...pkg.peerDependencies,
}
const major = (v: string) => Number(v.split('.')[0])

describe('MCP SDK guard (no protocol 2026-07-28)', () => {
  test('package.json pins the SDK to a 1.x range', () => {
    const range = allDeps[SDK]
    expect(range).toBeString()
    // Only ^1.x.y, ~1.x.y or an exact 1.x.y: anything wider (>=, *, ||, ^2, latest) could pull v2.
    expect(range).toMatch(/^[\^~]?1\.\d+\.\d+$/)
  })

  test('no v2 split packages are declared', () => {
    for (const name of V2_PACKAGES) expect(allDeps[name]).toBeUndefined()
  })

  test('bun.lock resolves the SDK to 1.x and has no v2 packages', () => {
    const lock = readFileSync(join(ROOT, 'bun.lock'), 'utf8')
    const resolved = [...lock.matchAll(/"@modelcontextprotocol\/sdk@(\d+\.\d+\.\d+[^"]*)"/g)].map(m => m[1])
    expect(resolved.length).toBeGreaterThan(0)
    for (const v of resolved) expect(major(v)).toBe(1)
    for (const name of V2_PACKAGES) expect(lock.includes(`"${name}@`)).toBe(false)
  })

  test('installed SDK is 1.x and does not support the revision', () => {
    const installed = JSON.parse(readFileSync(join(ROOT, 'node_modules', SDK, 'package.json'), 'utf8')).version
    expect(major(installed)).toBe(1)
    expect(LATEST_PROTOCOL_VERSION).not.toBe(BAD_REVISION)
    expect(SUPPORTED_PROTOCOL_VERSIONS).not.toContain(BAD_REVISION)
    // Revisions are ISO dates: none at or after the bad one may be supported.
    for (const r of SUPPORTED_PROTOCOL_VERSIONS) expect(r < BAD_REVISION).toBe(true)
  })
})
