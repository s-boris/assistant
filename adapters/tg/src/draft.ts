// Streaming drafts (the draft tool), lifted from telegram-plus server.ts (commit 974bed5, lines 1191-1347:
// Stream, flushStream, runFlush, updateStream, endStream). Private chats: sendMessageDraft /
// sendRichMessageDraft (refreshed every 20 s, they live ~30 s); groups or a refused draft: one progress
// message that is edited and deleted by the next send. Change from 0.1.22: every flush, draft or edit, is
// at least MIN_GAP_MS (1.2 s) after the last one; updates in between are coalesced into the next flush (the
// outbox may hand over a burst of draft rows at once). A stopped_message_generation update stops the stream
// (the adapter writes a tg.stop row).
import { randomBytes } from 'crypto'
import { GrammyError } from 'grammy'
import { errText, isBadRequest, isPrivateChatId, MAX_CHUNK_LIMIT, parseModeOf, RICH_CHUNK_LIMIT, textBody, type Format, type TgCall } from './format.ts'

export const MIN_GAP_MS = 1_200
export const DRAFT_REFRESH_MS = 20_000
export const STREAM_TTL_MS = 15 * 60_000

export type Stream = {
  chat_id: string
  mode: 'draft' | 'edit'
  draftId: number
  text: string
  format: Format
  canStop: boolean
  messageId?: number
  lastFlush: number
  pending?: ReturnType<typeof setTimeout>
  keepalive?: ReturnType<typeof setInterval>
  expires: number
  inflight: Promise<unknown>
  stopped: boolean
  notes: string[]
}

export class Drafts {
  readonly streams = new Map<string, Stream>()
  constructor(private readonly o: { tg: TgCall; log: (m: string) => void; withSend: <T>(chat_id: string, fn: () => Promise<T>) => Promise<T>;
    minGapMs?: number; refreshMs?: number }) {}

  get minGap() { return this.o.minGapMs ?? MIN_GAP_MS }
  has(chat_id: string): boolean { return this.streams.has(chat_id) }

  private view(s: Stream): string {
    const limit = s.format === 'rich' ? RICH_CHUNK_LIMIT : MAX_CHUNK_LIMIT
    return s.text.length <= limit ? s.text : '…' + s.text.slice(s.text.length - limit + 1)
  }

  private async flush(s: Stream): Promise<void> {
    if (s.pending) { clearTimeout(s.pending); s.pending = undefined }
    if (this.streams.get(s.chat_id) !== s || s.stopped) return
    s.lastFlush = Date.now()
    const view = this.view(s)
    const tg = this.o.tg
    if (s.mode === 'draft') {
      const base = { chat_id: s.chat_id, draft_id: s.draftId, ...(s.canStop ? { can_stop: true } : {}) }
      try {
        if (s.format === 'rich' && view !== '') await tg('sendRichMessageDraft', { ...base, ...textBody(view, 'rich') })
        else {
          const pm = parseModeOf(s.format)
          await tg('sendMessageDraft', { ...base, text: view, ...(pm && view ? { parse_mode: pm } : {}) })
        }
        return
      } catch (err) {
        if (!isBadRequest(err)) throw err
        if (s.format !== 'text') {
          s.notes.push(`${s.format} draft rejected (${errText(err)}); streaming as plain text`)
          s.format = 'text'
          return this.flush(s)
        }
        s.notes.push(`sendMessageDraft refused (${errText(err)}); using edit fallback`)
        s.mode = 'edit'
        if (s.keepalive) { clearInterval(s.keepalive); s.keepalive = undefined }
      }
    }
    const shown = view === '' ? '…' : view
    if (s.messageId == null) {
      const sent = s.format === 'rich'
        ? await tg('sendRichMessage', { chat_id: s.chat_id, ...textBody(shown, 'rich'), disable_notification: true })
        : await tg('sendMessage', { chat_id: s.chat_id, ...textBody(shown, s.format), disable_notification: true })
      s.messageId = sent.message_id
    } else {
      try {
        await tg('editMessageText', { chat_id: s.chat_id, message_id: s.messageId, ...textBody(shown, s.format) })
      } catch (err) {
        if (!(err instanceof GrammyError && /not modified/i.test(err.description))) throw err
      }
    }
  }

  private runFlush(s: Stream): Promise<void> {
    const p = s.inflight.then(() => this.o.withSend(s.chat_id, () => this.flush(s)))
    s.inflight = p.catch(err => this.o.log(`draft flush failed: ${errText(err)}`))
    return p
  }

  /** Update (or start) the chat's draft; a flush at most every minGap, bursts coalesced. */
  async update(chat_id: string, text: string, append: boolean, f: Format, canStop: boolean): Promise<Stream> {
    let s = this.streams.get(chat_id)
    const fresh = !s || s.stopped
    if (fresh) {
      if (s) await this.end(chat_id, false)
      s = {
        chat_id, mode: isPrivateChatId(chat_id) ? 'draft' : 'edit',
        draftId: 1 + (randomBytes(4).readUInt32BE(0) % 2_000_000_000),
        text: '', format: f, canStop, lastFlush: 0, expires: Date.now() + STREAM_TTL_MS,
        inflight: Promise.resolve(), stopped: false, notes: [],
      }
      this.streams.set(chat_id, s)
      if (s.mode === 'draft') {
        const st = s
        const every = this.o.refreshMs ?? DRAFT_REFRESH_MS
        st.keepalive = setInterval(() => {
          if (this.streams.get(chat_id) !== st || st.mode !== 'draft') return clearInterval(st.keepalive)
          if (Date.now() > st.expires) return void this.end(chat_id, false)
          if (!st.pending && Date.now() - st.lastFlush >= every - 500) void this.runFlush(st)
        }, every)
        ;(st.keepalive as { unref?: () => void }).unref?.()
      }
    }
    s = s!
    s.text = append ? s.text + text : text
    s.expires = Date.now() + STREAM_TTL_MS
    const wait = s.lastFlush + this.minGap - Date.now()
    if (fresh || wait <= 0) await this.runFlush(s).catch(() => {})
    else if (!s.pending) {
      const st = s
      st.pending = setTimeout(() => void this.runFlush(st).catch(() => {}), wait)
    }
    return s
  }

  /** Stop the chat's stream; deleteProgress removes the edit fallback's progress message. Before every send. */
  async end(chat_id: string, deleteProgress: boolean): Promise<Stream | undefined> {
    const s = this.streams.get(chat_id)
    if (!s) return undefined
    this.streams.delete(chat_id)
    if (s.pending) clearTimeout(s.pending)
    if (s.keepalive) clearInterval(s.keepalive)
    await s.inflight
    if (deleteProgress && s.mode === 'edit' && s.messageId != null) {
      await this.o.tg('deleteMessage', { chat_id, message_id: s.messageId }).catch(() => {})
    }
    return s
  }

  /** The user pressed Stop on the draft `draft_id` (stopped_message_generation). */
  stopped(chat_id: string, draft_id: number): void {
    const s = this.streams.get(chat_id)
    if (s && s.draftId === draft_id) {
      s.stopped = true
      if (s.keepalive) clearInterval(s.keepalive)
      if (s.pending) clearTimeout(s.pending)
    }
  }

  stopAll(): void {
    for (const s of this.streams.values()) { if (s.pending) clearTimeout(s.pending); if (s.keepalive) clearInterval(s.keepalive) }
    this.streams.clear()
  }
}
