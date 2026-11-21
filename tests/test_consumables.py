"""消耗品：以周期为主，日常只点「用完了」。"""

from __future__ import annotations

from datetime import date, timedelta

from core.constants import CODE_CONFLICT, CODE_NOT_FOUND, CODE_VALIDATION_ERROR


def _bearer(api, email: str = "consume@example.com"):
    user_id = api.seed_user(email)
    return user_id, api.token(user_id)


def _create(api, token: str, **overrides):
    payload = {"name": "垃圾袋", "category": "clean", "unit": "只", "quantity": 60}
    payload.update(overrides)
    return api.post("/api/consumables", payload, token=token)


def test_consumables_require_a_token(api):
    assert api.get("/api/consumables").status == 401
    assert api.post("/api/consumables", {"name": "纸巾"}).status == 401


def test_create_opens_the_first_cycle(api):
    _, token = _bearer(api)
    today = date.today().isoformat()
    response = _create(api, token, started_on=today, expected_days=60)
    assert response.status == 201
    data = response.data
    assert data["cycle"]["started_on"] == today
    assert data["cycle"]["ended_on"] is None
    # 刚开始用：不进入提醒区间
    assert data["status"] == "ok"
    assert data["days_left"] == 60


def test_without_expected_days_status_is_unknown(api):
    _, token = _bearer(api)
    data = _create(api, token, started_on=date.today().isoformat()).data
    assert data["status"] == "unknown"
    assert data["days_left"] is None
    assert data["due_on"] is None


def test_finishing_closes_the_cycle_and_learns_the_actual_days(api):
    _, token = _bearer(api)
    start = (date.today() - timedelta(days=55)).isoformat()
    item = _create(api, token, started_on=start, expected_days=60).data

    finished = api.post(
        f"/api/consumables/{item['id']}/finish",
        {"ended_on": date.today().isoformat()},
        token=token,
    )
    assert finished.status == 200
    data = finished.data
    # 关轮后没有进行中周期 = 待补货
    assert data["cycle"] is None
    assert data["status"] == "idle"
    assert data["last_days"] == 55
    assert data["suggested_days"] == 55


def test_restock_starts_another_cycle_and_uses_the_suggestion(api):
    _, token = _bearer(api)
    start = (date.today() - timedelta(days=40)).isoformat()
    end = (date.today() - timedelta(days=5)).isoformat()
    item = _create(api, token, started_on=start, expected_days=40).data
    api.post(f"/api/consumables/{item['id']}/finish", {"ended_on": end}, token=token)

    restocked = api.post(
        f"/api/consumables/{item['id']}/cycles",
        {"started_on": end, "expected_days": 50},
        token=token,
    )
    assert restocked.status == 201
    assert restocked.data["status"] == "ok"
    assert restocked.data["cycle"]["expected_days"] == 50

    again = api.post(
        f"/api/consumables/{item['id']}/cycles",
        {"started_on": date.today().isoformat()},
        token=token,
    )
    assert again.status == 409
    assert again.code == CODE_CONFLICT


def test_someone_elses_consumable_is_404(api):
    _, token = _bearer(api)
    stranger = api.seed_user("stranger@example.com")
    stranger_token = api.token(stranger)
    item = _create(api, stranger_token).data
    response = api.patch(f"/api/consumables/{item['id']}", {"name": "改你的"}, token=token)
    assert response.status == 404
    assert response.code == CODE_NOT_FOUND


def test_cycle_dates_must_be_ordered(api):
    _, token = _bearer(api)
    today = date.today().isoformat()
    item = _create(api, token, started_on=today, expected_days=30).data
    cycle_id = item["cycle"]["id"]
    response = api.patch(
        f"/api/consumables/{item['id']}/cycles/{cycle_id}",
        {"ended_on": "2020-01-01"},
        token=token,
    )
    assert response.status == 422
    assert response.code == CODE_VALIDATION_ERROR


def test_correcting_the_start_date_recomputes_usage(api):
    _, token = _bearer(api)
    item = _create(
        api, token, started_on=date.today().isoformat(), expected_days=60
    ).data
    cycle_id = item["cycle"]["id"]
    earlier = (date.today() - timedelta(days=40)).isoformat()
    fixed = api.patch(
        f"/api/consumables/{item['id']}/cycles/{cycle_id}",
        {"started_on": earlier},
        token=token,
    )
    assert fixed.status == 200
    assert fixed.data["days_used"] == 40
    assert fixed.data["days_left"] == 20


def test_undoing_a_finish_reopens_the_cycle(api):
    _, token = _bearer(api)
    today = date.today().isoformat()
    item = _create(api, token, started_on=today, expected_days=30).data
    api.post(f"/api/consumables/{item['id']}/finish", {"ended_on": today}, token=token)
    cycles = api.get(f"/api/consumables/{item['id']}/cycles", token=token).data["items"]
    cycle_id = cycles[0]["id"]
    reopened = api.patch(
        f"/api/consumables/{item['id']}/cycles/{cycle_id}",
        {"ended_on": None},
        token=token,
    )
    assert reopened.status == 200
    assert reopened.data["cycle"]["ended_on"] is None
    assert reopened.data["status"] == "ok"


def test_reopening_a_cycle_is_409_when_another_is_open(api):
    _, token = _bearer(api)
    today = date.today().isoformat()
    item = _create(api, token, started_on=today, expected_days=30).data
    first = item["cycle"]["id"]
    api.post(f"/api/consumables/{item['id']}/finish", {"ended_on": today}, token=token)
    api.post(
        f"/api/consumables/{item['id']}/cycles",
        {"started_on": today, "expected_days": 30},
        token=token,
    )
    response = api.patch(
        f"/api/consumables/{item['id']}/cycles/{first}",
        {"ended_on": None},
        token=token,
    )
    assert response.status == 409
    assert response.code == CODE_CONFLICT


def test_finish_without_an_open_cycle_is_409(api):
    _, token = _bearer(api)
    today = date.today().isoformat()
    item = _create(api, token, started_on=today, expected_days=30).data
    assert api.post(
        f"/api/consumables/{item['id']}/finish", {"ended_on": today}, token=token
    ).status == 200
    again = api.post(
        f"/api/consumables/{item['id']}/finish", {"ended_on": today}, token=token
    )
    assert again.status == 409
    assert again.code == CODE_CONFLICT


def test_create_uses_the_client_day_for_the_first_cycle(api):
    _, token = _bearer(api)
    data = api.post(
        "/api/consumables?today=2026-01-02",
        {"name": "洗衣液", "expected_days": 10},
        token=token,
    ).data
    assert data["cycle"]["started_on"] == "2026-01-02"
    # 返回结果也按客户端这天算，不是服务端 UTC 今天
    assert data["days_used"] == 0


def test_list_only_returns_the_callers_items(api):
    user_id, token = _bearer(api)
    stranger = api.seed_user("stranger@example.com")
    _create(api, token, name="我的纸巾")
    _create(api, api.token(stranger), name="别人的纸巾")
    rows = api.get(f"/api/consumables?today={date.today().isoformat()}", token=token).data[
        "items"
    ]
    assert [row["name"] for row in rows] == ["我的纸巾"]
    assert user_id


def test_deleted_items_leave_the_list(api):
    _, token = _bearer(api)
    item = _create(api, token).data
    assert api.delete(f"/api/consumables/{item['id']}", token=token).status == 200
    rows = api.get("/api/consumables", token=token).data["items"]
    assert rows == []
