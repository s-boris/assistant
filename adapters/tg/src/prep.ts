// Inbound work before a row shows (Phase 1 plan P2.3), lifted from telegram-plus server.ts (commit 974bed5):
//   photo       downloaded eagerly into the instance inbox → payload.image_path (lines 4026-4046)
//   transcribe  voice notes, audio and video notes: downloaded and run through ~/bin/transcribe (60 s, ≤ 2 at
//               once, ≤ 20 MB, ≤ 10 min); the row's text becomes "🎙️ (voice, 12 s) …" + meta transcript="auto",
//               or meta transcribe_error (lines 1411-1419, 2069-2192); the audio stays 10 min for the download op
//   scan        "/scan private" | "/sp" photos and documents (and the rest of such an album) are filed into
//               paperless (`paperless upload … --tag Inbox --tag private --create`) and never reach the brain: the
//               row carries the command, handled="scan-private" and doc_id or a scan_error code, never the
//               caption, title or file id (lines 1873-2068, scan.ts verbatim). Videos, audio and voice notes with
//               such a caption are refused, not forwarded. With scan_private off the message is refused the same
//               way (scan_error "off"): its content never shows.
// Crash safety and order: a row that needs work is committed with its offset as status 'pending' and only
// becomes 'new' (visible to the agenda and the bridge) when its work is done (until then its delivery reads
// "held:now"; the release re-inserts it under a fresh id, see Store.releaseRow); later rows of the same chat are
// committed 'pending' behind it and released in id order, as 0.1.22's per-chat delivery queue did. Rows left
// pending by a killed process are released at the next start with transcribe_error / scan_error "interrupted".
import { chmodSync, mkdtempSync, readdirSync, renameSync, rmSync, statSync } from 'fs'
import { tmpdir } from 'os'
import { basename, dirname, join } from 'path'
import { accessSync, constants as fsConstants } from 'fs'
import { gateMessage, type Access } from './access.ts'
import { runLocal } from './compat.ts'
import type { EventRow, Store } from './db.ts'
import { errText, type TgCall } from './format.ts'
import { truncate } from './inbound.ts'
import { parseScanPrivate, scanDocId, scanFailReason, scanFileName, scanTitle } from './scan.ts'

export const TRANSCRIBE_TIMEOUT_MS = 60_000
export const TRANSCRIBE_KINDS = new Set(['voice', 'audio', 'video_note'])
export const TRANSCRIBE_MAX_BYTES = 20 * 1024 * 1024
export const TRANSCRIBE_MAX_S = 10 * 60
export const TRANSCRIBE_PARALLEL = 2
export const TRANSCRIPT_KEEP_MS = 10 * 60_000
export const TX_PREFIX = 'transcribe-'
export const SCAN_TIMEOUT_MS = 300_000
export const SCAN_MAX_BYTES = 20 * 1024 * 1024
export const SCAN_ALBUM_MS = 10 * 60_000
export const SCAN_PREFIX = 'tga-scan-'
const KIND_LABEL: Record<string, string> = { voice: 'voice', audio: 'audio', video_note: 'video note' }

const isExecutableFile = (p: string) => { try { accessSync(p, fsConstants.X_OK); return statSync(p).isFile() } catch { return false } }
const splitArgv = (v: string, home: string) => v.trim().split(/\s+/).filter(Boolean).map(a => a.replace(/^~(?=\/)/, home))

/** TG_ADAPTER_TRANSCRIBE_CMD ("" = off), else ~/bin/transcribe when executable. */
export function transcribeArgv(env: Record<string, string | undefined>, home: string): string[] | null {
  const v = env.TG_ADAPTER_TRANSCRIBE_CMD
  if (v !== undefined) { const a = splitArgv(v, home); return a.length ? a : null }
  const p = join(home, 'bin', 'transcribe')
  return isExecutableFile(p) ? [p] : null
}

/** TG_ADAPTER_SCAN_CMD ("" = off), else `paperless` on PATH, else ~/bin/paperless. */
export function scanArgv(env: Record<string, string | undefined>, home: string): string[] | null {
  const v = env.TG_ADAPTER_SCAN_CMD
  if (v !== undefined) { const a = splitArgv(v, home); return a.length ? a : null }
  for (const d of (env.PATH ?? '').split(':')) if (d && isExecutableFile(join(d, 'paperless'))) return [join(d, 'paperless')]
  const own = join(home, 'bin', 'paperless')
  return isExecutableFile(own) ? [own] : null
}

export function fmtDuration(s: number | undefined): string | null {
  if (s == null || !Number.isFinite(s) || s < 0) return null
  if (s < 60) return `${Math.round(s)} s`
  return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')} min`
}

