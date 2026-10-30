// Hub mode (v2 Phase 2 H6): the adapter in the hub's tg container talks to hubd (the tg caller), and the companion in
// the brain container talks to hubd as the brain. This file: the HTTP client both use, hub file refs and the token.
//
// HubClient: fetch with keep-alive (Bun reuses connections), "Authorization: Bearer <token>", JSON in and out, a
// fresh request id ("rid") on every POST (hubd answers a repeated rid from its record), "X-Hub-Schema: t_<name>" for
// test schemas. Retries: network errors, timeouts and 5xx (incl. 503 store unavailable) are retried with backoff up to
// `retries` times (0.2 s, 0.6 s, 1.8 s, …); 4xx are not (HubError with the hub's {error, hint}). Every write the adapter
// makes is idempotent by design (idem_key rows, conditional claims, set/patch merges), so a retry after an unknown
// outcome never duplicates anything.
//
// Hub files (contract: ~/jobs/v2-h6/contract.md): a shared dir mounted at hub.files_dir in the tg container and served
// by hubd. `hubfile:in/<name>` = the adapter's downloads (files_dir/in), `hubfile:out/<name>` = brain uploads
// (files_dir/out). The brain never sees a hub path, the adapter never a brain path.
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'fs'
import { basename, dirname, isAbsolute, join, relative, resolve } from 'path'
import { randomBytes } from 'crypto'

export const REF_RE = /^hubfile:(in|out)\/([A-Za-z0-9_][A-Za-z0-9._-]{0,119})$/
export const NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,119}$/ // hubd's FILE_NAME (hubtg.py)
export const DEFAULT_TIMEOUT_MS = 10_000

export class HubError extends Error {
  constructor(message: string, readonly status: number | null, readonly code: string, readonly retryable: boolean) { super(message) }
}

export type HubOpts = {
  url: string
  token: string
  /** A t_* test schema (tests and rehearsals; unset = live). */
  schema?: string
  /** Tests: a fake hub (no sockets). */
  fetch?: (req: Request) => Promise<Response>
  timeoutMs?: number
  retries?: number
  log?: (m: string) => void
}

export const newRid = (p = 'tga') => `${p}-${Date.now().toString(36)}-${randomBytes(6).toString('hex')}`

export class HubClient {
  readonly url: string
  lastOkAt = 0
  lastError = ''
  lastErrorAt = 0
  calls = 0
  errors = 0

  constructor(readonly o: HubOpts) {
    this.url = o.url.replace(/\/+$/, '')
    if (!/^https?:\/\//.test(this.url)) throw new Error('hub url must be http(s)')
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    return {
      authorization: `Bearer ${this.o.token}`,
      ...(this.o.schema ? { 'x-hub-schema': this.o.schema } : {}),
      ...extra,
    }
  }

  private async send(method: string, path: string, init: { body?: BodyInit; headers?: Record<string, string>; timeoutMs?: number }): Promise<Response> {
    const req = new Request(this.url + path, {
      method, headers: this.headers(init.headers), body: init.body,
      signal: AbortSignal.timeout(init.timeoutMs ?? this.o.timeoutMs ?? DEFAULT_TIMEOUT_MS),
    })
    return this.o.fetch ? this.o.fetch(req) : fetch(req)
  }

  /** One JSON call. POST bodies get a fresh rid unless they carry one. Throws HubError. */
  async call<T = any>(method: 'GET' | 'POST', path: string, body?: Record<string, unknown>, opts: { timeoutMs?: number; retries?: number } = {}): Promise<T> {
    const payload = method === 'POST' ? JSON.stringify({ rid: newRid(), ...(body ?? {}) }) : undefined
    const retries = opts.retries ?? this.o.retries ?? 2
    let last: HubError | null = null
    for (let attempt = 0; attempt <= retries; attempt++) {
      if (attempt) await Bun.sleep(200 * 3 ** (attempt - 1))
      this.calls++
      let res: Response
      try {
        res = await this.send(method, path, { body: payload, headers: payload ? { 'content-type': 'application/json' } : {}, timeoutMs: opts.timeoutMs })
      } catch (err) {
        last = new HubError(`hub unreachable: ${oneLine(err)}`, null, 'unreachable', true)
        this.fail(last)
        continue
      }
      let data: any = null
      const text = await res.text().catch(() => '')
      try { data = text ? JSON.parse(text) : null } catch { data = null }
      if (res.ok) { this.lastOkAt = Date.now(); return data as T }
      const code = typeof data?.error === 'string' ? data.error : `http_${res.status}`
      const hint = typeof data?.hint === 'string' ? `: ${data.hint}` : ''
      last = new HubError(`hub ${method} ${path.split('?')[0]} → ${res.status} ${code}${hint}`.slice(0, 300), res.status, code, res.status >= 500)
      this.fail(last)
      if (!last.retryable) throw last
    }
    throw last ?? new HubError('hub call failed', null, 'unknown', true)
  }

  /** Raw bytes in (POST /tg/files) → the hub ref. */
  async upload(bytes: Uint8Array, name: string, timeoutMs = 120_000): Promise<string> {
    const safe = safeName(name)
    let last: unknown = null
    for (let attempt = 0; attempt < 3; attempt++) {
      if (attempt) await Bun.sleep(300 * 3 ** (attempt - 1))
      try {
        const res = await this.send('POST', '/tg/files', { body: bytes, headers: { 'content-type': 'application/octet-stream', 'x-file-name': safe }, timeoutMs })
        const data: any = await res.json().catch(() => null)
        if (res.ok && typeof data?.ref === 'string' && REF_RE.test(data.ref)) { this.lastOkAt = Date.now(); return data.ref }
        last = new HubError(`hub upload → ${res.status} ${data?.error ?? ''}`.trim(), res.status, data?.error ?? `http_${res.status}`, res.status >= 500)
        if (res.status < 500) break
      } catch (err) { last = err }
    }
    const e = last instanceof HubError ? last : new HubError(`hub upload failed: ${oneLine(last)}`, null, 'unreachable', true)
    this.fail(e)
    throw e
  }

  /** A hub file's bytes (GET /tg/files/<in|out>/<name>). */
  async download(ref: string, timeoutMs = 120_000): Promise<Uint8Array> {
    const m = REF_RE.exec(ref)
    if (!m) throw new HubError(`not a hub file ref: ${ref.slice(0, 80)}`, null, 'bad_ref', false)
    const res = await this.send('GET', `/tg/files/${m[1]}/${m[2]}`, { timeoutMs }).catch(err => {
      const e = new HubError(`hub unreachable: ${oneLine(err)}`, null, 'unreachable', true)
      this.fail(e)
      throw e
    })
    if (!res.ok) {
      const e = new HubError(`hub file ${m[1]}/${m[2]} → ${res.status}`, res.status, `http_${res.status}`, res.status >= 500)
      this.fail(e)
      throw e
    }
    this.lastOkAt = Date.now()
    return new Uint8Array(await res.arrayBuffer())
  }

  private fail(e: HubError): void {
    this.errors++
    this.lastError = e.message
    this.lastErrorAt = Date.now()
  }
}

export function oneLine(err: unknown): string {
  const s = String((err as Error)?.message ?? err).replace(/\s+/g, ' ').trim()
  return s.length <= 200 ? s : s.slice(0, 199) + '…'
}

/** A name hubd will serve (NAME_RE): starts with [A-Za-z0-9_], then [A-Za-z0-9._-], ≤ 120 chars. */
export function safeName(name: string): string {
  let n = basename(String(name || 'file')).replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^[.-]+/, '_')
  if (n.length > 120) { const ext = n.match(/\.[A-Za-z0-9]{1,8}$/)?.[0] ?? ''; n = n.slice(0, 120 - ext.length) + ext }
  return n || 'file'
}

