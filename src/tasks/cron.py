"""Cron entry point: the one place ``entry.py`` calls on a cron trigger.

Wrangler fires a single trigger today (``15 18 * * *`` in ``wrangler.jsonc``),
so every invocation runs the same job set. Adding a second trigger means
matching on the cron expression here rather than in ``entry.py``.
"""

from __future__ import annotations

from infra.db import Database
from tasks.activity_retention import ACTIVITY_RETENTION_DAYS, purge_old_activity
from tasks.cache_cleanup import purge_expired_cache


async def run_cron(env, cron: str = "cron") -> dict[str, int]:
    """Run all cron jobs; returns the row count each job removed.

    ``env`` is the entrypoint's own environment: the ``env`` argument of the
    runtime's ``scheduled()`` handler is not guaranteed to be populated on
    every invocation.

    Everything is printed, not just returned: the scheduled handler's return
    value is discarded by the runtime, and Worker logs are the only place a run
    leaves a trace.
    """
    db = Database(env.DB)

    deleted = await purge_old_activity(db, days=ACTIVITY_RETENTION_DAYS)
    print(
        f"activity retention ({cron}): "
        f"removed {deleted} rows older than {ACTIVITY_RETENTION_DAYS} days"
    )

    purged = await purge_expired_cache(db)
    print(f"cache purge ({cron}): removed {purged} expired entries")

    return {"activity_events": deleted, "cache_entries": purged}