type Job =
  | { kind: 'photo'; file_id: string }
  | { kind: 'transcribe'; file_id: string; size?: number; duration?: number; forwarded: boolean; caption?: string; akind: string }
  | { kind: 'scan'; command: string; title: string; src: { file_id: string; size?: number; name?: string } | null; date: Date; off?: boolean }

type Patch = { patch: Record<string, unknown>; subject?: string }
type Entry = { id: number; kind: string; payload: Record<string, unknown>; done?: Patch }

export type PrepDeps = {
  store: Store
  tg: TgCall
  bot: () => string
  access: () => Access
  download: (file_id: string, dir: string | null, prefix: string, timeoutMs: number) => Promise<string>
  env: Record<string, string | undefined>
  home: string
  transcribe: boolean
  scanPrivate: boolean
  /** Replies and reactions of the scan (only with send on). */
  canSend: boolean
  withSend: <T>(chat_id: string, fn: () => Promise<T>) => Promise<T>
  clean: (s: string) => string
  log: (m: string) => void
  /** A row became visible (compat journal, typing, recent text). */
  shown: (row: { id: number; kind: string; payload: Record<string, unknown> }) => void
  preparing: (chat_id: string, delta: 1 | -1) => void
  /** H6 hub mode: transcripts and private scans run in the brain container (companion jobs); downloads land in the
   *  shared files dir and travel as refs. */
  remote?: {
    transcribe: (ref: string) => Promise<{ ok: boolean; stdout: string; error?: string; unclaimed?: boolean }>
    scan: (ref: string, name: string, title: string) => Promise<{ ok: boolean; stdout: string; error?: string; unclaimed?: boolean }>
  }
  /** H6 hub mode: a downloaded file → its hub ref, and back. */
  ref?: (path: string) => string
  refPath?: (ref: string) => string
}

export class InboundPrep {
  private chains = new Map<string, Entry[]>()
  private scanAlbums = new Map<string, { command: string; title: string; date: Date; parts: number; at: number }>()
  private scanChain: Promise<unknown> = Promise.resolve()
  private scanDirs = new Set<string>()
  private txActive = 0
  private txWaiters: (() => void)[] = []
  private kept = new Map<string, { path: string; timer: ReturnType<typeof setTimeout> }>()
  private inflight = new Set<Promise<unknown>>()

  constructor(private readonly d: PrepDeps) {}

  /** The work a message needs before its row shows, if any; a scan rewrites the row so that nothing of the
   *  document is ever stored. Call before committing the row. */
  plan(msg: any, row: EventRow, ctxForGate: any, botUsername: string): Job | null {
    const p = row.payload
    const chat_id = String(p.chat_id)
    const scan = this.scanFor(msg, chat_id, ctxForGate, botUsername)
    if (scan) {
      const keep = ['chat_id', 'message_id', 'user', 'user_id', 'ts', 'event_type', 'update_id', 'bot']
      for (const k of Object.keys(p)) if (!keep.includes(k)) delete p[k]
      p.handled = 'scan-private'
      p.text = scan.command
      row.subject = truncate(`${p.user}: ${scan.command}`, 120)
      row.next = 'no reply needed: filed privately'
      return scan
    }
    const kind = p.attachment_kind as string | undefined
    if (kind === 'photo') return { kind: 'photo', file_id: String(p.attachment_file_id) }
    if (kind && TRANSCRIBE_KINDS.has(kind) && this.d.transcribe && (this.d.remote || transcribeArgv(this.d.env, this.d.home))) {
      return { kind: 'transcribe', file_id: String(p.attachment_file_id), size: p.attachment_size != null ? Number(p.attachment_size) : undefined,
        duration: p.attachment_duration != null ? Number(p.attachment_duration) : undefined, forwarded: msg?.forward_origin != null,
        caption: msg?.caption, akind: kind }
    }
    return null
  }

  /** Must this chat's next row wait (an earlier row of the chat is still pending)? */
  busy(chat_id: string): boolean { return (this.chains.get(chat_id)?.length ?? 0) > 0 }

  /** After the commit of a pending row: queue it (and start its job). */
  track(id: number, kind: string, payload: Record<string, unknown>, job: Job | null): void {
    const chat_id = String(payload.chat_id ?? '')
    const e: Entry = { id, kind, payload }
    const list = this.chains.get(chat_id) ?? []
    list.push(e)
    this.chains.set(chat_id, list)
    if (!job) { e.done = { patch: {} }; this.release(chat_id); return }
    const p = this.run(job, payload).then(done => { e.done = done }, err => {
      e.done = { patch: { prep_error: this.d.clean(errText(err)) } }
    }).finally(() => { this.inflight.delete(p); this.release(chat_id) })
    this.inflight.add(p)
  }

