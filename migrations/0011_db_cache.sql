-- A D1-backed key/value cache, shaped after django.core.cache.backends.db.
-- One row per key. The payload is a pickle blob base64-encoded into TEXT, so the
-- column stays portable and the row can be read by any SQL client.
-- expires_at is a UTC 'YYYY-MM-DD HH:MM:SS' string on purpose: it compares
-- lexicographically, so "expires_at <= datetime('now')" works without an index
-- on a computed value. '9999-12-31 00:00:00' means "never expires".

CREATE TABLE IF NOT EXISTS cache_entries (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The cron purge scans by expiry, not by key.
CREATE INDEX IF NOT EXISTS idx_cache_entries_expires_at ON cache_entries (expires_at);
