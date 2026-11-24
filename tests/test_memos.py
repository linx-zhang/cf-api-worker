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


def _memo_ids(response) -> list[int]:
    return [int(row["id"]) for row in response.data["items"]]


def test_pagination_is_stable_across_pages_and_filters(api):
    """20 条一页，翻页只靠 offset；三页拼起来必须覆盖全集且不重复。"""
    token = _token(api, "paging@example.com")

    # 待办：25 条，带不同到期日，保证跨页边界。
    pending_ids = [
        _create(api, token, f"2026-12-{day:02d} 15:59:59", f"待办 {day}")
        for day in range(1, 26)
    ]
    # 已完成：21 条，单独验证 done=true 的排序与分页。
    done_ids = []
    for index in range(1, 22):
        memo_id = _create(api, token, None, f"已完成 {index}")
        assert api.patch(f"/api/memos/{memo_id}", {"done": True}, token=token).status == 200
        done_ids.append(memo_id)

    first = _list(api, token, limit=20, offset=0)
    second = _list(api, token, limit=20, offset=20)
    third = _list(api, token, limit=20, offset=40)
    assert [first.status, second.status, third.status] == [200, 200, 200]
    assert first.data["has_next"] is True
    assert second.data["has_next"] is True
    assert third.data["has_next"] is False
    all_ids = _memo_ids(first) + _memo_ids(second) + _memo_ids(third)
    assert len(all_ids) == 46
    assert len(set(all_ids)) == 46
    assert set(all_ids) == set(pending_ids) | set(done_ids)

    pending_first = _list(api, token, done="false", limit=20, offset=0)
    pending_second = _list(api, token, done="false", limit=20, offset=20)
    assert pending_first.data["has_next"] is True
    assert pending_second.data["has_next"] is False
    pending_page_ids = _memo_ids(pending_first) + _memo_ids(pending_second)
    assert pending_page_ids == pending_ids
    assert len(set(pending_page_ids)) == 25

    done_first = _list(api, token, done="true", limit=20, offset=0)
    done_second = _list(api, token, done="true", limit=20, offset=20)
    assert done_first.data["has_next"] is True
    assert done_second.data["has_next"] is False
    done_page_ids = _memo_ids(done_first) + _memo_ids(done_second)
    assert len(done_page_ids) == 21
    assert set(done_page_ids) == set(done_ids)
    assert _memo_ids(done_first) == list(reversed(done_ids))[:20]
    assert _memo_ids(done_second) == [done_ids[0]]


def test_offset_past_end_returns_empty_page(api):
    token = _token(api, "paging-empty@example.com")
    _create(api, token, None, "只有一条")

    response = _list(api, token, limit=20, offset=20)
    assert response.status == 200
    assert response.data == {"items": [], "has_next": False}
