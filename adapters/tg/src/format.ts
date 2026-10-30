// Message formatting, lifted from telegram-plus server.ts (commit 974bed5): grapheme-safe
// chunking, the four formats (text / markdownv2 / html / rich) with rich hard breaks, inline and
// reply keyboards, link previews, effects, the plain-text fallback for rejected formatting, the
// fold (details) edit helper and Telegram's reaction whitelist. Changes from the plugin: functions
// are exported; sendFormatted takes the Bot API call and a sent-callback as parameters instead of
// the plugin's globals (tg(), remember()). Behaviour is unchanged.
import { GrammyError } from 'grammy'
import { withFold, type Fold, type Piece, type Plain } from './fold.ts'

/** One raw Bot API call (grammY's raw API: methods newer than its bundled types work too). */
export type TgCall = (method: string, params: Record<string, unknown>) => Promise<any>

export const MAX_CHUNK_LIMIT = 4096
export const RICH_CHUNK_LIMIT = 32000 // rich messages: 32768 chars

let graphemes: Intl.Segmenter | undefined

/** The last safe place to cut `text` at or before `cut` (a UTF-16 index, so
 *  limits stay in Telegram's UTF-16 units): a grapheme boundary, so an emoji
 *  (surrogate pair, ZWJ sequence, flag, skin tone) or a letter with its accents
 *  is never split. A single grapheme longer than `cut` falls back to a code
 *  point boundary; the result is always > 0, so chunk() makes progress. */
export function safeCut(text: string, cut: number): number {
  if (cut >= text.length) return text.length
  if (cut <= 0) return Math.min(text.length, isHighSurrogate(text, 0) && isLowSurrogate(text, 1) ? 2 : 1)
  // A boundary at `cut` depends on the code point that follows it, so keep a
  // few code units past it; everything before is segmented from the start.
  graphemes ??= new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  let best = 0
  for (const { index } of graphemes.segment(text.slice(0, cut + 4))) {
    if (index > cut) break
    best = index
  }
  if (best > 0) return best
  // One grapheme longer than the limit (e.g. a long run of combining marks):
  // cut between code points instead.
  return isLowSurrogate(text, cut) && isHighSurrogate(text, cut - 1) ? (cut > 1 ? cut - 1 : 2) : cut
}

export function isHighSurrogate(s: string, i: number): boolean {
  const c = s.charCodeAt(i)
  return c >= 0xd800 && c <= 0xdbff
}

export function isLowSurrogate(s: string, i: number): boolean {
  const c = s.charCodeAt(i)
  return c >= 0xdc00 && c <= 0xdfff
}

export function chunk(text: string, limit: number, mode: 'length' | 'newline'): string[] {
  if (text.length <= limit) return [text]
  const out: string[] = []
  let rest = text
  while (rest.length > limit) {
    let cut = limit
    if (mode === 'newline') {
      const para = rest.lastIndexOf('\n\n', limit)
      const line = rest.lastIndexOf('\n', limit)
      const space = rest.lastIndexOf(' ', limit)
      cut = para > limit / 2 ? para : line > limit / 2 ? line : space > 0 ? space : limit
    }
    // A cut right before a newline or space is already safe (and keeps CRLF as before).
    if (rest[cut] !== '\n' && rest[cut] !== ' ') cut = safeCut(rest, cut)
    out.push(rest.slice(0, cut))
    rest = rest.slice(cut).replace(/^\n+/, '')
  }
  if (rest) out.push(rest)
  return out
}

export type Format = 'text' | 'markdownv2' | 'html' | 'rich'
export const FORMATS: Format[] = ['text', 'markdownv2', 'html', 'rich']

export function asFormat(v: unknown): Format {
  const f = (v as string | undefined) ?? 'text'
  if (!FORMATS.includes(f as Format)) throw new Error(`unknown format: ${f}`)
  return f as Format
}

export function parseModeOf(f: Format): 'MarkdownV2' | 'HTML' | undefined {
  return f === 'markdownv2' ? 'MarkdownV2' : f === 'html' ? 'HTML' : undefined
}

/** Body fields for a text-ish message in the given format. */
export function textBody(text: string, f: Format): Record<string, unknown> {
  if (f === 'rich') return { rich_message: { markdown: richMarkdown(text) } }
  const pm = parseModeOf(f)
  return { text, ...(pm ? { parse_mode: pm } : {}) }
}

