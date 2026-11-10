-- Raw client activity events used for presence/heartbeat analytics.
-- ts is the client clock in epoch milliseconds; created_at is the server clock.
CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id),
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    ts INTEGER NOT NULL,
    duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_events_user_created
    ON activity_events (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_activity_events_created
    ON activity_events (created_at);
