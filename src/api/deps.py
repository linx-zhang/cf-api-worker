"""FastAPI dependencies: settings, database access, authentication and
permission-code authorization."""

from __future__ import annotations

from fastapi import Depends, Query, Request, status

from api.responses import ApiError
from core.constants import (
    CODE_FORBIDDEN,
    CODE_UNAUTHORIZED,
    DETAIL_FORBIDDEN,
    DETAIL_UNAUTHORIZED,
)
from core.permissions import has_permission
from core.security import decode_access_token
from infra.config import Settings
from infra.db import Database

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100

_UNAUTHORIZED = ApiError(
    CODE_UNAUTHORIZED,
    DETAIL_UNAUTHORIZED,
    status.HTTP_401_UNAUTHORIZED,
    headers={"WWW-Authenticate": "Bearer"},
)


def get_settings(request: Request) -> Settings:
    return Settings.from_env(request.scope["env"])


def get_db(request: Request) -> Database:
    return Database(request.scope["env"].DB)


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _UNAUTHORIZED
    return token.strip()


async def get_current_user(
    request: Request,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    from jwt import PyJWTError

    token = _bearer_token(request)
    try:
        payload = decode_access_token(token, settings)
        # The role is always read from the database, never from the token.
        user_id = int(payload["sub"])
    except (PyJWTError, KeyError, TypeError, ValueError):
        raise _UNAUTHORIZED from None

    user = await db.fetch_one(
        "SELECT id, email, role, created_at, nickname, first_name, last_name, "
        "avatar_url FROM users WHERE id = ? AND deleted_at IS NULL",
        (user_id,),
    )
    if user is None:
        raise _UNAUTHORIZED
    return user


def _forbidden() -> ApiError:
    return ApiError(CODE_FORBIDDEN, DETAIL_FORBIDDEN, status.HTTP_403_FORBIDDEN)


def require_permission(code: str):
    """Dependency factory: the current user must hold ``code``."""

    async def dependency(user: dict = Depends(get_current_user)) -> dict:
        if not has_permission(user["role"], code):
            raise _forbidden()
        return user

    return dependency


def ensure_resource_permission(
    user: dict, owner_id: int, own_code: str, any_code: str
) -> None:
    """Authorize an ownership-scoped action, raising 403 when not allowed."""
    if owner_id == user["id"] and has_permission(user["role"], own_code):
        return
    if has_permission(user["role"], any_code):
        return
    raise _forbidden()


def pagination(
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
) -> tuple[int, int]:
    """Validate the shared list pagination query parameters."""
    return limit, offset
