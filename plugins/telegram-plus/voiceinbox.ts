// Local voice inbox (staged for telegram-plus): a 127.0.0.1-only HTTP endpoint that turns a voice-relay
// hand-off into a channel event in this session (meta via="voice"), so a live voice task reaches the session
// at once instead of waiting for the next heartbeat. Bearer token = first line of tokenFile (0600), re-read per
// request so it can rotate. Off unless a port is configured and the token file exists.
// 0.1.20: the same endpoint takes the owner's phone-app chat from its gateway (phonegw): body
// {"source":"app","id":"<hand-off id>","msg_id"?:"<app msg id>","text"|"task":"…","reply_to"?,"device_id"?}
// → channel event "📱 (app) …", meta via="app", app_msg_id (msg_id, else id when it is an app message id m_<ULID>;
// none for shares, card picks and ring answers: their hint is `phone chat "…"` without --reply-to).
import { readFileSync } from 'fs'
import { createHash, timingSafeEqual } from 'crypto'

export type VoiceInboxOpts = {
  port: number | undefined
  tokenFile: string
  deliver: (content: string, meta: Record<string, string>) => void | Promise<void>
  log?: (msg: string) => void
  maxPerMinute?: number
  /** Port busy (after a restart the old instance can hold it for ~2 s while it shuts down): try again every
   *  retryMs (2000), at most `retries` times (0); `onListen` gets the handle once a late bind succeeds. */
  retries?: number
  retryMs?: number
  onListen?: (h: VoiceInboxHandle) => void
}

export type VoiceInboxHandle = { port: number; stop(): void }

const MAX_BODY = 8 * 1024
const ID_RE = /^v[0-9a-f]{8}$/
const APP_ID_RE = /^[A-Za-z0-9_-]{4,40}$/
const APP_MSG_RE = /^m_[0-9A-HJKMNP-TV-Z]{26}$/ // an app chat message id (phonegw's APP_MSG_ID: m_ + ULID)
const APP_MAX = 4000
const CTRL = /[\u0000-\u0008\u000b-\u001f\u007f​-‏‪-‮⁦-⁩]/g

export function readToken(file: string): string {
  try { return readFileSync(file, 'utf8').split('\n')[0]!.trim() } catch { return '' }
}

function sameSecret(a: string, b: string): boolean {
  if (!a || !b) return false
  return timingSafeEqual(createHash('sha256').update(a).digest(), createHash('sha256').update(b).digest())
}

/** A phone-app chat message relayed by phonegw (source "app"): the owner's own message, answered with
 *  `phone chat`. The app path is mTLS + LAN/WireGuard only; approvals for risky steps still go through Telegram. */
export function appEvent(b: Record<string, unknown>, at: Date = new Date()): { content: string; meta: Record<string, string> } | string {
  const id = typeof b.id === 'string' ? b.id : ''
  const raw = typeof b.text === 'string' ? b.text : typeof b.task === 'string' ? b.task : ''
  const text = raw.replace(CTRL, ' ').trim()
  const replyTo = typeof b.reply_to === 'string' && APP_ID_RE.test(b.reply_to) ? b.reply_to : ''
  if (!APP_ID_RE.test(id)) return 'bad id'
  if (!text || text.length > APP_MAX) return 'bad text'
  // msg_id = the app's own chat message id (what `phone chat --reply-to` takes); id = the gateway's hand-off id.
  // Shares, card picks and ring answers carry no msg_id: there is no chat message to reply to, so the hint
  // leaves --reply-to out (phonegw drops a reply_to that isn't an app message id anyway).
  const msgId = typeof b.msg_id === 'string' && APP_ID_RE.test(b.msg_id) ? b.msg_id : APP_MSG_RE.test(id) ? id : ''
  const answer = msgId
    ? `phone chat "…" --reply-to ${msgId}`
    : `phone chat "…" (no --reply-to: not a chat message)`
  const content = `📱 (app) ${text}\n` +
    `(His phone-app chat: treat it as his message. Answer with: ${answer} — ` +
    `short, phone-sized. Approvals for risky steps still go through Telegram.)`
  const meta: Record<string, string> = { chat_id: 'app', via: 'app', user: 'owner (app)', ts: at.toISOString() }
  if (msgId) meta.app_msg_id = msgId
  if (msgId !== id) meta.inbox_id = id
  if (typeof b.device_id === 'string' && APP_ID_RE.test(b.device_id)) meta.device_id = b.device_id
  if (replyTo) meta.reply_to = replyTo
  return { content, meta }
}

