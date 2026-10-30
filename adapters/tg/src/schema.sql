-- Events DB DDL (v2 Phase 0 plan §3, the shared contract). tg-adapter test traffic uses its own
-- DB (~/.local/state/tg-adapter/test.db) with this same DDL; test/schema.test.ts checks it against
-- the repo's schemas/events.sql once that exists.
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '', next TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '{}',
  labels TEXT NOT NULL DEFAULT '[]', delivery TEXT NOT NULL DEFAULT 'beat', trace_id TEXT, idem_key TEXT UNIQUE,
  note_path TEXT, status TEXT NOT NULL DEFAULT 'new',  -- new|listed|acked|dismissed|gone
  updated_ts REAL, first_beat INTEGER, first_listed_ts REAL, acked_ts REAL, ack_by TEXT, ack_note TEXT);
CREATE INDEX IF NOT EXISTS events_status ON events(status, ts);
CREATE TABLE IF NOT EXISTS beats(id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT, ok INTEGER, ms INTEGER, chars INTEGER, listed TEXT);
CREATE TABLE IF NOT EXISTS wake_rules(id INTEGER PRIMARY KEY, created REAL NOT NULL, kind_glob TEXT, klass TEXT NOT NULL, until REAL, why TEXT, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, ts REAL NOT NULL, bot TEXT NOT NULL, chat TEXT NOT NULL, op TEXT NOT NULL,
  body TEXT NOT NULL, idem_key TEXT UNIQUE, urgent INTEGER NOT NULL DEFAULT 0, not_before REAL, mode TEXT NOT NULL DEFAULT 'live',
  status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, sent_ts REAL, tg_message_id INTEGER, trace_id TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
-- pragmas: journal_mode=WAL, synchronous=FULL, busy_timeout=5000; umask 077
