#!/usr/bin/env bun
/**
 * telegram-plus: the main session's Telegram channel, as a bridge (bridge.ts).
 *
 * tg-adapter polls the bot and sends; this MCP server moves events-store rows in (channel notifications) and
 * tool calls out (outbox rows). It reads no bot token, never polls and opens no network connection except the
 * voice inbox on 127.0.0.1. TELEGRAM_MODE is not read: unset or any value runs the bridge.
 *
 * Entry point of package.json "start" (.mcp.json) and the tests; all code is in bridge.ts and its modules.
 */
import './bridge.ts'
