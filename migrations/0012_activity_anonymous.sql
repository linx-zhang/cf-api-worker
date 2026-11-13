-- Anonymous visitors, and the network context of every event.
--
-- Two changes that cannot be done with ALTER TABLE on SQLite:
--   1. user_id must become nullable, so a visitor without an account can be
--      recorded. Dropping NOT NULL requires a table rebuild.
--   2. ip / country / region / city are added in the same rebuild instead of
--      four separate ALTERs, because the table is recreated anyway.
--
-- Geo values are captured at ingestion time from Cloudflare (cf-ipcountry /
-- request.cf) and stored as-is; nothing is reverse-geocoded later.
-- Old rows keep NULL geo: the data never existed, and inventing it would be
-- worse than a gap.
CREATE TABLE activity_events_v12 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NULL = anonymous visitor; the event is still attributable by ip.
    user_id INTEGER REFERENCES users (id),
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    ts INTEGER NOT NULL,
    duration_ms INTEGER,
    ip TEXT,
    country TEXT,
    region TEXT,
    city TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO activity_events_v12
    (id, user_id, type, path, ts, duration_ms, ip, country, region, city, created_at)
SELECT id, user_id, type, path, ts, duration_ms, NULL, NULL, NULL, NULL, created_at
FROM activity_events;

DROP TABLE activity_events;

ALTER TABLE activity_events_v12 RENAME TO activity_events;

-- Same two indexes as before, plus one for the ip-based throttle.
CREATE INDEX idx_activity_events_created_id
    ON activity_events (created_at, id);
CREATE INDEX idx_activity_events_user_created
    ON activity_events (user_id, created_at);
CREATE INDEX idx_activity_events_ip_created
    ON activity_events (ip, created_at);
