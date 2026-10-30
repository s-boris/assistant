// Inbound updates → event rows. The per-update logic (what a message, button press, reaction or
// poll answer says, and its meta fields) is lifted from telegram-plus server.ts (commit 974bed5):
// handleInbound and the message:* handlers, the callback_query handler, message_reaction and
// poll_answer. Where the plugin sent an MCP channel notification (content + meta), the adapter
// writes one events row: payload = the plugin's meta + text (the notification's content).
// Dropped here: downloads (photos are not fetched; file ids are kept), transcription, instant
// commands, private scans, the permission relay ("yes abcde" replies, perm: buttons), instant
// buttons and reaction shortcuts (local side effects), typing, the ack reaction, pairing replies.
import { Context } from 'grammy'
import type { Update, UserFromGetMe } from 'grammy/types'
import { gateMessage, senderAllowed, type Access } from './access.ts'
import type { EventRow } from './db.ts'

/** Update types requested from getUpdates. message_reaction must be listed explicitly (it is not
 *  in Telegram's default set); stopped_message_generation = Stop pressed on a streaming draft (P2). */
export const ALLOWED_UPDATES = ['message', 'callback_query', 'message_reaction', 'poll_answer', 'stopped_message_generation']

export const SOURCE = 'tg-adapter'

/** Delivery class for every inbound kind until the kind registry (schemas/kinds.toml) owns it:
 *  someone allowlisted wrote to, tapped on or reacted to the bot. */
export const INBOUND_DELIVERY = 'now'

export type Recent = { by: 'bot'; text: string } | { by: 'user'; user_id: string; user: string; text: string }
export type PollMeta = { chat_id: string; chat_type: string; message_id: number; question: string; options: string[] }

export type InboundDeps = {
  me: UserFromGetMe
  botId: string
  mode: string
  access: Access
  recent: (chat_id: string, message_id: number) => Recent | null
  poll: (poll_id: string) => PollMeta | null
  now?: () => number
}

export type CallbackAck = { ack_ms: number; ack_ok: boolean; ack_error?: string }

export type Inbound = { row: EventRow | null; drop?: string; remember?: { chat_id: string; message_id: number; recent: Recent } }

export function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1) + '…'
}

const oneLine = (s: string) => s.replace(/\s+/g, ' ').trim()

// Uploader-controlled strings that land in content.
export function safeName(s: string | undefined): string | undefined {
  return s?.replace(/[<>\[\]\r\n;]/g, '_')
}

export const idemKey = (botId: string, update_id: number) => `tg:${botId}:${update_id}`

type AttachmentMeta = { kind: string; file_id: string; size?: number; mime?: string; name?: string; duration?: number }

/** What a message says (content text, attachment, extra meta), per the plugin's message:* handlers.
 *  null: a message type the plugin had no handler for. */
