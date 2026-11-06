"""Registration, login and the current user endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from api.deps import get_current_user, get_db, get_settings
from api.models import LoginIn, RegisterIn, TokenOut, UserOut
from api.responses import ApiError, success
from core.constants import (
    CODE_EMAIL_EXISTS,
    CODE_INVALID_CREDENTIALS,
    CODE_INVALID_INVITE_CODE,
    DETAIL_EMAIL_EXISTS,
    DETAIL_INVALID_CREDENTIALS,
    DETAIL_INVITE_CODE_CONFLICT,
    DETAIL_INVITE_CODE_INVALID,
)
from core.security import create_access_token
from infra.config import Settings
from infra.db import Database
from infra.passwords import hash_password, verify_password

router = APIRouter()


def _token_response(user: dict, settings: Settings) -> dict:
    token, expires_at = create_access_token(user["id"], user["role"], settings)
    return TokenOut(
        access_token=token,
        expires_at=expires_at,
        user=UserOut(**user),
    ).model_dump()


async def _claim_invite(db: Database, code: str) -> dict:
    """Return a still-usable invite code or raise 400."""
    invite = await db.fetch_one(
        "SELECT code, role FROM invite_codes "
        "WHERE code = ? AND used_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > datetime('now'))",
        (code,),
    )
    if invite is None:
        raise ApiError(
            CODE_INVALID_INVITE_CODE,
            DETAIL_INVITE_CODE_INVALID,
            status.HTTP_400_BAD_REQUEST,
        )
    return invite


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterIn,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    role = "user"
    invite = None
    if payload.invite_code:
        invite = await _claim_invite(db, payload.invite_code.strip())
        role = invite["role"]

    password_hash = await hash_password(payload.password)
    try:
        user = await db.insert_returning(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?) "
            "RETURNING id, email, role, created_at",
            (payload.email, password_hash, role),
        )
    except Exception as exc:  # D1 surfaces SQLite constraint errors as JS exceptions
        if "UNIQUE" in str(exc).upper():
            raise ApiError(
                CODE_EMAIL_EXISTS,
                DETAIL_EMAIL_EXISTS,
                status.HTTP_409_CONFLICT,
            ) from None
        raise

    if invite is not None:
        claimed = await db.execute(
            "UPDATE invite_codes SET used_at = datetime('now'), used_by = ? "
            "WHERE code = ? AND used_at IS NULL",
            (user["id"], invite["code"]),
        )
        if claimed == 0:  # someone else consumed the code in between
            await db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
            raise ApiError(
                CODE_INVALID_INVITE_CODE,
                DETAIL_INVITE_CODE_CONFLICT,
                status.HTTP_409_CONFLICT,
            )

    return success(_token_response(user, settings), status_code=status.HTTP_201_CREATED)


@router.post("/login")
async def login(
    payload: LoginIn,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    user = await db.fetch_one(
        "SELECT id, email, role, created_at, password_hash FROM users WHERE email = ?",
        (payload.email,),
    )
    if user is None or not await verify_password(
        payload.password, user["password_hash"]
    ):
        raise ApiError(
            CODE_INVALID_CREDENTIALS,
            DETAIL_INVALID_CREDENTIALS,
            status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
    user.pop("password_hash", None)
    return success(_token_response(user, settings))


@router.get("/me")
async def me(user: dict = Depends(get_current_user)) -> JSONResponse:
    return success(UserOut(**user).model_dump())
