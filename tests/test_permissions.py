"""权限码矩阵：码的集合、角色之间的一致性、以及 PUBLIC 的边界。

这些断言故意只依赖 ``core.permissions``（无框架依赖），所以不需要
workerd / pywrangler 也能跑。前端 Admin 页靠同一批码做分区守卫，
一旦有人给角色加码却漏了另一处，这里会先红。
"""

import re

import pytest

from core import permissions as perms

# 形如 ``post:read:any``：三段的资源-动作-范围
CODE_SHAPE = re.compile(r"^[a-z_]+:[a-z_]+:[a-z_]+$")


def _codes(role: str) -> set[str]:
    return set(perms.permissions_for_role(role))


def test_every_role_has_only_well_formed_codes():
    for role in ("user", "internal", "admin"):
        for code in perms.ROLE_PERMISSIONS[role]:
            assert CODE_SHAPE.match(code), f"{role} 里出现不合形状的码: {code}"


def test_no_duplicate_codes_inside_a_role():
    for role in ("user", "internal", "admin"):
        codes = list(perms.ROLE_PERMISSIONS[role])
        assert len(codes) == len(set(codes)), f"{role} 里有重复码"


def test_admin_is_a_superset_of_the_member_set():
    assert _codes("user") <= _codes("admin")
    assert _codes("internal") <= _codes("admin")


def test_member_roles_are_identical_today():
    # internal 目前和 user 同权；不是设计目标，但换了要显式改测试，避免静默漂移
    assert _codes("user") == _codes("internal")


@pytest.mark.parametrize(
    "code",
    [
        perms.POST_UPDATE_ANY,
        perms.POST_DELETE_ANY,
        perms.COMMENT_DELETE_ANY,
        perms.INVITE_CODE_CREATE_ANY,
        perms.INVITE_CODE_READ_ANY,
        perms.INVITE_CODE_DELETE_ANY,
        perms.USER_READ_ANY,
        perms.USER_UPDATE_ANY,
        perms.USER_DELETE_ANY,
        perms.USER_RESET_PASSWORD_ANY,
        perms.ACTIVITY_READ_ANY,
        perms.CLIP_DELETE_ANY,
    ],
)
def test_admin_only_codes_never_leak_to_members(code):
    assert code in _codes("admin")
    assert code not in _codes("user")
    assert code not in _codes("internal")


@pytest.mark.parametrize(
    "code",
    [
        perms.ACCOUNT_READ_SELF,
        perms.ACCOUNT_UPDATE_SELF,
        perms.POST_CREATE_ANY,
        perms.POST_UPDATE_OWN,
        perms.POST_DELETE_OWN,
        perms.COMMENT_CREATE_ANY,
        perms.COMMENT_DELETE_OWN,
        perms.ACTIVITY_WRITE_SELF,
        perms.CLIP_DELETE_OWN,
    ],
)
def test_self_scoped_codes_are_granted_to_every_signed_in_role(code):
    for role in ("user", "internal", "admin"):
        assert perms.has_permission(role, code), f"{role} 缺少 {code}"


def test_public_role_sees_only_public_read_codes():
    assert set(perms.PUBLIC_PERMISSIONS) == {perms.POST_READ_ANY, perms.COMMENT_READ_ANY}
    for code in perms.PUBLIC_PERMISSIONS:
        assert perms.has_permission(perms.PUBLIC, code)
    # 公开角色不能读到任何管理码，也不能写
    for code in _codes("admin"):
        if code in perms.PUBLIC_PERMISSIONS:
            continue
        assert not perms.has_permission(perms.PUBLIC, code), code


def test_public_permissions_are_not_merged_into_signed_in_roles():
    for role in ("user", "internal", "admin"):
        assert not set(perms.PUBLIC_PERMISSIONS) & _codes(role)


def test_unknown_role_gets_nothing():
    assert perms.permissions_for_role("superuser") == []
    assert not perms.has_permission("superuser", perms.POST_READ_ANY)


def test_permissions_for_role_returns_a_copy():
    codes = perms.permissions_for_role("admin")
    codes.append("planted:code:any")
    assert "planted:code:any" not in perms.ROLE_PERMISSIONS["admin"]
