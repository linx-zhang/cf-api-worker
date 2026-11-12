"""Cron entry point: the one place ``entry.py`` calls on a cron trigger.

Wrangler fires a single trigger today (``15 18 * * *`` in ``wrangler.jsonc``),
so every invocation runs the same job set. Adding a second trigger means
matching on the cron expression here rather than in ``entry.py``.
"""

from __future__ import annotations

from infra.db import Database
from tasks.activity_retention import ACTIVITY_RETENTION_DAYS, purge_old_activity


async def run_cron(env, cron: str = "cron") -> int:
    """Run all cron jobs; returns the number of ``activity_events`` rows removed.

    ``env`` is the entrypoint's own environment: the ``env`` argument of the
    runtime's ``scheduled()`` handler is not guaranteed to be populated on
    every invocation.
    """
    deleted = await purge_old_activity(Database(env.DB), days=ACTIVITY_RETENTION_DAYS)
    print(
        f"activity retention ({cron}): "
        f"removed {deleted} rows older than {ACTIVITY_RETENTION_DAYS} days"
    )
    return deleted
