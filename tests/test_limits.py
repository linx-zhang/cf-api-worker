"""公开写入端点的预算：page_leave 不再是无限通行证，注册先限流后哈希。"""

from __future__ import annotations

import time

from api.routers import auth as auth_router
from core.constants import CODE_RATE_LIMITED


def _event(kind: str, path: str = "/blog", duration_ms: int | None = None) -> dict:
    event = {"type": kind, "path": path, "ts": int(time.time() * 1000)}
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    return event


def _age_rows(api, *, user_id: int | None = None, ip: str | None = None) -> None:
    if user_id is not None:
        api.execute(
            "UPDATE activity_events SET created_at = datetime('now', '-5 seconds') "
            "WHERE user_id = ?",
            (user_id,),
        )
    else:
        api.execute(
            "UPDATE activity_events SET created_at = datetime('now', '-5 seconds') "
            "WHERE ip = ?",
            (ip,),
        )


def test_signed_in_page_leave_gets_one_bypass_then_is_throttled(api):
    user_id = api.seed_user("leave@example.com")
    token = api.token(user_id)

    first = api.post(
        "/api/activity", {"events": [_event("page_view")]}, token=token
    )
    assert first.status == 200

    _age_rows(api, user_id=user_id)

    leave = api.post(
        "/api/activity",
        {"events": [_event("page_leave", duration_ms=5_000)]},
        token=token,
    )
    assert leave.status == 200, "page_leave 应在普通批次之后保留一次"

    repeat = api.post(
        "/api/activity",
        {"events": [_event("page_leave", duration_ms=5_000)]},
        token=token,
    )
    assert repeat.status == 429
    assert repeat.code == CODE_RATE_LIMITED


def test_anonymous_page_leave_gets_one_bypass_then_is_throttled(api):
    headers = {"cf-connecting-ip": "203.0.113.9"}
    first = api.post(
        "/api/activity/visitor",
        {"events": [_event("page_view")]},
        headers=headers,
    )
    assert first.status == 200

    _age_rows(api, ip="203.0.113.9")

    leave = api.post(
        "/api/activity/visitor",
        {"events": [_event("page_leave", duration_ms=5_000)]},
        headers=headers,
    )
    assert leave.status == 200

    repeat = api.post(
        "/api/activity/visitor",
        {"events": [_event("page_leave", duration_ms=5_000)]},
        headers=headers,
    )
    assert repeat.status == 429
    assert repeat.code == CODE_RATE_LIMITED


def test_registration_is_ip_limited_before_hashing(api, monkeypatch):
    hashed: list[str] = []

    async def fake_hash(password: str) -> str:
        hashed.append(password)
        return "fake-hash"

    monkeypatch.setattr(auth_router, "hash_password", fake_hash)
    monkeypatch.setattr(auth_router, "REGISTER_IP_LIMIT", 2)

    headers = {"cf-connecting-ip": "198.51.100.7"}
    for index in range(2):
        response = api.post(
            "/api/auth/register",
            {"email": f"new{index}@example.com", "password": "Mango-Orbit-7421"},
            headers=headers,
        )
        assert response.status == 201

    blocked = api.post(
        "/api/auth/register",
        {"email": "new2@example.com", "password": "Mango-Orbit-7421"},
        headers=headers,
    )
    assert blocked.status == 429
    assert blocked.code == CODE_RATE_LIMITED
    assert len(hashed) == 2, "超限请求不应进入 PBKDF2"

    other_ip = api.post(
        "/api/auth/register",
        {"email": "new3@example.com", "password": "Mango-Orbit-7421"},
        headers={"cf-connecting-ip": "198.51.100.8"},
    )
    assert other_ip.status == 201