  private release(chat_id: string): void {
    const list = this.chains.get(chat_id)
    if (!list) return
    while (list.length && list[0]!.done) {
      const e = list.shift()!
      try {
        const id = this.d.store.releaseRow(e.id, e.done!.patch, e.done!.subject)
        if (id != null) this.d.shown({ id, kind: e.kind, payload: { ...e.payload, ...e.done!.patch } })
      } catch (err) { this.d.log(`releasing row #${e.id} failed: ${errText(err)}`) }
    }
    if (!list.length) this.chains.delete(chat_id)
  }

  /** Rows a killed process left pending: shown now, flagged interrupted. */
  recover(): number {
    const rows = this.d.store.pendingRows(this.d.bot())
    for (const r of rows) {
      const p = r.payload
      const patch: Record<string, unknown> = p.handled === 'scan-private'
        ? { scan_error: 'internal', text: `${p.text} (the adapter's private scan was interrupted; nothing was kept; tell the owner it may not be filed)` }
        : p.attachment_kind && TRANSCRIBE_KINDS.has(String(p.attachment_kind)) && !p.transcript
          ? { transcribe_error: 'interrupted (the adapter restarted)' }
          : {}
      const id = this.d.store.releaseRow(r.id, patch)
      if (id != null) this.d.shown({ id, kind: r.kind, payload: { ...p, ...patch } })
    }
    return rows.length
  }

  /** Wait for running jobs (tests, shutdown). */
  async idle(): Promise<void> { while (this.inflight.size) await Promise.allSettled([...this.inflight]) }

  stop(): void {
    for (const k of this.kept.values()) { clearTimeout(k.timer); rmSync(k.path, { force: true }) }
    this.kept.clear()
    for (const d of this.scanDirs) rmSync(d, { recursive: true, force: true })
  }

  /** download op: the audio kept from a transcription, renamed like a normal download. */
  takeKept(file_id: string): string | null {
    const k = this.kept.get(file_id)
    if (!k) return null
    this.kept.delete(file_id)
    clearTimeout(k.timer)
    const dest = join(dirname(k.path), basename(k.path).slice(TX_PREFIX.length))
    try { renameSync(k.path, dest); return dest } catch { return null }
  }

  /** Kept audio and scan dirs an earlier process left behind. */
  sweep(inboxDir: string): void {
    try {
      for (const n of readdirSync(inboxDir)) {
        if (!n.startsWith(TX_PREFIX)) continue
        const p = join(inboxDir, n)
        try { if (Date.now() - statSync(p).mtimeMs > TRANSCRIPT_KEEP_MS + 60_000) rmSync(p, { force: true }) } catch {}
      }
    } catch {}
    try {
      for (const n of readdirSync(tmpdir())) {
        const m = /^tga-scan-(\d+)-/.exec(n)
        if (!m) continue
        let alive = Number(m[1]) === process.pid
        if (!alive) try { process.kill(Number(m[1]), 0); alive = true } catch (err: any) { alive = err?.code === 'EPERM' }
        if (!alive) rmSync(join(tmpdir(), n), { recursive: true, force: true })
      }
    } catch {}
  }

  // --- jobs ---------------------------------------------------------------------------------------------

  private async run(job: Job, payload: Record<string, unknown>): Promise<Patch> {
    if (job.kind === 'photo') {
      try { const p = await this.d.download(job.file_id, null, '', 60_000); return { patch: { image_path: this.d.ref ? this.d.ref(p) : p } } } catch (err) {
        this.d.log(`photo download failed: ${this.d.clean(errText(err))}`)
        return { patch: {} }
      }
    }
    if (job.kind === 'transcribe') return this.transcribe(job, payload)
    return this.scan(job, payload)
  }

  private async slot<T>(fn: () => Promise<T>): Promise<T> {
    if (this.txActive < TRANSCRIBE_PARALLEL) this.txActive++
    else await new Promise<void>(r => this.txWaiters.push(r))
    try { return await fn() } finally {
      const next = this.txWaiters.shift()
      if (next) next(); else this.txActive--
    }
  }

  private keep(file_id: string, path: string): void {
    const old = this.kept.get(file_id)
    if (old) { clearTimeout(old.timer); rmSync(old.path, { force: true }) }
    const timer = setTimeout(() => { this.kept.delete(file_id); rmSync(path, { force: true }) }, TRANSCRIPT_KEEP_MS)
    ;(timer as { unref?: () => void }).unref?.()
    this.kept.set(file_id, { path, timer })
  }

