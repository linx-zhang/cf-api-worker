-- Generalise the "password reset stamp" into a token invalidation stamp:
-- an admin password reset and a user logout both bump it, and any token issued
-- before it stops working.
ALTER TABLE users ADD COLUMN token_invalidated_at TEXT;
UPDATE users SET token_invalidated_at = password_changed_at;
ALTER TABLE users DROP COLUMN password_changed_at;
