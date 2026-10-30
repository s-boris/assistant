// Access gate, lifted from telegram-plus server.ts (commit 974bed5): the inbound gate (DM
// allowlist; groups with their own allowFrom and mention-triggering), the gate for non-message
// events (button presses, reactions, poll answers: same allowlists, no mention needed) and the
// outbound gate (only chats the inbound gate would deliver from).
// Dropped: pairing. The adapter's access is static: it never writes the access file, and dmPolicy
// "pairing" counts as "allowlist" (as the plugin's TELEGRAM_ACCESS_MODE=static did); unknown
// senders are dropped without a reply.
import { readFileSync } from 'fs'
import type { Context } from 'grammy'
import type { Config } from './config.ts'

export type GroupPolicy = { requireMention: boolean; allowFrom: string[] }

export type Access = {
  dmPolicy: 'allowlist' | 'disabled'
  allowFrom: string[]
  groups: Record<string, GroupPolicy>
  mentionPatterns?: string[]
  /** true (default): added/changed reactions become rows; 'all': also pure removals; false: off. */
  forwardReactions?: boolean | 'all'
  /** access.json ackReaction: the adapter reacts with it to each inbound message (send on only). */
  ackReaction?: string
}

const DISABLED: Access = { dmPolicy: 'disabled', allowFrom: [], groups: {} }

/** The access file as the plugin reads it (missing or corrupt: nobody gets in). */
export function readAccessFile(file: string): Access {
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8')) as Record<string, any>
    return {
      dmPolicy: parsed.dmPolicy === 'disabled' ? 'disabled' : 'allowlist',
      allowFrom: Array.isArray(parsed.allowFrom) ? parsed.allowFrom.map(String) : [],
      groups: parsed.groups && typeof parsed.groups === 'object' ? parsed.groups : {},
      mentionPatterns: parsed.mentionPatterns,
      forwardReactions: parsed.forwardReactions,
      ...(typeof parsed.ackReaction === 'string' && parsed.ackReaction ? { ackReaction: parsed.ackReaction } : {}),
    }
  } catch {
    return DISABLED
  }
}

/** The access in force now: the config's inline list, else the file (read live, like the plugin). */
export function loadAccess(cfg: Config): Access {
  if (cfg.allowFrom) return { dmPolicy: 'allowlist', allowFrom: cfg.allowFrom, groups: {} }
  return readAccessFile(cfg.allowFromFile!)
}

export function gateMessage(ctx: Context, access: Access, botUsername: string): boolean {
  if (access.dmPolicy === 'disabled') return false
  const from = ctx.from
  if (!from) return false
  const senderId = String(from.id)
  const chatType = ctx.chat?.type
  if (chatType === 'private') return access.allowFrom.includes(senderId)
  if (chatType === 'group' || chatType === 'supergroup') {
    const policy = access.groups[String(ctx.chat!.id)]
    if (!policy) return false
    const groupAllowFrom = policy.allowFrom ?? []
    const requireMention = policy.requireMention ?? true
    if (groupAllowFrom.length > 0 && !groupAllowFrom.includes(senderId)) return false
    if (requireMention && !isMentioned(ctx, botUsername, access.mentionPatterns)) return false
    return true
  }
  return false
}

/** Gate for non-message events (button presses, reactions, poll answers): same allowlists as
 *  gateMessage(), no mention requirement (the user is interacting with the bot's own message). */
export function senderAllowed(access: Access, chatType: string | undefined, chatId: string, senderId: string): boolean {
  if (access.dmPolicy === 'disabled') return false
  if (chatType === 'private') return access.allowFrom.includes(senderId)
  if (chatType === 'group' || chatType === 'supergroup') {
    const policy = access.groups[chatId]
    if (!policy) return false
    const groupAllowFrom = policy.allowFrom ?? []
    return groupAllowFrom.length === 0 || groupAllowFrom.includes(senderId)
  }
  return false
}

/** Outbound gate: Telegram DM chat_id == user_id, so allowFrom covers DMs. */
export function chatAllowed(access: Access, chat_id: string): boolean {
  return access.allowFrom.includes(chat_id) || chat_id in access.groups
}

export function isMentioned(ctx: Context, botUsername: string, extraPatterns?: string[]): boolean {
  const entities = ctx.message?.entities ?? ctx.message?.caption_entities ?? []
  const text = ctx.message?.text ?? ctx.message?.caption ?? ''
  for (const e of entities) {
    if (e.type === 'mention') {
      const mentioned = text.slice(e.offset, e.offset + e.length)
      if (mentioned.toLowerCase() === `@${botUsername}`.toLowerCase()) return true
    }
    if (e.type === 'text_mention' && e.user?.is_bot && e.user.username === botUsername) {
      return true
    }
  }
  if (ctx.message?.reply_to_message?.from?.username === botUsername) return true
  for (const pat of extraPatterns ?? []) {
    try {
      if (new RegExp(pat, 'i').test(text)) return true
    } catch {}
  }
  return false
}
