"""Admin-only resume management plus public current/share reads.

Route order is load-bearing: ``/current`` and ``/shared/{code}`` are declared
before ``/{resume_id}`` so a static segment is never parsed as an integer id.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from api.deps import (
    client_ip,
    get_db,
    pagination,
    reject_legacy_paging,
    require_permission,
)
from api.models import (
    ResumeDetailOut,
    ResumeIn,
    ResumeOut,
    ResumePublicOut,
    ResumeShareIn,
    ResumeShareOut,
    ResumeUpdateIn,
)
from api.responses import ApiError, split_page, success
from core.constants import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    CODE_RATE_LIMITED,
    DETAIL_RESUME_COPIED,
    DETAIL_RESUME_DEFAULT_DELETE,
    DETAIL_RESUME_DEFAULT_SET,
    DETAIL_RESUME_NOT_FOUND,
    DETAIL_RESUME_READ_RATE_LIMITED,
    DETAIL_RESUME_SHARE_CREATED,
    DETAIL_RESUME_SHARE_NOT_FOUND,
    DETAIL_RESUME_SHARE_REVOKED,
)
from core.permissions import RESUME_MANAGE_ANY
from core.utils import utc_now
from infra.cache import Cache
from infra.db import Database
from services.resumes import (
    copy_resume,
    create_resume,
    create_share,
    current_resume,
    delete_resume,
    get_resume,
    list_resumes,
    list_shares,
    resolve_share,
    revoke_share,
    set_default,
    update_resume,
)

router = APIRouter()

# Public share reads: 12-char codes are hard to guess, but an accidentally
# pasted link in a busy group still deserves a ceiling. One per-ip and one
# per-code fixed window, same shape as clips.
_SHARE_READ_WINDOW_SECONDS = 60
_SHARE_READ_IP_LIMIT = 30
_SHARE_READ_CODE_LIMIT = 60
_SHARE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{12}$")

_SHARE_NOT_FOUND = ApiError(
    CODE_NOT_FOUND,
    DETAIL_RESUME_SHARE_NOT_FOUND,
    status.HTTP_404_NOT_FOUND,
)
_RESUME_NOT_FOUND = ApiError(
    CODE_NOT_FOUND, DETAIL_RESUME_NOT_FOUND, status.HTTP_404_NOT_FOUND
)


def _share_cache(db: Database) -> Cache:
    return Cache(db, namespace="resume_share")


def _read_window() -> tuple[str, int]:
    now = int(utc_now().timestamp())
    stamp = now // _SHARE_READ_WINDOW_SECONDS
    remaining = _SHARE_READ_WINDOW_SECONDS - now % _SHARE_READ_WINDOW_SECONDS
    return str(stamp), remaining + 1


def _share_out(row: dict) -> dict:
    return ResumeShareOut(
        code=row["code"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    ).model_dump()


@router.get("/current")
async def read_current_resume(db: Database = Depends(get_db)) -> JSONResponse:
    """The resume `/profile` loads by default; 404 means "use the built-in mock"."""
    resume = await current_resume(db)
    if resume is None:
        raise _RESUME_NOT_FOUND
    return success(
        ResumePublicOut(
            title=resume["title"],
            content_html=resume["content_html"],
            updated_at=resume["updated_at"],
        ).model_dump()
    )


@router.get("/shared/{code}")
async def read_shared_resume(
    code: str,
    request: Request,
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Public capability read: code + expiry + not revoked + resume not deleted."""
    checked = (code or "").strip()
    if not _SHARE_CODE_RE.match(checked):
        raise _SHARE_NOT_FOUND

    cache = _share_cache(db)
    window, ttl = _read_window()
    ip = client_ip(request)
    if ip is not None:
        hits = await cache.record_hit(f"read:ip:{ip}:{window}", ttl)
        if hits > _SHARE_READ_IP_LIMIT:
            raise ApiError(
                CODE_RATE_LIMITED,
                DETAIL_RESUME_READ_RATE_LIMITED,
                status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(ttl)},
            )
    hits = await cache.record_hit(f"read:code:{checked}:{window}", ttl)
    if hits > _SHARE_READ_CODE_LIMIT:
        raise ApiError(
            CODE_RATE_LIMITED,
            DETAIL_RESUME_READ_RATE_LIMITED,
            status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(ttl)},
        )

    row = await resolve_share(db, checked)
    if row is None:
        raise _SHARE_NOT_FOUND
    return success(
        ResumePublicOut(
            title=row["title"],
            content_html=row["content_html"],
            updated_at=row["updated_at"],
        ).model_dump()
    )


