"""Pydantic models shared by the routers."""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from core.utils import normalize_email

# Client timestamps older than this, or further in the future than the
# tolerance, are rejected as clock errors.
ACTIVITY_TS_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000
ACTIVITY_TS_FUTURE_TOLERANCE_MS = 5 * 60 * 1000


def _check_activity_ts(value: int) -> int:
    """Client clock sanity check shared by signed-in and anonymous events."""
    now_ms = int(time.time() * 1000)
    if value < now_ms - ACTIVITY_TS_MAX_AGE_MS:
        raise ValueError("ts must be epoch milliseconds within the last 30 days")
    if value > now_ms + ACTIVITY_TS_FUTURE_TOLERANCE_MS:
        raise ValueError(
            "ts must be epoch milliseconds no more than 5 minutes in the future"
        )
    return value


class RegisterIn(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    invite_code: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)


class LoginIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    created_at: str
    nickname: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None


class MeOut(UserOut):
    permissions: list[str] = Field(default_factory=list)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: int
    user: UserOut


class PostIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body_md: str = Field(min_length=1)


class PostUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body_md: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> PostUpdateIn:
        if self.title is None and self.body_md is None:
            raise ValueError("at least one of title or body_md is required")
        return self


class PostOut(BaseModel):
    id: int
    author_id: int
    author_name: str
    title: str
    body_md: str
    created_at: str
    updated_at: str


class PostBatchDeleteIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=50)

    @field_validator("ids")
    @classmethod
    def _distinct(cls, value: list[int]) -> list[int]:
        """Collapse duplicates while keeping the order the client sent."""
        return list(dict.fromkeys(value))


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    parent_id: int | None = None


class CommentOut(BaseModel):
    id: int
    post_id: int
    author_id: int
    author_name: str
    parent_id: int | None = None
    body: str
    created_at: str


class AdminUserOut(UserOut):
    last_active_at: str | None = None


class UserPasswordResetIn(BaseModel):
    """Optional custom password; omitting it makes the server generate one."""

    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserPasswordResetOut(BaseModel):
    """One-time response: the value to hand over, never stored in clear text."""

    id: int
    email: str
    password: str
    generated: bool
    password_reset_at: str


PROFILE_FIELDS = ("nickname", "first_name", "last_name", "avatar_url")

_PROFILE_LIMITS = {
    "nickname": 32,
    "first_name": 64,
    "last_name": 64,
    "avatar_url": 512,
}


def _clean_profile_text(value: str | None, field: str) -> str | None:
    """Trim a profile field; blank input clears it, over-long input is rejected."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    limit = _PROFILE_LIMITS[field]
    if len(text) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return text


class ProfileFieldsIn(BaseModel):
    """Optional profile fields shared by the self-service and admin updates."""

    nickname: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None

    @field_validator("nickname", "first_name", "last_name")
    @classmethod
    def _text(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _clean_profile_text(value, info.field_name)

    @field_validator("avatar_url")
    @classmethod
    def _avatar_url(cls, value: str | None) -> str | None:
        text = _clean_profile_text(value, "avatar_url")
        if text is not None and not text.startswith(("http://", "https://")):
            raise ValueError("avatar_url must start with http:// or https://")
        return text


class ProfileUpdateIn(ProfileFieldsIn):
    """Self-service patch: email and role are rejected as unknown fields."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _at_least_one_field(self) -> ProfileUpdateIn:
        if not self.model_fields_set:
            raise ValueError("at least one profile field is required")
        return self


class UserUpdateIn(ProfileFieldsIn):
    email: str | None = None
    role: Literal["user", "internal", "admin"] | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_email(value)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UserUpdateIn:
        if not self.model_fields_set:
            raise ValueError("at least one of email, role or profile is required")
        return self


class ActivityEventIn(BaseModel):
    type: Literal["page_view", "heartbeat", "page_leave"]
    path: str = Field(min_length=1, max_length=512)
    ts: int
    duration_ms: int | None = Field(default=None, ge=0)

    @field_validator("ts")
    @classmethod
    def _ts_range(cls, value: int) -> int:
        return _check_activity_ts(value)


class ActivityBatchIn(BaseModel):
    events: list[ActivityEventIn] = Field(min_length=1, max_length=50)


