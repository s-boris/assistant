// "typing…" while the session works on an owner's event, lifted from telegram-plus server.ts (commit 974bed5,
// lines 975-1212: chatTyping, readBoard, watchBoard, evaluateTyping, scheduleTyping, typingEvent, withSend) into
// one class. The rules are 0.1.22's:
//  - an inbound message or button press is pending until an answer in that chat (send, draft, poll, location),
//    a turn end on the board (tg-status's state.json: idle_at > event + 1 s) or TYPING_MAX_MS;
//  - typing shows while an event is pending AND the board says "working", or the newest event is younger
//    than the pickup grace (12 s); without a readable board, until 90 s after the newest event;
//  - while a voice note is transcribed (preparing) typing shows regardless of the board;
//  - an explicit chat_action lasts until the next answer, stop, a turn end or 2 min;
//  - no chat action while a send to the chat is in flight or within 1.5 s after one, and every send first
//    waits (≤ 3 s) for an in-flight chat action, so "typing…" never lands after the answer;
//  - nothing shows while a draft streams in the chat.
import { readFileSync, statSync, watch } from 'fs'
import { basename, dirname } from 'path'
import type { TgCall } from './format.ts'

export const CHAT_ACTIONS = ['typing', 'upload_photo', 'record_video', 'upload_video', 'record_voice', 'upload_voice', 'upload_document', 'choose_sticker', 'find_location']

export type TypingTimes = { refreshMs: number; maxMs: number; noBoardMs: number; graceMs: number; turnEndMarginMs: number; chatActionMs: number;
  sendQuietMs: number; inflightWaitMs: number }
export const TYPING_DEFAULTS: TypingTimes = { refreshMs: 4_500, maxMs: 5 * 60_000, noBoardMs: 90_000, graceMs: 12_000, turnEndMarginMs: 1_000,
  chatActionMs: 120_000, sendQuietMs: 1_500, inflightWaitMs: 3_000 }

type ChatTyping = {
  events: number[]
  explicit?: { action: string; started: number; until: number }
  shown: string | null
  lastAction: number
  inflight?: Promise<unknown>
  timer?: ReturnType<typeof setTimeout>
  sending: number
  lastSendEnd: number
  preparing: number
}
type Board = { working: boolean; idleAt: number }

export class Typing {
  private chats = new Map<string, ChatTyping>()
  private boardCache: { mtimeMs: number; size: number; ino: number; value: Board | null } | null = null
  private watcher: { close(): void } | null = null
  private kick: ReturnType<typeof setTimeout> | undefined
  private stopped = false
  readonly t: TypingTimes

  constructor(private readonly o: { tg: TgCall; boardFile: string; enabled: boolean; streaming: (chat_id: string) => boolean; times?: Partial<TypingTimes> }) {
    this.t = { ...TYPING_DEFAULTS, ...(o.times ?? {}) }
  }

  private chat(chat_id: string): ChatTyping {
    let c = this.chats.get(chat_id)
    if (!c) { c = { events: [], shown: null, lastAction: 0, sending: 0, lastSendEnd: 0, preparing: 0 }; this.chats.set(chat_id, c) }
    return c
  }

