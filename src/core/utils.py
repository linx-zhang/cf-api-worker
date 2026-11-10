"""Small framework-free helpers shared by models, routers and scripts."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from core.constants import DETAIL_INVALID_EMAIL_REASON

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SQL_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def normalize_email(value: str) -> str:
    """Trim and lower-case an email, rejecting obviously malformed values."""
    value = value.strip().lower()
    if not EMAIL_PATTERN.match(value):
        raise ValueError(DETAIL_INVALID_EMAIL_REASON)
    return value


def display_name(email: str) -> str:
    """Public author name: the local part of the email, never the full address."""
    return email.split("@", 1)[0]


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_seconds() -> int:
    return int(utc_now().timestamp())


def sqlite_timestamp(moment: datetime | None = None) -> str:
    """Format a moment the same way D1's ``datetime('now')`` does."""
    moment = moment or utc_now()
    return moment.strftime(SQL_TIMESTAMP_FORMAT)
