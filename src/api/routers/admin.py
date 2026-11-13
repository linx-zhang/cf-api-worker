"""Admin-only management of invite codes, users and activity logs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from api.deps import get_db, pagination, reject_legacy_paging, require_permission
from api.models import (
    PROFILE_FIELDS,
    ActivityOut,
    AdminUserOut,
    AnalyticsSummaryOut,
    InviteCodeCreateIn,
    InviteCodeOut,
    UserPasswordResetIn,
    UserPasswordResetOut,
    UserUpdateIn,
)
from api.responses import ApiError, split_page, success
from core.constants import (
    CODE_CANNOT_CHANGE_OWN_ROLE,
    CODE_CANNOT_DELETE_SELF,
    CODE_EMAIL_EXISTS,
    CODE_INVALID_TIMESTAMP,
    CODE_INVITE_CODE_IN_USE,
    CODE_LAST_ADMIN_REQUIRED,
    CODE_NOT_FOUND,
    DETAIL_CANNOT_CHANGE_OWN_ROLE,
    DETAIL_CANNOT_DELETE_SELF,
    DETAIL_EMAIL_EXISTS,
    DETAIL_INVALID_TIMESTAMP,
    DETAIL_INVITE_CODE_DELETED,
    DETAIL_INVITE_CODE_IN_USE,
    DETAIL_INVITE_CODE_NOT_FOUND,
    DETAIL_INVITE_CODES_CREATED,
    DETAIL_LAST_ADMIN_REQUIRED,
    DETAIL_PASSWORD_RESET,
    DETAIL_TIMESTAMP_RANGE,
    DETAIL_USER_DELETED,
    DETAIL_USER_NOT_FOUND,
)
from core.permissions import (
    ACTIVITY_READ_ANY,
    INVITE_CODE_CREATE_ANY,
    INVITE_CODE_DELETE_ANY,
    INVITE_CODE_READ_ANY,
    USER_DELETE_ANY,
    USER_READ_ANY,
    USER_RESET_PASSWORD_ANY,
    USER_UPDATE_ANY,
)
from core.utils import sqlite_timestamp
from infra.db import Database
from infra.passwords import generate_temporary_password, hash_password
from services.analytics import load_summary
from services.invite_codes import (
    delete_unused,
    generate_invite_codes,
    insert_codes,
    list_codes,
)

router = APIRouter()

_ADMIN_USER_SELECT = (
    "SELECT u.id, u.email, u.role, u.created_at, u.deleted_at, "
    "u.nickname, u.first_name, u.last_name, u.avatar_url, "
    "u.last_active_at FROM users u"
)

_TIMESTAMP_HELP = "expected an ISO date/datetime or epoch milliseconds"


def _like_pattern(term: str) -> str:
    """Build a ``%term%`` pattern, escaping LIKE wildcards in user input."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _admin_user_out(row: dict) -> dict:
    data = dict(row)
    data.pop("deleted_at", None)
    return AdminUserOut(**data).model_dump()


async def _fetch_admin_user(db: Database, user_id: int) -> dict | None:
    """Fetch a live (not soft-deleted) user together with last activity."""
    return await db.fetch_one(
        _ADMIN_USER_SELECT + " WHERE u.id = ? AND u.deleted_at IS NULL", (user_id,)
    )


async def _live_admin_count(db: Database) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS total FROM users WHERE role = 'admin' "
        "AND deleted_at IS NULL"
    )
    return int(row["total"]) if row else 0


def _ensure_role_change_allowed(
    target: dict, new_role: str, actor_id: int, admin_count: int
) -> None:
    if target["id"] == actor_id:
        raise ApiError(
            CODE_CANNOT_CHANGE_OWN_ROLE,
            DETAIL_CANNOT_CHANGE_OWN_ROLE,
            status.HTTP_400_BAD_REQUEST,
        )
    if target["role"] == "admin" and new_role != "admin" and admin_count <= 1:
        raise ApiError(
            CODE_LAST_ADMIN_REQUIRED,
            DETAIL_LAST_ADMIN_REQUIRED,
            status.HTTP_409_CONFLICT,
        )