class AnonymousActivityEventIn(BaseModel):
    """One event from a visitor without an account.

    ``heartbeat`` is excluded on purpose: a signed-in user is one row stream we
    can afford, but every anonymous visitor sending a heartbeat every 60s would
    dominate the table and bury the page views it is supposed to explain.
    """

    type: Literal["page_view", "page_leave"]
    path: str = Field(min_length=1, max_length=512)
    ts: int
    duration_ms: int | None = Field(default=None, ge=0)

    @field_validator("ts")
    @classmethod
    def _ts_range(cls, value: int) -> int:
        return _check_activity_ts(value)


class AnonymousActivityBatchIn(BaseModel):
    events: list[AnonymousActivityEventIn] = Field(min_length=1, max_length=20)


class ActivityOut(BaseModel):
    id: int
    # NULL user_id is an anonymous visitor; ``ip`` is then the only handle.
    user_id: int | None = None
    user_email: str | None = None
    type: str
    path: str
    ts: int
    duration_ms: int | None = None
    ip: str | None = None
    country: str | None = None
    created_at: str


class AnalyticsTodayOut(BaseModel):
    new_users: int
    active_users: int
    anonymous_visitors: int
    page_views: int


class AnalyticsDayOut(BaseModel):
    date: str
    page_views: int
    actors: int


class AnalyticsPathOut(BaseModel):
    path: str
    views: int
    dwell_ms: int


class AnalyticsActorOut(BaseModel):
    """One identity seen in the window: a signed-in user, or an anonymous IP."""

    key: str
    user_id: int | None = None
    user_email: str | None = None
    ip: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    page_views: int
    dwell_ms: int
    last_seen: str
    first_seen: str
    paths: list[AnalyticsPathOut] = Field(default_factory=list)


class AnalyticsSummaryOut(BaseModel):
    days: int
    timezone_offset_hours: int
    today: AnalyticsTodayOut
    series: list[AnalyticsDayOut]
    actors: list[AnalyticsActorOut]
    # More identities exist than were returned; the UI says so instead of
    # pretending the list is complete.
    has_next: bool = False


class InviteCodeCreateIn(BaseModel):
    role: Literal["internal", "admin"]
    count: int = Field(default=1, ge=1, le=50)
    expires_in_days: int = Field(default=7, ge=1, le=365)
    note: str | None = Field(default=None, max_length=64)

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class InviteCodeOut(BaseModel):
    code: str
    role: str
    expires_at: str | None = None
    created_at: str
    used_at: str | None = None
    created_by: int | None = None
    created_by_email: str | None = None
    note: str | None = None
    used_by: int | None = None
    used_by_email: str | None = None


# --- Personal-space memos ---------------------------------------------------

# 到期日只有"天"，没有时刻和时区：存 YYYY-MM-DD，客户端按本地日期解释。
_MEMO_DUE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MEMO_BODY_MAX = 2000


def _check_due_at(value: str | None) -> str | None:
    """Validate a memo due date; blank clears it, anything else must be a real day."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not _MEMO_DUE_PATTERN.match(text):
        raise ValueError("due_at must be a date in YYYY-MM-DD form")
    try:
        # 正则挡不住 2026-02-30 这种"格式对、日期不存在"的值
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ValueError("due_at must be a real calendar date") from None
    return text


def _check_memo_body(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("body must not be blank")
    return text


class MemoIn(BaseModel):
    body: str = Field(min_length=1, max_length=MEMO_BODY_MAX)
    due_at: str | None = None

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        return _check_memo_body(value)

    @field_validator("due_at")
    @classmethod
    def _due_at(cls, value: str | None) -> str | None:
        return _check_due_at(value)


class MemoUpdateIn(BaseModel):
    """PATCH 允许只改一个字段；``due_at: null`` 表示清空到期日。"""

    body: str | None = Field(default=None, min_length=1, max_length=MEMO_BODY_MAX)
    done: bool | None = None
    due_at: str | None = None

    @field_validator("body")
    @classmethod
    def _body(cls, value: str | None) -> str | None:
        return None if value is None else _check_memo_body(value)

    @field_validator("due_at")
    @classmethod
    def _due_at(cls, value: str | None) -> str | None:
        return _check_due_at(value)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> MemoUpdateIn:
        if not self.model_fields_set:
            raise ValueError("at least one of body, done or due_at is required")
        return self


class MemoOut(BaseModel):
    id: int
    body: str
    done: bool
    done_at: str | None = None
    due_at: str | None = None
    created_at: str
    updated_at: str


class MemoSummaryOut(BaseModel):
    """个人空间的计数：列表本身走 limit/offset，不在分页接口里塞 total。

    ``due`` 只数"未完成且到期日不晚于今天"（含逾期），因为界面上标红的就是这批。
    """

    pending: int
    done: int
    due: int
