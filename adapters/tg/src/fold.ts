// Fold-out replies: extra text ("details") that Telegram shows collapsed
// behind a tap, appended to a reply or an edit.
//
//   rich        <details><summary>Title</summary>\n…\n</details>   (rich message
//               details block: always-shown summary, Markdown body, closed by default)
//   html        <blockquote expandable><b>Title</b>\n…</blockquote>
//   markdownv2  **>*Title*\n>…\n>…||   (expandable block quotation)
//   text        plain text + an expandable_blockquote entity (no escaping at all)
//
// The details body is written in the message's own format (Markdown for rich,
// HTML for html, escaped MarkdownV2 for markdownv2, plain for text); only the
// title is escaped here. A block never straddles two messages: it joins the
// last text chunk when both fit, else it is its own message, and details too
// long for one message become several complete blocks.

export type FoldFormat = 'text' | 'markdownv2' | 'html' | 'rich'
export type Fold = { body: string; title?: string }
export type Entity = { type: string; offset: number; length: number }
export type Plain = { text: string; entities: Entity[] }
/** One message to send: the formatted text, entities (format "text" only) and
 *  the plain-text version used when Telegram rejects the formatting. */
export type Piece = { text: string; entities?: Entity[]; plain?: Plain }

export const RICH_DEFAULT_TITLE = 'Details'
const TITLE_MAX = 120

export function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** MarkdownV2 outside code/links: every special character gets a backslash. */
export function escapeMarkdownV2(s: string): string {
  return s.replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, m => '\\' + m)
}

/** The fold from tool args (details, details_title); undefined when there is
 *  no non-blank details text. */
export function foldFromArgs(details: unknown, title: unknown): Fold | undefined {
  if (details == null) return undefined
  if (typeof details !== 'string') throw new Error('details must be a string')
  const body = details.replace(/\r\n?/g, '\n').replace(/^(\s*\n)+/, '').replace(/\s+$/, '')
  if (body === '') return undefined
  if (title != null && typeof title !== 'string') throw new Error('details_title must be a string')
  const t = (title ?? '').replace(/\s+/g, ' ').trim().slice(0, TITLE_MAX)
  return { body, ...(t ? { title: t } : {}) }
}

/** Separator between the message text and the block. */
export function foldSeparator(f: FoldFormat): string {
  return f === 'rich' ? '\n\n' : '\n'
}

/** One complete block for a details part. */
export function foldBlock(body: string, f: FoldFormat, title?: string): { text: string; entities?: Entity[] } {
  switch (f) {
    case 'rich': {
      // Markdown is parsed inside <details>; a literal closing tag in the body would end it early.
      const safe = body.replace(/<\/details/gi, '&lt;/details')
      return { text: `<details><summary>${escapeHtml(title ?? RICH_DEFAULT_TITLE)}</summary>\n${safe}\n</details>` }
    }
    case 'html':
      return { text: `<blockquote expandable>${title ? `<b>${escapeHtml(title)}</b>\n` : ''}${body}</blockquote>` }
    case 'markdownv2': {
      // "**" (an empty bold entity) also separates it from a quote that ends the text.
      const lines = [...(title ? [`*${escapeMarkdownV2(title)}*`] : []), ...body.split('\n')]
      return { text: lines.map((l, i) => (i === 0 ? '**>' : '>') + l).join('\n') + '||' }
    }
    case 'text': {
      const text = title ? `${title}\n${body}` : body
      return {
        text,
        entities: [
          { type: 'expandable_blockquote', offset: 0, length: text.length },
          ...(title ? [{ type: 'bold', offset: 0, length: title.length }] : []),
        ],
      }
    }
  }
}

function partTitle(f: FoldFormat, title: string | undefined, i: number, n: number): string | undefined {
  const t = title ?? (f === 'rich' ? RICH_DEFAULT_TITLE : undefined)
  return t && n > 1 ? `${t} (${i + 1}/${n})` : t
}

type Block = { text: string; entities?: Entity[]; raw: string; title?: string }

/** The details as complete blocks, each at most `limit` chars (raw = the
 *  details part inside it, title = its title). `split` cuts a string into
 *  parts of at most the given length (the server's chunker). */
export function foldBlocks(
  fold: Fold, f: FoldFormat, limit: number, split: (s: string, limit: number) => string[],
): Block[] {
  const longTitle = partTitle(f, fold.title, 98, 99)
  let budget = limit - foldBlock('', f, longTitle).text.length
  for (;;) {
    const parts = split(fold.body, Math.max(1, budget))
    const blocks = parts.map((raw, i) => {
      const title = partTitle(f, fold.title, i, parts.length)
      return { ...foldBlock(raw, f, title), raw, ...(title ? { title } : {}) }
    })
    const over = Math.max(...blocks.map(b => b.text.length)) - limit
    if (over <= 0 || budget <= 1) return blocks
    // markdownv2 adds a ">" per line: shrink by the overflow and try again.
    budget = Math.max(1, budget - Math.max(over, 16))
  }
}

function shift(es: Entity[] | undefined, by: number): Entity[] {
  return (es ?? []).map(e => ({ ...e, offset: e.offset + by }))
}

/** Plain-text version of text + details: the raw text, then the details as an
 *  expandable quote entity. Used when Telegram rejects the formatting. */
export function plainWithFold(text: string, fold: Fold): Plain {
  const b = foldBlock(fold.body, 'text', fold.title)
  const head = text.replace(/\s+$/, '')
  const pre = head === '' ? '' : head + '\n'
  return { text: pre + b.text, entities: shift(b.entities, pre.length) }
}

/** The messages for a reply: the text chunks with the details folded in. The
 *  first block joins the last chunk when both fit within `limit`; the rest
 *  (or all, when it doesn't fit) follow as their own messages. */
export function withFold(
  chunks: string[], fold: Fold | undefined, f: FoldFormat, limit: number,
  split: (s: string, limit: number) => string[],
): Piece[] {
  const pieces: Piece[] = chunks.map(text => ({ text }))
  if (!fold) return pieces
  const blocks = foldBlocks(fold, f, limit, split)
  const piece = (pre: string, b: Block): Piece => {
    const sep = pre === '' ? '' : foldSeparator(f)
    return {
      text: pre + sep + b.text,
      ...(b.entities ? { entities: shift(b.entities, pre.length + sep.length) } : {}),
      ...(f !== 'text' ? { plain: plainWithFold(pre, { body: b.raw, ...(b.title ? { title: b.title } : {}) }) } : {}),
    }
  }
  let start = 0
  const head = pieces.at(-1)?.text.replace(/\s+$/, '') ?? ''
  if (head !== '' && head.length + foldSeparator(f).length + blocks[0]!.text.length <= limit) {
    pieces[pieces.length - 1] = piece(head, blocks[0]!)
    start = 1
  }
  for (let i = start; i < blocks.length; i++) pieces.push(piece('', blocks[i]!))
  return pieces
}
