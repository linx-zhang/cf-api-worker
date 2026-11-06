"""FastAPI dependencies: settings, database access and the authenticated user."""

from __future__ import annotations

from fastapi import Depends, Query, Request, status

from api.responses import ApiError
from core.constants import (
    CODE_FORBIDDEN,
    CODE_UNAUTHORIZED,
    DETAIL_FORBIDDEN,
    DETAIL_UNAUTHORIZED,
)
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
        user_id = int(payload["sub"])
    except (PyJWTError, KeyError, TypeError, ValueError):
        raise _UNAUTHORIZED from None

    user = await db.fetch_one(
        "SELECT id, email, role, created_at FROM users WHERE id = ?", (user_id,)
    )
    if user is None:
        raise _UNAUTHORIZED
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise ApiError(
            CODE_FORBIDDEN,
            DETAIL_FORBIDDEN,
            status.HTTP_403_FORBIDDEN,
        )
    return user


def pagination(
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
) -> tuple[int, int]:
    """Validate the shared list pagination query parameters."""
    return limit, offset
