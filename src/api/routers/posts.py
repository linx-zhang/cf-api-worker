"""Blog post endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from api.deps import (
    ensure_resource_permission,
    get_current_user,
    get_db,
    pagination,
    require_permission,
)
from api.models import PostIn, PostOut, PostUpdateIn
from api.responses import ApiError, success
from core.constants import (
    CODE_INTERNAL_ERROR,
    CODE_NOT_FOUND,
    DELETED_AUTHOR_NAME,
    DETAIL_POST_CREATE_FAILED,
    DETAIL_POST_DELETED,
    DETAIL_POST_NOT_FOUND,
)
from core.permissions import (
    POST_CREATE_ANY,
    POST_DELETE_ANY,
    POST_DELETE_OWN,
    POST_UPDATE_ANY,
    POST_UPDATE_OWN,
)
from core.utils import display_name
from infra.db import Database

router = APIRouter()

_POST_SELECT = (
    "SELECT p.id, p.author_id, u.email AS author_email, "
    "u.deleted_at AS author_deleted_at, p.title, p.body_md, "
    "p.created_at, p.updated_at FROM posts p JOIN users u ON u.id = p.author_id "
    "WHERE p.deleted_at IS NULL"
)


def _post_out(row: dict) -> dict:
    """Map a joined row to the public shape (no email, only a display name)."""
    data = dict(row)
    email = data.pop("author_email", "") or ""
    deleted = data.pop("author_deleted_at", None)
    data["author_name"] = DELETED_AUTHOR_NAME if deleted else display_name(email)
    return PostOut(**data).model_dump()


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
    return success([_post_out(post) for post in posts])


@router.get("/{post_id}")
async def get_post(post_id: int, db: Database = Depends(get_db)) -> JSONResponse:
    return success(_post_out(await _get_post(db, post_id)))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_post(
    payload: PostIn,
    user: dict = Depends(require_permission(POST_CREATE_ANY)),
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
    post["author_name"] = display_name(user["email"])
    return success(PostOut(**post).model_dump(), status_code=status.HTTP_201_CREATED)


@router.patch("/{post_id}")
async def update_post(
    post_id: int,
    payload: PostUpdateIn,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Update a post: the author needs ``post:update:own``, admins ``any``."""
    post = await _get_post(db, post_id)
    ensure_resource_permission(
        user, post["author_id"], POST_UPDATE_OWN, POST_UPDATE_ANY
    )

    assignments: list[str] = []
    params: list[object] = []
    if payload.title is not None:
        assignments.append("title = ?")
        params.append(payload.title.strip())
    if payload.body_md is not None:
        assignments.append("body_md = ?")
        params.append(payload.body_md)
    params.append(post_id)

    await db.execute(
        f"UPDATE posts SET {', '.join(assignments)}, "
        "updated_at = datetime('now') WHERE id = ? AND deleted_at IS NULL",
        params,
    )
    return success(_post_out(await _get_post(db, post_id)))


@router.delete("/{post_id}")
async def delete_post(
    post_id: int,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Soft delete a post and all of its comments."""
    post = await _get_post(db, post_id)
    ensure_resource_permission(
        user, post["author_id"], POST_DELETE_OWN, POST_DELETE_ANY
    )

    deleted_comments = await db.execute(
        "UPDATE comments SET deleted_at = datetime('now') "
        "WHERE post_id = ? AND deleted_at IS NULL",
        (post_id,),
    )
    await db.execute(
        "UPDATE posts SET deleted_at = datetime('now') "
        "WHERE id = ? AND deleted_at IS NULL",
        (post_id,),
    )

    return success(
        {"id": post_id, "deleted_comments": deleted_comments},
        detail=DETAIL_POST_DELETED,
    )
