// Outbox consumer: queued outbox rows → Bot API calls. The ops are the plugin's tools, lifted from
// telegram-plus server.ts (commit 974bed5): send = reply (chunking, rich with plain fallback, details fold,
// buttons/keyboards, reply_to, silent, effect, link_preview; files from the row's files column or body.files:
// photos, documents, albums, voice .ogg), edit = edit_message, react = react, poll = send_poll, draft = draft
// (draft.ts), chat_action = chat_action (typing.ts), delete = delete_message, pin = pin_message, location =
// send_location, commands = set_commands (the list as given: the adapter has no instant commands), download =
// download_attachment (into the instance inbox; runs beside the queue, ≤ 60 s). A row's body holds the tool's
// arguments (chat_id comes from the row's chat column; commands and download ignore it). What the op returned
// for the bridge (ids, notes, path, …) goes into the row's result column as JSON.
// Rules (WP7, rules.ts): quiet hours and sleep-in hold non-urgent send/poll rows, urgent bypasses
// them, repeats of the same text within the dedupe window are skipped, every failed row gets an
// outbox.failed event row (at the final failure only, never per retry). Errors (retryPlan): 429 → not_before =
// retry_after; network trouble (no answer: fetch failure, DNS, reset, timeout; or a 5xx) → retry with capped
// backoff until the row's retry window ends (6 h of retrying for messages, 10 min for edits, reactions, drafts,
// typing and downloads), unless part of the message already went out; a row that keeps failing while Telegram
// answers other calls (a message-specific error, not an outage) fails after 5 such failures, as before; anything
// else (4xx) → failed at once. Held rows (quiet hours, sleep-in) stay held while they retry. A row is 'sending'
// while its calls run: one found at start was interrupted → failed (never re-sent blind: it may have arrived).
import { GrammyError, HttpError } from 'grammy'
import { chatAllowed, type Access } from './access.ts'
import { savePress, type OnPress } from './buttons.ts'
import { foldFromArgs, withFold, type Piece } from './fold.ts'
import {
  asFormat, chunk, editWithFold, effectId, errText, foldSplit, inlineKeyboard, isBadRequest, isPrivateChatId, linkPreviewOptions,
  MAX_CHUNK_LIMIT, normalizeReaction, REACTION_EMOJI, REACTION_SET, RICH_CHUNK_LIMIT, replyMarkupFrom, sendFormatted, textBody,
  type TgCall,
} from './format.ts'
import type { PollMeta } from './inbound.ts'
import { nowS, type OutboxRow, type Store } from './db.ts'
import { dedupeKey, exemptFromRules, HELD_OPS, holdOff, holdReason, NO_RULES, type Rules } from './rules.ts'
import type { Drafts } from './draft.ts'
import { assertSendable, MAX_ATTACHMENT_BYTES, sendAlbum, sendFile, VOICE_EXTS, type Guard } from './files.ts'
import { extname } from 'path'
import { CHAT_ACTIONS, type Typing } from './typing.ts'

export const OPS = ['send', 'edit', 'react', 'poll', 'draft', 'chat_action', 'delete', 'pin', 'location', 'commands', 'download'] as const
/** Failures while Telegram answered other calls (strikes, retryPlan) before a row fails. */
export const MAX_ATTEMPTS = 5
/** Network trouble: how long a row keeps retrying, counted in time spent retrying from its first failure. */
export const RETRY_WINDOW_S = 6 * 3600
/** The same for ops that are useless late: an edit, reaction, draft, typing or download hours later. */
export const SHORT_RETRY_WINDOW_S = 600
export const SHORT_RETRY_OPS = new Set(['edit', 'react', 'draft', 'chat_action', 'download'])
/** Backoff between retries: 2, 4, 8, … s, capped (a healed link delivers within this). */
export const MAX_BACKOFF_S = 60
/** A gap past a planned retry beyond this (held, blocked behind the chat's head row, adapter down) is not retry time. */
const IDLE_SLACK_S = 30
/** Ops not bound to one chat: no chat gate, no per-chat order. */
export const UNCHATTED = new Set(['commands', 'download'])
/** Ops that answer the chat (end its draft first; clear its typing). */
export const ANSWERS = new Set(['send', 'poll', 'location'])

export type OpResult = { ids: number[]; notes: string[]; poll?: { id: string; meta: PollMeta }; extra?: Record<string, unknown> }

