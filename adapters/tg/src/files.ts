// Files out and in (Phase 1 P2): the reply tool's files (photos, documents, albums; voice .ogg via sendVoice
// with a sendAudio fallback, as tg-voice does), downloads into the instance inbox (download op, eager photo
// downloads, audio to transcribe), and the file guard.
// sendFile / sendAlbum are lifted from telegram-plus server.ts (commit 974bed5, lines 2502-2560) with photo.ts
// verbatim. The guard is wider than 0.1.22's (which refused only its own state dir): the plugin's state dir
// except its inbox, the adapter's config and state dirs except the instance inbox, ~/.secrets, ~/.claude, the
// token and access files are never sent, by path and by real path.
import { InputFile } from 'grammy'
import { mkdirSync, realpathSync, statSync, writeFileSync } from 'fs'
import { basename, extname, isAbsolute, join } from 'path'
import { errText, isBadRequest, type TgCall } from './format.ts'
import { isInside, realHome } from './paths.ts'
import { photoCheck, withDocumentFallback } from './photo.ts'

export const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
export const MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024 // the Bot API's download limit
export const PHOTO_EXTS = new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp'])
export const VIDEO_EXTS = new Set(['.mp4', '.mov', '.m4v', '.webm'])
export const VOICE_EXTS = new Set(['.ogg', '.oga', '.opus'])

export type Guard = { deny: string[]; allow: string[]; files: string[] }

/** The places a file to send may never come from. `allow` dirs (inboxes) win over `deny`. */
export function fileGuard(o: { home: string; configDir: string; stateRoot: string; inboxDir: string; tokenFile: string; allowFromFile?: string;
  configFile: string }): Guard {
  const homes = [...new Set([o.home, realHome()].filter(Boolean))]
  const deny = [o.configDir, o.stateRoot, ...homes.flatMap(h => [join(h, '.secrets'), join(h, '.claude')])]
  const allow = [o.inboxDir, ...homes.map(h => join(h, '.claude', 'channels', 'telegram', 'inbox'))]
  return { deny, allow, files: [o.tokenFile, o.configFile, ...(o.allowFromFile ? [o.allowFromFile] : [])] }
}

const real = (p: string) => { try { return realpathSync(p) } catch { return p } }

/** Throws unless `f` may be sent: absolute, a regular file ≤ 50 MB, outside the guarded places. */
export function assertSendable(f: string, g: Guard): number {
  if (typeof f !== 'string' || !isAbsolute(f)) throw new Error(`files must be absolute paths: ${f}`)
  let r: string
  try { r = realpathSync(f) } catch { throw new Error(`file not found: ${f}`) }
  for (const p of [f, r]) {
    if (g.files.some(x => x === p || real(x) === p)) throw new Error(`refusing to send a config or secret file: ${f}`)
    const ok = g.allow.some(d => isInside(p, d) || isInside(p, real(d)))
    if (!ok && g.deny.some(d => isInside(p, d) || isInside(p, real(d)))) throw new Error(`refusing to send state, config or secret files: ${f}`)
  }
  const st = statSync(r)
  if (!st.isFile()) throw new Error(`not a file: ${f}`)
  if (st.size > MAX_ATTACHMENT_BYTES) throw new Error(`file too large: ${f} (${(st.size / 1024 / 1024).toFixed(1)}MB, max 50MB)`)
  return st.size
}

type Remember = (chat_id: string, message_id: number, text: string) => void

/** One file as its own message: images as photos (photo.ts decides when a document reads better), .ogg/.oga/.opus
 *  as a voice message (sendAudio when Telegram refuses a voice message), the rest as documents. */