  readBoard(force = false): Board | null {
    let st
    try { st = statSync(this.o.boardFile) } catch { this.boardCache = null; return null }
    if (!st.isFile() || st.size > 1 << 20) { this.boardCache = null; return null }
    const bc = this.boardCache
    if (!force && bc && bc.mtimeMs === st.mtimeMs && bc.size === st.size && bc.ino === st.ino) return bc.value
    let value: Board | null = null
    try {
      const s = JSON.parse(readFileSync(this.o.boardFile, 'utf8'))
      const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)
      const idle = num(s?.idle_at) ?? (s?.mode === 'idle' ? num(s?.since) : undefined)
      if (s?.mode === 'working') value = { working: true, idleAt: (idle ?? 0) * 1000 }
      else if (s?.mode === 'idle' && idle !== undefined) value = { working: false, idleAt: idle * 1000 }
    } catch {}
    this.boardCache = { mtimeMs: st.mtimeMs, size: st.size, ino: st.ino, value }
    return value
  }

  /** Re-evaluate at once when tg-status rewrites state.json (temp file + rename: watch the directory). */
  start(): void {
    if (!this.o.enabled || this.watcher || this.stopped) return
    const name = basename(this.o.boardFile)
    try {
      const w = watch(dirname(this.o.boardFile), { persistent: false }, (_e, file) => {
        if (file != null && !String(file).startsWith(name)) return
        if (this.kick) return
        this.kick = setTimeout(() => {
          this.kick = undefined
          this.readBoard(true)
          for (const id of this.chats.keys()) this.evaluate(id, 'board')
        }, 15)
      })
      w.on('error', () => { try { w.close() } catch {} ; this.watcher = null })
      this.watcher = w
    } catch {} // no tg-status dir yet: the ticks still read the file
  }

  stop(): void {
    this.stopped = true
    try { this.watcher?.close() } catch {}
    this.watcher = null
    if (this.kick) clearTimeout(this.kick)
    for (const c of this.chats.values()) if (c.timer) clearTimeout(c.timer)
  }

  /** An inbound message or button press from an allowed user. */
  event(chat_id: string): void {
    if (!this.o.enabled) return
    const c = this.chat(chat_id)
    c.events.push(Date.now())
    if (c.events.length > 50) c.events.splice(0, c.events.length - 50)
    this.evaluate(chat_id, 'inbound', true)
  }

  /** A voice note of this chat is being transcribed (+1) / done (-1); done counts as a fresh event. */
  preparing(chat_id: string, delta: 1 | -1): void {
    if (!this.o.enabled) return
    const c = this.chat(chat_id)
    c.preparing = Math.max(0, c.preparing + delta)
    if (delta < 0) this.event(chat_id)
    else this.evaluate(chat_id, 'preparing', true)
  }

  /** chat_action op: show `action` (≤ 2 min) or stop. */
  explicit(chat_id: string, action: string | null): void {
    const c = this.chat(chat_id)
    if (!action) { c.explicit = undefined; c.events = []; this.evaluate(chat_id, 'stop'); return }
    const now = Date.now()
    c.explicit = { action, started: now, until: now + this.t.chatActionMs }
    this.evaluate(chat_id, 'explicit', true)
  }

  evaluate(chat_id: string, why: string, kick = false): void {
    const c = this.chats.get(chat_id)
    if (!c || this.stopped) return
    const now = Date.now()
    const board = this.readBoard()
    c.events = c.events.filter(t => now - t < this.t.maxMs && !(board && board.idleAt > t + this.t.turnEndMarginMs))
    if (c.explicit && (now >= c.explicit.until || (board && board.idleAt > c.explicit.started))) c.explicit = undefined
    const streaming = this.o.streaming(chat_id)
    let action: string | null = null
    let wake = Infinity
    if (c.explicit) { action = c.explicit.action; wake = c.explicit.until }
    else if (c.preparing > 0 && !streaming) action = 'typing'
    else if (c.events.length && !streaming) {
      const newest = c.events[c.events.length - 1]!
      if (board) {
        if (board.working) { action = 'typing'; wake = newest + this.t.maxMs }
        else if (now - newest < this.t.graceMs) { action = 'typing'; wake = newest + this.t.graceMs }
      } else if (now - newest < this.t.noBoardMs) { action = 'typing'; wake = newest + this.t.noBoardMs }
    }
    if (c.timer) { clearTimeout(c.timer); c.timer = undefined }
    if (!action) { c.shown = null; return }
    if (c.sending > 0) return
    const quietUntil = c.lastSendEnd + this.t.sendQuietMs
    const newest = c.explicit ? c.explicit.started : c.preparing > 0 ? now : c.events[c.events.length - 1]!
    if (now < quietUntil && newest <= c.lastSendEnd) return this.schedule(chat_id, c, quietUntil + 5)
    if (c.shown !== action || now - c.lastAction >= this.t.refreshMs - 50 || kick) {
      c.lastAction = now
      const p = this.o.tg('sendChatAction', { chat_id, action }).catch(() => {})
      c.inflight = p
      void p.then(() => { if (c.inflight === p) c.inflight = undefined })
    }
    c.shown = action
    this.schedule(chat_id, c, Math.min(c.lastAction + this.t.refreshMs, wake + 5))
  }

  private schedule(chat_id: string, c: ChatTyping, at: number): void {
    if (c.timer) clearTimeout(c.timer)
    if (this.stopped) return
    c.timer = setTimeout(() => { c.timer = undefined; this.evaluate(chat_id, 'tick') }, Math.max(20, at - Date.now()))
    ;(c.timer as { unref?: () => void }).unref?.()
  }

  /** Every send to a chat goes through here. answer: clears earlier events and an explicit action. */
  async withSend<T>(chat_id: string, answer: boolean, fn: () => Promise<T>): Promise<T> {
    const c = this.chat(chat_id)
    const startedAt = Date.now()
    c.sending++
    if (c.timer) { clearTimeout(c.timer); c.timer = undefined }
    if (answer) { c.events = c.events.filter(t => t > startedAt); c.explicit = undefined; c.shown = null }
    try {
      if (c.inflight) await Promise.race([c.inflight, Bun.sleep(this.t.inflightWaitMs)])
      return await fn()
    } finally {
      c.sending--
      c.lastSendEnd = Date.now()
      if (c.sending === 0 && this.o.enabled) this.schedule(chat_id, c, c.lastSendEnd + this.t.sendQuietMs + 5)
    }
  }

  /** Tests and status: what shows in a chat. */
  shown(chat_id: string): string | null { return this.chats.get(chat_id)?.shown ?? null }
}
