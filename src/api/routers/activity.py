"""Client activity ingestion (page views, heartbeats, page leaves).

Two entry points share one sink:

- ``POST /activity`` — signed-in users, one batch per ``RATE_LIMIT_SECONDS``
  throttled by the newest row of that user.
- ``POST /activity/visitor`` — anonymous visitors. Same table, ``user_id``
  NULL, throttled by ip and restricted to public content paths.

Both stamp the network context (ip / country / region / city) at write time.
Geography is read from Cloudflare, never reverse-geocoded later: the answer
that matters is "where was this request seen from", and asking again tomorrow
would only produce a different answer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from api.deps import get_db, require_permission
from api.models import ActivityBatchIn, AnonymousActivityBatchIn
from api.responses import ApiError, success
from core.constants import (
    CODE_INVALID_ACTIVITY_PATH,
    CODE_INVALID_CLIENT_ADDRESS,
    CODE_RATE_LIMITED,
    DETAIL_ACTIVITY_ADDRESS_MISSING,
    DETAIL_ACTIVITY_PATH_REJECTED,
    DETAIL_ACTIVITY_RATE_LIMITED,
    DETAIL_ACTIVITY_RECORDED,
    DETAIL_ACTIVITY_VISITOR_LIMITED,
)
from core.permissions import ACTIVITY_WRITE_SELF
from infra.db import Database
from services.visitor_paths import is_public_path

router = APIRouter()

# D1 caps bound parameters per statement, so rows are inserted in chunks.
_INSERT_CHUNK = 10  # 9 bound values per row
_INSERT_COLUMNS = (
    "user_id, type, path, ts, duration_ms, ip, country, region, city, created_at"
)

# Minimum gap between two accepted batches of the same user. The check reads the
# newest ``activity_events`` row for that user, so it is shared by every isolate
# and colo instead of living in per-process memory.
RATE_LIMIT_SECONDS = 10

# Anonymous traffic is unauthenticated and therefore spam-able; a longer window
# and a smaller batch ceiling are the cheapest defence that still records a
# visit faithfully (page_view + page_leave land ~30s or more apart).
VISITOR_RATE_LIMIT_SECONDS = 30


# Cloudflare reports these placeholders when it cannot locate the request.
_UNKNOWN_COUNTRIES = frozenset({"", "XX", "T1"})


def _client_ip(request: Request) -> str | None:
    """Best-effort client ip, most trustworthy header first."""
    for header in ("cf-connecting-ip", "x-real-ip"):
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    return None


def _network_context(request: Request) -> dict[str, str | None]:
    """ip + geo as seen by Cloudflare for this request.

    ``x-cf-*`` headers are injected by ``entry.py`` from ``request.cf``
    (city/region are not available as plain headers). ``cf-ipcountry`` is
    Cloudflare's own header and stays as the fallback for country.
    """
    country = (
        (
            request.headers.get("x-cf-country")
            or request.headers.get("cf-ipcountry")
            or ""
        )
        .strip()
        .upper()
    )
    return {
        "ip": _client_ip(request),
        "country": None if country in _UNKNOWN_COUNTRIES else country[:2],
        "region": (request.headers.get("x-cf-region") or "").strip() or None,
        "city": (request.headers.get("x-cf-city") or "").strip() or None,
    }


async def _seconds_since_last_batch(
    db: Database, column: str, value: object
) -> int | None:
    """Age of the newest batch for ``column = value``, or ``None`` if none."""
    row = await db.fetch_one(
        "SELECT strftime('%s', 'now') - strftime('%s', MAX(created_at)) AS age "
        f"FROM activity_events WHERE {column} = ?",
        (value,),
    )
    age = row["age"] if row else None
    return None if age is None else max(0, int(age))


async def _insert_events(db: Database, rows: list[tuple]) -> None:
    for start in range(0, len(rows), _INSERT_CHUNK):
        chunk = rows[start : start + _INSERT_CHUNK]
        values = ", ".join(
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))" for _ in chunk
        )
        params = [value for row in chunk for value in row]
        await db.execute(
            f"INSERT INTO activity_events ({_INSERT_COLUMNS}) VALUES {values}",
            params,
        )


def _event_rows(payload, user_id: int | None, context: dict) -> list[tuple]:
    return [
        (
            user_id,
            event.type,
            event.path,
            event.ts,
            event.duration_ms,
            context["ip"],
            context["country"],
            context["region"],
            context["city"],
        )
        for event in payload.events
    ]


@router.post("")
async def record_activity(
    payload: ActivityBatchIn,
    request: Request,
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
        age = await _seconds_since_last_batch(db, "user_id", user["id"])
        if age is not None and age < RATE_LIMIT_SECONDS:
            raise ApiError(
                CODE_RATE_LIMITED,
                DETAIL_ACTIVITY_RATE_LIMITED,
                status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(RATE_LIMIT_SECONDS - age)},
            )

    rows = _event_rows(payload, user["id"], _network_context(request))
    await _insert_events(db, rows)

    # Cache "last active" on the user row: the daily retention purge deletes
    # activity_events rows, and MAX() keeps this value monotonic when batches race.
    await db.execute(
        "UPDATE users SET last_active_at = "
        "MAX(COALESCE(last_active_at, datetime('now')), datetime('now')) "
        "WHERE id = ?",
        (user["id"],),
    )
    return success({"accepted": len(rows)}, detail=DETAIL_ACTIVITY_RECORDED)


@router.post("/visitor")
async def record_visitor_activity(
    payload: AnonymousActivityBatchIn,
    request: Request,
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Store 1-20 events for a visitor without an account.

    No authentication on purpose: this is what produces the only real
    "audience" data the site has. Everything that limits abuse happens here —
    public-path allow list, ip throttle, no heartbeats — and nothing is written
    for a request that fails any of them.
    """
    for event in payload.events:
        if not is_public_path(event.path):
            raise ApiError(
                CODE_INVALID_ACTIVITY_PATH,
                DETAIL_ACTIVITY_PATH_REJECTED,
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    context = _network_context(request)
    ip = context["ip"]
    if ip is None:
        raise ApiError(
            CODE_INVALID_CLIENT_ADDRESS,
            DETAIL_ACTIVITY_ADDRESS_MISSING,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    has_page_leave = any(event.type == "page_leave" for event in payload.events)
    if not has_page_leave:
        age = await _seconds_since_last_batch(db, "ip", ip)
        if age is not None and age < VISITOR_RATE_LIMIT_SECONDS:
            raise ApiError(
                CODE_RATE_LIMITED,
                DETAIL_ACTIVITY_VISITOR_LIMITED,
                status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(VISITOR_RATE_LIMIT_SECONDS - age)},
            )

    rows = _event_rows(payload, None, context)
    await _insert_events(db, rows)
    return success({"accepted": len(rows)}, detail=DETAIL_ACTIVITY_RECORDED)
