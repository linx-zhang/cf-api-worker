"""Admin-only management of invite codes and users."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from api.deps import get_db, pagination, require_permission
from api.models import (
    AdminUserOut,
    InviteCodeCreateIn,
    InviteCodeOut,
    UserUpdateIn,
)
from api.responses import ApiError, success
from core.constants import (
    CODE_CANNOT_CHANGE_OWN_ROLE,
    CODE_CANNOT_DELETE_SELF,
    CODE_EMAIL_EXISTS,
    CODE_INVITE_CODE_IN_USE,
    CODE_LAST_ADMIN_REQUIRED,
    CODE_NOT_FOUND,
    DETAIL_CANNOT_CHANGE_OWN_ROLE,
    DETAIL_CANNOT_DELETE_SELF,
    DETAIL_EMAIL_EXISTS,
    DETAIL_INVITE_CODE_DELETED,
    DETAIL_INVITE_CODE_IN_USE,
    DETAIL_INVITE_CODE_NOT_FOUND,
    DETAIL_INVITE_CODES_CREATED,
    DETAIL_LAST_ADMIN_REQUIRED,
    DETAIL_USER_DELETED,
    DETAIL_USER_NOT_FOUND,
)
from core.permissions import (
    INVITE_CODE_CREATE_ANY,
    INVITE_CODE_DELETE_ANY,
    INVITE_CODE_READ_ANY,
    USER_DELETE_ANY,
    USER_READ_ANY,
    USER_UPDATE_ANY,
)
from infra.db import Database
from infra.invite_codes import (
    delete_unused,
    generate_invite_codes,
    insert_codes,
    list_codes,
)

router = APIRouter()

_ADMIN_USER_SELECT = (
    "SELECT u.id, u.email, u.role, u.created_at, u.deleted_at FROM users u"
)


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


@router.post("/invite-codes", status_code=status.HTTP_201_CREATED)
async def create_invite_codes(
    payload: InviteCodeCreateIn,
    admin: dict = Depends(require_permission(INVITE_CODE_CREATE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    codes = generate_invite_codes(payload.role, payload.count, payload.expires_in_days)
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
    admin: dict = Depends(require_permission(INVITE_CODE_READ_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    limit, offset = page
    rows, total = await list_codes(
        db, status=status_filter, role=role, limit=limit, offset=offset
    )
    return success(
        {
            "items": [InviteCodeOut(**row).model_dump() for row in rows],
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
        [*params, limit, offset],
    )
    return success({"items": [_admin_user_out(row) for row in rows], "total": total})


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
