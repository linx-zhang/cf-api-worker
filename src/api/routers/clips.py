"""Shareable clipboard snippets: one text blob behind an unguessable code.

Storage is the D1 cache rather than a table, and that is the whole design:

- a shared log is disposable by definition — one day, then gone;
- ``cache_entries`` already has an expiry column and a cron purge, so nothing
  new has to be cleaned up;
- there is no list endpoint by design: a code is the only handle, rows cannot
  be enumerated, and expired rows are simply misses.

The key layout is fixed by the cache namespace (``clip:``) plus a validated
uuid4 hex code. Two guards keep a forged code from reaching another key:

1. the code must match ``^[0-9a-f]{32}$`` *and* carry the uuid4 version/variant
   nibbles, so a phrase like ``posts:list:v1`` can never be addressed here;
2. the namespace prefix is added by :class:`Cache`, never by the caller, so the
   final key is always ``clip:<32 hex>``.
"""

from __future__ import annotations

import re
from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from api.deps import (
    client_ip,
    ensure_resource_permission,
    get_current_user,
    get_db,
)
from api.models import ClipIn, ClipOut
from api.responses import ApiError, success
from core.constants import (
    CODE_NOT_FOUND,
    CODE_RATE_LIMITED,
    DETAIL_CLIP_CREATED,
    DETAIL_CLIP_NOT_FOUND,
    DETAIL_CLIP_RATE_LIMITED,
    DETAIL_CLIP_READ_RATE_LIMITED,
    DETAIL_CLIP_REVOKED,
)
from core.permissions import CLIP_DELETE_ANY, CLIP_DELETE_OWN
from core.utils import sqlite_timestamp, utc_now
from infra.cache import Cache
from infra.db import Database

router = APIRouter()

# A shared log is a day-trip: long enough for someone in another timezone to
# open it tomorrow morning, short enough that nothing lingers.
CLIP_TTL_SECONDS = 24 * 60 * 60

# One snippet per user per 10 seconds; the same shape as the activity throttle,
# but kept in the cache because a clip leaves no row to read a timestamp from.
CLIP_THROTTLE_SECONDS = 10

# Reads are public, so they need their own budget: one per-ip bucket and one
# per-code bucket, counted in fixed one-minute windows. The per-code limit is
# the one that always applies — a code that leaks into a busy group chat may
# see several readers at once, and a scraper hammering a single link should not
# be able to turn it into free bandwidth.
CLIP_READ_WINDOW_SECONDS = 60
CLIP_READ_IP_LIMIT = 60
CLIP_READ_CODE_LIMIT = 120

# uuid4 hex: 32 chars, version nibble at index 12, variant nibble at index 16.
_CODE_RE = re.compile(r"^[0-9a-f]{32}$")
_VARIANT_NIBBLES = frozenset("89ab")


def _cache(db: Database) -> Cache:
    return Cache(db, namespace="clip")


def _checked_code(raw: str) -> str:
    """Normalize and validate a public code, or answer 404 like any other miss.

    A malformed code is not a 422: from the reader's point of view there is no
    difference between "never existed" and "not a code at all", and spelling out
    the difference would only help someone probe the key space.
    """
    code = (raw or "").strip().lower()
    if not _CODE_RE.match(code) or code[12] != "4" or code[16] not in _VARIANT_NIBBLES:
        raise ApiError(CODE_NOT_FOUND, DETAIL_CLIP_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return code


def _clip_out(code: str, value: dict) -> dict:
    return ClipOut(
        code=code,
        title=value.get("title", ""),
        body=value.get("body", ""),
        created_at=value.get("created_at", ""),
        expires_at=value.get("expires_at", ""),
    ).model_dump()


def _read_window() -> tuple[str, int]:
    """Current fixed window id plus the seconds left in it."""
    now = int(utc_now().timestamp())
    stamp = now // CLIP_READ_WINDOW_SECONDS
    remaining = CLIP_READ_WINDOW_SECONDS - now % CLIP_READ_WINDOW_SECONDS
    return str(stamp), remaining + 1


async def _take(cache: Cache, key: str, limit: int, ttl: int) -> bool:
    """Count one hit against a fixed window; ``False`` means over budget.

    Read-modify-write, not atomic: two simultaneous requests may both read the
    old count. That is acceptable here — the purpose is to stop a loop, not to
    bill anyone to the request.
    """
    used = await cache.get(key, 0) or 0
    if used >= limit:
        return False
    await cache.set(key, used + 1, timeout=ttl)
    return True


def _read_limited(retry_after: int) -> ApiError:
    return ApiError(
        CODE_RATE_LIMITED,
        DETAIL_CLIP_READ_RATE_LIMITED,
        status.HTTP_429_TOO_MANY_REQUESTS,
        headers={"Retry-After": str(retry_after)},
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_clip(
    payload: ClipIn,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Store a snippet for 24 hours and hand back the code to share."""
    cache = _cache(db)
    if not await cache.add(f"throttle:{user['id']}", 1, timeout=CLIP_THROTTLE_SECONDS):
        # ``add`` only fails while the previous key is alive; once it expires the
        # same call takes the key over, so the throttle never needs cleanup.
        raise ApiError(
            CODE_RATE_LIMITED,
            DETAIL_CLIP_RATE_LIMITED,
            status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(CLIP_THROTTLE_SECONDS)},
        )

    code = uuid4().hex
    created = utc_now()
    expires = created + timedelta(seconds=CLIP_TTL_SECONDS)
    value = {
        "title": payload.title,
        "body": payload.body,
        "owner_id": user["id"],
        "created_at": sqlite_timestamp(created),
        "expires_at": sqlite_timestamp(expires),
    }
    await cache.set(code, value, CLIP_TTL_SECONDS)
    return success(
        _clip_out(code, value),
        status_code=status.HTTP_201_CREATED,
        detail=DETAIL_CLIP_CREATED,
    )


@router.get("/{code}")
async def read_clip(
    code: str,
    request: Request,
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Public read: anyone with the code can open the snippet until it expires."""
    checked = _checked_code(code)
    cache = _cache(db)
    window, ttl = _read_window()

    ip = client_ip(request)
    if ip is not None and not await _take(
        cache, f"read:ip:{ip}:{window}", CLIP_READ_IP_LIMIT, ttl
    ):
        raise _read_limited(ttl)
    if not await _take(
        cache, f"read:code:{checked}:{window}", CLIP_READ_CODE_LIMIT, ttl
    ):
        raise _read_limited(ttl)

    value = await cache.get(checked)
    if value is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_CLIP_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return success(_clip_out(checked, value))


@router.delete("/{code}")
async def revoke_clip(
    code: str,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Drop a snippet early. The owner may always revoke their own code."""
    checked = _checked_code(code)
    cache = _cache(db)
    value = await cache.get(checked)
    if value is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_CLIP_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    ensure_resource_permission(
        user, value.get("owner_id"), CLIP_DELETE_OWN, CLIP_DELETE_ANY
    )
    await cache.delete(checked)
    return success({"code": checked}, detail=DETAIL_CLIP_REVOKED)
