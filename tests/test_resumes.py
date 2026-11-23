"""Resume CRUD, default switching and time-limited public shares.

The route tests run against the same migrations as production, so the partial
unique index and the live-pointer share semantics are exercised for real.
"""

from __future__ import annotations

from core.constants import (
    CODE_CONFLICT,
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    CODE_VALIDATION_ERROR,
)

RESUMES = "/api/resumes"


def _admin(api):
    user_id = api.seed_user("resume-admin@example.com", "admin")
    return api.token(user_id)


def _create(api, token, title="Linx 简历", html="<section class=\"resume-section\">正文</section>"):
    return api.post(
        RESUMES,
        {"title": title, "note": "给猎头", "content_html": html},
        token=token,
    )


def test_member_cannot_touch_resume_admin(api):
    user_id = api.seed_user("member@example.com", "user")
    token = api.token(user_id)
    assert api.get(RESUMES, token=token).status == 403
    assert api.post(RESUMES, {"title": "x", "content_html": "<p>x</p>"}, token=token).status == 403


def test_admin_crud_and_copy(api):
    token = _admin(api)
    created = _create(api, token)
    assert created.status == 201
    resume_id = created.data["id"]
    assert created.data["is_default"] is False
    assert created.data["content_html"].startswith("<section")

    listed = api.get(RESUMES, token=token)
    assert listed.status == 200
    assert listed.data["total"] == 1
    assert "content_html" not in listed.data["items"][0]

    detail = api.get(f"{RESUMES}/{resume_id}", token=token)
    assert detail.status == 200
    assert detail.data["note"] == "给猎头"

    updated = api.patch(
        f"{RESUMES}/{resume_id}",
        {"note": "更新备注", "content_html": "<p>新正文</p>"},
        token=token,
    )
    assert updated.status == 200
    assert updated.data["note"] == "更新备注"
    assert updated.data["content_html"] == "<p>新正文</p>"

    copied = api.post(f"{RESUMES}/{resume_id}/copy", token=token)
    assert copied.status == 201
    assert copied.data["title"].endswith("副本")
    assert copied.data["content_html"] == "<p>新正文</p>"
    assert copied.data["is_default"] is False


def test_current_is_404_until_a_default_exists(api):
    token = _admin(api)
    assert api.get(f"{RESUMES}/current").status == 404

    first = _create(api, token, "第一版").data
    second = _create(api, token, "第二版").data
    assert api.post(f"{RESUMES}/{first['id']}/default", token=token).status == 200

    current = api.get(f"{RESUMES}/current")
    assert current.status == 200
    assert current.data["title"] == "第一版"

    assert api.post(f"{RESUMES}/{second['id']}/default", token=token).status == 200
    assert api.get(f"{RESUMES}/current").data["title"] == "第二版"

    rows = api.get(RESUMES, token=token).data["items"]
    defaults = [row for row in rows if row["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["title"] == "第二版"


def test_default_resume_cannot_be_deleted(api):
    token = _admin(api)
    resume = _create(api, token).data
    assert api.post(f"{RESUMES}/{resume['id']}/default", token=token).status == 200
    response = api.delete(f"{RESUMES}/{resume['id']}", token=token)
    assert response.status == 409
    assert response.code == CODE_CONFLICT

    # 先设别的为默认，再删原来的默认
    other = _create(api, token, "备用").data
    assert api.post(f"{RESUMES}/{other['id']}/default", token=token).status == 200
    assert api.delete(f"{RESUMES}/{resume['id']}", token=token).status == 200
    assert api.get(f"{RESUMES}/{resume['id']}", token=token).status == 404


def test_share_is_live_and_can_be_revoked(api):
    token = _admin(api)
    resume = _create(api, token, "对外版本").data
    share = api.post(
        f"{RESUMES}/{resume['id']}/shares", {"expires_in_minutes": 60}, token=token
    )
    assert share.status == 201
    code = share.data["code"]
    assert len(code) == 12

    public = api.get(f"{RESUMES}/shared/{code}")
    assert public.status == 200
    assert public.data["title"] == "对外版本"
    assert "note" not in public.data
    assert "id" not in public.data

    # 活指针：后台改了正文，同一个链接立刻读到新内容
    api.patch(f"{RESUMES}/{resume['id']}", {"content_html": "<p>更新后</p>"}, token=token)
    assert api.get(f"{RESUMES}/shared/{code}").data["content_html"] == "<p>更新后</p>"

    assert api.delete(f"{RESUMES}/shares/{code}", token=token).status == 200
    assert api.get(f"{RESUMES}/shared/{code}").status == 404


def test_expired_and_deleted_share_are_404(api):
    token = _admin(api)
    resume = _create(api, token).data
    share = api.post(
        f"{RESUMES}/{resume['id']}/shares", {"expires_in_minutes": 1}, token=token
    ).data

    api.execute(
        "UPDATE resume_shares SET expires_at = datetime('now', '-1 minute') WHERE code = ?",
        (share["code"],),
    )
    assert api.get(f"{RESUMES}/shared/{share['code']}").status == 404

    other = _create(api, token, "删除后失效").data
    live = api.post(
        f"{RESUMES}/{other['id']}/shares", {"expires_in_minutes": 60}, token=token
    ).data
    assert api.delete(f"{RESUMES}/{other['id']}", token=token).status == 200
    assert api.get(f"{RESUMES}/shared/{live['code']}").status == 404


def test_malformed_share_code_is_404(api):
    response = api.get(f"{RESUMES}/shared/not-a-code")
    assert response.status == 404
    assert response.code == CODE_NOT_FOUND


def test_resume_payload_limits_and_legacy_paging(api):
    token = _admin(api)
    too_big = api.post(
        RESUMES,
        {"title": "x", "content_html": "a" * 50_001},
        token=token,
    )
    assert too_big.status == 422
    assert too_big.code == CODE_VALIDATION_ERROR

    assert api.get(f"{RESUMES}?page=2", token=token).status == 422
    assert api.get(f"{RESUMES}?limit=0", token=token).status == 422


def test_member_is_forbidden_on_admin_create(api):
    user_id = api.seed_user("blocked@example.com", "internal")
    token = api.token(user_id)
    response = api.post(
        RESUMES, {"title": "x", "content_html": "<p>x</p>"}, token=token
    )
    assert response.status == 403
    assert response.code == CODE_FORBIDDEN
