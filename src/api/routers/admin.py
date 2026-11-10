"""Admin-only management of invite codes and users."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from api.deps import get_db, pagination, require_permission
from api.models import InviteCodeCreateIn, InviteCodeOut, UserOut, UserRoleUpdateIn
from api.responses import ApiError, success
from core.constants import (
    CODE_CANNOT_CHANGE_OWN_ROLE,
    CODE_INVITE_CODE_IN_USE,
    CODE_LAST_ADMIN_REQUIRED,
    CODE_NOT_FOUND,
    DETAIL_CANNOT_CHANGE_OWN_ROLE,
    DETAIL_INVITE_CODE_DELETED,
    DETAIL_INVITE_CODE_IN_USE,
    DETAIL_INVITE_CODE_NOT_FOUND,
    DETAIL_INVITE_CODES_CREATED,
    DETAIL_LAST_ADMIN_REQUIRED,
    DETAIL_USER_NOT_FOUND,
)
from core.permissions import (
    INVITE_CODE_CREATE_ANY,
    INVITE_CODE_DELETE_ANY,
    INVITE_CODE_READ_ANY,
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

_USER_COLUMNS = "id, email, role, created_at"


def _like_pattern(term: str) -> str:
    """Build a ``%term%`` pattern, escaping LIKE wildcards in user input."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
    """List users with optional email search and role filter."""
    limit, offset = page
    clauses: list[str] = []
    params: list[object] = []
    term = (q or "").strip()
    if term:
        clauses.append("email LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(term))
    if role is not None:
        clauses.append("role = ?")
        params.append(role)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    total_row = await db.fetch_one(
        f"SELECT COUNT(*) AS total FROM users{where}", params
    )
    total = int(total_row["total"]) if total_row else 0
    rows = await db.fetch_all(
        f"SELECT {_USER_COLUMNS} FROM users{where} "
        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return success(
        {"items": [UserOut(**row).model_dump() for row in rows], "total": total}
    )


@router.patch("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    payload: UserRoleUpdateIn,
    admin: dict = Depends(require_permission(USER_UPDATE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Change a user's role, refusing self-edits and demoting the last admin."""
    target = await db.fetch_one(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
    )
    if target is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_USER_NOT_FOUND, status.HTTP_404_NOT_FOUND)

    if target["id"] == admin["id"]:
        raise ApiError(
            CODE_CANNOT_CHANGE_OWN_ROLE,
            DETAIL_CANNOT_CHANGE_OWN_ROLE,
            status.HTTP_400_BAD_REQUEST,
        )

    if target["role"] == "admin" and payload.role != "admin":
        count_row = await db.fetch_one(
            "SELECT COUNT(*) AS total FROM users WHERE role = 'admin'"
        )
        admins = int(count_row["total"]) if count_row else 0
        if admins <= 1:
            raise ApiError(
                CODE_LAST_ADMIN_REQUIRED,
                DETAIL_LAST_ADMIN_REQUIRED,
                status.HTTP_409_CONFLICT,
            )

    updated = await db.fetch_one(
        f"UPDATE users SET role = ? WHERE id = ? RETURNING {_USER_COLUMNS}",
        (payload.role, user_id),
    )
    if updated is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_USER_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return success(UserOut(**updated).model_dump())
