-- Public profile fields; every column is nullable so existing accounts keep
-- working and a display name still falls back to the email local part.

ALTER TABLE users ADD COLUMN nickname TEXT;
ALTER TABLE users ADD COLUMN first_name TEXT;
ALTER TABLE users ADD COLUMN last_name TEXT;
ALTER TABLE users ADD COLUMN avatar_url TEXT;
