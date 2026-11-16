"""路由层用例：真正走 ASGI 的那一层（认证、权限、归属、分页口径）。

``test_permissions.py`` / ``test_due_at.py`` 只验纯函数；这里验它们挂上去之后
的行为——token 失效链、``own``/``any`` 双码、legacy 分页、admin 守卫。
这些规则此前只活在 ``deps.py`` / ``admin.py`` 的注释里。

用例不碰 workerd：数据库是内存 SQLite + 全量迁移，客户端见 ``conftest.Client``。
"""

from __future__ import annotations

import jwt
import pytest

from api.responses import ApiError
from api.routers.admin import _ensure_role_change_allowed
from core.constants import (
    CODE_CANNOT_CHANGE_OWN_ROLE,
    CODE_FORBIDDEN,
    CODE_LAST_ADMIN_REQUIRED,
    CODE_NOT_FOUND,
    CODE_UNAUTHORIZED,
    CODE_VALIDATION_ERROR,
)

ADMIN = "/api/admin/users"


def _bearer(api, role: str = "user", email: str | None = None) -> tuple[int, str]:
    user_id = api.seed_user(email or f"{role}@example.com", role)
    return user_id, api.token(user_id)


# ---- 认证 -------------------------------------------------------------------


def test_health_check_stays_public(api):
    response = api.get("/")
    assert response.status == 200
    assert response.code == "ok"


def test_me_without_authorization_is_401(api):
    response = api.get("/api/auth/me")
    assert response.status == 401
    assert response.code == CODE_UNAUTHORIZED


def test_me_rejects_a_non_bearer_scheme(api):
    # 只有 "Bearer <token>" 算凭据；scheme 不对不能被当成裸 token 兜底
    _, token = _bearer(api)
    response = api.get("/api/auth/me", authorization=token)
    assert response.status == 401
    assert response.code == CODE_UNAUTHORIZED


def test_me_returns_permissions_of_the_role(api):
    _, token = _bearer(api, "user")
    response = api.get("/api/auth/me", token=token)
    assert response.status == 200
    permissions = response.data["permissions"]
    assert "post:update:own" in permissions
    assert "user:read:any" not in permissions


def test_token_signed_with_another_secret_is_401(api):
    user_id = api.seed_user("forged@example.com")
    bad = jwt.encode(
        {"sub": str(user_id), "iat": 0, "exp": 10**9}, "not-the-secret", algorithm="HS256"
    )
    response = api.get("/api/auth/me", token=bad)
    assert response.status == 401


def test_soft_deleted_user_cannot_use_a_live_token(api):
    user_id, token = _bearer(api)
    api.execute("UPDATE users SET deleted_at = datetime('now') WHERE id = ?", (user_id,))
    assert api.get("/api/auth/me", token=token).status == 401


def test_token_issued_before_invalidation_is_rejected(api):
    # 登出 / 管理员重置密码都会写 token_invalidated_at，比它早签的 token 作废
    user_id, token = _bearer(api)
    api.execute(
        "UPDATE users SET token_invalidated_at = datetime('now', '+30 seconds') "
        "WHERE id = ?",
        (user_id,),
    )
    assert api.get("/api/auth/me", token=token).status == 401


def test_token_issued_after_invalidation_still_works(api):
    user_id, token = _bearer(api)
    api.execute(
        "UPDATE users SET token_invalidated_at = datetime('now', '-30 seconds') "
        "WHERE id = ?",
        (user_id,),
    )
    assert api.get("/api/auth/me", token=token).status == 200


