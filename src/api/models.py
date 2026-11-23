"""Pydantic models shared by the routers."""

from __future__ import annotations

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

from core.timestamps import normalize_utc_datetime
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

# 到期时刻与全站其他时间列同形：UTC 的 'YYYY-MM-DD HH:MM:SS'。
# 「哪天到期」是客户端按自己的本地日末（23:59:59）换算成 UTC 再上送的，
# 服务端不做时区猜测，也不接受只给日期的写法——那样又退回本地日期语义。
MEMO_BODY_MAX = 2000


def _check_due_at(value: str | None) -> str | None:
    """Validate a memo due instant; the rule itself lives in ``core.timestamps``."""
    return normalize_utc_datetime(value)


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
    ``undated`` 是月历底部「未排到期日」的总数；月历只预览前若干条，不能拿
    预览数组的长度冒充总数。
    """

    pending: int
    done: int
    due: int
    undated: int


# --- Personal-space consumables --------------------------------------------

CONSUMABLE_NAME_MAX = 40
CONSUMABLE_NOTE_MAX = 200
CONSUMABLE_UNIT_MAX = 8
CONSUMABLE_LEAD_DAYS_MAX = 60
CONSUMABLE_EXPECTED_DAYS_MAX = 3650

ConsumableCategory = Literal["paper", "clean", "food", "pet", "other"]


def _check_local_day(value: str | None) -> str | None:
    """「哪一天」一律是本地日字符串，不猜时区，也不接受 datetime。"""
    if value is None:
        return None
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if not 1900 <= parsed.year <= 2200:
        raise ValueError("date is out of range")
    return text


def _clean_optional_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise ValueError(f"text must be at most {limit} characters")
    return text


class ConsumableIn(BaseModel):
    """新建一个消耗品；顺手开出第一轮周期，平时不用再记录。

    ``started_on`` 由客户端按自己的本地今天上送：服务端不猜时区，
    缺省才退回 UTC 的今天（跨零点前后可能差一天，前端始终会传）。
    """

    name: str = Field(min_length=1, max_length=CONSUMABLE_NAME_MAX)
    category: ConsumableCategory = "other"
    unit: str | None = Field(default=None, max_length=CONSUMABLE_UNIT_MAX)
    quantity: float | None = Field(default=None, ge=0, le=100_000)
    lead_days: int = Field(default=7, ge=0, le=CONSUMABLE_LEAD_DAYS_MAX)
    note: str | None = Field(default=None, max_length=CONSUMABLE_NOTE_MAX)
    started_on: str | None = None
    expected_days: int | None = Field(
        default=None, ge=1, le=CONSUMABLE_EXPECTED_DAYS_MAX
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("name must not be blank")
        return text

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str | None) -> str | None:
        return _clean_optional_text(value, CONSUMABLE_UNIT_MAX)

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return _clean_optional_text(value, CONSUMABLE_NOTE_MAX)

    @field_validator("started_on")
    @classmethod
    def _started_on(cls, value: str | None) -> str | None:
        return _check_local_day(value)


class ConsumableUpdateIn(BaseModel):
    """只改物品本身的设置；周期有单独的接口，免得一次 PATCH 改两处。"""

    name: str | None = Field(
        default=None, min_length=1, max_length=CONSUMABLE_NAME_MAX
    )
    category: ConsumableCategory | None = None
    unit: str | None = Field(default=None, max_length=CONSUMABLE_UNIT_MAX)
    quantity: float | None = Field(default=None, ge=0, le=100_000)
    lead_days: int | None = Field(default=None, ge=0, le=CONSUMABLE_LEAD_DAYS_MAX)
    note: str | None = Field(default=None, max_length=CONSUMABLE_NOTE_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("name must not be blank")
        return text

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str | None) -> str | None:
        return _clean_optional_text(value, CONSUMABLE_UNIT_MAX)

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return _clean_optional_text(value, CONSUMABLE_NOTE_MAX)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> ConsumableUpdateIn:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class CycleStartIn(BaseModel):
    """开一轮（新买/换新）；物品已有进行中周期时返回 409。"""

    started_on: str
    expected_days: int | None = Field(
        default=None, ge=1, le=CONSUMABLE_EXPECTED_DAYS_MAX
    )
    note: str | None = Field(default=None, max_length=CONSUMABLE_NOTE_MAX)

    @field_validator("started_on")
    @classmethod
    def _started_on(cls, value: str) -> str:
        result = _check_local_day(value)
        assert result is not None
        return result

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return _clean_optional_text(value, CONSUMABLE_NOTE_MAX)


class CycleFinishIn(BaseModel):
    """点「用完了」：只关掉进行中的那一轮；不开下一轮 = 先不买。"""

    ended_on: str | None = None

    @field_validator("ended_on")
    @classmethod
    def _ended_on(cls, value: str | None) -> str | None:
        return _check_local_day(value)


class CycleUpdateIn(BaseModel):
    """修正：改开始日/结束日/预计天数；``ended_on: null`` 等于撤回「用完」。"""

    started_on: str | None = None
    ended_on: str | None = None
    expected_days: int | None = Field(
        default=None, ge=1, le=CONSUMABLE_EXPECTED_DAYS_MAX
    )
    note: str | None = Field(default=None, max_length=CONSUMABLE_NOTE_MAX)

    @field_validator("started_on", "ended_on")
    @classmethod
    def _days(cls, value: str | None) -> str | None:
        return _check_local_day(value)

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return _clean_optional_text(value, CONSUMABLE_NOTE_MAX)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> CycleUpdateIn:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class ConsumableCycleOut(BaseModel):
    id: int
    started_on: str
    ended_on: str | None = None
    expected_days: int | None = None
    note: str | None = None
    created_at: str
    updated_at: str
    # 已结束周期的实际天数；进行中的为 null
    actual_days: int | None = None


class ConsumableOut(BaseModel):
    """一个物品 + 它当前那一轮 + 由日期算出来的展示字段（一个都不入库）。"""

    id: int
    name: str
    category: str
    unit: str | None = None
    quantity: float | None = None
    lead_days: int
    note: str | None = None
    created_at: str
    updated_at: str
    cycle: ConsumableCycleOut | None = None
    days_used: int | None = None
    days_left: int | None = None
    due_on: str | None = None
    progress: float | None = None
    status: Literal["ok", "soon", "due", "unknown", "idle"] = "idle"
    last_days: int | None = None
    suggested_days: int | None = None


# --- Shared clipboard -------------------------------------------------------

CLIP_BODY_MAX = 20_000
CLIP_TITLE_MAX = 80


class ClipIn(BaseModel):
    """一段日志或文本；title 只是给收件人看的标签，可以为空。"""

    title: str = Field(default="", max_length=CLIP_TITLE_MAX)
    body: str = Field(min_length=1, max_length=CLIP_BODY_MAX)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return value.strip()

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        # 全是空白的日志没有分享价值；首尾空白保留，缩进本身就是信息
        if not value.strip():
            raise ValueError("body must not be blank")
        return value


class ClipOut(BaseModel):
    code: str
    title: str = ""
    body: str
    created_at: str
    expires_at: str


# --- Resume management ------------------------------------------------------

RESUME_TITLE_MAX = 100
RESUME_NOTE_MAX = 200
RESUME_HTML_MAX = 50_000
RESUME_SHARE_MAX_MINUTES = 30 * 24 * 60


class ResumeIn(BaseModel):
    """新建简历：正文是 HTML，清洗只在渲染端做，库里保留作者原文。"""

    title: str = Field(min_length=1, max_length=RESUME_TITLE_MAX)
    note: str = Field(default="", max_length=RESUME_NOTE_MAX)
    content_html: str = Field(min_length=1, max_length=RESUME_HTML_MAX)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        return value.strip()

    @field_validator("content_html")
    @classmethod
    def _content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content_html must not be blank")
        return value


class ResumeUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=RESUME_TITLE_MAX)
    note: str | None = Field(default=None, max_length=RESUME_NOTE_MAX)
    content_html: str | None = Field(
        default=None, min_length=1, max_length=RESUME_HTML_MAX
    )

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()

    @field_validator("content_html")
    @classmethod
    def _content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("content_html must not be blank")
        return value

    @model_validator(mode="after")
    def _at_least_one(self) -> ResumeUpdateIn:
        if self.title is None and self.note is None and self.content_html is None:
            raise ValueError("at least one field is required")
        return self


class ResumeOut(BaseModel):
    id: int
    title: str
    note: str
    is_default: bool
    created_at: str
    updated_at: str


class ResumeDetailOut(ResumeOut):
    content_html: str


class ResumePublicOut(BaseModel):
    """公开响应：不带 note / id / 创建人，只给渲染所需的最小形状。"""

    title: str
    content_html: str
    updated_at: str


class ResumeShareIn(BaseModel):
    expires_in_minutes: int = Field(default=24 * 60, ge=1, le=RESUME_SHARE_MAX_MINUTES)


class ResumeShareOut(BaseModel):
    code: str
    expires_at: str
    created_at: str

