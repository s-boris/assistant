// Mock Bot API for offline tests, ported from telegram-plus test/offline.ts: getUpdates keeps
// updates queued until a later call confirms them with a higher offset (a returned but unconfirmed
// update comes again, like Telegram), long-polls up to holdMs while empty, and answers the send/
// edit/react/poll methods with plausible results ("BADPARSE" / "BADRICH" in a text are refused
// like bad formatting). Two ways in, neither binds a TCP port: `fetch` (inject into grammY's
// client options, in-process) or `serveUnix(path)` (a unix socket for a subprocess adapter).
// The token below is a fake used only against this mock.
export const TOKEN = '4242:TEST-not-a-real-token' // secretscan:allow
export const OWNER = 111
export const STRANGER = 999
export const GROUP = -100123

export type Call = { method: string; params: any; at: number; doneAt?: number }

type Canned = { status: number; body: unknown }

export class MockBot {
  calls: Call[] = []
  queue: any[] = []
  username = 'testadapter_bot'
  botId = 4242
  holdMs = 300
  /** Max updates per getUpdates answer (mid-stream tests). */
  maxBatch = 100
  /** getUpdates answers 409 (another poller holds the token). */
  conflict = false
  /** Next getUpdates ignores the offset once (Telegram re-sending confirmed updates). */
  redeliverOnce = false
  /** "<method>" → ms the mock waits before answering. */
  delays: Record<string, number> = {}
  /** "<method>" → canned error answers, used up one per call. */
  failures: Record<string, Canned[]> = {}
  /** The link is down (a WAN outage): every request, getUpdates and downloads too, fails like a dropped connection. */
  down = false
  getUpdatesCalls: { offset: number; at: number; returned: number[]; doneAt?: number }[] = []
  /** getFile + the file URL: file_id → stored bytes (addFile). */
  files: Record<string, { path: string; data: Uint8Array; unique: string }> = {}
  /** Multipart uploads seen: method → field name → { name, size } of each file part. */
  uploads: { method: string; files: { field: string; name: string; size: number }[] }[] = []
  private msgId = 100
  private updateId = 1
  private pollN = 0
  private server: { stop(force?: boolean): void } | null = null

  push(u: any): number {
    const update_id = this.updateId++
    this.queue.push({ update_id, ...u })
    return update_id
  }

  user(id: number) {
    return { id, is_bot: false, first_name: 'U', username: id === OWNER ? 'owner' : 'stranger' }
  }

  /** An incoming text message from `from` in a private chat (or `chat`). */
  text(from: number, text: string, extra: any = {}, chat?: number): number {
    const cid = chat ?? from
    return this.push({ message: { message_id: ++this.msgId, date: Math.floor(Date.now() / 1000), chat: { id: cid, type: cid < 0 ? 'supergroup' : 'private' }, from: this.user(from), text, ...extra } })
  }

  /** A press on a bot message with an inline keyboard. */
  press(from: number, data: string, label: string, message: any = {}): number {
    const chat = message.chat ?? { id: from, type: 'private' }
    return this.push({ callback_query: { id: `cq${this.updateId}`, from: this.user(from), chat_instance: 'ci', data,
      message: { message_id: message.message_id ?? 500, date: Math.floor(Date.now() / 1000), chat, from: { id: this.botId, is_bot: true, first_name: 'T', username: this.username },
        text: message.text ?? 'Pick one', ...(message.text === null ? { text: undefined } : {}),
        reply_markup: { inline_keyboard: [[{ text: label, callback_data: data }, { text: 'Other', callback_data: 'other' }]] } } } })
  }

  react(from: number, message_id: number, added: string[], old: string[] = []): number {
    return this.push({ message_reaction: { chat: { id: from, type: 'private' }, message_id, user: this.user(from), date: Math.floor(Date.now() / 1000),
      old_reaction: old.map(emoji => ({ type: 'emoji', emoji })), new_reaction: added.map(emoji => ({ type: 'emoji', emoji })) } })
  }

  pollAnswer(from: number, poll_id: string, option_ids: number[]): number {
    return this.push({ poll_answer: { poll_id, user: this.user(from), option_ids } })
  }

  addFile(file_id: string, data: Uint8Array | string, ext = 'jpg'): void {
    const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data
    this.files[file_id] = { path: `files/${file_id}.${ext}`, data: bytes, unique: `u${file_id.replace(/[^a-zA-Z0-9]/g, '')}` }
  }

  sent(method?: string): Call[] {
    return method ? this.calls.filter(c => c.method === method) : this.calls
  }

  readonly fetch = async (input: any, init: any = {}): Promise<Response> => {
    if (this.down) throw Object.assign(new TypeError('ECONNRESET: The socket connection was closed unexpectedly'), { code: 'ECONNRESET' })
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const body = init.body
    const req = new Request(url, { method: init.method ?? 'POST', headers: init.headers, body })
    const signal = init.signal as { aborted?: boolean; addEventListener?: Function } | undefined
    if (!signal?.addEventListener) return this.handle(req)
    return new Promise<Response>((resolve, reject) => {
      if (signal.aborted) return reject(new Error('aborted'))
      signal.addEventListener!('abort', () => reject(new Error('aborted')), { once: true })
      this.handle(req).then(resolve, reject)
    })
  }

  serveUnix(path: string): void {
    this.server = Bun.serve({ unix: path, fetch: req => this.handle(req) }) as any
  }

