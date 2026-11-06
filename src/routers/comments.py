"""Comments attached to a post, with optional one-level replies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from core.constants import (
    CODE_INTERNAL_ERROR,
    CODE_INVALID_PARENT_COMMENT,
    CODE_NOT_FOUND,
    DETAIL_COMMENT_CREATE_FAILED,
    DETAIL_COMMENT_DELETED,
    DETAIL_COMMENT_NOT_FOUND,
    DETAIL_INVALID_PARENT_COMMENT,
    DETAIL_POST_NOT_FOUND,
)
from db import Database
from deps import get_current_user, get_db, require_admin
from models import CommentIn, CommentOut
from responses import ApiError, success

router = APIRouter()
delete_router = APIRouter()

_COMMENT_SELECT = (
    "SELECT c.id, c.post_id, c.author_id, u.email AS author_email, c.parent_id, "
    "c.body, c.created_at FROM comments c JOIN users u ON u.id = c.author_id "
    "WHERE c.deleted_at IS NULL"
)


async def _ensure_post_exists(db: Database, post_id: int) -> None:
    post = await db.fetch_one(
        "SELECT id FROM posts WHERE id = ? AND deleted_at IS NULL", (post_id,)
    )
    if post is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_POST_NOT_FOUND, status.HTTP_404_NOT_FOUND)


@router.get("/{post_id}/comments")
async def list_comments(post_id: int, db: Database = Depends(get_db)) -> JSONResponse:
    await _ensure_post_exists(db, post_id)
    comments = await db.fetch_all(
        _COMMENT_SELECT + " AND c.post_id = ? ORDER BY c.created_at ASC, c.id ASC",
        (post_id,),
    )
    return success([CommentOut(**comment).model_dump() for comment in comments])


@router.post("/{post_id}/comments", status_code=status.HTTP_201_CREATED)
async def create_comment(
    post_id: int,
    payload: CommentIn,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    await _ensure_post_exists(db, post_id)

    if payload.parent_id is not None:
        parent = await db.fetch_one(
            "SELECT id FROM comments WHERE id = ? AND post_id = ? AND deleted_at IS NULL",
            (payload.parent_id, post_id),
        )
        if parent is None:
            raise ApiError(
                CODE_INVALID_PARENT_COMMENT,
                DETAIL_INVALID_PARENT_COMMENT,
                status.HTTP_400_BAD_REQUEST,
            )

    comment = await db.insert_returning(
        "INSERT INTO comments (post_id, author_id, parent_id, body) VALUES (?, ?, ?, ?) "
        "RETURNING id, post_id, author_id, parent_id, body, created_at",
        (post_id, user["id"], payload.parent_id, payload.body.strip()),
    )
    if comment is None:
        raise ApiError(
            CODE_INTERNAL_ERROR,
            DETAIL_COMMENT_CREATE_FAILED,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    comment["author_email"] = user["email"]
    return success(
        CommentOut(**comment).model_dump(), status_code=status.HTTP_201_CREATED
    )


@delete_router.delete("/{comment_id}")
async def delete_comment(
    comment_id: int,
    admin: dict = Depends(require_admin),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Hard delete a comment and every reply below it (admin only)."""
    rows = await db.fetch_all(
        "WITH RECURSIVE subtree(id) AS ("
        "SELECT id FROM comments WHERE id = ? "
        "UNION ALL "
        "SELECT c.id FROM comments c JOIN subtree s ON c.parent_id = s.id"
        ") SELECT id FROM subtree",
        (comment_id,),
    )
    ids = [row["id"] for row in rows]
    if not ids:
        raise ApiError(
            CODE_NOT_FOUND, DETAIL_COMMENT_NOT_FOUND, status.HTTP_404_NOT_FOUND
        )

    placeholders = ",".join("?" * len(ids))
    deleted = await db.execute(
        f"DELETE FROM comments WHERE id IN ({placeholders})", ids
    )
    return success(
        {"id": comment_id, "deleted_comments": deleted},
        detail=DETAIL_COMMENT_DELETED,
    )
