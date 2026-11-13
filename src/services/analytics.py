"""Activity analytics: the queries behind the admin overview.

Kept out of the router and free of FastAPI/pydantic imports so the statements
can be exercised directly with ``sqlite3`` (see
``scripts/agent/2026-11-13-analytics-smoke.py``). The router only adds
permissions and response models on top.

Vocabulary used below:

- **actor** — one identity in the window: a signed-in user (``u:<id>``) or an
  anonymous visitor (``ip:<address>``). Nothing links those two rows; a visitor
  who signs in legitimately appears twice.
- **dwell** — the sum of ``duration_ms`` from ``page_leave`` events. It is
  observed time, not wall-clock time: a tab killed without ``pagehide`` sends
  no leave event and contributes nothing.
"""

from __future__ import annotations

from infra.cache import Cache
from infra.db import Database

# Every stored timestamp is UTC. "Today" for this site means Beijing time, so
# all date bucketing shifts by this many hours first; a day boundary that is
# silently 8 hours off would make every daily number wrong.
ANALYTICS_TZ_OFFSET_HOURS = 8

# The page is read by one person, not a dashboard wall: one minute of staleness
# is invisible, and it collapses a refresh burst into a single set of queries.
ANALYTICS_CACHE_SECONDS = 60

# A summary nobody reads past the first screen is wasted work; these caps keep
# the payload bounded no matter how much traffic arrives.
ANALYTICS_MAX_ACTORS = 60
ANALYTICS_MAX_PATHS_PER_ACTOR = 8
ANALYTICS_MAX_PATH_ROWS = 400

_ACTOR_EXPR = "COALESCE('u:' || CAST(user_id AS TEXT), 'ip:' || ip)"
_ACTOR_EXPR_E = "COALESCE('u:' || CAST(e.user_id AS TEXT), 'ip:' || e.ip)"


def _modifier(days_offset_hours: int = ANALYTICS_TZ_OFFSET_HOURS) -> str:
    """SQLite datetime modifier for the display timezone, sign preserved."""
    return f"{days_offset_hours:+d} hours"


def _today_sql() -> str:
    mod = _modifier()
    day = f"date(datetime(created_at, '{mod}')) = date(datetime('now', '{mod}'))"
    return (
        "SELECT "
        "(SELECT COUNT(*) FROM users WHERE deleted_at IS NULL AND "
        f"date(datetime(created_at, '{mod}')) = date(datetime('now', '{mod}'))"
        ") AS new_users, "
        "(SELECT COUNT(DISTINCT user_id) FROM activity_events "
        f"WHERE user_id IS NOT NULL AND {day}) AS active_users, "
        "(SELECT COUNT(DISTINCT ip) FROM activity_events "
        f"WHERE user_id IS NULL AND ip IS NOT NULL AND {day}) AS anonymous_visitors, "
        "(SELECT COUNT(*) FROM activity_events "
        f"WHERE type = 'page_view' AND {day}) AS page_views"
    )


def _series_sql(days: int) -> str:
    mod = _modifier()
    return (
        "SELECT "
        f"date(datetime(created_at, '{mod}')) AS day, "
        "SUM(CASE WHEN type = 'page_view' THEN 1 ELSE 0 END) AS page_views, "
        f"COUNT(DISTINCT {_ACTOR_EXPR}) AS actors "
        f"FROM activity_events WHERE created_at >= datetime('now', '-{int(days)} days') "
        "AND (user_id IS NOT NULL OR ip IS NOT NULL) "
        "GROUP BY day ORDER BY day"
    )


def _actors_sql(days: int) -> str:
    return (
        f"SELECT {_ACTOR_EXPR_E} AS ident, "
        "e.user_id, MAX(u.email) AS user_email, e.ip, "
        "MAX(e.country) AS country, MAX(e.region) AS region, MAX(e.city) AS city, "
        "SUM(CASE WHEN e.type = 'page_view' THEN 1 ELSE 0 END) AS page_views, "
        "SUM(COALESCE(e.duration_ms, 0)) AS dwell_ms, "
        "MIN(e.created_at) AS first_seen, MAX(e.created_at) AS last_seen "
        "FROM activity_events e LEFT JOIN users u ON u.id = e.user_id "
        f"WHERE e.created_at >= datetime('now', '-{int(days)} days') "
        "AND (e.user_id IS NOT NULL OR e.ip IS NOT NULL) "
        "GROUP BY ident ORDER BY last_seen DESC LIMIT ?"
    )


def _paths_sql(days: int) -> str:
    return (
        f"SELECT {_ACTOR_EXPR} AS ident, path, "
        "SUM(CASE WHEN type = 'page_view' THEN 1 ELSE 0 END) AS views, "
        "SUM(COALESCE(duration_ms, 0)) AS dwell_ms, "
        "MAX(created_at) AS last_seen "
        f"FROM activity_events WHERE created_at >= datetime('now', '-{int(days)} days') "
        "AND (user_id IS NOT NULL OR ip IS NOT NULL) "
        "GROUP BY ident, path ORDER BY dwell_ms DESC, views DESC LIMIT ?"
    )


async def build_summary(db: Database, days: int) -> dict:
    """Query D1 and assemble the summary payload (uncached)."""
    today_row = (await db.fetch_one(_today_sql())) or {}
    series_rows = await db.fetch_all(_series_sql(days))
    actor_rows = await db.fetch_all(_actors_sql(days), (ANALYTICS_MAX_ACTORS + 1,))
    path_rows = await db.fetch_all(_paths_sql(days), (ANALYTICS_MAX_PATH_ROWS,))

    # One row past the cap tells the UI whether the list is truncated.
    actors, has_next = (
        actor_rows[:ANALYTICS_MAX_ACTORS],
        len(actor_rows) > ANALYTICS_MAX_ACTORS,
    )

    paths_by_ident: dict[str, list[dict]] = {}
    for row in path_rows:
        bucket = paths_by_ident.setdefault(row["ident"], [])
        if len(bucket) < ANALYTICS_MAX_PATHS_PER_ACTOR:
            bucket.append(
                {
                    "path": row["path"],
                    "views": int(row["views"] or 0),
                    "dwell_ms": int(row["dwell_ms"] or 0),
                }
            )

    return {
        "days": days,
        "timezone_offset_hours": ANALYTICS_TZ_OFFSET_HOURS,
        "today": {
            "new_users": int(today_row.get("new_users") or 0),
            "active_users": int(today_row.get("active_users") or 0),
            "anonymous_visitors": int(today_row.get("anonymous_visitors") or 0),
            "page_views": int(today_row.get("page_views") or 0),
        },
        "series": [
            {
                "date": row["day"],
                "page_views": int(row["page_views"] or 0),
                "actors": int(row["actors"] or 0),
            }
            for row in series_rows
        ],
        "actors": [
            {
                "key": row["ident"],
                "user_id": row["user_id"],
                "user_email": row["user_email"],
                "ip": row["ip"],
                "country": row["country"],
                "region": row["region"],
                "city": row["city"],
                "page_views": int(row["page_views"] or 0),
                "dwell_ms": int(row["dwell_ms"] or 0),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "paths": paths_by_ident.get(row["ident"], []),
            }
            for row in actors
        ],
        "has_next": has_next,
    }


async def load_summary(db: Database, days: int) -> dict:
    """Cache the summary for ``ANALYTICS_CACHE_SECONDS``, then recompute."""
    return await Cache(db).get_or_set(
        f"analytics:summary:v1:{days}",
        lambda: build_summary(db, days),
        ANALYTICS_CACHE_SECONDS,
    )
