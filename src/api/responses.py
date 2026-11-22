"""Uniform response envelope for every endpoint: {"code", "detail", "data"}."""

from __future__ import annotations

import re
import traceback
import uuid
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
    CODE_INVALID_EVENT_TYPE,
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
    DETAIL_INVALID_EVENT_TYPE,
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


def split_page(rows: list[dict], limit: int) -> tuple[list[dict], bool]:
    """The one paging standard for this API.

    Callers fetch ``limit + 1`` rows; this drops the extra one and reports
    whether a further page exists. The probe row is fetched from D1 but never
    serialized, so a page costs a single statement and no wasted payload —
    never a second COUNT, and never a ``limit + 1`` trick in the client.
    """
    return rows[:limit], len(rows) > limit


_FIELD_ERRORS = {
    "email": (CODE_INVALID_EMAIL, DETAIL_INVALID_EMAIL),
    "password": (CODE_INVALID_PASSWORD, DETAIL_INVALID_PASSWORD),
    "invite_code": (CODE_INVALID_INVITE_CODE, DETAIL_INVITE_CODE_MALFORMED),
    "role": (CODE_INVALID_INVITE_ROLE, DETAIL_INVITE_ROLE),
    "type": (CODE_INVALID_EVENT_TYPE, DETAIL_INVALID_EVENT_TYPE),
}


def _validation_summary(errors: list[dict], path: str = "") -> tuple[str, str]:
    first = errors[0] if errors else {}
    loc = [str(part) for part in first.get("loc", ())]
    field = loc[-1] if loc else ""
    if first.get("type") == "extra_forbidden":
        # An unknown field (e.g. email/role on the self-service profile patch)
        # is not a field-level value error, so report it as a plain 422.
        message = str(first.get("msg", DETAIL_INVALID_VALUE))
        return CODE_VALIDATION_ERROR, DETAIL_VALIDATION_ERROR.format(reason=message)
    # 路径带 /api 前缀，所以用包含判断而不是 startswith：前缀再调整也不会漏。
    if field == "role" and "/admin/users/" in path:
        # Admin user management allows all three roles, unlike invite creation.
        code, template = CODE_INVALID_ROLE, DETAIL_INVALID_ROLE
    else:
        code, template = _FIELD_ERRORS.get(
            field, (CODE_VALIDATION_ERROR, DETAIL_VALIDATION_ERROR)
        )
    message = str(first.get("msg", DETAIL_INVALID_VALUE))
    return code, template.format(reason=message)


_REQUEST_ID_SAFE = re.compile(r"[^A-Za-z0-9._:-]+")


def request_id(request: Request) -> str:
    """Correlate a 500 with the Worker log without trusting client input.

    Cloudflare adds ``CF-Ray`` to every request, so it is the primary id.
    ``X-Request-Id`` is accepted for callers that already have one; both are
    reduced to a safe header subset and capped. Local tests get a generated id.
    """
    for name in ("cf-ray", "x-request-id"):
        value = request.headers.get(name)
        if value:
            safe = _REQUEST_ID_SAFE.sub("", value)[:128]
            if safe:
                return safe
    return f"local-{uuid.uuid4().hex[:16]}"


def log_unhandled(request: Request, exc: Exception) -> str:
    """Print one structured, greppable block and return the request id.

    Worker logs are the only durable trace of a caught exception; FastAPI's
    generic 500 handler otherwise swallows both the type and the stack.
    """
    rid = request_id(request)
    stack = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).strip()
    print(
        f"[api-worker] unhandled request_id={rid} "
        f"method={request.method} path={request.url.path} "
        f"exception={type(exc).__name__}: {exc}\n{stack}",
        flush=True,
    )
    return rid


async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    rid = log_unhandled(request, exc)
    return _envelope(
        CODE_INTERNAL_ERROR,
        DETAIL_INTERNAL_ERROR,
        None,
        500,
        {"x-request-id": rid},
    )


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

    app.add_exception_handler(Exception, unhandled_error)
