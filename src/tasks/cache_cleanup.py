"""Cron job: drop ``cache_entries`` rows whose ``expires_at`` already passed.

The cache reads filter expired rows out themselves, so this job only exists to
stop dead weight from accumulating. Deleting in bounded batches keeps D1's
statement budget and the scheduled run's CPU time in the same place as
``activity_retention``.
"""

from __future__ import annotations

from infra.db import Database

# Rows per DELETE: same reasoning as the activity sweep.
CACHE_PURGE_BATCH = 5000

# Hard stop so one invocation cannot spin forever on a huge expired backlog.
MAX_CACHE_PURGE_BATCHES = 20


async def purge_expired_cache(db: Database) -> int:
    """Delete expired ``cache_entries`` rows; return the number removed."""
    purged_total = 0
    for _ in range(MAX_CACHE_PURGE_BATCHES):
        purged = await db.execute(
            "DELETE FROM cache_entries WHERE key IN ("
            "SELECT key FROM cache_entries "
            "WHERE expires_at <= datetime('now') LIMIT ?"
            ")",
            (CACHE_PURGE_BATCH,),
        )
        purged_total += purged
        if purged < CACHE_PURGE_BATCH:
            break
    return purged_total
