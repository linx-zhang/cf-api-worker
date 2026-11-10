"""Comments attached to a post, with optional one-level replies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from api.deps import (
    ensure_resource_permission,
    get_current_user,
    get_db,
    require_permission,
)
from api.models import CommentIn, CommentOut
from api.responses import ApiError, success
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
from core.permissions import (
    COMMENT_CREATE_ANY,
    COMMENT_DELETE_ANY,
    COMMENT_DELETE_OWN,
)
from core.utils import display_name
from infra.db import Database

router = APIRouter()
delete_router = APIRouter()

_COMMENT_SELECT = (
    "SELECT c.id, c.post_id, c.author_id, u.email AS author_email, c.parent_id, "
    "c.body, c.created_at FROM comments c JOIN users u ON u.id = c.author_id "
    "WHERE c.deleted_at IS NULL"
)


def _comment_out(row: dict) -> dict:
    """Map a joined row to the public shape (no email, only a display name)."""
    data = dict(row)
    data["author_name"] = display_name(data.pop("author_email", "") or "")
    return CommentOut(**data).model_dump()


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
    return success([_comment_out(comment) for comment in comments])


@router.post("/{post_id}/comments", status_code=status.HTTP_201_CREATED)
async def create_comment(
    post_id: int,
    payload: CommentIn,
    user: dict = Depends(require_permission(COMMENT_CREATE_ANY)),
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
    comment["author_name"] = display_name(user["email"])
    return success(
        CommentOut(**comment).model_dump(), status_code=status.HTTP_201_CREATED
    )


@delete_router.delete("/{comment_id}")
async def delete_comment(
    comment_id: int,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Soft delete a comment and every reply below it."""
    comment = await db.fetch_one(
        "SELECT id, author_id FROM comments WHERE id = ? AND deleted_at IS NULL",
        (comment_id,),
    )
    if comment is None:
        raise ApiError(
            CODE_NOT_FOUND, DETAIL_COMMENT_NOT_FOUND, status.HTTP_404_NOT_FOUND
        )
    ensure_resource_permission(
        user, comment["author_id"], COMMENT_DELETE_OWN, COMMENT_DELETE_ANY
    )

    rows = await db.fetch_all(
        "WITH RECURSIVE subtree(id) AS ("
        "SELECT id FROM comments WHERE id = ? "
        "UNION ALL "
        "SELECT c.id FROM comments c JOIN subtree s ON c.parent_id = s.id"
        ") SELECT id FROM subtree",
        (comment_id,),
    )
    ids = [row["id"] for row in rows]

    placeholders = ",".join("?" * len(ids))
    deleted = await db.execute(
        "UPDATE comments SET deleted_at = datetime('now') "
        f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        ids,
    )
    return success(
        {"id": comment_id, "deleted_comments": deleted},
        detail=DETAIL_COMMENT_DELETED,
    )
