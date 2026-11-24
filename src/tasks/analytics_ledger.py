"""Cron job: fold new activity events into the analytics ledger.

The request path folds a tail in memory and never writes; this job is the only
place the ledger is persisted. It runs hourly, which bounds how large a tail can
grow between two folds. The daily retention pass does **not** fold the ledger:
the next hourly run is only 45 minutes away, and a request read folds the tail
in memory anyway.
"""

from __future__ import annotations

from infra.db import Database
from services.analytics_ledger import refresh_ledger


async def persist_analytics_ledger(db: Database) -> dict:
    """Fold the tail, store the ledger, and report what happened."""
    return await refresh_ledger(db)