export const RICH_MAX_CHARS = 32768

/** Rich markdown as sent: hard line breaks added (richHardBreaks), unless that
 *  would push it past Telegram's rich message limit. */
export function richMarkdown(text: string): string {
  const md = richHardBreaks(text)
  return md.length <= RICH_MAX_CHARS ? md : text
}

export type RichLine = 'text' | 'quote' | 'other'

/** What a line is outside code blocks: plain paragraph text (may get a hard
 *  break), quoted paragraph text, or anything Markdown already lays out itself. */
export function richLineKind(line: string): RichLine {
  if (line.trim() === '' || /^(\t| {4})/.test(line)) return 'other' // blank, indented code / list continuation
  const t = line.trimStart()
  if (t.startsWith('>')) return richLineKind(t.replace(/^>+ ?/, '')) === 'text' ? 'quote' : 'other'
  if (
    /^#{1,6}(\s|$)/.test(t) || // heading
    t.startsWith('|') || // table row
    /^([-*+]|\d{1,9}[.)])(\s|$)/.test(t) || // list item
    /^([-*_])(\s*\1){2,}\s*$/.test(t) || /^=+\s*$/.test(t) || // rule, setext underline
    /^:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(t) || // table delimiter row without a leading pipe
    /^(<|!\[|\[[^\]]*\]:|\$\$)/.test(t) // HTML, media block, link/footnote definition, math
  ) return 'other'
  return 'text'
}

/** The fence marker (``` / ~~~ …) if the line opens a fenced code block. A
 *  backtick fence's info string has no backticks ("```x```" is inline code). */
export function opensFence(line: string): string | null {
  const m = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line)
  if (!m || (m[1]![0] === '`' && m[2]!.includes('`'))) return null
  return m[1]!
}

export function closesFence(line: string, fence: string): boolean {
  const m = /^ {0,3}(`{3,}|~{3,})\s*$/.exec(line)
  return !!m && m[1]![0] === fence[0] && m[1]!.length >= fence.length
}

/** Telegram's rich markdown follows GFM: a single newline is a soft break, so
 *  "🎬 …\n🛡 …" or "• a\n• b" render as one run-on paragraph. A single newline
 *  between two plain text lines (or two quoted ones) gets a "<br>" (a supported
 *  rich HTML tag, allowed inline in rich markdown; core.telegram.org/bots/api/
 *  rich-messages). Left alone: fenced code and $$ math blocks, tables, list
 *  items, headings, rules, HTML/media/definition lines, indented lines, blank-line
 *  paragraph breaks, lines already ending in a hard break. Idempotent. */
export function richHardBreaks(md: string): string {
  if (!md.includes('\n')) return md
  const lines = md.split('\n')
  const kinds: RichLine[] = []
  let fence: string | null = null // the open code fence (``` / ~~~), if any
  let quoteFence: string | null = null // the same, inside a block quote
  let math = false
  let table = false
  for (const raw of lines) {
    const line = raw.replace(/\r$/, '')
    if (fence != null) {
      if (closesFence(line, fence)) fence = null
      kinds.push('other')
      continue
    }
    if (math) {
      if (line.trim() === '$$') math = false
      kinds.push('other')
      continue
    }
    const open = opensFence(line)
    if (open) { fence = open; kinds.push('other'); continue }
    if (line.trim() === '$$') { math = true; kinds.push('other'); continue }
    // Block quotes: track fences inside them too; any non-quote line ends that.
    const t = line.trimStart()
    if (t.startsWith('>')) {
      const inner = t.replace(/^>+ ?/, '')
      if (quoteFence != null) {
        if (closesFence(inner, quoteFence)) quoteFence = null
        kinds.push('other')
        continue
      }
      const qopen = opensFence(inner)
      if (qopen) { quoteFence = qopen; kinds.push('other'); continue }
    } else quoteFence = null
    // Tables: rows after the delimiter row need no leading pipe.
    if (line.trim() === '' || !line.includes('|')) table = false
    if (/^ {0,3}\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(line)) table = true
    else if (table) { kinds.push('other'); continue }
    kinds.push(richLineKind(line))
  }
  const quoteDepth = (l: string) => /^\s*(>+)/.exec(l)?.[1]!.length ?? 0
  for (let i = 0; i < lines.length - 1; i++) {
    const a = kinds[i], b = kinds[i + 1]
    if (a === 'other' || a !== b) continue
    if (a === 'quote' && quoteDepth(lines[i]!) !== quoteDepth(lines[i + 1]!)) continue
    if (/( {2}|\\|<br\s*\/?>)\r?$/i.test(lines[i]!)) continue // already a hard break
    lines[i] = lines[i]!.replace(/\r?$/, m => '<br>' + m)
  }
  return lines.join('\n')
}

