"""Small helpers shared by models, routers and scripts."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import Query

from core.constants import DETAIL_INVALID_EMAIL_REASON

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100

SQL_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def normalize_email(value: str) -> str:
    """Trim and lower-case an email, rejecting obviously malformed values."""
    value = value.strip().lower()
    if not EMAIL_PATTERN.match(value):
        raise ValueError(DETAIL_INVALID_EMAIL_REASON)
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_seconds() -> int:
    return int(utc_now().timestamp())


def sqlite_timestamp(moment: datetime | None = None) -> str:
    """Format a moment the same way D1's ``datetime('now')`` does."""
    moment = moment or utc_now()
    return moment.strftime(SQL_TIMESTAMP_FORMAT)


def pagination(
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
) -> tuple[int, int]:
    """FastAPI dependency validating the shared list pagination parameters."""
    return limit, offset
