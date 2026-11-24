"""Cron job: remove resume share links the moment they stop working.

A share code is a temporary credential, not content. Once it is revoked, past
``expires_at``, or points at a soft-deleted resume, every public read already
answers 404. Keeping the row only preserves a dead token, so there is no grace
period here: if it is invalid, it goes on the next daily run.

This does **not** touch ``resumes`` itself. Soft-deleted resumes stay in the
database on purpose; only their share links are disposable.
"""

from __future__ import annotations

from infra.db import Database

DELETE_BATCH = 5000
MAX_BATCHES = 20


async def purge_invalid_resume_shares(db: Database) -> int:
    """Delete revoked/expired shares and shares of deleted resumes."""
    deleted_total = 0
    for _ in range(MAX_BATCHES):
        deleted = await db.execute(
            "DELETE FROM resume_shares WHERE code IN ("
            "SELECT s.code FROM resume_shares s "
            "WHERE s.revoked_at IS NOT NULL "
            "OR s.expires_at <= datetime('now') "
            "OR s.resume_id IN ("
            "SELECT r.id FROM resumes r WHERE r.deleted_at IS NOT NULL"
            ") LIMIT ?"
            ")",
            (DELETE_BATCH,),
        )
        deleted_total += deleted
        if deleted < DELETE_BATCH:
            break
    return deleted_total


__all__ = ["purge_invalid_resume_shares"]
