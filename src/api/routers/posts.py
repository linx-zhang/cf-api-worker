"""Blog post endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from api.deps import get_current_user, get_db, pagination, require_admin
from api.models import PostIn, PostOut
from api.responses import ApiError, success
from core.constants import (
    CODE_INTERNAL_ERROR,
    CODE_NOT_FOUND,
    DETAIL_POST_CREATE_FAILED,
    DETAIL_POST_DELETED,
    DETAIL_POST_NOT_FOUND,
)
from infra.db import Database

router = APIRouter()

_POST_SELECT = (
    "SELECT p.id, p.author_id, u.email AS author_email, p.title, p.body_md, "
    "p.created_at, p.updated_at FROM posts p JOIN users u ON u.id = p.author_id "
    "WHERE p.deleted_at IS NULL"
)


async def _get_post(db: Database, post_id: int) -> dict:
    post = await db.fetch_one(_POST_SELECT + " AND p.id = ?", (post_id,))
    if post is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_POST_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return post


@router.get("")
async def list_posts(
    page: tuple[int, int] = Depends(pagination),
    db: Database = Depends(get_db),
) -> JSONResponse:
    limit, offset = page
    posts = await db.fetch_all(
        _POST_SELECT + " ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return success([PostOut(**post).model_dump() for post in posts])


@router.get("/{post_id}")
async def get_post(post_id: int, db: Database = Depends(get_db)) -> JSONResponse:
    return success(PostOut(**await _get_post(db, post_id)).model_dump())


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_post(
    payload: PostIn,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    post = await db.insert_returning(
        "INSERT INTO posts (author_id, title, body_md) VALUES (?, ?, ?) "
        "RETURNING id, author_id, title, body_md, created_at, updated_at",
        (user["id"], payload.title.strip(), payload.body_md),
    )
    if post is None:
        raise ApiError(
            CODE_INTERNAL_ERROR,
            DETAIL_POST_CREATE_FAILED,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    post["author_email"] = user["email"]
    return success(PostOut(**post).model_dump(), status_code=status.HTTP_201_CREATED)


@router.delete("/{post_id}")
async def delete_post(
    post_id: int,
    admin: dict = Depends(require_admin),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Hard delete a post together with all of its comments (admin only)."""
    post = await db.fetch_one("SELECT id FROM posts WHERE id = ?", (post_id,))
    if post is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_POST_NOT_FOUND, status.HTTP_404_NOT_FOUND)

    deleted_comments = await db.execute(
        "DELETE FROM comments WHERE post_id = ?", (post_id,)
    )
    deleted_posts = await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    if deleted_posts == 0:
        raise ApiError(CODE_NOT_FOUND, DETAIL_POST_NOT_FOUND, status.HTTP_404_NOT_FOUND)

    return success(
        {"id": post_id, "deleted_comments": deleted_comments},
        detail=DETAIL_POST_DELETED,
    )
