"""Cron job: drop ``activity_events`` rows older than a fixed window.

The table is append-only from the API's point of view; this module is the only
place that deletes from it. Rows go out in bounded batches, because one huge
DELETE would hit D1's statement budget and burn the scheduled run's CPU time.
"""

from __future__ import annotations

from infra.db import Database

# Client-reported ``ts`` is only validated at write time, so the server clock
# (``created_at``) is the trustworthy age. Nothing older than this window is
# needed by the admin activity view.
ACTIVITY_RETENTION_DAYS = 30

# Rows per DELETE: small enough that each statement stays cheap, large enough
# that a normal sweep finishes in one or two round trips.
DELETE_BATCH = 5000

# Hard stop so a runaway sweep cannot spin inside a single scheduled invocation.
MAX_BATCHES = 20


async def purge_old_activity(db: Database, days: int = ACTIVITY_RETENTION_DAYS) -> int:
    """Delete ``activity_events`` rows older than ``days``; return rows removed."""
    modifier = f"-{int(days)} days"
    deleted_total = 0
    for _ in range(MAX_BATCHES):
        deleted = await db.execute(
            "DELETE FROM activity_events WHERE id IN ("
            "SELECT id FROM activity_events "
            "WHERE created_at < datetime('now', ?) LIMIT ?"
            ")",
            (modifier, DELETE_BATCH),
        )
        deleted_total += deleted
        if deleted < DELETE_BATCH:
            break
    return deleted_total