export type OutboxDeps = {
  tg: TgCall
  store: Store
  /** The config's bot username: the outbox key of rows enqueued by tg-adapter's CLI and the tg wrapper. */
  bot: string
  /** Every outbox key this adapter consumes (default [bot]); the bridge enqueues under the instance name. */
  bots?: string[]
  access: () => Access
  buttonsFile: string
  remember: (chat_id: string, message_id: number, text: string) => void
  clean: (s: string) => string
  log: (m: string) => void
  /** WP7 outbox rules; absent = v0 (no holds, no text dedupe). */
  rules?: Rules
  /** P2: file guard, drafts, typing, downloads (absent: those ops fail). */
  guard?: Guard
  drafts?: Drafts
  typing?: Typing
  download?: (file_id: string) => Promise<string>
  /** Sends to a chat: typing's race guard (answer = clears pending typing). */
  withSend?: <T>(chat_id: string, answer: boolean, fn: () => Promise<T>) => Promise<T>
  /** P3: save a sent card's buttons[].bind (returns notes for the brain). */
  binds?: (chat_id: string, message_id: number, buttons: unknown) => string[]
  /** P3: the instant commands the /commands menu lists first (0.1.22 menuCommands). */
  menu?: () => { command: string; description: string }[]
  /** P7: a send row's `approval` field → the args to send (✅/❌ row appended; the stored body keeps the nonce, never
   *  the action). Throws when the approval can't be made (the row fails). */
  approve?: (row: OutboxRow, args: Record<string, unknown>) => Record<string, unknown> | Promise<Record<string, unknown>>
  /** P7: a sent or edited keyboard carrying ap:<nonce> buttons: record the card for the expiry edit (notes). */
  cards?: (chat_id: string, message_id: number, buttons: unknown) => string[] | Promise<string[]>
  /** H6: the queue (default: the SQLite outbox of `store`; hub mode: hubd's outbox, huboutbox in hubsync.ts). */
  queue?: OutboxQueue
  /** H6 hub mode: a file entry (hubfile:out/<name>) → the path in this container. */
  resolveFile?: (f: string) => string
  /** When Telegram last answered a Bot API call (ok, or an error below 500), in the clock of drainOutbox's `now`.
   *  Tells a message-specific failure from an outage (retryPlan); absent = unknown (only the window ends a retry). */
  lastOk?: () => number
}

type Fields = Partial<Omit<OutboxRow, 'id'>>

/** The outbox as the consumer uses it. Local: the SQLite Store (sync); hub mode: hubd's consumer endpoints (async).
 *  Callers always await, so both behave the same. */
export type OutboxQueue = {
  queued(bots: string[]): OutboxRow[] | Promise<OutboxRow[]>
  claim(id: number, attempts: number): boolean | Promise<boolean>
  set(id: number, fields: Fields, row?: OutboxRow): void | Promise<void>
  fail(row: OutboxRow, fields: Fields & { last_error: string }, partial?: boolean): void | Promise<void>
  status(id: number): string | null | Promise<string | null>
  /** The rows duplicateOf compares with (same bot, chat, op; earlier id; ts ≥ since; queued|sending|sent). */
  earlier(row: OutboxRow, since: number): { id: number; body: string }[] | Promise<{ id: number; body: string }[]>
  /** Rows left 'sending' by a previous process → failed (+ outbox.failed). Returns how many. */
  interrupted(bots: string[]): number | Promise<number>
}

export function localQueue(store: Store): OutboxQueue {
  return {
    queued: bots => store.queued(bots),
    claim: (id, attempts) => store.claimOutbox(id, attempts),
    set: (id, fields) => store.setOutbox(id, fields),
    fail: (row, fields, partial) => store.failOutbox(row, fields, partial),
    status: id => (store.db.query('SELECT status FROM outbox WHERE id = ?').get(id) as { status: string } | null)?.status ?? null,
    earlier: (row, since) => store.db.query(`SELECT id, body FROM outbox WHERE bot = ? AND chat = ? AND op = ? AND mode = 'live' AND id < ?
    AND ts >= ? AND status IN ('queued', 'sending', 'sent') ORDER BY id DESC LIMIT 200`)
      .all(row.bot, row.chat, row.op, row.id, since) as { id: number; body: string }[],
    interrupted: bots => failInterrupted(store, bots),
  }
}

