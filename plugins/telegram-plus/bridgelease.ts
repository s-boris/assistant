// Bridge mode: who may take the owner's inbound events.
//
// Two conditions, both required (else the bridge serves tools only):
//  1. Eligible: the nearest Claude Code ancestor registers telegram-plus as a channel (--channels or
//     --dangerously-load-development-channels), or there is no Claude Code ancestor at all (a manual run,
//     tests). The check is lifted from server.ts 0.1.22 (procArgs, parentPid, isClaudeProcess,
//     claudeAncestorArgs, registersUsAsChannel), so a worker or agent that merely has the plugin enabled
//     never consumes events.
//  2. The lease: an exclusive flock(2) on <state dir>/lease, held for the life of the process (the kernel
//     drops it when the process dies, however it dies). The file holds the holder's pid, version and
//     start time as JSON for status tools; `flock -n <lease> true` exits 1 while a bridge holds it.
import { execFileSync } from 'child_process'
import { closeSync, ftruncateSync, mkdirSync, openSync, readFileSync, writeSync } from 'fs'
import { dirname } from 'path'
import { dlopen, FFIType } from 'bun:ffi'

export function procArgs(pid: number): string[] {
  try {
    return readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').filter(Boolean)
  } catch {}
  try {
    return execFileSync('ps', ['-p', String(pid), '-o', 'args='], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim().split(/\s+/)
  } catch {
    return []
  }
}

export function parentPid(pid: number): number {
  try {
    const stat = readFileSync(`/proc/${pid}/stat`, 'utf8')
    return Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]) || 0
  } catch {}
  try {
    return Number(execFileSync('ps', ['-p', String(pid), '-o', 'ppid='], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()) || 0
  } catch {
    return 0
  }
}

export function isClaudeProcess(args: string[]): boolean {
  const exe = args[0] ?? ''
  return /(^|\/)claude$/.test(exe) || /\/claude\/versions\//.test(exe)
}

/** argv of the nearest Claude Code ancestor of pid; null if there is none. */
export function claudeAncestorArgs(pid: number): string[] | null {
  let p = parentPid(pid)
  for (let i = 0; i < 8 && p > 1; i++) {
    const args = procArgs(p)
    if (isClaudeProcess(args)) return args
    p = parentPid(p)
  }
  return null
}

export function registersUsAsChannel(args: string[]): boolean {
  for (let i = 0; i < args.length; i++) {
    const m = /^--(?:channels|dangerously-load-development-channels)(?:=(.*))?$/.exec(args[i]!)
    if (!m) continue
    const values = m[1] != null ? [m[1]] : []
    for (let j = i + 1; m[1] == null && j < args.length && !args[j]!.startsWith('-'); j++) values.push(args[j]!)
    if (values.some(v => /(^|[\s,:])telegram-plus(@|[\s,]|$)/.test(v))) return true
  }
  return false
}

/** Why this bridge may never take inbound events, or null if it is eligible. TELEGRAM_BRIDGE_TOOLS_ONLY=1
 *  vetoes, TELEGRAM_BRIDGE_FORCE=1 skips the ancestor check (tests). */
export function bridgeVeto(env: Record<string, string | undefined> = process.env): string | null {
  if (env.TELEGRAM_BRIDGE_TOOLS_ONLY === '1') return 'TELEGRAM_BRIDGE_TOOLS_ONLY=1'
  if (env.TELEGRAM_BRIDGE_FORCE === '1') return null
  const claude = claudeAncestorArgs(process.pid)
  if (!claude) return null // not started by Claude Code (manual run)
  if (!registersUsAsChannel(claude)) return 'this Claude session has no --channels entry for telegram-plus'
  return null
}

// flock(2) through libc; bun has no flock in node:fs. Loaded lazily: a failure means no lease (tools only).
const LOCK_EX = 2
const LOCK_NB = 4
let flockFn: ((fd: number, op: number) => number) | null | undefined

function flock(): ((fd: number, op: number) => number) | null {
  if (flockFn !== undefined) return flockFn
  try {
    const libc = dlopen('libc.so.6', { flock: { args: [FFIType.i32, FFIType.i32], returns: FFIType.i32 } })
    flockFn = (fd: number, op: number) => libc.symbols.flock(fd, op) as number
  } catch (err) {
    process.stderr.write(`telegram bridge: flock unavailable (${err}); no lease, tools only\n`)
    flockFn = null
  }
  return flockFn
}

export class Lease {
  private fd: number | null = null
  constructor(readonly path: string) {}

  get held(): boolean { return this.fd != null }

  /** Non-blocking: true once this process holds the lease (stays true until release or exit). */
  tryAcquire(info: Record<string, unknown>): boolean {
    if (this.fd != null) return true
    const f = flock()
    if (!f) return false
    let fd: number
    try {
      mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 })
      fd = openSync(this.path, 'a+', 0o600)
    } catch (err) {
      process.stderr.write(`telegram bridge: lease file: ${err}\n`)
      return false
    }
    if (f(fd, LOCK_EX | LOCK_NB) !== 0) {
      closeSync(fd)
      return false
    }
    this.fd = fd
    try {
      ftruncateSync(fd, 0)
      writeSync(fd, JSON.stringify({ ...info, pid: process.pid, since: new Date().toISOString() }) + '\n', 0)
    } catch {}
    return true
  }

  release(): void {
    if (this.fd == null) return
    try { ftruncateSync(this.fd, 0) } catch {}
    try { closeSync(this.fd) } catch {}
    this.fd = null
  }
}
