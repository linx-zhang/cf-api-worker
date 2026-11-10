"""Admin-only invite-code management."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from api.deps import get_db, pagination, require_permission
from api.models import InviteCodeCreateIn, InviteCodeOut
from api.responses import ApiError, success
from core.constants import (
    CODE_INVITE_CODE_IN_USE,
    CODE_NOT_FOUND,
    DETAIL_INVITE_CODE_DELETED,
    DETAIL_INVITE_CODE_IN_USE,
    DETAIL_INVITE_CODE_NOT_FOUND,
    DETAIL_INVITE_CODES_CREATED,
)
from core.permissions import (
    INVITE_CODE_CREATE_ANY,
    INVITE_CODE_DELETE_ANY,
    INVITE_CODE_READ_ANY,
)
from infra.db import Database
from infra.invite_codes import (
    delete_unused,
    generate_invite_codes,
    insert_codes,
    list_codes,
)

router = APIRouter()


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
