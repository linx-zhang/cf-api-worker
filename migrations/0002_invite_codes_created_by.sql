-- Audit trail for invite codes: who created each code.
-- Nullable so codes created before this migration stay valid.

ALTER TABLE invite_codes ADD COLUMN created_by INTEGER REFERENCES users (id);