export type ButtonSpec = string | { text: string; data?: string; callback_data?: string; url?: string; style?: 'primary' | 'success' | 'danger' }

export function inlineKeyboard(rows: unknown): { inline_keyboard: unknown[][] } {
  if (!Array.isArray(rows)) throw new Error('buttons must be an array of rows')
  // Accept a flat list as a single row.
  const norm = rows.length > 0 && !Array.isArray(rows[0]) ? [rows] : rows
  return {
    inline_keyboard: (norm as ButtonSpec[][]).map(row => row.map(b => {
      const spec = typeof b === 'string' ? { text: b } : b
      if (!spec?.text) throw new Error('every button needs text')
      const out: Record<string, unknown> = { text: spec.text }
      if (spec.style) out.style = spec.style
      if (spec.url) {
        out.url = spec.url
      } else {
        // "callback_data" (the Bot API's own name) is accepted as an alias for "data".
        const data = spec.data ?? spec.callback_data ?? spec.text
        if (typeof data !== 'string') throw new Error('button data must be a string')
        if (Buffer.byteLength(data, 'utf8') > 64) throw new Error(`button data over 64 bytes: ${data}`)
        if (data.startsWith('perm:')) throw new Error('button data may not start with "perm:"')
        out.callback_data = data
      }
      return out
    })),
  }
}

export function replyMarkupFrom(args: Record<string, unknown>): Record<string, unknown> | undefined {
  const kinds = ['buttons', 'keyboard', 'remove_keyboard', 'force_reply'].filter(k => args[k] != null && args[k] !== false)
  if (kinds.length > 1) throw new Error(`only one of ${kinds.join(', ')} per message`)
  const placeholder = args.placeholder as string | undefined
  if (args.buttons != null) return inlineKeyboard(args.buttons)
  if (args.keyboard != null) {
    const rows = args.keyboard as unknown[]
    const norm = rows.length > 0 && !Array.isArray(rows[0]) ? [rows] : rows
    return {
      keyboard: (norm as ButtonSpec[][]).map(r => r.map(b => typeof b === 'string' ? { text: b } : { text: b.text, ...(b.style ? { style: b.style } : {}) })),
      resize_keyboard: true,
      one_time_keyboard: args.keyboard_once !== false,
      ...(placeholder ? { input_field_placeholder: placeholder } : {}),
    }
  }
  if (args.remove_keyboard) return { remove_keyboard: true }
  if (args.force_reply) return { force_reply: true, ...(placeholder ? { input_field_placeholder: placeholder } : {}) }
  return undefined
}

export function linkPreviewOptions(v: unknown): Record<string, unknown> | undefined {
  switch (v) {
    case undefined: case null: case 'on': return undefined
    case 'off': return { is_disabled: true }
    case 'small': return { prefer_small_media: true }
    case 'large': return { prefer_large_media: true }
    case 'above': return { show_above_text: true }
    default: throw new Error(`unknown link_preview: ${v}`)
  }
}

// Message effects (private chats only). Raw numeric ids are passed through.
export const EFFECTS: Record<string, string> = {
  fire: '5104841245755180586',
  like: '5107584321108051014',
  dislike: '5104858069142078462',
  heart: '5159385139981059251',
  party: '5046509860389126442',
  poop: '5046589136895476101',
}

export function effectId(v: unknown): string | undefined {
  if (v == null || v === '') return undefined
  const s = String(v)
  if (EFFECTS[s]) return EFFECTS[s]
  if (/^\d+$/.test(s)) return s
  throw new Error(`unknown effect: ${s} (use ${Object.keys(EFFECTS).join(', ')} or a numeric id)`)
}

export function isPrivateChatId(chat_id: string): boolean {
  return !chat_id.startsWith('-')
}

export function isBadRequest(err: unknown): boolean {
  return err instanceof GrammyError && err.error_code === 400
}

