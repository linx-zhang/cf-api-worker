-- Server receive time of the newest stored activity event, cached on the user row.
-- The daily activity_events retention purge drops rows older than 30 days, which
-- would otherwise turn "last active" into NULL for dormant accounts. Backfilled
-- from activity_events, which still holds the full history at migration time.
-- NULL means the account never reported activity.

ALTER TABLE users ADD COLUMN last_active_at TEXT;

UPDATE users
SET last_active_at = (
    SELECT MAX(e.created_at) FROM activity_events e WHERE e.user_id = users.id
);