def _parse_timestamp_bound(raw: str) -> str:
    """Accept epoch milliseconds or ISO 8601, return a D1-comparable string."""
    text = raw.strip()
    try:
        millis = int(text)
    except ValueError:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            raise ApiError(
                CODE_INVALID_TIMESTAMP,
                DETAIL_INVALID_TIMESTAMP.format(reason=_TIMESTAMP_HELP),
                422,
            ) from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return sqlite_timestamp(moment.astimezone(UTC))
    return sqlite_timestamp(datetime.fromtimestamp(millis / 1000, UTC))


@router.post("/invite-codes", status_code=status.HTTP_201_CREATED)
async def create_invite_codes(
    payload: InviteCodeCreateIn,
    admin: dict = Depends(require_permission(INVITE_CODE_CREATE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    codes = generate_invite_codes(
        payload.role, payload.count, payload.expires_in_days, payload.note
    )
    rows = await insert_codes(db, codes, created_by=admin["id"])
    return success(
        [InviteCodeOut(**row).model_dump() for row in rows],
        status_code=status.HTTP_201_CREATED,
        detail=DETAIL_INVITE_CODES_CREATED,
    )


@router.get("/invite-codes")
async def list_invite_codes(
    status_filter: Literal["unused", "used", "expired"] | None = Query(
        default=None, alias="status"
    ),
    role: Literal["internal", "admin"] | None = Query(default=None),
    page: tuple[int, int] = Depends(pagination),
    _legacy: None = Depends(reject_legacy_paging),
    admin: dict = Depends(require_permission(INVITE_CODE_READ_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    limit, offset = page
    rows, total = await list_codes(
        db, status=status_filter, role=role, limit=limit + 1, offset=offset
    )
    items, has_next = split_page(rows, limit)
    return success(
        {
            "items": [InviteCodeOut(**row).model_dump() for row in items],
            "has_next": has_next,
            "total": total,
        }
    )


@router.delete("/invite-codes/{code}")
async def delete_invite_code(
    code: str,
    admin: dict = Depends(require_permission(INVITE_CODE_DELETE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    result = await delete_unused(db, code)
    if result == "not_found":
        raise ApiError(
            CODE_NOT_FOUND, DETAIL_INVITE_CODE_NOT_FOUND, status.HTTP_404_NOT_FOUND
        )
    if result == "used":
        raise ApiError(
            CODE_INVITE_CODE_IN_USE,
            DETAIL_INVITE_CODE_IN_USE,
            status.HTTP_409_CONFLICT,
        )
    return success({"code": code}, detail=DETAIL_INVITE_CODE_DELETED)


@router.get("/users")
async def list_users(
    q: str | None = Query(default=None, max_length=200),
    role: Literal["user", "internal", "admin"] | None = Query(default=None),
    page: tuple[int, int] = Depends(pagination),
    _legacy: None = Depends(reject_legacy_paging),
    admin: dict = Depends(require_permission(USER_READ_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """List live users with optional email search and role filter."""
    limit, offset = page
    clauses = ["u.deleted_at IS NULL"]
    params: list[object] = []
    term = (q or "").strip()
    if term:
        clauses.append("u.email LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(term))
    if role is not None:
        clauses.append("u.role = ?")
        params.append(role)
    where = f" WHERE {' AND '.join(clauses)}"

    total_row = await db.fetch_one(
        f"SELECT COUNT(*) AS total FROM users u{where}", params
    )
    total = int(total_row["total"]) if total_row else 0
    rows = await db.fetch_all(
        f"{_ADMIN_USER_SELECT}{where} "
        "ORDER BY u.created_at DESC, u.id DESC LIMIT ? OFFSET ?",
        [*params, limit + 1, offset],
    )
    items, has_next = split_page(rows, limit)
    return success(
        {
            "items": [_admin_user_out(row) for row in items],
            "has_next": has_next,
            "total": total,
        }
    )


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdateIn,
    admin: dict = Depends(require_permission(USER_UPDATE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Update email and/or role, keeping the self-edit and last-admin guards."""
    target = await _fetch_admin_user(db, user_id)
    if target is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_USER_NOT_FOUND, status.HTTP_404_NOT_FOUND)

    role_changing = payload.role is not None and payload.role != target["role"]
    if role_changing:
        _ensure_role_change_allowed(
            target, payload.role, admin["id"], await _live_admin_count(db)
        )

    assignments: list[str] = []
    params: list[object] = []
    if payload.email is not None and payload.email != target["email"]:
        duplicate = await db.fetch_one(
            "SELECT id FROM users WHERE email = ? AND id <> ?", (payload.email, user_id)
        )
        if duplicate is not None:
            raise ApiError(
                CODE_EMAIL_EXISTS, DETAIL_EMAIL_EXISTS, status.HTTP_409_CONFLICT
            )
        assignments.append("email = ?")
        params.append(payload.email)
    if role_changing:
        assignments.append("role = ?")
        params.append(payload.role)
    for field in PROFILE_FIELDS:
        if field in payload.model_fields_set and getattr(payload, field) != target.get(
            field
        ):
            assignments.append(f"{field} = ?")
            params.append(getattr(payload, field))

    if assignments:
        params.append(user_id)
        try:
            await db.execute(
                f"UPDATE users SET {', '.join(assignments)} "
                "WHERE id = ? AND deleted_at IS NULL",
                params,
            )
        except Exception as exc:  # D1 surfaces SQLite constraint errors as JS errors
            if "UNIQUE" in str(exc).upper():
                raise ApiError(
                    CODE_EMAIL_EXISTS, DETAIL_EMAIL_EXISTS, status.HTTP_409_CONFLICT
                ) from None
            raise

    updated = await _fetch_admin_user(db, user_id)
    if updated is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_USER_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return success(_admin_user_out(updated))


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    payload: UserPasswordResetIn,
    admin: dict = Depends(require_permission(USER_RESET_PASSWORD_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Replace a user's password, either with one supplied by the admin or with
    a generated temporary one.

    The chosen password is returned exactly once and never stored in clear text.
    ``token_invalidated_at`` makes tokens issued before this call stop working,
    so an admin reset also ends existing sessions.
    """
    target = await _fetch_admin_user(db, user_id)
    if target is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_USER_NOT_FOUND, status.HTTP_404_NOT_FOUND)

    generated = payload.password is None
    password = generate_temporary_password() if generated else payload.password
    password_hash = await hash_password(password)
    reset_at = sqlite_timestamp()
    changed = await db.execute(
        "UPDATE users SET password_hash = ?, token_invalidated_at = ? "
        "WHERE id = ? AND deleted_at IS NULL",
        (password_hash, reset_at, user_id),
    )
    if changed == 0:
        raise ApiError(CODE_NOT_FOUND, DETAIL_USER_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return success(
        UserPasswordResetOut(
            id=user_id,
            email=target["email"],
            password=password,
            generated=generated,
            password_reset_at=reset_at,
        ).model_dump(),
        detail=DETAIL_PASSWORD_RESET,
    )


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: dict = Depends(require_permission(USER_DELETE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Soft delete a user; the row and its email reservation are kept."""
    target = await db.fetch_one(
        "SELECT id, role, deleted_at FROM users WHERE id = ?", (user_id,)
    )
    if target is None or target["deleted_at"] is not None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_USER_NOT_FOUND, status.HTTP_404_NOT_FOUND)

    if target["role"] == "admin" and await _live_admin_count(db) <= 1:
        raise ApiError(
            CODE_LAST_ADMIN_REQUIRED,
            DETAIL_LAST_ADMIN_REQUIRED,
            status.HTTP_409_CONFLICT,
        )
    if target["id"] == admin["id"]:
        raise ApiError(
            CODE_CANNOT_DELETE_SELF,
            DETAIL_CANNOT_DELETE_SELF,
            status.HTTP_400_BAD_REQUEST,
        )

    changed = await db.execute(
        "UPDATE users SET deleted_at = datetime('now') "
        "WHERE id = ? AND deleted_at IS NULL",
        (user_id,),
    )
    if changed == 0:
        raise ApiError(CODE_NOT_FOUND, DETAIL_USER_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return success({"id": user_id}, detail=DETAIL_USER_DELETED)


@router.get("/activity")
async def list_activity(
    user_id: int | None = Query(default=None, ge=1),
    type_filter: Literal["page_view", "heartbeat", "page_leave"] | None = Query(
        default=None, alias="type"
    ),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    page: tuple[int, int] = Depends(pagination),
    _legacy: None = Depends(reject_legacy_paging),
    admin: dict = Depends(require_permission(ACTIVITY_READ_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Query client activity; ``from``/``to`` filter on server receive time.

    No ``total``: a COUNT over the one unbounded table here would cost a second
    round trip on every page, and the only thing the UI needs is "is there a next
    page". So one extra row is fetched and ``has_next`` is derived from it.

    Paging uses the shared ``pagination`` dependency like every other list:
    one default (20) and one ceiling (100) in the whole API.
    """
    limit, offset = page
    clauses: list[str] = []
    params: list[object] = []
    if user_id is not None:
        clauses.append("e.user_id = ?")
        params.append(user_id)
    if type_filter is not None:
        clauses.append("e.type = ?")
        params.append(type_filter)
    start = _parse_timestamp_bound(from_) if from_ else None
    end = _parse_timestamp_bound(to) if to else None
    if start is not None and end is not None and start > end:
        raise ApiError(
            CODE_INVALID_TIMESTAMP,
            DETAIL_TIMESTAMP_RANGE,
            422,
        )
    if start is not None:
        clauses.append("e.created_at >= ?")
        params.append(start)
    if end is not None:
        clauses.append("e.created_at <= ?")
        params.append(end)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    # Fetch one row past the page: its presence is the only signal the UI needs.
    # LEFT JOIN: an anonymous visitor has no user row, and dropping those events
    # from the log would hide exactly the traffic this page exists to show.
    rows = await db.fetch_all(
        "SELECT e.id, e.user_id, u.email AS user_email, e.type, e.path, e.ts, "
        "e.duration_ms, e.ip, e.country, e.created_at FROM activity_events e "
        f"LEFT JOIN users u ON u.id = e.user_id{where} "
        "ORDER BY e.created_at DESC, e.id DESC LIMIT ? OFFSET ?",
        [*params, limit + 1, offset],
    )
    items, has_next = split_page(rows, limit)
    return success(
        {
            "items": [ActivityOut(**row).model_dump() for row in items],
            "has_next": has_next,
        }
    )


# --- Activity analytics ----------------------------------------------------


@router.get("/analytics/summary")
async def analytics_summary(
    days: int = Query(default=7, ge=1, le=90),
    admin: dict = Depends(require_permission(ACTIVITY_READ_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Activity overview: today's numbers, a daily series, per-identity detail.

    All SQL lives in ``services/analytics.py`` so it can be exercised without
    FastAPI; this route only adds the permission check, the shared cache and the
    response model. An identity is ``u:<id>`` (signed in) or ``ip:<addr>``
    (anonymous): nothing links the two, so a visitor who signs in appears as
    both instead of being guessed into one.
    """
    summary = await load_summary(db, days)
    return success(AnalyticsSummaryOut(**summary).model_dump())
