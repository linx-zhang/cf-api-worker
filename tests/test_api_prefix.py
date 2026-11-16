"""路径前缀与错误码映射的护栏。

`/api` 前缀是前后端唯一靠约定对齐的东西（没有编译期检查），所以这里用测试
把它钉住：改 app.py 的挂载方式，这一组必须跟着改，否则 CI 会红。
"""

from __future__ import annotations

from api.app import API_PREFIX, app
from api.responses import _validation_summary


def _paths() -> set[str]:
    # app.routes 里是延迟展开的 _IncludedRouter，取 OpenAPI 的真实路径最省事
    return set(app.openapi()["paths"])


def test_business_routes_live_under_api_prefix():
    paths = _paths()
    assert f"{API_PREFIX}/posts" in paths
    assert f"{API_PREFIX}/posts/{{post_id}}" in paths
    assert f"{API_PREFIX}/activity/visitor" in paths


def test_only_platform_routes_sit_outside_prefix():
    # 根路径是健康检查；其余（含 FastAPI 自带 /docs、/openapi.json）都不该是业务端点。
    business = {
        path
        for path in _paths()
        if path.startswith("/") and not path.startswith(API_PREFIX)
    }
    assert business - {"/", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"} == set()


def test_admin_role_validation_recognised_under_prefix():
    """`responses.py` 用路径判断「管理员改角色」这个特例，前缀变了也不能失效。"""
    errors = [{"loc": ("body", "role"), "msg": "bad", "type": "literal_error"}]
    code, _ = _validation_summary(errors, f"{API_PREFIX}/admin/users/3")
    assert code != "validation_error"
    code_invite, _ = _validation_summary(errors, f"{API_PREFIX}/admin/invite-codes")
    assert code_invite != code  # 邀请码和用户管理用的是两组错误码
