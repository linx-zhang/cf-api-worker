"""Blog post endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from api.deps import (
    ensure_resource_permission,
    get_current_user,
    get_db,
    pagination,
    require_permission,
)
from api.models import PostBatchDeleteIn, PostIn, PostOut, PostUpdateIn
from api.responses import ApiError, split_page, success
from core.constants import (
    CODE_INTERNAL_ERROR,
    CODE_NOT_FOUND,
    DETAIL_POST_CREATE_FAILED,
    DETAIL_POST_DELETED,
    DETAIL_POST_NOT_FOUND,
    DETAIL_POSTS_DELETED,
)
from core.permissions import (
    POST_CREATE_ANY,
    POST_DELETE_ANY,
    POST_DELETE_OWN,
    POST_UPDATE_ANY,
    POST_UPDATE_OWN,
)
from core.utils import author_display_name, profile_display_name
from infra.db import Database

router = APIRouter()

_POST_SELECT = (
    "SELECT p.id, p.author_id, u.email AS author_email, "
    "u.deleted_at AS author_deleted_at, u.nickname AS author_nickname, "
    "u.first_name AS author_first_name, u.last_name AS author_last_name, "
    "p.title, p.body_md, p.created_at, p.updated_at FROM posts p "
    "JOIN users u ON u.id = p.author_id WHERE p.deleted_at IS NULL"
)


def _post_out(row: dict) -> dict:
    """Map a joined row to the public shape (no email, only a display name)."""
    data = dict(row)
    data["author_name"] = author_display_name(data)
    return PostOut(**data).model_dump()


async def _get_post(db: Database, post_id: int) -> dict:
    post = await db.fetch_one(_POST_SELECT + " AND p.id = ?", (post_id,))
    if post is None:
        raise ApiError(CODE_NOT_FOUND, DETAIL_POST_NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return post


async def _soft_delete_posts(
    db: Database, post_ids: list[int]
) -> tuple[list[int], int]:
    """Soft delete live posts and their comments in bulk.

    Returns the ids that were actually deleted (missing or already deleted ids
    are simply absent) and the number of comments removed along with them.
    """
    marks = ", ".join("?" for _ in post_ids)
    rows = await db.fetch_all(
        "UPDATE posts SET deleted_at = datetime('now') "
        f"WHERE id IN ({marks}) AND deleted_at IS NULL RETURNING id",
        post_ids,
    )
    deleted_ids = [int(row["id"]) for row in rows]
    if not deleted_ids:
        return [], 0
    inner = ", ".join("?" for _ in deleted_ids)
    deleted_comments = await db.execute(
        "UPDATE comments SET deleted_at = datetime('now') "
        f"WHERE post_id IN ({inner}) AND deleted_at IS NULL",
        deleted_ids,
    )
    return deleted_ids, deleted_comments


_POST_LIST_FILTERS = "SELECT p.id, p.title FROM posts p JOIN users u ON u.id = p.author_id WHERE p.deleted_at IS NULL"


def _filter_conditions(q: str | None, author: str | None) -> tuple[list[str], list[object]]:
    """Shared ``q`` / ``author`` predicates so list and neighbours never drift."""
    conditions: list[str] = []
    params: list[object] = []
    keyword = (q or "").strip()
    if keyword:
        conditions.append("p.title LIKE ?")
        params.append(f"%{keyword}%")
    author_keyword = (author or "").strip()
    if author_keyword:
        # 作者既能按邮箱/昵称搜，也能按「名 姓」搜
        conditions.append(
            "(u.email LIKE ? OR u.nickname LIKE ? "
            "OR (COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')) LIKE ?)"
        )
        like = f"%{author_keyword}%"
        params.extend([like, like, like])
    return conditions, params


@router.get("")
async def list_posts(
    page: tuple[int, int] = Depends(pagination),
    q: str | None = Query(None, max_length=64),
    author: str | None = Query(None, max_length=64),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """List posts; ``q`` is a fuzzy title filter, ``author`` a fuzzy author filter.

    Returns ``{items, has_next}`` — see ``split_page`` in ``api/responses.py``
    for the paging standard.
    """
    limit, offset = page
    conditions, params = _filter_conditions(q, author)
    where = "".join(f" AND {condition}" for condition in conditions)
    posts = await db.fetch_all(
        _POST_SELECT
        + where
        + " ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?",
        (*params, limit + 1, offset),
    )
    rows, has_next = split_page(posts, limit)
    return success({"items": [_post_out(post) for post in rows], "has_next": has_next})


@router.get("/{post_id}")
async def get_post(post_id: int, db: Database = Depends(get_db)) -> JSONResponse:
    return success(_post_out(await _get_post(db, post_id)))


@router.get("/{post_id}/neighbors")
async def get_post_neighbors(
    post_id: int,
    q: str | None = Query(None, max_length=64),
    author: str | None = Query(None, max_length=64),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Neighbouring posts in the *currently filtered* list order.

    ``prev`` is the newer post (earlier in the list) and ``next`` the older one,
    matching how the list page reads top to bottom. ``index`` is the 0-based
    position in the whole list and is only returned when no filter is active —
    a filtered page number would need its own COUNT over the same predicates.
    When a filter is active and this post itself does not match it, both
    neighbours are null so the detail page hides the navigation instead of
    jumping outside the reader's own result set.
    """
    post = await _get_post(db, post_id)
    created_at = post["created_at"]
    pid = post["id"]
    conditions, filter_params = _filter_conditions(q, author)
    where = "".join(f" AND {condition}" for condition in conditions)
    params = (*filter_params, created_at, created_at, pid)

    newer = await db.fetch_one(
        _POST_LIST_FILTERS
        + where
        + " AND (p.created_at > ? OR (p.created_at = ? AND p.id > ?))"
        + " ORDER BY p.created_at ASC, p.id ASC LIMIT 1",
        params,
    )
    older = await db.fetch_one(
        _POST_LIST_FILTERS
        + where
        + " AND (p.created_at < ? OR (p.created_at = ? AND p.id < ?))"
        + " ORDER BY p.created_at DESC, p.id DESC LIMIT 1",
        params,
    )

    in_filter = True
    if conditions:
        in_filter = (
            await db.fetch_one(_POST_LIST_FILTERS + where + " AND p.id = ?", (*filter_params, pid))
            is not None
        )

    index: int | None = None
    if not conditions:
        counted = await db.fetch_one(
            "SELECT COUNT(*) AS newer FROM posts WHERE deleted_at IS NULL "
            "AND (created_at > ? OR (created_at = ? AND id > ?))",
            (created_at, created_at, pid),
        )
        index = int(counted["newer"]) if counted else 0

    return success(
        {
            "index": index,
            "prev": dict(newer) if newer and in_filter else None,
            "next": dict(older) if older and in_filter else None,
        }
    )


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
    post["author_name"] = profile_display_name(
        user.get("nickname"),
        user.get("first_name"),
        user.get("last_name"),
        user["email"],
    )
    return success(PostOut(**post).model_dump(), status_code=status.HTTP_201_CREATED)


@router.post("/batch-delete")
async def batch_delete_posts(
    payload: PostBatchDeleteIn,
    user: dict = Depends(require_permission(POST_DELETE_ANY)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Soft delete up to 50 posts at once, reporting per-id failures."""
    deleted_ids, _ = await _soft_delete_posts(db, payload.ids)
    deleted_set = set(deleted_ids)
    return success(
        {
            "deleted": [pid for pid in payload.ids if pid in deleted_set],
            "failed": [
                {"id": pid, "code": CODE_NOT_FOUND, "detail": DETAIL_POST_NOT_FOUND}
                for pid in payload.ids
                if pid not in deleted_set
            ],
        },
        detail=DETAIL_POSTS_DELETED,
    )


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

    _, deleted_comments = await _soft_delete_posts(db, [post_id])

    return success(
        {"id": post_id, "deleted_comments": deleted_comments},
        detail=DETAIL_POST_DELETED,
    )