def test_logout_bumps_the_invalidation_stamp(api):
    user_id, token = _bearer(api)
    assert api.post("/api/auth/logout", token=token).status == 200
    stamp = api.conn.execute(
        "SELECT token_invalidated_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()["token_invalidated_at"]
    assert stamp is not None


# ---- 权限 -------------------------------------------------------------------


def test_member_is_forbidden_on_admin_area(api):
    _, token = _bearer(api, "user")
    response = api.get(ADMIN, token=token)
    assert response.status == 403
    assert response.code == CODE_FORBIDDEN


def test_admin_can_list_users(api):
    admin_id, token = _bearer(api, "admin", "admin@example.com")
    response = api.get(ADMIN, token=token)
    assert response.status == 200
    assert response.data["total"] == 1
    assert response.data["items"][0]["id"] == admin_id


def test_admin_cannot_change_own_role(api):
    admin_id, token = _bearer(api, "admin", "admin@example.com")
    response = api.patch(f"{ADMIN}/{admin_id}", {"role": "user"}, token=token)
    assert response.status == 400
    assert response.code == CODE_CANNOT_CHANGE_OWN_ROLE


def test_admin_can_demote_another_admin(api):
    # 两个管理员时降级一个是允许的：actor 自己还在，库里不会变成零个 admin
    actor = api.seed_user("admin@example.com", "admin")
    target = api.seed_user("admin2@example.com", "admin")
    token = api.token(actor)
    response = api.patch(f"{ADMIN}/{target}", {"role": "user"}, token=token)
    assert response.status == 200
    assert response.data["role"] == "user"


def test_role_is_read_from_the_database_not_the_token(api):
    # token 里只有 sub；把库里的 admin 改成 user，同一个 token 立刻失去管理权
    actor = api.seed_user("admin@example.com", "admin")
    api.seed_user("admin2@example.com", "admin")
    token = api.token(actor)
    api.execute("UPDATE users SET role = 'user' WHERE id = ?", (actor,))
    assert api.get(ADMIN, token=token).status == 403


def test_last_admin_guard_is_unreachable_through_the_route(api):
    """``admin_count <= 1`` 在路由层永远不成立：actor 自己就是那一个 admin。

    所以这条 409 分支是防御性的，只能直接调下层函数验证。把它写进用例是为了
    将来若有人改成"统计时排除自己"，会立刻看到行为变了。
    """
    with pytest.raises(ApiError) as caught:
        _ensure_role_change_allowed(
            {"id": 7, "role": "admin"}, "user", actor_id=1, admin_count=1
        )
    assert caught.value.status_code == 409
    assert caught.value.code == CODE_LAST_ADMIN_REQUIRED


# ---- 归属（own / any）-------------------------------------------------------


def test_author_can_patch_own_post(api):
    author, token = _bearer(api)
    post_id = api.seed_post(author)
    response = api.patch(f"/api/posts/{post_id}", {"title": "新标题"}, token=token)
    assert response.status == 200
    assert response.data["title"] == "新标题"


def test_member_cannot_patch_someone_elses_post(api):
    _, token = _bearer(api)
    stranger = api.seed_user("stranger@example.com")
    post_id = api.seed_post(stranger)
    response = api.patch(f"/api/posts/{post_id}", {"title": "改你的"}, token=token)
    assert response.status == 403
    assert response.code == CODE_FORBIDDEN


def test_admin_can_patch_someone_elses_post(api):
    actor = api.seed_user("admin@example.com", "admin")
    stranger = api.seed_user("stranger@example.com")
    post_id = api.seed_post(stranger)
    response = api.patch(
        f"/api/posts/{post_id}", {"title": "管理员改"}, token=api.token(actor)
    )
    assert response.status == 200
    assert response.data["title"] == "管理员改"


def test_patch_post_with_no_field_is_422(api):
    author, token = _bearer(api)
    post_id = api.seed_post(author)
    response = api.patch(f"/api/posts/{post_id}", {}, token=token)
    assert response.status == 422


# ---- 分页口径 ---------------------------------------------------------------


def test_legacy_page_parameter_is_422(api):
    response = api.get("/api/posts?page=2")
    assert response.status == 422
    assert response.code == CODE_VALIDATION_ERROR


def test_limit_above_the_ceiling_is_422(api):
    assert api.get("/api/posts?limit=500").status == 422


def test_list_shape_is_items_and_has_next(api):
    response = api.get("/api/posts?limit=1")
    assert response.status == 200
    assert set(response.data) == {"items", "has_next"}


# ---- 备忘录的 self 语义 ------------------------------------------------------


def test_memos_require_a_token(api):
    assert api.get("/api/memos").status == 401
    assert api.post("/api/memos", {"body": "x"}).status == 401


def test_memo_due_at_must_be_a_valid_instant(api):
    _, token = _bearer(api)
    response = api.post(
        "/api/memos", {"body": "带日期", "due_at": "2026-13-45"}, token=token
    )
    assert response.status == 422


def test_memo_due_at_accepts_a_utc_instant(api):
    _, token = _bearer(api)
    response = api.post(
        "/api/memos", {"body": "带日期", "due_at": "2026-12-01 15:59:59"}, token=token
    )
    assert response.status == 201
    assert response.data["due_at"] == "2026-12-01 15:59:59"


def test_someone_elses_memo_is_404_not_403(api):
    _, token = _bearer(api)
    stranger = api.seed_user("stranger@example.com")
    memo_id = api.seed_memo(stranger)
    response = api.patch(f"/api/memos/{memo_id}", {"body": "改你的"}, token=token)
    assert response.status == 404
    assert response.code == CODE_NOT_FOUND


def test_unchecking_a_memo_clears_done_at(api):
    user_id, token = _bearer(api)
    memo_id = api.seed_memo(user_id)
    checked = api.patch(f"/api/memos/{memo_id}", {"done": True}, token=token)
    assert checked.data["done"] is True
    assert checked.data["done_at"] is not None

    unchecked = api.patch(f"/api/memos/{memo_id}", {"done": False}, token=token)
    # done 与 done_at 必须同时改，否则会留下「没完成却有完成时间」
    assert unchecked.data["done"] is False
    assert unchecked.data["done_at"] is None


def test_list_memos_only_returns_the_callers_rows(api):
    user_id, token = _bearer(api)
    stranger = api.seed_user("stranger@example.com")
    api.seed_memo(user_id, "我的")
    api.seed_memo(stranger, "别人的")
    response = api.get("/api/memos", token=token)
    assert [row["body"] for row in response.data["items"]] == ["我的"]