  private async transcribe(job: Extract<Job, { kind: 'transcribe' }>, payload: Record<string, unknown>): Promise<Patch> {
    const chat_id = String(payload.chat_id)
    const fail = (why: string): Patch => ({ patch: { transcribe_error: this.d.clean(why) } })
    if (job.size != null && job.size > TRANSCRIBE_MAX_BYTES) return fail(`not transcribed: ${(job.size / 1024 / 1024).toFixed(1)} MB is over the 20 MB Bot API download limit`)
    if (job.duration != null && job.duration > TRANSCRIBE_MAX_S) return fail(`not transcribed: longer than ${TRANSCRIBE_MAX_S / 60} min`)
    const argv = this.d.remote ? [] : transcribeArgv(this.d.env, this.d.home)
    if (!argv) return fail('no transcribe command')
    this.d.preparing(chat_id, 1)
    try {
      let path: string
      try { path = await this.d.download(job.file_id, null, TX_PREFIX, 60_000) } catch (err) {
        const m = errText(err)
        return fail(m.startsWith('download failed') ? m : `download failed: ${m}`)
      }
      const r = await this.slot(() => this.d.remote ? this.d.remote.transcribe(this.d.ref!(path)) : runLocal([...argv, path], TRANSCRIBE_TIMEOUT_MS, this.d.clean))
      this.keep(job.file_id, path)
      if (!r.ok) return fail(r.error ?? 'failed')
      const transcript = r.stdout.trim()
      const dur = fmtDuration(job.duration)
      const label = `${job.forwarded ? 'forwarded ' : ''}${KIND_LABEL[job.akind] ?? job.akind}${dur ? `, ${dur}` : ''}`
      const text = `🎙️ (${label}) ${transcript || '(no speech recognized)'}${job.caption ? `\n(caption: ${job.caption})` : ''}`
      return { patch: { text, transcript: 'auto' }, subject: truncate(`${payload.user}: ${text}`.replace(/\s+/g, ' '), 120) }
    } finally {
      this.d.preparing(chat_id, -1)
    }
  }

  /** The private scan this message asks for (a "/scan private" or "/sp" caption, or a later item of such an
   *  album), from the owner; else null. */
  private scanFor(msg: any, chat_id: string, ctx: any, botUsername: string): Extract<Job, { kind: 'scan' }> | null {
    if (!msg || !(msg.photo?.length || msg.document || msg.video || msg.audio || msg.voice)) return null
    const req = parseScanPrivate(msg.caption, botUsername)
    const key = msg.media_group_id ? `${chat_id}:${msg.media_group_id}` : null
    const now = Date.now()
    for (const [k, a] of this.scanAlbums) if (now - a.at > SCAN_ALBUM_MS) this.scanAlbums.delete(k)
    const album = key ? this.scanAlbums.get(key) : undefined
    if (!req && !album) return null
    const access = this.d.access()
    const from = msg.from?.id != null ? String(msg.from.id) : ''
    if (access.dmPolicy === 'disabled' || !access.allowFrom.includes(from)) return null
    if (msg.chat?.type !== 'private' && !gateMessage(ctx, access, botUsername)) return null
    const date = new Date((msg.date ?? now / 1000) * 1000)
    const src = msg.photo?.length
      ? { file_id: msg.photo[msg.photo.length - 1].file_id, size: msg.photo[msg.photo.length - 1].file_size }
      : msg.document ? { file_id: msg.document.file_id, size: msg.document.file_size, name: msg.document.file_name } : null
    const off = !this.d.scanPrivate
    if (req) {
      if (key && !album) this.scanAlbums.set(key, { command: req.command, title: req.title, date, parts: 1, at: now })
      return { kind: 'scan', command: req.command, title: scanTitle(req.title, date), src, date, off }
    }
    album!.parts++
    album!.at = now
    return { kind: 'scan', command: album!.command, title: scanTitle(album!.title, album!.date, album!.parts), src, date, off }
  }

