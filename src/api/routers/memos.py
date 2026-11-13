"""Personal-space memos: one row per note, scoped to the caller.

Everything here is ``self``-scoped by construction: the owner id comes from the
token, never from the request body, and a row that belongs to someone else is
reported as ``404 not_found`` rather than ``403`` — whether the id exists is not
the caller's business.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from api.deps import (
    get_current_user,
    get_db,
    pagination,
    reject_legacy_paging,
)
from api.models import MemoIn, MemoOut, MemoSummaryOut, MemoUpdateIn
from api.responses import ApiError, split_page, success
from core.constants import (
    CODE_INTERNAL_ERROR,
    CODE_NOT_FOUND,
    DETAIL_MEMO_CREATE_FAILED,
    DETAIL_MEMO_DELETED,
    DETAIL_MEMO_NOT_FOUND,
)
from infra.db import Database

router = APIRouter()

_MEMO_COLUMNS = "id, body, done, done_at, due_at, created_at, updated_at"
_MEMO_SELECT = f"SELECT {_MEMO_COLUMNS} FROM memos WHERE deleted_at IS NULL"

# 「今天」按北京时区算，与 due_at（本地日期）同一套语义。
_TODAY_CN = "date('now', '+8 hours')"


def _memo_out(row: dict) -> dict:
    return MemoOut(**row).model_dump()


async def _get_memo(db: Database, memo_id: int, user_id: int) -> dict:
    """Load one memo of the caller, or raise 404."""
    memo = await db.fetch_one(
        _MEMO_SELECT + " AND id = ? AND user_id = ?", (memo_id, user_id)
    )
    if memo is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_MEMO_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return memo


@router.get("/summary")
async def memo_summary(
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Counters for the segmented filter and the greeting line.

    Deliberately not part of the list response: the list follows the API-wide
    ``{items, has_next}`` standard and carries no total, while these three
    numbers are a different question (how many are left) answered by one
    aggregate over the caller's own rows.
    """
    row = await db.fetch_one(
        "SELECT "
        "COALESCE(SUM(CASE WHEN done = 0 THEN 1 ELSE 0 END), 0) AS pending, "
        "COALESCE(SUM(CASE WHEN done = 1 THEN 1 ELSE 0 END), 0) AS done, "
        "COALESCE(SUM(CASE WHEN done = 0 AND due_at IS NOT NULL "
        f"AND due_at <= {_TODAY_CN} THEN 1 ELSE 0 END), 0) AS due "
        "FROM memos WHERE user_id = ? AND deleted_at IS NULL",
        (user["id"],),
    )
    return success(MemoSummaryOut(**(row or {})).model_dump())


@router.get("")
async def list_memos(
    page: tuple[int, int] = Depends(pagination),
    _legacy: None = Depends(reject_legacy_paging),
    done: bool | None = Query(None),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """List the caller's memos, newest first; ``done`` filters the two segments."""
    limit, offset = page
    conditions = ["user_id = ?"]
    params: list[object] = [user["id"]]
    if done is not None:
        conditions.append("done = ?")
        params.append(1 if done else 0)
    memos = await db.fetch_all(
        _MEMO_SELECT
        + " AND "
        + " AND ".join(conditions)
        + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit + 1, offset),
    )
    rows, has_next = split_page(memos, limit)
    return success({"items": [_memo_out(row) for row in rows], "has_next": has_next})


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_memo(
    payload: MemoIn,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    # done / done_at 走默认值：新记的一条永远是待办
    memo = await db.insert_returning(
        f"INSERT INTO memos (user_id, body, due_at) VALUES (?, ?, ?) "
        f"RETURNING {_MEMO_COLUMNS}",
        (user["id"], payload.body, payload.due_at),
    )
    if memo is None:
        raise ApiError(
            CODE_INTERNAL_ERROR,
            DETAIL_MEMO_CREATE_FAILED,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return success(_memo_out(memo), status_code=status.HTTP_201_CREATED)


@router.patch("/{memo_id}")
async def update_memo(
    memo_id: int,
    payload: MemoUpdateIn,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Update body / done / due_at. Unchecking a memo also clears ``done_at``."""
    await _get_memo(db, memo_id, user["id"])

    assignments: list[str] = []
    params: list[object] = []
    if payload.body is not None:
        assignments.append("body = ?")
        params.append(payload.body)
    if "done" in payload.model_fields_set:
        # done 与 done_at 必须同时改：只写 done 会留下一个"没完成却有完成时间"的行
        if payload.done:
            assignments.extend(["done = 1", "done_at = datetime('now')"])
        else:
            assignments.extend(["done = 0", "done_at = NULL"])
    if "due_at" in payload.model_fields_set:
        assignments.append("due_at = ?")
        params.append(payload.due_at)
    params.append(memo_id)

    await db.execute(
        f"UPDATE memos SET {', '.join(assignments)}, updated_at = datetime('now') "
        "WHERE id = ? AND deleted_at IS NULL",
        params,
    )
    return success(_memo_out(await _get_memo(db, memo_id, user["id"])))


@router.delete("/{memo_id}")
async def delete_memo(
    memo_id: int,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Soft delete: the row and its ``deleted_at`` stay in D1, reads filter it out."""
    await _get_memo(db, memo_id, user["id"])
    await db.execute(
        "UPDATE memos SET deleted_at = datetime('now'), updated_at = datetime('now') "
        "WHERE id = ? AND deleted_at IS NULL",
        (memo_id,),
    )
    return success({"id": memo_id}, detail=DETAIL_MEMO_DELETED)
