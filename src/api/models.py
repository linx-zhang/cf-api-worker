"""Pydantic models shared by the routers."""

from __future__ import annotations

import time
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
        now_ms = int(time.time() * 1000)
        if value < now_ms - ACTIVITY_TS_MAX_AGE_MS:
            raise ValueError("ts must be epoch milliseconds within the last 30 days")
        if value > now_ms + ACTIVITY_TS_FUTURE_TOLERANCE_MS:
            raise ValueError(
                "ts must be epoch milliseconds no more than 5 minutes in the future"
            )
        return value


class ActivityBatchIn(BaseModel):
    events: list[ActivityEventIn] = Field(min_length=1, max_length=50)


class ActivityOut(BaseModel):
    id: int
    user_id: int
    user_email: str
    type: str
    path: str
    ts: int
    duration_ms: int | None = None
    created_at: str


class InviteCodeCreateIn(BaseModel):
    role: Literal["internal", "admin"]
    count: int = Field(default=1, ge=1, le=50)
    expires_in_days: int = Field(default=7, ge=1, le=365)


class InviteCodeOut(BaseModel):
    code: str
    role: str
    expires_at: str | None = None
    created_at: str
    used_at: str | None = None
    created_by: int | None = None
    created_by_email: str | None = None
