-- approvals.db (v2 Phase 1 P7): one-time approvals the owner gives with a tap on a ✅/❌ card. 0600, WAL.
-- Live: ~/.local/state/assistant/approvals.db; tg-adapter's test instance keeps its own beside its state.
-- Writers: tg-adapter (cards it sends, his taps, the expiry edit) and the approvals library/CLI (new, consume, cancel,
-- poll-mode decisions read from the telegram-plus inbound journal). Never by hand: vault-guard rule approvals-state.
-- The action itself is never stored: only its sha256 and a summary of at most 80 characters.
CREATE TABLE IF NOT EXISTS approvals(nonce TEXT PRIMARY KEY, action_sha TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  ref TEXT, chat TEXT, message_id INTEGER, created REAL NOT NULL, expires REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied|expired|cancelled
  decided_ts REAL, decided_by TEXT, decided_via TEXT,  -- his tap: when, his user id, adapter|journal
  used_ts REAL, used_by TEXT,  -- consume: once, before expires
  refused INTEGER NOT NULL DEFAULT 0,  -- consume attempts refused (wrong action, not approved)
  card TEXT);  -- the card's expiry edit: edited|failed
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status, expires);
