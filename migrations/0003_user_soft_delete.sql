-- Soft-deleted users: the row (and its email reservation) stays, access is cut.
ALTER TABLE users ADD COLUMN deleted_at TEXT;
