"""Uniform response envelope for every endpoint: {"code", "detail", "data"}."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.constants import (
    CODE_HTTP_ERROR,
    CODE_INTERNAL_ERROR,
    CODE_INVALID_EMAIL,
    CODE_INVALID_INVITE_CODE,
    CODE_INVALID_INVITE_ROLE,
    CODE_INVALID_PASSWORD,
    CODE_INVALID_ROLE,
    CODE_METHOD_NOT_ALLOWED,
    CODE_NOT_FOUND,
    CODE_OK,
    CODE_VALIDATION_ERROR,
    DETAIL_INTERNAL_ERROR,
    DETAIL_INVALID_EMAIL,
    DETAIL_INVALID_PASSWORD,
    DETAIL_INVALID_ROLE,
    DETAIL_INVALID_VALUE,
    DETAIL_INVITE_CODE_MALFORMED,
    DETAIL_INVITE_ROLE,
    DETAIL_METHOD_NOT_ALLOWED,
    DETAIL_NOT_FOUND,
    DETAIL_OK,
    DETAIL_VALIDATION_ERROR,
)


class ApiError(Exception):
    """Business error carrying a machine-readable code and a human sentence."""

    def __init__(
        self,
        code: str,
        detail: str,
        status_code: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.headers = headers


def _envelope(
    code: str, detail: str, data: Any, status_code: int, headers=None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "detail": detail, "data": data},
        headers=headers,
    )


def success(
    data: Any = None,
    *,
    status_code: int = 200,
    detail: str = DETAIL_OK,
    code: str = CODE_OK,
) -> JSONResponse:
    return _envelope(code, detail, jsonable_encoder(data), status_code)


_FIELD_ERRORS = {
    "email": (CODE_INVALID_EMAIL, DETAIL_INVALID_EMAIL),
    "password": (CODE_INVALID_PASSWORD, DETAIL_INVALID_PASSWORD),
    "invite_code": (CODE_INVALID_INVITE_CODE, DETAIL_INVITE_CODE_MALFORMED),
    "role": (CODE_INVALID_INVITE_ROLE, DETAIL_INVITE_ROLE),
}


def _validation_summary(errors: list[dict], path: str = "") -> tuple[str, str]:
    first = errors[0] if errors else {}
    loc = [str(part) for part in first.get("loc", ())]
    field = loc[-1] if loc else ""
    if field == "role" and path.startswith("/admin/users/"):
        # Admin user management allows all three roles, unlike invite creation.
        code, template = CODE_INVALID_ROLE, DETAIL_INVALID_ROLE
    else:
        code, template = _FIELD_ERRORS.get(
            field, (CODE_VALIDATION_ERROR, DETAIL_VALIDATION_ERROR)
        )
    message = str(first.get("msg", DETAIL_INVALID_VALUE))
    return code, template.format(reason=message)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _envelope(exc.code, exc.detail, None, exc.status_code, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        code, detail = _validation_summary(exc.errors(), request.url.path)
        return _envelope(code, detail, None, 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 404:
            code, detail = CODE_NOT_FOUND, DETAIL_NOT_FOUND
        elif exc.status_code == 405:
            code, detail = CODE_METHOD_NOT_ALLOWED, DETAIL_METHOD_NOT_ALLOWED
        else:
            code, detail = CODE_HTTP_ERROR, str(exc.detail)
        return _envelope(code, detail, None, exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        return _envelope(CODE_INTERNAL_ERROR, DETAIL_INTERNAL_ERROR, None, 500)
