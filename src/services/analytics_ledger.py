"""Ledger orchestration: where the SQL statements meet the merge functions.

Three responsibilities, all on top of ``services.analytics`` (SQL) and
``services.analytics_merge`` (pure folding):

* **serve** a summary without touching the event table twice — read the ledger
  row, fold the tail (the rows that arrived since the ledger cursor) in memory,
  render. A warm read writes nothing, so concurrent readers never race and a
  refresh burst costs one cache row plus a small tail; only a cold read (no
  ledger yet) stores the freshly rebuilt one.
* **persist** the ledger from the hourly cron: fold once, store once.
* **rebuild** when the ledger is missing, corrupt, or the tail grew past
  ``ANALYTICS_MAX_TAIL_ROWS``. A rebuild is the reference implementation
  (``build_summary``) reading the full 30-day window; the consistency test
  asserts the two paths agree.

The ledger lives in the existing ``cache_entries`` table under a key that is
written with ``timeout=None`` (``never expires``). That is a deliberate
trade-off: no migration is needed, and the ledger is derived data that may be
dropped at any time — the rebuild path above is what makes dropping safe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from infra.cache import Cache
from infra.db import Database
from services.analytics import (
    ANALYTICS_CACHE_SECONDS,
    ANALYTICS_MAX_ACTORS,
    ANALYTICS_MAX_PATH_ROWS,
    ANALYTICS_MAX_PATHS_PER_ACTOR,
    ANALYTICS_MAX_TAIL_ROWS,
    ANALYTICS_TZ_OFFSET_HOURS,
    ANALYTICS_WINDOW_DAYS,
    actor_rows_sql,
    day_rows_sql,
    max_activity_id_sql,
    new_users_today_sql,
    path_rows_sql,
    tail_count_sql,
)
from services.analytics_merge import empty_ledger, merge_tail, render_summary

# Never-expiring ledger. Keep the summary cache on its own key: anything that
# clears the cache must not be able to erase the ledger silently, and anything
# that reads the ledger must not be able to mutate the 60s summary.
LEDGER_KEY = "analytics:ledger:v1"
SUMMARY_KEY_PREFIX = "analytics:summary:v1"

_TAIL_EMPTY = {"day_rows": [], "actor_rows": [], "path_rows": []}


def beijing_today(now: datetime | None = None) -> str:
    """The site's "today" as ``YYYY-MM-DD``: UTC now shifted to Beijing."""
    moment = now or datetime.now(UTC)
    return (moment + timedelta(hours=ANALYTICS_TZ_OFFSET_HOURS)).date().isoformat()


async def _rows_in_range(db: Database, after_id: int, max_id: int) -> dict:
    """Aggregate the events with ``after_id < id <= max_id``."""
    params = (after_id, max_id)
    return {
        "day_rows": await db.fetch_all(day_rows_sql(ANALYTICS_WINDOW_DAYS, tail=True), params),
        "actor_rows": await db.fetch_all(actor_rows_sql(ANALYTICS_WINDOW_DAYS, tail=True), params),
        "path_rows": await db.fetch_all(path_rows_sql(ANALYTICS_WINDOW_DAYS, tail=True), params),
    }


async def _max_id(db: Database) -> int:
    row = await db.fetch_one(max_activity_id_sql())
    return int((row or {}).get("max_id") or 0)


async def _rebuild(db: Database, today: str) -> dict:
    """Fold the whole window into an empty ledger, bounded by MAX(id)."""
    max_id = await _max_id(db)
    rows = await _rows_in_range(db, 0, max_id) if max_id else dict(_TAIL_EMPTY)
    return merge_tail(
        empty_ledger(),
        {"max_id": max_id, **rows},
        today=today,
        window_days=ANALYTICS_WINDOW_DAYS,
    )