/** The hub token file: one line, the token. Never in a Claude config dir; not readable by group or others. */
export function readHubToken(file: string): string {
  let st
  try { st = statSync(file) } catch { throw new Error(`hub token file missing: ${file}`) }
  if (!st.isFile()) throw new Error(`hub token file is not a file: ${file}`)
  if ((st.mode & 0o077) !== 0) throw new Error(`hub token file must not be readable by group or others (chmod 600): ${file}`)
  const line = readFileSync(file, 'utf8').split('\n').map(l => l.trim()).find(Boolean) ?? ''
  const tok = line.includes('=') && /^[A-Z_]+=/.test(line) ? line.slice(line.indexOf('=') + 1).trim() : line
  if (tok.length < 16) throw new Error(`hub token file holds no token: ${file}`)
  return tok
}

// --- hub files on the adapter's side -----------------------------------------------------------------------------

export type HubFiles = { dir: string; inDir: string; outDir: string }

export function hubFiles(dir: string): HubFiles {
  return { dir, inDir: join(dir, 'in'), outDir: join(dir, 'out') }
}

export function ensureHubFiles(f: HubFiles): void {
  for (const d of [f.dir, f.inDir, f.outDir]) if (!existsSync(d)) mkdirSync(d, { recursive: true, mode: 0o770 })
}

/** A file under files_dir/in → its ref (renamed to a servable name when needed). Anything else is refused. */
export function toRef(f: HubFiles, path: string): string {
  const p = resolve(path)
  const rel = relative(f.inDir, p)
  if (!rel || rel.startsWith('..') || isAbsolute(rel) || rel.includes('/')) throw new Error(`not in the hub's download dir: ${basename(p)}`)
  if (NAME_RE.test(rel) && rel !== '.' && rel !== '..') return `hubfile:in/${rel}`
  const safe = safeName(rel)
  const to = join(f.inDir, safe)
  renameSync(p, to)
  return `hubfile:in/${safe}`
}

/** A ref (or a path already under files_dir) → the absolute path in this container; other strings pass through
 *  unchanged (the file guard refuses them in hub mode). */
export function fromRef(f: HubFiles, s: string): string {
  const m = REF_RE.exec(s)
  if (!m) return s
  return join(m[1] === 'in' ? f.inDir : f.outDir, m[2]!)
}

export function isRef(s: unknown): s is string {
  return typeof s === 'string' && REF_RE.test(s)
}

/** A 0600 file write through a temp name (the presence mirror). */
export function writeAtomic(path: string, text: string): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
  const tmp = `${path}.tmp-${process.pid}`
  writeFileSync(tmp, text, { mode: 0o600 })
  try { chmodSync(tmp, 0o600) } catch {}
  renameSync(tmp, path)
}
