"""Small framework-free helpers shared by models, routers and scripts."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from core.constants import DELETED_AUTHOR_NAME, DETAIL_INVALID_EMAIL_REASON

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


def profile_display_name(
    nickname: str | None,
    first_name: str | None,
    last_name: str | None,
    email: str,
) -> str:
    """Name shown for a live account: nickname, then full name, then email."""
    if nickname:
        return nickname
    full_name = " ".join(part for part in (first_name, last_name) if part)
    return full_name or display_name(email)


def author_display_name(row: dict) -> str:
    """Resolve the author columns a posts/comments join adds, popping them.

    Soft-deleted accounts are masked, so the resolver must run before the row is
    handed to a response model.
    """
    email = row.pop("author_email", "") or ""
    deleted = row.pop("author_deleted_at", None)
    nickname = row.pop("author_nickname", None)
    first_name = row.pop("author_first_name", None)
    last_name = row.pop("author_last_name", None)
    if deleted:
        return DELETED_AUTHOR_NAME
    return profile_display_name(nickname, first_name, last_name, email)


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_seconds() -> int:
    return int(utc_now().timestamp())


def sqlite_timestamp(moment: datetime | None = None) -> str:
    """Format a moment the same way D1's ``datetime('now')`` does."""
    moment = moment or utc_now()
    return moment.strftime(SQL_TIMESTAMP_FORMAT)


def parse_sqlite_timestamp(value: str) -> int | None:
    """Parse a ``datetime('now')`` style string into epoch seconds."""
    try:
        moment = datetime.strptime(value, SQL_TIMESTAMP_FORMAT)
    except (TypeError, ValueError):
        return None
    return int(moment.replace(tzinfo=UTC).timestamp())
