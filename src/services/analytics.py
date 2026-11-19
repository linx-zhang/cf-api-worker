"""Activity analytics: the SQL behind the admin overview.

Kept out of the router and free of FastAPI/pydantic imports so the statements
can be exercised directly with ``sqlite3`` (see
``tests/test_analytics_ledger.py``). The router only adds permissions and
response models on top.

Two families of statements live here:

* the **ledger** statements — ``day_rows_sql`` / ``actor_rows_sql`` /
  ``path_rows_sql`` aggregate a bounded id range (``tail=True``) or the whole
  window (``tail=False``). ``services.analytics_ledger`` folds their output into
  the stored ledger, so a page read touches one cache row plus the rows that
  arrived since the last fold.
* ``build_summary`` — the original per-request implementation. It is no longer
  on the request path; it stays as the reference the ledger output is compared
  against, because two implementations that must agree are easier to trust when
  the comparison is an assertion rather than a comment.

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

# How much history the ledger keeps, and how large a tail may grow before a
# fold is more expensive than a rebuild. 30 days matches the activity retention
# job: anything older has already been deleted from the source table.
ANALYTICS_WINDOW_DAYS = 30
ANALYTICS_MAX_TAIL_ROWS = 500

_ACTOR_EXPR = "COALESCE('u:' || CAST(user_id AS TEXT), 'ip:' || ip)"
_ACTOR_EXPR_E = "COALESCE('u:' || CAST(e.user_id AS TEXT), 'ip:' || e.ip)"


def _modifier(days_offset_hours: int = ANALYTICS_TZ_OFFSET_HOURS) -> str:
    """SQLite datetime modifier for the display timezone, sign preserved."""
    return f"{days_offset_hours:+d} hours"


def _window_since(days: int) -> str:
    """Lower bound of the window: the first day's 00:00, in UTC.

    The window is counted in **display days** (``days`` buckets ending today),
    not in a rolling ``days * 24h`` span. That matters because the ledger keeps
    per-day buckets: a rolling cutoff sits up to a day earlier or later than the
    first bucket depending on the clock, and the two implementations would then
    disagree on events around the boundary.

    The expression is a constant string, so it still hits the
    ``idx_activity_events_created_id`` prefix (``created_at >= ?``).
    """
    mod = _modifier()
    return (
        f"datetime(date(datetime('now', '{mod}'), '-{max(1, int(days)) - 1} days'), "
        f"'{int(ANALYTICS_TZ_OFFSET_HOURS) * -1:+d} hours')"
    )


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
        f"FROM activity_events WHERE created_at >= {_window_since(days)} "
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
        f"WHERE e.created_at >= {_window_since(days)} "
        "AND (e.user_id IS NOT NULL OR e.ip IS NOT NULL) "
        "GROUP BY ident ORDER BY last_seen DESC, ident ASC LIMIT ?"
    )


def _paths_sql(days: int) -> str:
    return (
        f"SELECT {_ACTOR_EXPR} AS ident, path, "
        "SUM(CASE WHEN type = 'page_view' THEN 1 ELSE 0 END) AS views, "
        "SUM(COALESCE(duration_ms, 0)) AS dwell_ms, "
        "MAX(created_at) AS last_seen "
        f"FROM activity_events WHERE created_at >= {_window_since(days)} "
        "AND (user_id IS NOT NULL OR ip IS NOT NULL) "
        "GROUP BY ident, path ORDER BY dwell_ms DESC, views DESC, path ASC LIMIT ?"
    )


# --- ledger statements ------------------------------------------------------
# The range bound (``id > ? AND id <= ?``) is what keeps a read cheap: the
# upper bound comes from a MAX(id) taken *before* the aggregate queries, so
# rows inserted while the queries run cannot be advanced past by the cursor.


def max_activity_id_sql() -> str:
    """Highest event id, or 0 on an empty table."""
    return "SELECT COALESCE(MAX(id), 0) AS max_id FROM activity_events"


def tail_count_sql() -> str:
    """Rows between the two cursors: decides fold vs rebuild."""
    return "SELECT COUNT(*) AS rows_total FROM activity_events WHERE id > ? AND id <= ?"


def _range_clause(tail: bool, alias: str = "") -> str:
    """Optional ``id`` bounds; ``alias`` prefixes the column for joined queries."""
    if not tail:
        return ""
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}id > ? AND {prefix}id <= ?"


def day_rows_sql(days: int, *, tail: bool) -> str:
    """(day, ident) -> views, dwell and the day's first/last event.

    Feeds both the series bucket (page views and the distinct-actor key list) and
    each actor's per-day cell, which is why dwell and the timestamps come along:
    one scan, two consumers.
    """
    mod = _modifier()
    return (
        "SELECT "
        f"date(datetime(created_at, '{mod}')) AS day, "
        f"{_ACTOR_EXPR} AS ident, "
        "SUM(CASE WHEN type = 'page_view' THEN 1 ELSE 0 END) AS page_views, "
        "SUM(COALESCE(duration_ms, 0)) AS dwell_ms, "
        "MIN(created_at) AS first_seen, MAX(created_at) AS last_seen "
        "FROM activity_events "
        f"WHERE created_at >= {_window_since(days)} "
        "AND (user_id IS NOT NULL OR ip IS NOT NULL)"
        f"{_range_clause(tail)} "
        "GROUP BY day, ident"
    )


def actor_rows_sql(days: int, *, tail: bool) -> str:
    """Per-identity profile fields.

    Counts live in ``day_rows_sql`` (they have to be per-day for the 7/30 split);
    this query only resolves who the identity is and its overall span.
    """
    return (
        f"SELECT {_ACTOR_EXPR_E} AS ident, "
        "e.user_id, MAX(u.email) AS user_email, e.ip, "
        "MAX(e.country) AS country, MAX(e.region) AS region, MAX(e.city) AS city, "
        "MIN(e.created_at) AS first_seen, MAX(e.created_at) AS last_seen "
        "FROM activity_events e LEFT JOIN users u ON u.id = e.user_id "
        f"WHERE e.created_at >= {_window_since(days)} "
        "AND (e.user_id IS NOT NULL OR e.ip IS NOT NULL)"
        f"{_range_clause(tail, 'e')} "
        "GROUP BY ident"
    )


def path_rows_sql(days: int, *, tail: bool) -> str:
    """Per (day, identity, path) totals.

    Grouped by day so the actor's path breakdown can be summed over whichever
    window the page asks for; the day is dropped when the ledger renders.
    """
    mod = _modifier()
    return (
        "SELECT "
        f"date(datetime(created_at, '{mod}')) AS day, "
        f"{_ACTOR_EXPR} AS ident, path, "
        "SUM(CASE WHEN type = 'page_view' THEN 1 ELSE 0 END) AS views, "
        "SUM(COALESCE(duration_ms, 0)) AS dwell_ms "
        "FROM activity_events "
        f"WHERE created_at >= {_window_since(days)} "
        "AND (user_id IS NOT NULL OR ip IS NOT NULL)"
        f"{_range_clause(tail)} "
        "GROUP BY day, ident, path"
    )


def new_users_today_sql() -> str:
    """Accounts created today (Beijing time); users is small, so no window."""
    mod = _modifier()
    return (
        "SELECT COUNT(*) AS new_users FROM users WHERE deleted_at IS NULL AND "
        f"date(datetime(created_at, '{mod}')) = date(datetime('now', '{mod}'))"
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
    """Deprecated entry point kept for callers of the pre-ledger build.

    The request path lives in ``services.analytics_ledger`` now; this still
    answers correctly (it is the reference implementation), it just costs a
    full window scan per call.
    """
    return await Cache(db).get_or_set(
        f"analytics:summary:v1:{days}",
        lambda: build_summary(db, days),
        ANALYTICS_CACHE_SECONDS,
    )

