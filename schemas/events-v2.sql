-- DDL v2 (v2 Phase 1 plan §3): forward-only and additive on top of events.sql (schema 1).
-- core.db applies it once per store (meta schema -> 2) and adds the outbox columns only where missing
-- (the telegram-plus 0.2.0 bridge adds the same deliveries table and outbox columns when missing).
-- Kept apart from events.sql, the Phase 0 contract the tg-adapter's own schema.sql is compared with.
CREATE TABLE IF NOT EXISTS deliveries(event_id INTEGER PRIMARY KEY, sent_ts REAL,
  tries INTEGER NOT NULL DEFAULT 0, receipt_ts REAL, how TEXT);  -- how: channel|transcript|binding
CREATE TABLE IF NOT EXISTS bindings(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
  tool TEXT NOT NULL, args TEXT NOT NULL DEFAULT '[]', note INTEGER NOT NULL DEFAULT 0,
  on_error TEXT NOT NULL DEFAULT 'reply', why TEXT NOT NULL, created REAL NOT NULL, expires REAL,
  active INTEGER NOT NULL DEFAULT 1, UNIQUE(kind, key));  -- kind: command|button|prefix|card
ALTER TABLE outbox ADD COLUMN reply_to_event INTEGER;
ALTER TABLE outbox ADD COLUMN files TEXT;
ALTER TABLE outbox ADD COLUMN result TEXT;  -- the adapter's JSON outcome of a row (bridge 0.2.0)