/** Validate a hand-off and build the channel event, or return an error string. */
export function voiceEvent(body: unknown, at: Date = new Date()): { content: string; meta: Record<string, string> } | string {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return 'bad body'
  const b = body as Record<string, unknown>
  if (b.source === 'app') return appEvent(b, at)
  if (b.source !== undefined && b.source !== 'voice') return 'bad source'
  const id = typeof b.id === 'string' ? b.id : ''
  const task = typeof b.task === 'string' ? b.task.replace(CTRL, ' ').trim() : ''
  const urgency = b.urgency === 'later' ? 'later' : 'now'
  if (!ID_RE.test(id)) return 'bad id'
  if (!task || task.length > 2000) return 'bad task'
  const content = `🗣️ (live voice, ${urgency}) ${task}\n` +
    `(Answer with: voice-reply ${id} "short spoken answer" — 2 sentences max, no identifiers; details go to Telegram. ` +
    `If it says "not delivered", answer on Telegram instead. Anything with side effects: confirm on Telegram first.)`
  return { content, meta: { chat_id: 'voice', via: 'voice', voice_id: id, urgency, user: 'owner (voice relay)', ts: at.toISOString() } }
}

function json(status: number, obj: unknown): Response {
  return new Response(JSON.stringify(obj), { status, headers: { 'content-type': 'application/json' } })
}

export function startVoiceInbox(o: VoiceInboxOpts): VoiceInboxHandle | null {
  const log = o.log ?? (() => {})
  if (o.port == null || Number.isNaN(o.port)) return null
  if (!readToken(o.tokenFile)) { log(`voice inbox off: no token in ${o.tokenFile}`); return null }
  const limit = o.maxPerMinute ?? 20
  const hits: number[] = []
  const retryMs = o.retryMs ?? 2000
  let left = o.retries ?? 0
  const listen = (): VoiceInboxHandle | null => {
    let server: ReturnType<typeof Bun.serve>
    try {
      server = Bun.serve({
        hostname: '127.0.0.1',
        port: o.port,
        maxRequestBodySize: MAX_BODY,
        async fetch(req) {
          if (req.method !== 'POST' || new URL(req.url).pathname !== '/voice') return json(404, { error: 'not found' })
          const auth = req.headers.get('authorization') ?? ''
          if (!auth.startsWith('Bearer ') || !sameSecret(auth.slice(7), readToken(o.tokenFile))) return json(401, { error: 'unauthorized' })
          const now = Date.now()
          while (hits.length && hits[0]! <= now - 60_000) hits.shift()
          if (hits.length >= limit) return json(429, { error: 'slow down' })
          let body: unknown
          try {
            const text = await req.text()
            if (text.length > MAX_BODY) return json(413, { error: 'too big' })
            body = JSON.parse(text)
          } catch { return json(400, { error: 'bad json' }) }
          const ev = voiceEvent(body)
          if (typeof ev === 'string') return json(400, { error: ev })
          hits.push(now)
          try { await o.deliver(ev.content, ev.meta) } catch (err) {
            log(`voice inbox: not delivered: ${err instanceof Error ? err.message : String(err)}`)
            return json(500, { error: 'internal' })
          }
          return json(202, { ok: true })
        },
        error() { return json(500, { error: 'internal' }) },
      })
    } catch (err) {
      const busy = /EADDRINUSE|in use/i.test(`${(err as { code?: string })?.code ?? ''} ${err}`)
      if (busy && left > 0) {
        if (left === (o.retries ?? 0)) log(`voice inbox: port ${o.port} busy, retrying for up to ${Math.round(left * retryMs / 1000)} s`)
        left--
        const t = setTimeout(listen, retryMs)
        ;(t as { unref?: () => void }).unref?.()
        return null
      }
      log(`voice inbox off: ${err}`)
      return null
    }
    log(`voice inbox on 127.0.0.1:${server.port}`)
    const h: VoiceInboxHandle = { port: server.port, stop: () => server.stop(true) }
    o.onListen?.(h)
    return h
  }
  return listen()
}