/** After a keyboard went out on a message: P3 binds and P7 approval cards (notes for the brain). */
async function cardNotes(d: OutboxDeps, chat_id: string, message_id: number, buttons: unknown): Promise<string[]> {
  return [...(d.binds ? d.binds(chat_id, message_id, buttons) : []), ...(d.cards ? await d.cards(chat_id, message_id, buttons) : [])]
}

/** Partial send: some messages went out before the failure; never retried. */
class PartialSend extends Error {
  constructor(readonly sent: number[], readonly total: number, readonly cause: unknown) {
    super(`send failed after ${sent.length} of ${total} part(s) sent: ${errText(cause)}`)
  }
}

export async function runOp(op: string, chat_id: string, args: Record<string, unknown>, d: OutboxDeps, files: string[] = []): Promise<OpResult> {
  if (!UNCHATTED.has(op) && !chatAllowed(d.access(), chat_id)) throw new Error(`chat ${chat_id} is not allowlisted`)
  if (ANSWERS.has(op)) {
    const go = () => (d.drafts ? d.drafts.end(chat_id, true) : Promise.resolve()).then(() => runOpInner(op, chat_id, args, d, files))
    return d.withSend ? d.withSend(chat_id, true, go) : go()
  }
  return runOpInner(op, chat_id, args, d, files)
}

