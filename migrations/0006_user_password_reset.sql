-- Track the last administrator-initiated password reset. A token whose "iat"
-- predates this value is rejected, so a reset also ends existing sessions.
-- NULL means the account's password was only ever set at registration.

ALTER TABLE users ADD COLUMN password_changed_at TEXT;
