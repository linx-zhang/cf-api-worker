"""Client activity ingestion (page views, heartbeats, page leaves)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.deps import get_db, require_permission
from api.models import ActivityBatchIn
from api.responses import success
from core.constants import DETAIL_ACTIVITY_RECORDED
from core.permissions import ACTIVITY_WRITE_SELF
from infra.db import Database

router = APIRouter()

# D1 caps bound parameters per statement, so rows are inserted in chunks.
_INSERT_CHUNK = 20  # 5 bound values per row


@router.post("")
async def record_activity(
    payload: ActivityBatchIn,
    user: dict = Depends(require_permission(ACTIVITY_WRITE_SELF)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Store 1-50 client events for the current user."""
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
    return success({"accepted": len(rows)}, detail=DETAIL_ACTIVITY_RECORDED)