async function runOpInner(op: string, chat_id: string, args: Record<string, unknown>, d: OutboxDeps, files: string[]): Promise<OpResult> {
  const tg = d.tg
  switch (op) {
    case 'send': {
      const text = (args.text as string | undefined) ?? ''
      const reply_to = args.reply_to != null ? Number(args.reply_to) : undefined
      const format = asFormat(args.format)
      if (args.files != null && !Array.isArray(args.files)) throw new Error('files must be a list of absolute paths')
      // The row's files column wins; the bridge also keeps the paths in body.files (never send both).
      if (!files.length) files = (args.files as string[] | undefined) ?? []
      if (d.resolveFile) files = files.map(f => typeof f === 'string' ? d.resolveFile!(f) : f)
      if (files.length && !d.guard) throw new Error('files are not supported here')
      for (const f of files) assertSendable(f, d.guard!)
      const fold = foldFromArgs(args.details, args.details_title)
      if (text === '' && files.length === 0 && !fold) throw new Error('text is empty and no files given')
      const reply_markup = replyMarkupFrom(args)
      const effect = isPrivateChatId(chat_id) ? effectId(args.effect) : undefined
      const lpo = linkPreviewOptions(args.link_preview)
      const silent = args.silent === true
      const isRich = format === 'rich'
      const limit = isRich ? RICH_CHUNK_LIMIT : MAX_CHUNK_LIMIT
      const mode = isRich ? 'newline' : 'length'
      // details: folded behind a tap, never split across two messages (fold.ts).
      const chunks: Piece[] = withFold(text === '' ? [] : chunk(text, limit, mode), fold, format, limit, foldSplit)
      const sentIds: number[] = []
      const notes: string[] = []
      try {
        for (let i = 0; i < chunks.length; i++) {
          const last = i === chunks.length - 1
          const ids = await sendFormatted(tg, d.remember, chat_id, chunks[i]!.text, format, {
            ...(chunks[i]!.entities ? { entities: chunks[i]!.entities } : {}),
            ...(reply_to != null && i === 0 ? { reply_parameters: { message_id: reply_to } } : {}),
            ...(last && reply_markup ? { reply_markup } : {}),
            ...(i === 0 && effect ? { message_effect_id: effect } : {}),
            ...(lpo ? { link_preview_options: lpo } : {}),
            ...(silent ? { disable_notification: true } : {}),
          }, notes, false, chunks[i]!.plain)
          sentIds.push(...ids)
          if (last && args.buttons != null) {
            rememberPress(d, chat_id, ids[ids.length - 1]!, args.on_press)
            notes.push(...await cardNotes(d, chat_id, ids[ids.length - 1]!, args.buttons))
          }
        }
        // Files go as separate messages after the text (0.1.22).
        const fileOpts: Record<string, unknown> = {
          ...(reply_to != null ? { reply_parameters: { message_id: reply_to } } : {}),
          ...(silent ? { disable_notification: true } : {}),
        }
        const fileEffect = chunks.length === 0 && effect ? { message_effect_id: effect } : {}
        // P8 (tg-voice): caption and duration on the first file only (a voice note's line under it).
        const firstFile: Record<string, unknown> = {
          ...(typeof args.caption === 'string' && args.caption ? { caption: args.caption.slice(0, 1024) } : {}),
          ...(Number.isSafeInteger(args.duration) && (args.duration as number) > 0 && files[0] && VOICE_EXTS.has(extname(files[0]).toLowerCase())
            ? { duration: args.duration } : {}),
        }
        if (files.length > 0 && args.album) {
          if (chunks.length === 0 && reply_markup) notes.push('albums cannot carry buttons/keyboards; markup dropped')
          sentIds.push(...await sendAlbum(tg, d.remember, chat_id, files, { ...fileOpts, ...fileEffect }, notes))
        } else {
          for (let i = 0; i < files.length; i++) {
            const lastGetsMarkup = chunks.length === 0 && i === files.length - 1 && reply_markup
            const id = await sendFile(tg, d.remember, chat_id, files[i]!, { ...fileOpts, ...(i === 0 ? { ...fileEffect, ...firstFile } : {}), ...(lastGetsMarkup ? { reply_markup } : {}) }, notes)
            sentIds.push(id)
            if (lastGetsMarkup && args.buttons != null) {
              rememberPress(d, chat_id, id, args.on_press)
              notes.push(...await cardNotes(d, chat_id, id, args.buttons))
            }
          }
        }
      } catch (err) {
        if (sentIds.length) throw new PartialSend(sentIds, chunks.length + files.length, err)
        throw err
      }
      return { ids: sentIds, notes }
    }
    case 'edit': {
      const message_id = Number(args.message_id)
      if (!Number.isSafeInteger(message_id)) throw new Error('message_id is required')
      const format = asFormat(args.format)
      const markup = args.buttons != null ? inlineKeyboard(args.buttons) : undefined
      const fold = foldFromArgs(args.details, args.details_title)
      if (args.text == null) {
        if (fold) throw new Error('details needs text: pass the full new text together with details')
        if (!markup) throw new Error('pass text and/or buttons')
        await tg('editMessageReplyMarkup', { chat_id, message_id, reply_markup: markup })
        if (markup.inline_keyboard.length) rememberPress(d, chat_id, message_id, args.on_press)
        return { ids: [message_id], notes: await cardNotes(d, chat_id, message_id, args.buttons) }
      }
      const edit = editWithFold(args.text as string, fold, format)
      const text = edit.text
      const lpo = format === 'rich' ? undefined : linkPreviewOptions(args.link_preview)
      const extra = { ...(markup ? { reply_markup: markup } : {}), ...(lpo ? { link_preview_options: lpo } : {}) }
      const notes: string[] = []
      try {
        await tg('editMessageText', { chat_id, message_id, ...textBody(text, format), ...(edit.entities ? { entities: edit.entities } : {}), ...extra })
      } catch (err) {
        if (format === 'text' || !isBadRequest(err) || /not modified/i.test(errText(err))) throw err
        notes.push(`${format} rejected (${errText(err)}); edited as plain text`)
        const plain = edit.plain && edit.plain.text.length <= MAX_CHUNK_LIMIT ? edit.plain : undefined
        await tg('editMessageText', { chat_id, message_id, text: plain?.text ?? text, ...(plain ? { entities: plain.entities } : {}), ...extra })
      }
      d.remember(chat_id, message_id, text)
      if (markup?.inline_keyboard.length) rememberPress(d, chat_id, message_id, args.on_press)
      if (markup) notes.push(...await cardNotes(d, chat_id, message_id, args.buttons))
      return { ids: [message_id], notes }
    }
    case 'react': {
      const message_id = Number(args.message_id)
      if (!Number.isSafeInteger(message_id)) throw new Error('message_id is required')
      if (args.emoji == null && args.emojis == null && args.remove !== true) throw new Error('pass emoji, emojis or remove: true')
      const raw = args.remove === true ? [] : Array.isArray(args.emojis) ? (args.emojis as string[]) : [String(args.emoji ?? '')]
      let list = raw.map(normalizeReaction).filter(e => e !== '')
      const bad = list.filter(e => !REACTION_SET.has(e))
      if (bad.length) throw new Error(`not in Telegram's reaction whitelist: ${bad.join(' ')} — allowed: ${REACTION_EMOJI.join(' ')}`)
      const set = (l: string[]) => tg('setMessageReaction', {
        chat_id, message_id,
        reaction: l.map(emoji => ({ type: 'emoji', emoji })),
        ...(args.big === true && l.length ? { is_big: true } : {}),
      })
      const notes: string[] = []
      try {
        await set(list)
      } catch (err) {
        if (list.length < 2 || !isBadRequest(err)) throw err
        await set(list.slice(0, 1))
        notes.push(`Telegram refused ${list.length} reactions: ${errText(err)}; set ${list[0]} only`)
        list = list.slice(0, 1)
      }
      const note = notes.length ? ` (${notes[0]})` : ''
      return { ids: [message_id], notes, extra: { text: list.length ? `reacted ${list.join(' ')}${note}` : 'reaction removed' } }
    }
    case 'poll': {
      const options = (args.options as string[] | undefined) ?? []
      if (options.length < 2 || options.length > 12) throw new Error('2-12 options required')
      if (typeof args.question !== 'string' || !args.question) throw new Error('question is required')
      const quiz = typeof args.quiz_correct === 'number'
      const sent = await tg('sendPoll', {
        chat_id,
        question: args.question,
        options: options.map(text => ({ text })),
        is_anonymous: args.anonymous === true,
        ...(args.multiple ? { allows_multiple_answers: true } : {}),
        ...(quiz ? { type: 'quiz', correct_option_id: args.quiz_correct } : {}),
        ...(args.silent ? { disable_notification: true } : {}),
      })
      d.remember(chat_id, sent.message_id, `[poll] ${args.question}`)
      const poll = sent.poll?.id ? {
        id: String(sent.poll.id),
        meta: {
          chat_id,
          chat_type: sent.chat?.type ?? (isPrivateChatId(chat_id) ? 'private' : 'group'),
          message_id: sent.message_id,
          question: String(args.question),
          options,
        },
      } : undefined
      return { ids: [sent.message_id], notes: [], ...(poll ? { poll } : {}) }
    }
    case 'draft': {
      if (!d.drafts) throw new Error('drafts are not available here')
      if (args.clear) {
        const s = await (d.withSend ? d.withSend(chat_id, true, () => d.drafts!.end(chat_id, true)) : d.drafts.end(chat_id, true))
        return { ids: [], notes: [], extra: { cleared: !!s, text: s ? 'draft cleared' : 'no active draft' } }
      }
      // An answer: ends typing (a draft shows its own preview).
      const s = await (d.withSend ? d.withSend(chat_id, true, () => upd()) : upd())
      function upd() { return d.drafts!.update(chat_id, (args.text as string | undefined) ?? '', args.append === true, asFormat(args.format), args.can_stop === true) }
      const notes = s.notes.splice(0)
      const how = s.mode === 'draft' ? 'streaming draft' : `progress message ${s.messageId ?? '(pending)'}`
      return { ids: s.messageId != null ? [s.messageId] : [], notes, extra: { draft_mode: s.mode, chars: s.text.length,
        ...(s.messageId != null ? { message_id: s.messageId } : {}),
        text: `draft updated (${how}, ${s.text.length} chars)${notes.length ? `\nnote: ${notes.join('; ')}` : ''}` } }
    }
    case 'chat_action': {
      if (!d.typing) throw new Error('chat actions are not available here')
      if (args.stop) { d.typing.explicit(chat_id, null); return { ids: [], notes: [], extra: { stopped: true, text: 'stopped' } } }
      const action = (args.action as string | undefined) ?? 'typing'
      if (!CHAT_ACTIONS.includes(action)) throw new Error(`action must be one of ${CHAT_ACTIONS.join(', ')}`)
      d.typing.explicit(chat_id, action)
      return { ids: [], notes: [], extra: { action, text: `showing ${action}` } }
    }
    case 'delete': {
      const message_id = Number(args.message_id)
      if (!Number.isSafeInteger(message_id)) throw new Error('message_id is required')
      await tg('deleteMessage', { chat_id, message_id })
      return { ids: [message_id], notes: [] }
    }
    case 'pin': {
      const message_id = Number(args.message_id)
      if (!Number.isSafeInteger(message_id)) throw new Error('message_id is required')
      if (args.unpin) await tg('unpinChatMessage', { chat_id, message_id })
      else await tg('pinChatMessage', { chat_id, message_id, disable_notification: args.notify !== true })
      return { ids: [message_id], notes: [], extra: { pinned: !args.unpin } }
    }
    case 'location': {
      const latitude = Number(args.latitude), longitude = Number(args.longitude)
      if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) throw new Error('latitude and longitude are required')
      const base = { chat_id, latitude, longitude, ...(args.silent ? { disable_notification: true } : {}) }
      const sent = args.title && args.address
        ? await tg('sendVenue', { ...base, title: args.title, address: args.address })
        : await tg('sendLocation', base)
      d.remember(chat_id, sent.message_id, args.title ? `[venue: ${args.title}]` : `[location: ${latitude}, ${longitude}]`)
      return { ids: [sent.message_id], notes: [] }
    }
    case 'commands': {
      const list = (args.commands as { command: string; description: string }[] | undefined) ?? []
      if (!Array.isArray(list)) throw new Error('commands must be a list')
      const clean = list.map(c => ({ command: String(c.command).replace(/^\//, '').toLowerCase(), description: String(c.description) }))
      for (const c of clean) {
        if (!/^[a-z0-9_]{1,32}$/.test(c.command)) throw new Error(`invalid command name: ${c.command}`)
        if (c.description.length < 1 || c.description.length > 256) throw new Error(`description for /${c.command} must be 1-256 chars`)
      }
      // P3: the instant commands come first, then the custom ones (a custom entry with an instant name is left out).
      const first = d.menu?.() ?? []
      const taken = new Set(first.map(c => c.command))
      const shown = [...first, ...clean.filter(c => !taken.has(c.command))]
      const scope = { type: 'all_private_chats' }
      if (shown.length) await tg('setMyCommands', { commands: shown, scope })
      else await tg('deleteMyCommands', { scope })
      d.store.setMeta(`commands:${d.bot}`, JSON.stringify(clean))
      return { ids: [], notes: [], extra: { commands: shown.map(c => c.command),
        ...(first.length ? { text: shown.length ? `commands set: ${shown.map(c => '/' + c.command).join(' ')}` : 'menu cleared (no commands)' } : {}) } }
    }
    case 'download': {
      if (!d.download) throw new Error('downloads are not available here')
      const file_id = String(args.file_id ?? '')
      if (!file_id) throw new Error('file_id is required')
      return { ids: [], notes: [], extra: { path: await d.download(file_id) } }
    }
    default:
      throw new Error(`unknown op: ${op}`)
  }
}

export { MAX_ATTACHMENT_BYTES }

function rememberPress(d: OutboxDeps, chat_id: string, message_id: number, onPress: unknown): void {
  const mode = (onPress as OnPress | undefined) ?? 'mark'
  try { savePress(d.buttonsFile, `${chat_id}:${message_id}`, mode) } catch (err) {
    d.log(`saving on_press for ${chat_id}:${message_id} failed: ${err}`)
  }
}

/** Whether a failed op may go again: 429 (after retry_after), a 5xx, or no answer at all (grammy's HttpError wraps
 *  fetch failures, DNS, resets, its timeout and a non-JSON gateway page; raw fetches, as downloads use, fail with
 *  the codes below). A partial send and every other error (4xx, bad arguments) are final. */
export function retryable(err: unknown): { retry: boolean; after?: number } {
  if (err instanceof PartialSend) return { retry: false }
  if (err instanceof GrammyError) {
    if (err.error_code === 429) return { retry: true, after: Number(err.parameters?.retry_after) || 1 }
    return { retry: err.error_code >= 500 }
  }
  if (err instanceof HttpError) return { retry: true }
  return { retry: networkError(err) }
}

const NET_CODES = new Set(['ECONNRESET', 'ECONNREFUSED', 'ECONNABORTED', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT', 'EPIPE',
  'ENETUNREACH', 'ENETDOWN', 'EHOSTUNREACH', 'ConnectionRefused', 'ConnectionClosed', 'FailedToOpenSocket', 'UND_ERR_SOCKET',
  'UND_ERR_CONNECT_TIMEOUT'])

/** A transport failure outside grammy: a Bun/Node network error code or a timeout, on the error or its causes. */
export function networkError(err: unknown): boolean {
  let e: any = err
  for (let i = 0; e && typeof e === 'object' && i < 4; i++, e = e.cause) {
    if (typeof e.code === 'string' && NET_CODES.has(e.code)) return true
    if (e.name === 'TimeoutError') return true
  }
  return false
}

/** A retrying row's window, kept in its result column while it waits (the sent result replaces it):
 *  since = start of the window, at = the last failure, next = its planned retry, strikes (retryPlan). */
export type RetryState = { since: number; at: number; next: number; strikes: number }

export function retryState(row: Pick<OutboxRow, 'result'>): RetryState | null {
  try {
    const r = JSON.parse(row.result ?? 'null')?.retry
    return r && [r.since, r.at, r.next, r.strikes].every(Number.isFinite) ? r : null
  } catch { return null }
}

/** The next step for a row whose op just failed with a retryable error at t (`attempts` = this attempt's number).
 *  - The window counts time spent retrying from the first failure: a gap past the planned retry (held by quiet
 *    hours or sleep-in, blocked behind the chat's head row, adapter stopped) moves `since` on by that gap.
 *  - A strike is a failure while Telegram demonstrably answered: the row's first failure, or one after Telegram
 *    answered another call since the row's previous failure (lastOk). That is a message-specific error, not an
 *    outage; MAX_ATTEMPTS strikes end the row as before. A 429 is never a strike.
 *  - Past the window it gives up. The wait: retry_after for a 429, else 2^attempts s capped at MAX_BACKOFF_S. */
export function retryPlan(row: OutboxRow, t: number, attempts: number, after?: number, lastOk?: number):
  { wait: number; state: RetryState; end?: 'strikes' | 'window'; window: number } {
  const prev = retryState(row)
  const window = SHORT_RETRY_OPS.has(row.op) ? SHORT_RETRY_WINDOW_S : RETRY_WINDOW_S
  const since = prev ? prev.since + Math.max(0, t - prev.next - IDLE_SLACK_S) : t
  const strike = after == null && (!prev || (lastOk != null && lastOk > prev.at))
  const strikes = (prev?.strikes ?? 0) + (strike ? 1 : 0)
  const wait = after ?? Math.min(2 ** Math.min(attempts, 16), MAX_BACKOFF_S)
  const state = { since, at: t, next: t + wait, strikes }
  const end = strikes >= MAX_ATTEMPTS ? 'strikes' as const : t - since >= window ? 'window' as const : undefined
  return { wait, state, window, ...(end ? { end } : {}) }
}

/** 21600 → "6 h", 600 → "10 min". */
const dur = (s: number) => (s >= 3600 && s % 3600 === 0 ? `${s / 3600} h` : `${Math.round(s / 60)} min`)

/** Rows left 'sending' by a previous process: interrupted; may or may not have arrived. Each one
 *  becomes failed with an outbox.failed event row. */
export function failInterrupted(store: Store, bots: string | string[]): number {
  const keys = Array.isArray(bots) ? bots : [bots]
  const rows = store.db.query(`SELECT * FROM outbox WHERE status = 'sending' AND bot IN (${keys.map(() => '?').join(', ')}) ORDER BY id`)
    .all(...keys) as OutboxRow[]
  for (const row of rows) store.failOutbox(row, { last_error: 'interrupted while sending (may have been delivered)' })
  return rows.length
}

/** One pass over the queue, in id order (rules.ts): per chat, a row waiting for a retry holds back
 *  the chat's later rows; repeats are skipped; quiet hours / sleep-in hold non-urgent send/poll rows. */
export async function drainOutbox(d: OutboxDeps, now: () => number = nowS): Promise<number> {
  const rules = d.rules ?? NO_RULES
  const hold = holdReason(rules, now())
  const blocked = new Set<string>()
  const q = d.queue ?? localQueue(d.store)
  let done = 0
  for (const row of await q.queued(d.bots ?? [d.bot])) {
    if (!UNCHATTED.has(row.op) && blocked.has(row.chat)) continue
    if (row.not_before != null && row.not_before > now()) {
      if (row.attempts > 0) blocked.add(row.chat) // a retry wait keeps the chat's order; a scheduled row waits alone
      continue
    }
    const exempt = exemptFromRules(row)
    const dup = exempt ? null : await duplicateIn(q, row, rules.dedupeWindowS)
    if (dup != null) {
      await q.set(row.id, { status: 'skipped', last_error: `duplicate of #${dup} (same text within ${rules.dedupeWindowS}s)` }, row)
      d.log(`outbox #${row.id} ${row.op}: duplicate of #${dup}, skipped`)
      continue
    }
    if (hold && !row.urgent && !exempt && !holdOff(row) && HELD_OPS.has(row.op)) continue
    if (row.op === 'download') { void runRow(row, d, now, q).catch(err => d.log(`outbox #${row.id} download: ${errText(err)}`)); done++; continue } // ≤ 60 s: beside the queue
    await runRow(row, d, now, q)
    done++
    if ((await q.status(row.id)) === 'queued') blocked.add(row.chat)
  }
  return done
}

/** The id of an earlier live row this one repeats (same bot, chat, op and dedupe key, enqueued at
 *  most windowS before it, not failed or skipped), or null. */
export function duplicateOf(store: Store, row: OutboxRow, windowS: number): number | null {
  const key = dupKey(row, windowS)
  if (!key) return null
  return firstDup(row, key, localQueue(store).earlier(row, row.ts - windowS) as { id: number; body: string }[])
}

/** duplicateOf over any queue (hub mode: hubd's /outbox/dupes). */
export async function duplicateIn(q: OutboxQueue, row: OutboxRow, windowS: number): Promise<number | null> {
  const key = dupKey(row, windowS)
  if (!key) return null
  return firstDup(row, key, await q.earlier(row, row.ts - windowS))
}

function dupKey(row: OutboxRow, windowS: number): string | null {
  if (!(windowS > 0) || !HELD_OPS.has(row.op)) return null
  try { return dedupeKey(row.op, JSON.parse(row.body)) } catch { return null }
}

function firstDup(row: OutboxRow, key: string, earlier: { id: number; body: string }[]): number | null {
  for (const e of earlier) {
    try { if (dedupeKey(row.op, JSON.parse(e.body)) === key) return e.id } catch {}
  }
  return null
}

async function runRow(row: OutboxRow, d: OutboxDeps, now: () => number, q: OutboxQueue = d.queue ?? localQueue(d.store)): Promise<void> {
  let args: Record<string, unknown>
  try {
    args = JSON.parse(row.body)
    if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('body must be a JSON object')
  } catch (err) {
    await q.fail(row, { last_error: d.clean(`bad body: ${errText(err)}`) })
    return
  }
  // Claim it only if still queued: a writer may withdraw a row it gave up on (core.tg, status skipped) meanwhile.
  if (!(await q.claim(row.id, row.attempts + 1))) return
  try {
    let files: string[] = []
    if (row.files) {
      const f = JSON.parse(row.files)
      if (!Array.isArray(f) || !f.every(x => typeof x === 'string')) throw new Error('files column must be a JSON list of paths')
      files = f
    }
    if (row.op === 'send' && args.approval != null) { // P7: before the send, so a refused approval sends nothing
      if (!d.approve) throw new Error('approval cards are not available here')
      args = await d.approve(row, args)
    }
    const r = await runOp(row.op, row.chat, args, d, files)
    if (r.poll) d.store.setMeta(`poll:${r.poll.id}`, JSON.stringify(r.poll.meta))
    await q.set(row.id, {
      status: 'sent', sent_ts: now(), tg_message_id: r.ids[r.ids.length - 1] ?? null,
      last_error: r.notes.length ? d.clean(`note: ${r.notes.join('; ')}`) : null,
      result: JSON.stringify({ ids: r.ids, notes: r.notes, ...(r.extra ?? {}) }),
    }, row)
  } catch (err) {
    const { retry, after } = retryable(err)
    const attempts = row.attempts + 1
    const msg = d.clean(errText(err))
    const t = now()
    const plan = retry ? retryPlan(row, t, attempts, after, d.lastOk?.()) : null
    if (plan && !plan.end) {
      await q.set(row.id, { status: 'queued', not_before: plan.state.next, last_error: msg, result: JSON.stringify({ retry: plan.state }) }, row)
      // A long outage retries every MAX_BACKOFF_S: the first retries and then about one line per half hour.
      if (attempts <= MAX_ATTEMPTS || attempts % 30 === 0) d.log(`outbox #${row.id} ${row.op}: ${msg}; retry in ${plan.wait}s (attempt ${attempts})`)
    } else {
      const last_error = plan?.end === 'window' ? `${msg} (gave up after ${dur(plan.window)} of retries)` : msg
      const partial = err instanceof PartialSend ? { tg_message_id: err.sent[err.sent.length - 1] ?? null } : {}
      await q.fail({ ...row, attempts }, { last_error, attempts, ...partial }, err instanceof PartialSend)
      d.log(`outbox #${row.id} ${row.op} failed: ${last_error}`)
    }
  }
}