export function messageContent(m: any): { text: string; attachment?: AttachmentMeta; extra?: Record<string, string> } | null {
  if (typeof m.text === 'string') return { text: m.text }
  if (m.photo?.length) {
    const best = m.photo[m.photo.length - 1]
    return { text: m.caption ?? '(photo)', attachment: { kind: 'photo', file_id: best.file_id, size: best.file_size } }
  }
  if (m.document) {
    const doc = m.document, name = safeName(doc.file_name)
    return { text: m.caption ?? `(document: ${name ?? 'file'})`, attachment: { kind: 'document', file_id: doc.file_id, size: doc.file_size, mime: doc.mime_type, name } }
  }
  if (m.voice) {
    const v = m.voice
    return { text: m.caption ?? '(voice message)', attachment: { kind: 'voice', file_id: v.file_id, size: v.file_size, mime: v.mime_type, duration: v.duration } }
  }
  if (m.audio) {
    const a = m.audio, name = safeName(a.file_name)
    return { text: m.caption ?? `(audio: ${safeName(a.title) ?? name ?? 'audio'})`, attachment: { kind: 'audio', file_id: a.file_id, size: a.file_size, mime: a.mime_type, name, duration: a.duration } }
  }
  if (m.video) {
    const v = m.video
    return { text: m.caption ?? '(video)', attachment: { kind: 'video', file_id: v.file_id, size: v.file_size, mime: v.mime_type, name: safeName(v.file_name) } }
  }
  if (m.video_note) {
    const vn = m.video_note
    return { text: '(video note)', attachment: { kind: 'video_note', file_id: vn.file_id, size: vn.file_size, duration: vn.duration } }
  }
  if (m.sticker) {
    const s = m.sticker
    return { text: `(sticker${s.emoji ? ` ${s.emoji}` : ''})`, attachment: { kind: 'sticker', file_id: s.file_id, size: s.file_size } }
  }
  // Venue messages also carry .location, so venue first.
  if (m.venue) {
    const v = m.venue
    return { text: `(venue: ${safeName(v.title)}, ${safeName(v.address)})`, extra: {
      latitude: String(v.location.latitude), longitude: String(v.location.longitude), venue_title: v.title, venue_address: v.address } }
  }
  if (m.location) {
    const l = m.location
    return { text: `(location: ${l.latitude}, ${l.longitude}${l.live_period ? ', live' : ''})`, extra: { latitude: String(l.latitude), longitude: String(l.longitude) } }
  }
  if (m.contact) {
    const c = m.contact
    const name = safeName([c.first_name, c.last_name].filter(Boolean).join(' '))
    return { text: `(contact: ${name}, ${safeName(c.phone_number)})`, extra: {
      contact_name: name ?? '', contact_phone: c.phone_number, ...(c.user_id ? { contact_user_id: String(c.user_id) } : {}) } }
  }
  return null
}

function row(deps: InboundDeps, update_id: number, kind: string, subject: string, next: string, payload: Record<string, unknown>): EventRow {
  const key = idemKey(deps.botId, update_id)
  return {
    ts: (deps.now ?? Date.now)() / 1000,
    source: SOURCE,
    kind,
    subject: truncate(oneLine(subject), 120),
    next,
    payload: { ...payload, update_id, bot: deps.me.username },
    labels: ['tg', deps.mode],
    delivery: INBOUND_DELIVERY,
    trace_id: key,
    idem_key: key,
  }
}

/** The button a callback query pressed, and whether its sender may press it (decided before
 *  anything else, so the query can be answered at once). null: not a data callback. */
export function callbackInfo(ctx: Context, access: Access): { allowed: boolean; label: string; data: string } | null {
  const cq = ctx.callbackQuery
  if (!cq || typeof cq.data !== 'string') return null
  const data = cq.data
  const msg = cq.message
  const chat = msg?.chat
  const allowed = !!(msg && chat && senderAllowed(access, chat.type, String(chat.id), String(cq.from.id)))
  const rows = ((msg as any)?.reply_markup?.inline_keyboard ?? []) as { text: string; callback_data?: string }[][]
  const label = rows.flat().find(b => b.callback_data === data)?.text ?? data
  return { allowed, label, data }
}

/** The toast the press gets (the plugin's wording). */
export const callbackToast = (allowed: boolean, label: string) => allowed ? `✓ ${truncate(label, 180)}` : 'Not authorized.'

