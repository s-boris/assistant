-- Hub store: CI jobs and their signed records. Forward-only, additive.
-- One row per job the brain submits (a git bundle of one commit); the hub signs the runner's result (record + sig,
-- HMAC-SHA256 with the hub's own key) and keeps it here. Only hubd writes this table.
CREATE TABLE IF NOT EXISTS ci_jobs(id TEXT PRIMARY KEY, commit_sha TEXT NOT NULL, tree TEXT NOT NULL, base TEXT,
  branch TEXT, bundle_sha256 TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', ok BOOLEAN,
  created DOUBLE PRECISION NOT NULL, finished DOUBLE PRECISION, secs DOUBLE PRECISION, record JSONB, sig TEXT);
CREATE INDEX IF NOT EXISTS ci_jobs_tree ON ci_jobs(tree, finished);
CREATE INDEX IF NOT EXISTS ci_jobs_commit ON ci_jobs(commit_sha, finished);
