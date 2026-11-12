"""Registration, login and the current user endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from api.deps import get_current_user, get_db, get_settings, require_permission
from api.models import (
    PROFILE_FIELDS,
    LoginIn,
    MeOut,
    ProfileUpdateIn,
    RegisterIn,
    TokenOut,
    UserOut,
)
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
from core.permissions import ACCOUNT_UPDATE_SELF, permissions_for_role
from core.security import create_access_token
from infra.config import Settings
from infra.db import Database
from infra.passwords import hash_password, verify_password
from services.invite_codes import UNUSED_CONDITION

router = APIRouter()

_USER_COLUMNS = (
    "id, email, role, created_at, nickname, first_name, last_name, avatar_url"
)


def _token_response(user: dict, settings: Settings) -> dict:
    token, expires_at = create_access_token(user["id"], settings)
    return TokenOut(
        access_token=token,
        expires_at=expires_at,
        user=UserOut(**user),
    ).model_dump()


async def _claim_invite(db: Database, code: str) -> dict:
    """Return a still-usable invite code or raise 400."""
    invite = await db.fetch_one(
        f"SELECT code, role FROM invite_codes WHERE code = ? AND {UNUSED_CONDITION}",
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
            f"RETURNING {_USER_COLUMNS}",
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
        f"SELECT {_USER_COLUMNS}, password_hash FROM users "
        "WHERE email = ? AND deleted_at IS NULL",
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
    return success(
        MeOut(**user, permissions=permissions_for_role(user["role"])).model_dump()
    )


@router.post("/logout")
async def logout(
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """End the caller's live sessions: tokens issued before now stop working."""
    await db.execute(
        "UPDATE users SET token_invalidated_at = datetime('now') WHERE id = ?",
        (user["id"],),
    )
    return success({"id": user["id"]}, detail="logout")


@router.patch("/me")
async def update_me(
    payload: ProfileUpdateIn,
    user: dict = Depends(require_permission(ACCOUNT_UPDATE_SELF)),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """Update the caller's own profile; email and role are not editable here."""
    provided = [field for field in PROFILE_FIELDS if field in payload.model_fields_set]
    assignments = ", ".join(f"{field} = ?" for field in provided)
    params = [getattr(payload, field) for field in provided]
    params.append(user["id"])
    await db.execute(
        f"UPDATE users SET {assignments} WHERE id = ? AND deleted_at IS NULL",
        params,
    )
    updated = await db.fetch_one(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id = ? AND deleted_at IS NULL",
        (user["id"],),
    )
    return success(
        MeOut(**updated, permissions=permissions_for_role(updated["role"])).model_dump()
    )