@router.get("")
async def list_resumes_endpoint(
    page: tuple[int, int] = Depends(pagination),
    _legacy: None = Depends(reject_legacy_paging),
    _admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    limit, offset = page
    rows, total = await list_resumes(db, limit=limit + 1, offset=offset)
    items, has_next = split_page(rows, limit)
    return success(
        {
            "items": [ResumeOut(**row).model_dump() for row in items],
            "has_next": has_next,
            "total": total,
        }
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_resume_endpoint(
    payload: ResumeIn,
    admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    row = await create_resume(
        db,
        title=payload.title,
        note=payload.note,
        content_html=payload.content_html,
        created_by=admin["id"],
    )
    return success(
        ResumeDetailOut(**row).model_dump(), status_code=status.HTTP_201_CREATED
    )


@router.get("/{resume_id}")
async def get_resume_endpoint(
    resume_id: int,
    _admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    row = await get_resume(db, resume_id)
    if row is None:
        raise _RESUME_NOT_FOUND
    return success(ResumeDetailOut(**row).model_dump())


@router.patch("/{resume_id}")
async def update_resume_endpoint(
    resume_id: int,
    payload: ResumeUpdateIn,
    _admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    row = await update_resume(
        db,
        resume_id,
        title=payload.title,
        note=payload.note,
        content_html=payload.content_html,
    )
    if row is None:
        raise _RESUME_NOT_FOUND
    return success(ResumeDetailOut(**row).model_dump())


@router.delete("/{resume_id}")
async def delete_resume_endpoint(
    resume_id: int,
    _admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    result = await delete_resume(db, resume_id)
    if result == "not_found":
        raise _RESUME_NOT_FOUND
    if result == "default":
        raise ApiError(
            CODE_CONFLICT,
            DETAIL_RESUME_DEFAULT_DELETE,
            status.HTTP_409_CONFLICT,
        )
    return success({"id": resume_id})


@router.post("/{resume_id}/copy", status_code=status.HTTP_201_CREATED)
async def copy_resume_endpoint(
    resume_id: int,
    admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    row = await copy_resume(db, resume_id, created_by=admin["id"])
    if row is None:
        raise _RESUME_NOT_FOUND
    return success(
        ResumeDetailOut(**row).model_dump(),
        status_code=status.HTTP_201_CREATED,
        detail=DETAIL_RESUME_COPIED,
    )


@router.post("/{resume_id}/default")
async def set_default_endpoint(
    resume_id: int,
    _admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    row = await set_default(db, resume_id)
    if row is None:
        raise _RESUME_NOT_FOUND
    return success(ResumeOut(**row).model_dump(), detail=DETAIL_RESUME_DEFAULT_SET)


@router.post("/{resume_id}/shares", status_code=status.HTTP_201_CREATED)
async def create_share_endpoint(
    resume_id: int,
    payload: ResumeShareIn,
    admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    row = await create_share(
        db,
        resume_id,
        expires_in_minutes=payload.expires_in_minutes,
        created_by=admin["id"],
    )
    if row is None:
        raise _RESUME_NOT_FOUND
    return success(
        _share_out(row),
        status_code=status.HTTP_201_CREATED,
        detail=DETAIL_RESUME_SHARE_CREATED,
    )


@router.get("/{resume_id}/shares")
async def list_shares_endpoint(
    resume_id: int,
    _admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    resume = await get_resume(db, resume_id)
    if resume is None:
        raise _RESUME_NOT_FOUND
    rows = await list_shares(db, resume_id)
    return success([_share_out(row) for row in rows])


@router.delete("/shares/{code}")
async def revoke_share_endpoint(
    code: str,
    _admin: dict = Depends(require_permission(RESUME_MANAGE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    checked = (code or "").strip()
    if not _SHARE_CODE_RE.match(checked) or not await revoke_share(db, checked):
        raise _SHARE_NOT_FOUND
    return success({"code": checked}, detail=DETAIL_RESUME_SHARE_REVOKED)