  private scan(job: Extract<Job, { kind: 'scan' }>, payload: Record<string, unknown>): Promise<Patch> {
    const chat_id = String(payload.chat_id)
    const msgId = Number(payload.message_id)
    const react = (emoji: string | null) => this.d.canSend
      ? this.d.tg('setMessageReaction', { chat_id, message_id: msgId, reaction: emoji ? [{ type: 'emoji', emoji }] : [] }).catch(() => {})
      : Promise.resolve()
    void react('✍')
    const work = async (): Promise<Patch> => {
      let docId: string | null = null
      let fail: { code: string; reason: string } | null = null
      let dir: string | null = null
      try {
        if (job.off) throw { code: 'off', reason: 'private scans are off in this adapter' }
        if (!job.src) throw { code: 'unsupported', reason: 'only photos and documents (PDF) can be filed' }
        const argv = this.d.remote ? [] : scanArgv(this.d.env, this.d.home)
        if (!argv) throw { code: 'no_command', reason: 'the paperless command is not available here' }
        if (job.src.size != null && job.src.size > SCAN_MAX_BYTES) throw { code: 'too_big', reason: 'the file is over 20 MB (the Bot API limit)' }
        if (this.d.remote) { // hub mode: the file goes to the companion under its final name, then leaves the shared dir
          let got: string
          try { got = await this.d.download(job.src.file_id, null, `${SCAN_PREFIX}${process.pid}-`, 60_000) } catch (err) {
            throw { code: 'download', reason: `download failed: ${this.d.clean(errText(err))}` }
          }
          const ref = this.d.ref!(got)
          let r: { ok: boolean; stdout: string; error?: string; unclaimed?: boolean }
          try { r = await this.d.remote.scan(ref, scanFileName(job.src.name, got.split('.').pop() ?? 'bin', job.date), job.title) } finally {
            rmSync(this.d.refPath ? this.d.refPath(ref) : got, { force: true })
          }
          docId = r.ok ? scanDocId(r.stdout) : null
          if (!docId) {
            const code = r.unclaimed ? 'offline' : /^timed out after/.test(r.error ?? '') ? 'timeout' : /^exit 2: .*timed out .*waiting for task/i.test(r.error ?? '') ? 'processing' : 'upload'
            throw { code, reason: r.unclaimed ? 'the brain-side companion is offline' : scanFailReason(r.error, r.stdout) }
          }
          throw { done: true }
        }
        dir = mkdtempSync(join(tmpdir(), `${SCAN_PREFIX}${process.pid}-`))
        this.scanDirs.add(dir)
        chmodSync(dir, 0o700)
        let path: string
        try {
          const got = await this.d.download(job.src.file_id, dir, '', 60_000)
          path = join(dir, scanFileName(job.src.name, got.split('.').pop() ?? 'bin', job.date))
          renameSync(got, path)
        } catch (err) { throw { code: 'download', reason: `download failed: ${this.d.clean(errText(err))}` } }
        const r = await runLocal([...argv, 'upload', path, `--title=${job.title}`, '--tag', 'Inbox', '--tag', 'private', '--create'], SCAN_TIMEOUT_MS, this.d.clean)
        docId = r.ok ? scanDocId(r.stdout) : null
        if (!docId) {
          const code = /^timed out after/.test(r.error ?? '') ? 'timeout' : /^exit 2: .*timed out .*waiting for task/i.test(r.error ?? '') ? 'processing' : 'upload'
          throw { code, reason: scanFailReason(r.error, r.stdout) }
        }
      } catch (err: any) {
        if (err?.done === true && docId) { /* hub mode: filed by the companion */ }
        else fail = err && typeof err.code === 'string' && typeof err.reason === 'string' ? err : { code: 'internal', reason: this.d.clean(errText(err)) }
      } finally {
        if (dir) { try { rmSync(dir, { recursive: true, force: true }) } catch {} ; this.scanDirs.delete(dir) }
      }
      const text = docId ? `🔒 Filed privately → document ${docId}` : `⚠️ Private scan failed: ${fail!.reason}`
      let sendErr: string | null = this.d.canSend ? null : 'receive-only adapter'
      if (this.d.canSend) {
        try {
          await this.d.withSend(chat_id, () => this.d.tg('sendMessage', { chat_id, text, link_preview_options: { is_disabled: true },
            reply_parameters: { message_id: msgId, allow_sending_without_reply: true } }))
        } catch (err) { sendErr = this.d.clean(errText(err)) }
      }
      void react(null)
      const told = sendErr ? `telling the owner failed (${sendErr}): tell them` : 'the owner was told; no reply needed'
      if (docId) return { patch: { doc_id: docId, text: `${job.command} (filed privately by the plugin → paperless document ${docId}; ${told})` } }
      return { patch: { scan_error: fail!.code, text: `${job.command} (the plugin's private scan failed: ${fail!.code}; nothing was kept; ${told})` } }
    }
    const p = this.scanChain.then(work)
    this.scanChain = p.catch(() => {})
    return p
  }
}

