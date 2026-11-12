-- Cover the (created_at, id) ordering used by every list endpoint.
-- SQLite can only use a composite index for "ORDER BY created_at DESC, id DESC"
-- when both keys are in the index, so the single-column indexes from 0001
-- cannot satisfy the tie-break and leave a sort step behind.

-- posts: list ordering (created_at DESC, id DESC), neighbour ranges, newer-count
CREATE INDEX IF NOT EXISTS idx_posts_created_id ON posts (created_at DESC, id DESC);
DROP INDEX IF EXISTS idx_posts_created_at;

-- users: admin user list ordering
CREATE INDEX IF NOT EXISTS idx_users_created_id ON users (created_at DESC, id DESC);

-- invite_codes: admin list ordering; code stays the primary key for lookups
CREATE INDEX IF NOT EXISTS idx_invite_codes_created_code ON invite_codes (created_at DESC, code ASC);

-- comments: per-post thread ordering with the id tie-break
CREATE INDEX IF NOT EXISTS idx_comments_post_created_id ON comments (post_id, created_at, id);
DROP INDEX IF EXISTS idx_comments_post_created;

-- activity_events: admin activity list ordering
CREATE INDEX IF NOT EXISTS idx_activity_events_created_id ON activity_events (created_at DESC, id DESC);
DROP INDEX IF EXISTS idx_activity_events_created;