async def _advance(db: Database, ledger: dict, today: str) -> tuple[dict, int]:
    """Fold rows after the cursor; rebuild instead of folding a huge tail.

    Returns the new ledger and how many rows were folded (0 for a rebuild or an
    already-caught-up ledger). The id ceiling is read *before* the aggregates so
    a row inserted mid-query is left for the next fold rather than skipped.
    """
    cursor = int(ledger.get("cursor") or 0)
    max_id = await _max_id(db)
    if max_id <= cursor:
        return ledger, 0

    count_row = await db.fetch_one(tail_count_sql(), (cursor, max_id))
    pending = int((count_row or {}).get("rows_total") or 0)
    if pending > ANALYTICS_MAX_TAIL_ROWS:
        rebuilt = await _rebuild(db, today)
        return rebuilt, pending

    rows = await _rows_in_range(db, cursor, max_id)
    folded = merge_tail(
        ledger,
        {"max_id": max_id, **rows},
        today=today,
        window_days=ANALYTICS_WINDOW_DAYS,
    )
    return folded, pending


def _today_block(ledger: dict, today: str, new_users: int) -> dict:
    """Today's numbers, all derivable from the ledger plus one small query."""
    bucket = (ledger.get("daily") or {}).get(today) or {"page_views": 0, "actors": []}
    actors = bucket.get("actors") or []
    return {
        "new_users": new_users,
        "active_users": sum(1 for key in actors if key.startswith("u:")),
        "anonymous_visitors": sum(1 for key in actors if key.startswith("ip:")),
        "page_views": int(bucket.get("page_views") or 0),
    }


async def _summary_from_ledger(
    db: Database, cache: Cache, days: int, today: str
) -> dict:
    ledger = await cache.get(LEDGER_KEY, None)
    if not ledger:
        ledger = await _rebuild(db, today)
        await cache.set(LEDGER_KEY, ledger, timeout=None)
    else:
        ledger, _ = await _advance(db, ledger, today)

    rendered = render_summary(
        ledger,
        today=today,
        window_days=days,
        max_actors=ANALYTICS_MAX_ACTORS,
        max_paths_per_actor=ANALYTICS_MAX_PATHS_PER_ACTOR,
        max_path_rows=ANALYTICS_MAX_PATH_ROWS,
    )
    new_users_row = await db.fetch_one(new_users_today_sql())
    return {
        "days": days,
        "timezone_offset_hours": ANALYTICS_TZ_OFFSET_HOURS,
        "today": _today_block(ledger, today, int((new_users_row or {}).get("new_users") or 0)),
        "series": rendered["series"],
        "actors": rendered["actors"],
        "has_next": rendered["has_next"],
    }


async def load_summary(
    db: Database,
    days: int,
    *,
    today: str | None = None,
    cache: Cache | None = None,
) -> dict:
    """Serve the admin summary from the ledger, cached for a minute."""
    store = cache or Cache(db)
    day = today or beijing_today()
    return await store.get_or_set(
        f"{SUMMARY_KEY_PREFIX}:{days}",
        lambda: _summary_from_ledger(db, store, days, day),
        ANALYTICS_CACHE_SECONDS,
    )


async def refresh_ledger(
    db: Database,
    *,
    today: str | None = None,
    cache: Cache | None = None,
) -> dict:
    """Fold the tail into the ledger and persist it (the cron job's body)."""
    store = cache or Cache(db)
    day = today or beijing_today()
    ledger = await store.get(LEDGER_KEY, None)
    if not ledger:
        ledger = await _rebuild(db, day)
        folded = 0
        rebuilt = True
    else:
        ledger, folded = await _advance(db, ledger, day)
        rebuilt = folded > ANALYTICS_MAX_TAIL_ROWS
    await store.set(LEDGER_KEY, ledger, timeout=None)
    return {
        "cursor": int(ledger.get("cursor") or 0),
        "folded": folded,
        "rebuilt": rebuilt,
        "days": len(ledger.get("daily") or {}),
        "actors": len(ledger.get("actors") or {}),
    }


__all__ = [
    "LEDGER_KEY",
    "SUMMARY_KEY_PREFIX",
    "beijing_today",
    "load_summary",
    "refresh_ledger",
]