export function toInbound(update: Update, api: any, deps: InboundDeps, ack?: CallbackAck): Inbound {
  const ctx = new Context(update, api, deps.me)
  const uid = update.update_id

  if (update.message) {
    const m = update.message as any
    const what = messageContent(m)
    if (!what) return { row: null, drop: 'unsupported message type' }
    if (!gateMessage(ctx, deps.access, deps.me.username)) return { row: null, drop: 'not allowed' }
    const from = ctx.from!
    const chat_id = String(ctx.chat!.id)
    const msgId = m.message_id as number
    const user = from.username ?? String(from.id)
    const replied = m.reply_to_message
    const repliedText = replied ? (replied.text ?? replied.caption ?? '') as string : ''
    const a = what.attachment
    const meta: Record<string, unknown> = {
      chat_id,
      message_id: String(msgId),
      user,
      user_id: String(from.id),
      ts: new Date((m.date ?? 0) * 1000).toISOString(),
      event_type: 'message',
      text: what.text,
      ...(a ? {
        attachment_kind: a.kind,
        attachment_file_id: a.file_id,
        ...(a.size != null ? { attachment_size: String(a.size) } : {}),
        ...(a.mime ? { attachment_mime: a.mime } : {}),
        ...(a.name ? { attachment_name: a.name } : {}),
        ...(a.duration != null ? { attachment_duration: String(a.duration) } : {}),
      } : {}),
      ...(replied ? {
        reply_to_message_id: String(replied.message_id),
        ...(repliedText ? { reply_to_text: truncate(repliedText, 300) } : {}),
      } : {}),
      ...(what.extra ?? {}),
    }
    return {
      row: row(deps, uid, 'tg.message', `${user}: ${what.text}`, `reply: outbox op send, chat ${chat_id}, reply_to ${msgId}`, meta),
      remember: { chat_id, message_id: msgId, recent: { by: 'user', user_id: String(from.id), user, text: truncate(oneLine(what.text), 200) } },
    }
  }

  if (update.callback_query) {
    const info = callbackInfo(ctx, deps.access)
    if (!info) return { row: null, drop: 'callback without data' }
    if (info.data.startsWith('perm:')) return { row: null, drop: 'permission relay button (not in the adapter)' }
    if (!info.allowed) return { row: null, drop: 'not allowed' }
    const cq = update.callback_query
    const msg = cq.message as any
    const chat_id = String(msg.chat.id)
    const meta: Record<string, unknown> = {
      chat_id,
      message_id: String(msg.message_id),
      user: cq.from.username ?? String(cq.from.id),
      user_id: String(cq.from.id),
      ts: new Date((deps.now ?? Date.now)()).toISOString(),
      event_type: 'button',
      callback_data: info.data,
      button_text: info.label,
      message_text: truncate(msg.text ?? msg.caption ?? deps.recent(chat_id, msg.message_id)?.text ?? '', 500),
      text: `(pressed button: ${info.label})`,
      ...(ack ? { ack_ms: ack.ack_ms, ack_ok: ack.ack_ok, ...(ack.ack_error ? { ack_error: ack.ack_error } : {}) } : {}),
    }
    return { row: row(deps, uid, 'tg.button', `button "${info.label}"`, `answer: outbox, chat ${chat_id}`, meta) }
  }

  if (update.message_reaction) {
    const r = update.message_reaction as any
    const user = r.user
    if (!user) return { row: null, drop: 'anonymous reaction' }
    const chat_id = String(r.chat.id)
    const senderId = String(user.id)
    if (!senderAllowed(deps.access, r.chat.type, chat_id, senderId)) return { row: null, drop: 'not allowed' }
    const mode = deps.access.forwardReactions ?? true
    if (mode === false) return { row: null, drop: 'reactions off' }
    type AnyReaction = { type: string; emoji?: string; custom_emoji_id?: string }
    const reactionKey = (x: AnyReaction) => x.type === 'emoji' ? x.emoji! : x.type === 'custom_emoji' ? `custom:${x.custom_emoji_id}` : x.type
    const reactionLabel = (x: AnyReaction) => x.type === 'emoji' ? x.emoji! : x.type === 'custom_emoji' ? '(custom emoji)' : '⭐'
    const oldR = r.old_reaction as AnyReaction[]
    const newR = r.new_reaction as AnyReaction[]
    const oldKeys = new Set(oldR.map(reactionKey))
    const newKeys = new Set(newR.map(reactionKey))
    const added = newR.filter(x => !oldKeys.has(reactionKey(x))).map(reactionLabel)
    const removed = oldR.filter(x => !newKeys.has(reactionKey(x))).map(reactionLabel)
    // Pure removals are usually corrections: only with 'all'.
    if (added.length === 0 && (removed.length === 0 || mode !== 'all')) return { row: null, drop: 'pure removal' }
    const target = deps.recent(chat_id, r.message_id)
    const what = !target
      ? `message ${r.message_id} (not in the recent-message cache)`
      : target.by === 'bot'
        ? `your message: "${target.text}"`
        : target.user_id === senderId
          ? `their own message: "${target.text}"`
          : `a message from ${target.user}: "${target.text}"`
    const content = added.length && removed.length
      ? `Changed reaction from ${removed.join(' ')} to ${added.join(' ')} on ${what}`
      : added.length
        ? `Reacted ${added.join(' ')} to ${what}`
        : `Removed reaction ${removed.join(' ')} from ${what}`
    const meta: Record<string, unknown> = {
      chat_id,
      user: user.username ?? senderId,
      user_id: senderId,
      ts: new Date(r.date * 1000).toISOString(),
      event_type: 'reaction',
      ...(added.length ? { reaction: added.join(' ') } : {}),
      ...(removed.length ? { removed_reaction: removed.join(' ') } : {}),
      reacted_to_message_id: String(r.message_id),
      ...(target ? { reacted_to_from: target.by, reacted_to_text: target.text } : {}),
      text: content,
    }
    const subj = added.length ? `${added.join(' ')} on message ${r.message_id}` : `removed ${removed.join(' ')} on message ${r.message_id}`
    return { row: row(deps, uid, 'tg.reaction', subj, '', meta) }
  }

  if (update.poll_answer) {
    const a = update.poll_answer as any
    const poll = deps.poll(a.poll_id)
    const user = a.user
    if (!poll) return { row: null, drop: 'unknown poll' }
    if (!user) return { row: null, drop: 'anonymous poll answer' }
    const senderId = String(user.id)
    if (!senderAllowed(deps.access, poll.chat_type, poll.chat_id, senderId)) return { row: null, drop: 'not allowed' }
    const chosen = (a.option_ids as number[]).map(i => poll.options[i] ?? `#${i}`)
    const content = chosen.length
      ? `(poll answer to "${truncate(poll.question, 200)}": ${chosen.join(', ')})`
      : `(retracted vote on poll "${truncate(poll.question, 200)}")`
    const meta: Record<string, unknown> = {
      chat_id: poll.chat_id,
      message_id: String(poll.message_id),
      user: user.username ?? senderId,
      user_id: senderId,
      ts: new Date((deps.now ?? Date.now)()).toISOString(),
      event_type: 'poll_answer',
      poll_id: String(a.poll_id),
      poll_question: truncate(poll.question, 300),
      poll_options: chosen.join(' | '),
      option_ids: a.option_ids,
      text: content,
    }
    return { row: row(deps, uid, 'tg.poll_answer', chosen.length ? `poll: ${chosen.join(', ')}` : 'poll: vote retracted', '', meta) }
  }

  // User stopped a streaming draft (Bot API 10.3; grammY's types predate it). Lifted from server.ts 3713-3737.
  const stop = (update as any).stopped_message_generation as
    | { chat: { id: number; type: string }; draft_id: number; from?: { id: number; username?: string } } | undefined
  if (stop) {
    const chat_id = String(stop.chat.id)
    const senderId = stop.from ? String(stop.from.id) : chat_id // the update carries no sender; in a DM the chat is the user
    if (!senderAllowed(deps.access, stop.chat.type, chat_id, senderId)) return { row: null, drop: 'not allowed' }
    const meta: Record<string, unknown> = {
      chat_id, event_type: 'stop', draft_id: String(stop.draft_id), user_id: senderId,
      ts: new Date((deps.now ?? Date.now)()).toISOString(), text: '(the user pressed Stop on your streaming draft)',
    }
    return { row: row(deps, uid, 'tg.stop', 'Stop pressed on the draft', 'stop the work and summarize', meta) }
  }

  return { row: null, drop: 'update type not consumed' }
}