export function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** Send one text/rich message. A formatted message that Telegram rejects
 *  (bad MarkdownV2 escaping, malformed HTML, unsupported rich markdown) is
 *  re-sent as plain text so the content still arrives. When that plain text
 *  needs several parts, reply_parameters goes on the first part only, unless
 *  quoteAll (access.json replyToMode "all"). plain: the plain-text version to
 *  fall back to (text + details as an expandable quote entity), when it fits
 *  one message. tg: the Bot API call; remember: told about every message sent. */
export async function sendFormatted(
  tg: TgCall,
  remember: (chat_id: string, message_id: number, text: string) => void,
  chat_id: string,
  text: string,
  f: Format,
  opts: Record<string, unknown>,
  notes: string[],
  quoteAll = false,
  plain?: Plain,
): Promise<number[]> {
  const { link_preview_options, ...richOpts } = opts
  try {
    const sent = f === 'rich'
      ? await tg('sendRichMessage', { chat_id, ...textBody(text, f), ...richOpts })
      : await tg('sendMessage', { chat_id, ...textBody(text, f), ...opts })
    remember(chat_id, sent.message_id, text)
    return [sent.message_id]
  } catch (err) {
    if (f === 'text' || !isBadRequest(err)) throw err
    notes.push(`${f} rejected (${errText(err)}); sent as plain text`)
    if (plain && plain.text.length <= MAX_CHUNK_LIMIT) {
      const sent = await tg('sendMessage', { chat_id, text: plain.text, entities: plain.entities, ...opts })
      remember(chat_id, sent.message_id, plain.text)
      return [sent.message_id]
    }
    const ids: number[] = []
    const parts = chunk(plain?.text ?? text, MAX_CHUNK_LIMIT, 'newline')
    for (let i = 0; i < parts.length; i++) {
      const last = i === parts.length - 1
      const { reply_markup, message_effect_id, reply_parameters, ...rest } = opts
      const sent = await tg('sendMessage', {
        chat_id, text: parts[i], ...rest,
        ...((i === 0 || quoteAll) && reply_parameters ? { reply_parameters } : {}),
        ...(last && reply_markup ? { reply_markup } : {}),
        ...(i === 0 && message_effect_id ? { message_effect_id } : {}),
      })
      remember(chat_id, sent.message_id, parts[i]!)
      ids.push(sent.message_id)
    }
    return ids
  }
}

/** Splits details that don't fit one message (paragraph/line/space cuts). */
export function foldSplit(s: string, limit: number): string[] {
  return chunk(s, limit, 'newline')
}

/** edit_message text with details folded in: one message only, so text +
 *  details must fit Telegram's limit (an edit can't add messages). */
export function editWithFold(text: string, fold: Fold | undefined, f: Format): Piece {
  if (!fold) return { text }
  const limit = f === 'rich' ? RICH_MAX_CHARS : MAX_CHUNK_LIMIT
  const pieces = withFold(text === '' ? [] : [text], fold, f, limit, foldSplit)
  if (pieces.length !== 1) {
    throw new Error(`text + details don't fit one message (${limit} chars for ${f}): shorten the details, or send a new reply (reply splits them into complete blocks)`)
  }
  return pieces[0]!
}

// Telegram's reaction whitelist (ReactionTypeEmoji, Bot API 10.3). Note the
// bare ❤ (no U+FE0F); input is normalized by stripping variation selectors.
export const REACTION_EMOJI = [
  '❤', '👍', '👎', '🔥', '🥰', '👏', '😁', '🤔', '🤯', '😱', '🤬', '😢', '🎉', '🤩', '🤮', '💩', '🙏', '👌',
  '🕊', '🤡', '🥱', '🥴', '😍', '🐳', '❤‍🔥', '🌚', '🌭', '💯', '🤣', '⚡', '🍌', '🏆', '💔', '🤨', '😐',
  '🍓', '🍾', '💋', '🖕', '😈', '😴', '😭', '🤓', '👻', '👨‍💻', '👀', '🎃', '🙈', '😇', '😨', '🤝', '✍',
  '🤗', '🫡', '🎅', '🎄', '☃', '💅', '🤪', '🗿', '🆒', '💘', '🙉', '🦄', '😘', '💊', '🙊', '😎', '👾',
  '🤷‍♂', '🤷', '🤷‍♀', '😡',
]
export const REACTION_SET = new Set(REACTION_EMOJI)
export const normalizeReaction = (e: string) => e.replace(/️/g, '').trim()
