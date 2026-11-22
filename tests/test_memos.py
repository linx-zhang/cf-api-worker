"""月历取数窗口：只拿当前网格的到期条目，未排到期日单独取一页预览。"""

from __future__ import annotations

from urllib.parse import urlencode


def _token(api, email: str = "calendar@example.com") -> str:
    user_id = api.seed_user(email)
    return api.token(user_id)


def _create(api, token: str, due_at: str | None, body: str = "条目") -> int:
    response = api.post("/api/memos", {"body": body, "due_at": due_at}, token=token)
    assert response.status == 201
    return int(response.data["id"])


def _list(api, token: str, **params):
    query = urlencode(params)
    return api.get(f"/api/memos?{query}", token=token)


def test_due_range_returns_only_the_visible_window(api):
    token = _token(api)
    october = _create(api, token, "2026-10-31 15:59:59", "十月末")
    november = _create(api, token, "2026-11-15 15:59:59", "十一月中")
    december = _create(api, token, "2026-12-01 15:59:59", "十二月")
    _create(api, token, None, "没日期")

    response = _list(
        api,
        token,
        due_from="2026-10-31 00:00:00",
        due_to="2026-11-30 23:59:59",
    )
    assert response.status == 200
    ids = {row["id"] for row in response.data["items"]}
    assert ids == {october, november}
    assert december not in ids


def test_undated_filter_is_separate_from_due_window(api):
    token = _token(api)
    _create(api, token, "2026-11-15 15:59:59", "有日期")
    undated = _create(api, token, None, "没日期")

    response = _list(api, token, undated="true")
    assert response.status == 200
    assert [row["id"] for row in response.data["items"]] == [undated]


def test_invalid_calendar_bounds_are_422(api):
    token = _token(api)

    date_only = _list(api, token, due_from="2026-11-01")
    assert date_only.status == 422

    reversed_bounds = _list(
        api,
        token,
        due_from="2026-12-01 00:00:00",
        due_to="2026-11-01 00:00:00",
    )
    assert reversed_bounds.status == 422

    mixed = _list(api, token, undated="true", due_from="2026-11-01 00:00:00")
    assert mixed.status == 422


def test_summary_counts_undated_rows(api):
    token = _token(api)
    _create(api, token, "2020-01-01 00:00:00", "逾期")
    _create(api, token, "2026-12-01 15:59:59", "未来")
    _create(api, token, None, "没日期")
    done = _create(api, token, "2026-12-02 15:59:59", "已完成")
    assert api.patch(f"/api/memos/{done}", {"done": True}, token=token).status == 200

    summary = api.get("/api/memos/summary", token=token).data
    assert summary["pending"] == 3
    assert summary["done"] == 1
    assert summary["undated"] == 1
    assert summary["due"] >= 1
