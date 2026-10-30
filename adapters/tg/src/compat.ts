// Compat writers until P10 (Phase 1 plan P2.6), and the local command runner.
//
// Inbound journal: telegram-plus 0.1.22 kept inbound-journal.jsonl (last 100 events / 200 KB, 0600, written
// through a temp file + rename) and other tools read it (web-agent approval evidence, sleep-in, usage,
// menu-usage). While config.compat_journal names that file, every row that becomes visible is appended in
// 0.1.22's shape (server.ts lines 3176-3330: id, at, kind, owner, chat_id, message_id, ts, content, meta,
// status, tries, pid). status is "unverified": the adapter has no Claude Code log to check delivery against
// (the bridge's receipts do that).
//
// runLocal: telegram-plus server.ts lines 1794-1850 (no shell, own process group, the environment minus the
// bot token, stdout ≤ 1 MiB, group SIGTERM then SIGKILL on timeout; never rejects). P3 bindings pass their own
// environment (callback data, PATH) and a group set (killGroups at shutdown).
import { randomBytes } from 'crypto'
import { spawn } from 'child_process'
import { chmodSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'fs'
import { dirname } from 'path'

export const JOURNAL_MAX = 100
export const JOURNAL_MAX_BYTES = 200 * 1024

/** events kind → the plugin's journal kind (InboundKind). */
export const JOURNAL_KIND: Record<string, string> = {
  'tg.message': 'message', 'tg.button': 'button', 'tg.reaction': 'reaction', 'tg.poll_answer': 'poll_answer', 'tg.stop': 'stop',
  'approval.decided': 'button', // P7: 0.1.22 journaled the tap as a button press
}

/** The plugin's meta: every value a string (its channel meta was Record<string, string>). */
export function stringMeta(payload: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(payload)) {
    if (v == null || k === 'text' || k === 'bot' || k === 'update_id') continue
    out[k] = typeof v === 'string' ? v : Array.isArray(v) ? v.join(',') : String(v)
  }
  return out
}

export function journalEntry(kind: string, payload: Record<string, unknown>, owner: boolean, now = Date.now()) {
  const meta = stringMeta(payload)
  const content = String(payload.text ?? '')
  const jkind = meta.handled ? 'instant' : JOURNAL_KIND[kind] ?? kind
  return {
    id: `${now.toString(36)}-${randomBytes(3).toString('hex')}`,
    at: new Date(now).toISOString(),
    kind: jkind,
    owner,
    chat_id: meta.chat_id ?? '',
    ...(meta.message_id ? { message_id: meta.message_id } : {}),
    ...(meta.ts ? { ts: meta.ts } : {}),
    content: content.length > 8000 ? content.slice(0, 7999) + '…' : content,
    meta,
    status: 'unverified',
    tries: 1,
    last_sent_at: new Date(now).toISOString(),
    pid: process.pid,
  }
}

/** Append one entry (read-modify-write: another writer may have appended), capped like 0.1.22. */
export function journalAppend(file: string, entry: Record<string, unknown>): void {
  let lines: string[] = []
  try { lines = readFileSync(file, 'utf8').split('\n').filter(Boolean) } catch {}
  lines.push(JSON.stringify(entry))
  lines = lines.slice(-JOURNAL_MAX)
  let size = lines.reduce((n, l) => n + Buffer.byteLength(l) + 1, 0)
  while (lines.length > 1 && size > JOURNAL_MAX_BYTES) size -= Buffer.byteLength(lines.shift()!) + 1
  mkdirSync(dirname(file), { recursive: true, mode: 0o700 })
  const tmp = `${file}.tmp-${process.pid}`
  try {
    writeFileSync(tmp, lines.join('\n') + '\n', { mode: 0o600 })
    try { chmodSync(tmp, 0o600) } catch {}
    renameSync(tmp, file)
  } catch (err) {
    try { rmSync(tmp, { force: true }) } catch {}
    throw err
  }
}

export type LocalResult = { ok: boolean; stdout: string; error?: string }

/** Bindings (P3): the environment to run with instead of process.env (TELEGRAM_BOT_TOKEN is dropped either way),
 *  the working directory, and a set that holds the process group while it runs (killed at shutdown). */
export type LocalOpts = { env?: Record<string, string | undefined>; cwd?: string; groups?: Set<number> }

export function runLocal(argv: string[], timeoutMs: number, clean: (s: string) => string = s => s, opts: LocalOpts = {}): Promise<LocalResult> {
  return new Promise(resolve => {
    const { TELEGRAM_BOT_TOKEN: _token, ...env } = opts.env ?? process.env
    let child: ReturnType<typeof spawn>
    try {
      child = spawn(argv[0]!, argv.slice(1), { detached: true, stdio: ['ignore', 'pipe', 'pipe'], env, ...(opts.cwd ? { cwd: opts.cwd } : {}) })
    } catch (err) {
      return resolve({ ok: false, stdout: '', error: clean(String((err as Error)?.message ?? err)) })
    }
    const pid = child.pid
    if (pid && opts.groups) opts.groups.add(pid)
    const out: Buffer[] = [], errs: Buffer[] = []
    let outLen = 0, errLen = 0
    child.stdout?.on('data', (d: Buffer) => { if (outLen < 1 << 20) { out.push(d); outLen += d.length } })
    child.stderr?.on('data', (d: Buffer) => { if (errLen < 64 << 10) { errs.push(d); errLen += d.length } })
    let done = false
    const finish = (r: LocalResult) => { if (done) return; done = true; clearTimeout(timer); if (pid) opts.groups?.delete(pid); resolve(r) }
    const kill = (sig: NodeJS.Signals) => { try { if (pid) process.kill(-pid, sig) } catch {} }
    const timer = setTimeout(() => {
      kill('SIGTERM')
      setTimeout(() => kill('SIGKILL'), 2_000).unref?.()
      finish({ ok: false, stdout: '', error: `timed out after ${Math.round(timeoutMs / 100) / 10} s` })
    }, timeoutMs)
    child.on('error', err => finish({ ok: false, stdout: '', error: clean(err.message) }))
    child.on('close', (code, signal) => {
      const stdout = Buffer.concat(out).toString('utf8')
      if (code === 0) return finish({ ok: true, stdout })
      const lines = Buffer.concat(errs).toString('utf8').trim().split('\n')
      const last = lines[lines.length - 1] ?? ''
      finish({ ok: false, stdout, error: clean(`${code != null ? `exit ${code}` : `killed by ${signal}`}${last ? `: ${last}` : ''}`) })
    })
  })
}

/** Kill every process group in the set (SIGTERM, SIGKILL 2 s later): the adapter is stopping. */
export function killGroups(groups: Set<number>): void {
  for (const pid of groups) {
    try { process.kill(-pid, 'SIGTERM') } catch {}
    setTimeout(() => { try { process.kill(-pid, 'SIGKILL') } catch {} }, 2_000).unref?.()
  }
}