export async function sendFile(tg: TgCall, remember: Remember, chat_id: string, f: string, opts: Record<string, unknown>, notes: string[] = []): Promise<number> {
  const ext = extname(f).toLowerCase()
  if (VOICE_EXTS.has(ext)) {
    let sent: any
    try {
      sent = await tg('sendVoice', { chat_id, voice: new InputFile(f), ...opts })
    } catch (err) {
      if (!isBadRequest(err)) throw err
      notes.push(`${basename(f)} sent as audio: Telegram refused a voice message (${errText(err)})`)
      sent = await tg('sendAudio', { chat_id, audio: new InputFile(f), ...opts })
    }
    remember(chat_id, sent.message_id, `[voice: ${basename(f)}]`)
    return sent.message_id
  }
  const isImage = PHOTO_EXTS.has(ext)
  const problem = isImage ? photoCheck(f) : null
  if (problem) notes.push(`${basename(f)} sent as a file, not a photo (${problem})`)
  const { result: sent, asPhoto, rejection } = await withDocumentFallback(isImage && !problem, photo => photo
    ? tg('sendPhoto', { chat_id, photo: new InputFile(f), ...opts })
    : tg('sendDocument', { chat_id, document: new InputFile(f), ...opts }))
  if (rejection) notes.push(`${basename(f)} sent as a file: Telegram refused it as a photo (${rejection})`)
  remember(chat_id, (sent as any).message_id, `[${asPhoto ? 'photo' : 'file'}: ${basename(f)}]`)
  return (sent as any).message_id
}

/** 2-10 files per album (more: several albums); photos/videos as media, else all as documents. */
export async function sendAlbum(tg: TgCall, remember: Remember, chat_id: string, files: string[], opts: Record<string, unknown>, notes: string[] = []): Promise<number[]> {
  let visual = files.every(f => PHOTO_EXTS.has(extname(f).toLowerCase()) || VIDEO_EXTS.has(extname(f).toLowerCase()))
  if (visual) {
    for (const f of files) {
      const problem = PHOTO_EXTS.has(extname(f).toLowerCase()) ? photoCheck(f) : null
      if (problem) { visual = false; notes.push(`album sent as files (${basename(f)}: ${problem})`); break }
    }
  }
  const ids: number[] = []
  for (let i = 0; i < files.length; i += 10) {
    const group = files.slice(i, i + 10)
    if (group.length === 1) { ids.push(await sendFile(tg, remember, chat_id, group[0]!, opts, notes)); continue }
    const send = (vis: boolean) => tg('sendMediaGroup', {
      chat_id,
      media: group.map(f => {
        const ext = extname(f).toLowerCase()
        return { type: !vis ? 'document' : VIDEO_EXTS.has(ext) ? 'video' : 'photo', media: new InputFile(f) }
      }),
      ...opts,
    })
    const { result: sent, rejection } = visual && group.some(f => PHOTO_EXTS.has(extname(f).toLowerCase()))
      ? await withDocumentFallback(true, send)
      : { result: await send(visual), rejection: undefined }
    if (rejection) notes.push(`album part sent as files: Telegram refused a photo (${rejection})`)
    ;(sent as any[]).forEach((m, k) => { remember(chat_id, m.message_id, `[album item: ${basename(group[k]!)}]`); ids.push(m.message_id) })
  }
  return ids
}

export type FetchFile = (url: string, timeoutMs: number) => Promise<Response>

/** getFile + download into `dir` (0700; file 0600). Returns the local path. Lifted from downloadToInbox. */
export async function downloadFile(tg: TgCall, fetchFile: FetchFile, fileUrl: (file_path: string) => string, file_id: string, dir: string,
  prefix = '', timeoutMs = 60_000): Promise<string> {
  const file = await tg('getFile', { file_id })
  if (!file?.file_path) throw new Error('Telegram returned no file_path — file may have expired')
  if (file.file_size != null && file.file_size > MAX_DOWNLOAD_BYTES) throw new Error('file is over the 20 MB Bot API download limit')
  const res = await fetchFile(fileUrl(file.file_path), timeoutMs)
  if (!res.ok) throw new Error(`download failed: HTTP ${res.status}`)
  const buf = Buffer.from(await res.arrayBuffer())
  const rawExt = String(file.file_path).includes('.') ? String(file.file_path).split('.').pop()! : 'bin'
  const ext = rawExt.replace(/[^a-zA-Z0-9]/g, '') || 'bin'
  const uniqueId = String(file.file_unique_id ?? '').replace(/[^a-zA-Z0-9_-]/g, '') || 'dl'
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  const path = join(dir, `${prefix}${Date.now()}-${uniqueId}.${ext}`)
  writeFileSync(path, buf, { mode: 0o600 })
  return path
}
