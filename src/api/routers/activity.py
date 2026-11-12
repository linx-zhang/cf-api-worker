"""Client activity ingestion (page views, heartbeats, page leaves)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from api.deps import get_db, require_permission
from api.models import ActivityBatchIn
from api.responses import ApiError, success
from core.constants import (
    CODE_RATE_LIMITED,
    DETAIL_ACTIVITY_RATE_LIMITED,
    DETAIL_ACTIVITY_RECORDED,
)
from core.permissions import ACTIVITY_WRITE_SELF
from infra.db import Database

router = APIRouter()

# D1 caps bound parameters per statement, so rows are inserted in chunks.
_INSERT_CHUNK = 20  # 5 bound values per row

# Minimum gap between two accepted batches of the same user. The check reads the
# newest ``activity_events`` row for that user, so it is shared by every isolate
# and colo instead of living in per-process memory.
RATE_LIMIT_SECONDS = 10


async def _seconds_since_last_batch(db: Database, user_id: int) -> int | None:
    """Age of the user's most recent batch, or ``None`` if none was ever stored."""
    row = await db.fetch_one(
        "SELECT strftime('%s', 'now') - strftime('%s', MAX(created_at)) AS age "
        "FROM activity_events WHERE user_id = ?",
        (user_id,),
    )
    age = row["age"] if row else None
    return None if age is None else max(0, int(age))


@router.post("")
async def record_activity(
    payload: ActivityBatchIn,
    user: dict = Depends(require_permission(ACTIVITY_WRITE_SELF)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Store 1-50 client events for the current user.

    Batches are throttled to one per ``RATE_LIMIT_SECONDS`` per user; extra
    heartbeats are dropped with 429 instead of multiplying writes. A batch that
    contains ``page_leave`` is always stored, because it carries the only
    ``duration_ms`` value and losing it would skew time-on-page.
    """
    has_page_leave = any(event.type == "page_leave" for event in payload.events)
    if not has_page_leave:
        age = await _seconds_since_last_batch(db, user["id"])
        if age is not None and age < RATE_LIMIT_SECONDS:
            raise ApiError(
                CODE_RATE_LIMITED,
                DETAIL_ACTIVITY_RATE_LIMITED,
                status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(RATE_LIMIT_SECONDS - age)},
            )

    rows = [
        (user["id"], event.type, event.path, event.ts, event.duration_ms)
        for event in payload.events
    ]
    for start in range(0, len(rows), _INSERT_CHUNK):
        chunk = rows[start : start + _INSERT_CHUNK]
        values = ", ".join("(?, ?, ?, ?, ?, datetime('now'))" for _ in chunk)
        params = [value for row in chunk for value in row]
        await db.execute(
            "INSERT INTO activity_events "
            "(user_id, type, path, ts, duration_ms, created_at) "
            f"VALUES {values}",
            params,
        )

    # Cache "last active" on the user row: the daily retention purge deletes
    # activity_events rows, and MAX() keeps this value monotonic when batches race.
    await db.execute(
        "UPDATE users SET last_active_at = "
        "MAX(COALESCE(last_active_at, datetime('now')), datetime('now')) "
        "WHERE id = ?",
        (user["id"],),
    )
    return success({"accepted": len(rows)}, detail=DETAIL_ACTIVITY_RECORDED)
