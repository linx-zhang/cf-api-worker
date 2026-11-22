"""500 响应必须留痕：日志里能找到请求 id，响应头能把日志和请求对上。"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from api.responses import unhandled_error


def _request(*, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/boom",
        "raw_path": b"/api/boom",
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_unhandled_500_logs_the_stack_and_returns_cf_ray(capsys):
    request = _request(headers=[(b"cf-ray", b"abc123-HKG")])
    response = asyncio.run(unhandled_error(request, RuntimeError("d1 exploded")))

    body = json.loads(response.body)
    assert response.status_code == 500
    assert body["code"] == "internal_error"
    assert response.headers["x-request-id"] == "abc123-HKG"

    logged = capsys.readouterr().out
    assert "request_id=abc123-HKG" in logged
    assert "path=/api/boom" in logged
    assert "RuntimeError: d1 exploded" in logged


def test_request_id_is_sanitized_and_falls_back_when_absent():
    request = _request(headers=[(b"cf-ray", b"bad value-HKG")])
    assert asyncio.run(unhandled_error(request, RuntimeError("x"))).headers[
        "x-request-id"
    ] == "badvalue-HKG"

    generated = asyncio.run(unhandled_error(_request(), RuntimeError("x"))).headers[
        "x-request-id"
    ]
    assert generated.startswith("local-")