  stop(): void {
    this.server?.stop(true)
    this.server = null
  }

  private ok(result: unknown) { return Response.json({ ok: true, result }) }
  private bad(description: string, code = 400, parameters?: unknown) {
    return Response.json({ ok: false, error_code: code, description, ...(parameters ? { parameters } : {}) }, { status: code })
  }

  private msg(chat_id: any, extra: any = {}) {
    const id = Number(chat_id)
    return { message_id: ++this.msgId, date: Math.floor(Date.now() / 1000), chat: { id, type: id < 0 ? 'supergroup' : 'private' }, ...extra }
  }

  async handle(req: Request): Promise<Response> {
    const url = new URL(req.url)
    const fm = /^\/file\/bot([^/]+)\/(.+)$/.exec(url.pathname)
    if (fm) {
      if (fm[1] !== TOKEN) return new Response('nope', { status: 404 })
      const f = Object.values(this.files).find(x => x.path === fm[2])
      this.calls.push({ method: 'GET file', params: { path: fm[2] }, at: performance.now() })
      return f ? new Response(f.data) : new Response('gone', { status: 404 })
    }
    const m = /^\/bot([^/]+)\/(\w+)$/.exec(url.pathname)
    if (!m || m[1] !== TOKEN) return new Response('nope', { status: 404 })
    const method = m[2]!
    const ct = req.headers.get('content-type') ?? ''
    let p: any = {}
    if (ct.includes('json')) p = await req.json()
    else if (ct.includes('multipart')) {
      const fd = await req.formData()
      const up: { field: string; name: string; size: number }[] = []
      for (const [k, v] of fd.entries()) {
        if (typeof v === 'string') { try { p[k] = JSON.parse(v) } catch { p[k] = v } }
        else { up.push({ field: k, name: (v as File).name, size: (v as File).size }); p[k] = { file: (v as File).name } }
      }
      this.uploads.push({ method, files: up })
    }
    else { const t = await req.text(); if (t) { try { p = JSON.parse(t) } catch {} } }
    if (method === 'getUpdates') return this.getUpdates(p)
    const call: Call = { method, params: p, at: performance.now() }
    this.calls.push(call)
    const delay = this.delays[method]
    if (delay) await Bun.sleep(delay)
    call.doneAt = performance.now()
    const canned = this.failures[method]?.shift()
    if (canned) return Response.json(canned.body, { status: canned.status })
    switch (method) {
      case 'getMe': return this.ok({ id: this.botId, is_bot: true, first_name: 'T', username: this.username })
      case 'answerCallbackQuery': return this.ok(true)
      case 'setMessageReaction':
        if ((p.reaction ?? []).length > 1) return this.bad('Bad Request: REACTIONS_TOO_MANY')
        return this.ok(true)
      case 'sendRichMessage':
        if (String(p.rich_message?.markdown).includes('BADRICH')) return this.bad('Bad Request: rich message is invalid')
        return this.ok(this.msg(p.chat_id, { rich_message: {} }))
      case 'sendMessage':
        if (p.parse_mode && String(p.text).includes('BADPARSE')) return this.bad("Bad Request: can't parse entities")
        return this.ok(this.msg(p.chat_id, { text: p.text }))
      case 'editMessageText':
        if (p.parse_mode && String(p.text).includes('BADPARSE')) return this.bad("Bad Request: can't parse entities")
        return this.ok(this.msg(p.chat_id, { text: p.text }))
      case 'getFile': {
        const f = this.files[p.file_id]
        if (!f) return this.bad('Bad Request: invalid file_id')
        return this.ok({ file_id: p.file_id, file_unique_id: f.unique, file_size: f.data.length, file_path: f.path })
      }
      case 'sendPhoto': case 'sendDocument': case 'sendVoice': case 'sendAudio': case 'sendVideo': case 'sendLocation': case 'sendVenue':
        return this.ok(this.msg(p.chat_id, { [method.slice(4).toLowerCase()]: {} }))
      case 'sendMediaGroup':
        return this.ok((Array.isArray(p.media) ? p.media : []).map(() => this.msg(p.chat_id)))
      case 'sendPoll': {
        const id = `poll-${++this.pollN}`
        return this.ok(this.msg(p.chat_id, { poll: { id, question: p.question } }))
      }
      default: return this.ok(true)
    }
  }

  private getUpdates(p: any): Promise<Response> {
    const rec = { offset: Number(p.offset ?? 0), at: performance.now(), returned: [] as number[], doneAt: undefined as number | undefined }
    this.getUpdatesCalls.push(rec)
    if (this.conflict) {
      return Promise.resolve(this.bad('Conflict: terminated by other getUpdates request; make sure that only one bot instance is running', 409))
    }
    const offset = rec.offset
    if (this.redeliverOnce) this.redeliverOnce = false
    else while (offset > 0 && this.queue.length && this.queue[0].update_id < offset) this.queue.shift()
    const limit = Math.min(Number(p.limit) || 100, this.maxBatch)
    const t0 = Date.now()
    return new Promise<Response>(resolve => {
      const tick = () => {
        if (this.queue.length || Date.now() - t0 > this.holdMs) {
          const batch = this.queue.slice(0, limit)
          rec.returned = batch.map(u => u.update_id)
          rec.doneAt = performance.now()
          resolve(this.ok(batch))
        } else setTimeout(tick, 10)
      }
      tick()
    })
  }
}
