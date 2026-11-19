"""增量账本合并：纯函数，不碰数据库、不读时钟。

这组用例钉住的是语义，不是实现细节：按 cursor 折叠（不按事件去重）、
按天去重访客、窗口裁剪、以及页面侧那三个 cap 的排序口径。
"""

import copy

import pytest

from services.analytics_merge import (
    empty_ledger,
    merge_tail,
    render_summary,
    window_start,
)

TODAY = "2026-11-19"


def actor_row(ident, **overrides):
    row = {
        "ident": ident,
        "user_id": None,
        "user_email": None,
        "ip": ident.split(":", 1)[1] if ident.startswith("ip:") else None,
        "country": None,
        "region": None,
        "city": None,
        "page_views": 0,
        "dwell_ms": 0,
        "first_seen": None,
        "last_seen": None,
    }
    row.update(overrides)
    return row


def day_row(day, ident, page_views=0):
    return {"day": day, "ident": ident, "page_views": page_views}


def path_row(ident, path, views=0, dwell_ms=0, last_seen=None):
    return {
        "ident": ident,
        "path": path,
        "views": views,
        "dwell_ms": dwell_ms,
        "last_seen": last_seen,
    }


def tail(max_id=0, day_rows=(), actor_rows=(), path_rows=()):
    return {
        "max_id": max_id,
        "day_rows": list(day_rows),
        "actor_rows": list(actor_rows),
        "path_rows": list(path_rows),
    }


def merge(ledger, payload, window_days=30):
    return merge_tail(ledger, payload, today=TODAY, window_days=window_days)


def test_empty_ledger_has_zero_cursor_and_no_rows():
    assert empty_ledger() == {"cursor": 0, "daily": {}, "actors": {}}


def test_merging_an_empty_tail_changes_nothing_but_the_cursor():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=10,
            actor_rows=[actor_row("ip:1.1.1.1", page_views=2, last_seen="2026-11-19 01:00:00")],
        ),
    )
    merged = merge(ledger, tail(max_id=10))
    assert merged == ledger
    assert merged["cursor"] == 10


def test_daily_rows_sum_views_and_keep_distinct_actors():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=5,
            day_rows=[
                day_row(TODAY, "ip:1.1.1.1", 3),
                day_row(TODAY, "ip:1.1.1.1", 2),  # same actor, same day
                day_row(TODAY, "u:8", 1),
            ],
        ),
    )
    assert ledger["daily"][TODAY]["page_views"] == 6
    assert ledger["daily"][TODAY]["actors"] == ["ip:1.1.1.1", "u:8"]


def test_actor_totals_accumulate_across_merges():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=1,
            actor_rows=[
                actor_row(
                    "ip:1.1.1.1",
                    page_views=2,
                    dwell_ms=1000,
                    last_seen="2026-11-19 01:00:00",
                )
            ],
        ),
    )
    ledger = merge(
        ledger,
        tail(
            max_id=2,
            actor_rows=[
                actor_row(
                    "ip:1.1.1.1",
                    page_views=3,
                    dwell_ms=500,
                    last_seen="2026-11-19 02:00:00",
                )
            ],
        ),
    )
    actor = ledger["actors"]["ip:1.1.1.1"]
    assert actor["page_views"] == 5
    assert actor["dwell_ms"] == 1500


def test_first_and_last_seen_take_the_extremes():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=1,
            actor_rows=[
                actor_row(
                    "u:8",
                    first_seen="2026-11-19 03:00:00",
                    last_seen="2026-11-19 05:00:00",
                )
            ],
        ),
    )
    ledger = merge(
        ledger,
        tail(
            max_id=2,
            actor_rows=[
                actor_row(
                    "u:8",
                    first_seen="2026-11-18 09:00:00",
                    last_seen="2026-11-19 04:00:00",
                )
            ],
        ),
    )
    actor = ledger["actors"]["u:8"]
    assert actor["first_seen"] == "2026-11-18 09:00:00"
    assert actor["last_seen"] == "2026-11-19 05:00:00"


def test_geo_fields_keep_the_lexicographic_max_like_sql():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=1,
            actor_rows=[
                actor_row(
                    "ip:1.1.1.1",
                    country="SG",
                    city="Singapore",
                    last_seen="2026-11-19 01:00:00",
                )
            ],
        ),
    )
    ledger = merge(
        ledger,
        tail(
            max_id=2,
            actor_rows=[
                actor_row(
                    "ip:1.1.1.1",
                    country="US",
                    city="Boston",
                    last_seen="2026-11-19 02:00:00",
                )
            ],
        ),
    )
    actor = ledger["actors"]["ip:1.1.1.1"]
    assert actor["country"] == "US"  # 'US' > 'SG'
    assert actor["city"] == "Singapore"  # 'Singapore' > 'Boston'


def test_email_unknown_at_first_sight_is_filled_in_later():
    ledger = merge(
        empty_ledger(),
        tail(max_id=1, actor_rows=[actor_row("u:8", last_seen="2026-11-19 01:00:00")]),
    )
    ledger = merge(
        ledger,
        tail(
            max_id=2,
            actor_rows=[
                actor_row(
                    "u:8",
                    user_id=8,
                    user_email="a@b.c",
                    last_seen="2026-11-19 02:00:00",
                )
            ],
        ),
    )
    actor = ledger["actors"]["u:8"]
    assert actor["user_id"] == 8
    assert actor["user_email"] == "a@b.c"


def test_paths_accumulate_and_keep_their_own_last_seen():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=1,
            actor_rows=[actor_row("ip:1.1.1.1", page_views=1, last_seen="2026-11-19 01:00:00")],
            path_rows=[path_row("ip:1.1.1.1", "/wiki", views=1, dwell_ms=500, last_seen="2026-11-19 01:00:00")],
        ),
    )
    ledger = merge(
        ledger,
        tail(
            max_id=2,
            actor_rows=[actor_row("ip:1.1.1.1", page_views=1, last_seen="2026-11-19 02:00:00")],
            path_rows=[
                path_row("ip:1.1.1.1", "/wiki", views=2, dwell_ms=700, last_seen="2026-11-19 02:00:00"),
                path_row("ip:1.1.1.1", "/blog", views=1, dwell_ms=100, last_seen="2026-11-19 02:00:00"),
            ],
        ),
    )
    paths = ledger["actors"]["ip:1.1.1.1"]["paths"]
    assert paths["/wiki"] == {"views": 3, "dwell_ms": 1200, "last_seen": "2026-11-19 02:00:00"}
    assert paths["/blog"]["views"] == 1


def test_path_row_without_its_actor_row_is_skipped():
    ledger = merge(
        empty_ledger(),
        tail(max_id=1, path_rows=[path_row("ip:9.9.9.9", "/wiki", views=1)]),
    )
    assert ledger["actors"] == {}


def test_days_outside_the_window_are_dropped_and_their_actors_pruned():
    old_day = window_start(TODAY, 30)  # the first day still inside the window
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=1,
            day_rows=[day_row(old_day, "ip:1.1.1.1", 1)],
            actor_rows=[
                actor_row("ip:1.1.1.1", page_views=1, last_seen=f"{old_day} 10:00:00")
            ],
        ),
    )
    ledger = merge(
        ledger,
        tail(max_id=2, day_rows=[day_row(TODAY, "ip:2.2.2.2", 1)]),
    )
    # 一个 30 天前的日子，在新的一天推进后被挤出窗口
    ledger = merge_tail(
        {"cursor": ledger["cursor"], "daily": ledger["daily"], "actors": ledger["actors"]},
        tail(max_id=3),
        today="2026-11-20",
        window_days=30,
    )
    assert "2026-11-20" not in ledger["daily"]
    assert old_day not in ledger["daily"]
    assert "ip:1.1.1.1" not in ledger["actors"]


def test_day_rows_outside_the_window_are_ignored_on_entry():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=1,
            day_rows=[
                day_row("2026-10-01", "ip:1.1.1.1", 5),  # too old
                day_row("2026-11-20", "ip:1.1.1.1", 5),  # in the future
                day_row(TODAY, "ip:1.1.1.1", 5),
            ],
        ),
    )
    assert list(ledger["daily"]) == [TODAY]
    assert ledger["daily"][TODAY]["page_views"] == 5


def test_cursor_never_moves_backwards():
    ledger = merge(empty_ledger(), tail(max_id=50))
    assert merge(ledger, tail(max_id=30))["cursor"] == 50


def test_stored_actors_are_capped_to_the_most_recent():
    rows = [
        actor_row(f"ip:10.0.0.{index}", last_seen=f"2026-11-19 0{index}:00:00")
        for index in range(1, 5)
    ]
    ledger = merge_tail(
        empty_ledger(),
        tail(max_id=4, day_rows=[day_row(TODAY, row["ident"], 1) for row in rows], actor_rows=rows),
        today=TODAY,
        window_days=30,
        max_stored_actors=2,
    )
    assert set(ledger["actors"]) == {"ip:10.0.0.4", "ip:10.0.0.3"}
    # 被挤掉的是明细；「来过」这件事仍然记在当天的访客集合里
    assert ledger["daily"][TODAY]["actors"] == [
        "ip:10.0.0.1",
        "ip:10.0.0.2",
        "ip:10.0.0.3",
        "ip:10.0.0.4",
    ]


def test_merge_does_not_mutate_its_inputs():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=1,
            day_rows=[day_row(TODAY, "ip:1.1.1.1", 1)],
            actor_rows=[actor_row("ip:1.1.1.1", page_views=1, last_seen="2026-11-19 01:00:00")],
            path_rows=[path_row("ip:1.1.1.1", "/wiki", views=1)],
        ),
        window_days=30,
    )
    before = copy.deepcopy(ledger)
    payload = tail(
        max_id=2,
        day_rows=[day_row(TODAY, "ip:1.1.1.1", 1)],
        actor_rows=[actor_row("ip:1.1.1.1", page_views=1, last_seen="2026-11-19 02:00:00")],
        path_rows=[path_row("ip:1.1.1.1", "/wiki", views=1, last_seen="2026-11-19 02:00:00")],
    )
    merged = merge(ledger, payload)
    assert ledger == before
    assert payload == tail(
        max_id=2,
        day_rows=[day_row(TODAY, "ip:1.1.1.1", 1)],
        actor_rows=[actor_row("ip:1.1.1.1", page_views=1, last_seen="2026-11-19 02:00:00")],
        path_rows=[path_row("ip:1.1.1.1", "/wiki", views=1, last_seen="2026-11-19 02:00:00")],
    )
    assert merged["actors"]["ip:1.1.1.1"]["page_views"] == 2


def test_replaying_the_same_tail_counts_twice_by_design():
    """折叠靠 cursor 保证一次，不靠去重；重复投喂必须表现为重复计数。"""
    payload = tail(
        max_id=7,
        day_rows=[day_row(TODAY, "ip:1.1.1.1", 2)],
        actor_rows=[actor_row("ip:1.1.1.1", page_views=2, last_seen="2026-11-19 01:00:00")],
    )
    once = merge(empty_ledger(), payload)
    twice = merge(once, payload)
    assert once["daily"][TODAY]["page_views"] == 2
    assert twice["daily"][TODAY]["page_views"] == 4
    assert twice["cursor"] == 7


def test_render_summary_sorts_series_and_actors():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=3,
            day_rows=[day_row("2026-11-18", "ip:1.1.1.1", 2), day_row(TODAY, "u:8", 4)],
            actor_rows=[
                actor_row("ip:1.1.1.1", page_views=2, last_seen="2026-11-18 10:00:00"),
                actor_row("u:8", user_id=8, user_email="a@b.c", page_views=4, last_seen="2026-11-19 10:00:00"),
            ],
        ),
    )
    summary = render_summary(ledger, today=TODAY, window_days=30)
    assert [day["date"] for day in summary["series"]] == ["2026-11-18", TODAY]
    assert [actor["key"] for actor in summary["actors"]] == ["u:8", "ip:1.1.1.1"]
    assert summary["actors"][0]["paths"] == []
    assert summary["cursor"] == 3


def test_render_summary_caps_actors_and_flags_truncation():
    rows = [
        actor_row(f"ip:10.0.0.{index}", last_seen=f"2026-11-19 1{index}:00:00")
        for index in range(3)
    ]
    ledger = merge(empty_ledger(), tail(max_id=3, actor_rows=rows))
    summary = render_summary(ledger, today=TODAY, window_days=30, max_actors=2)
    assert len(summary["actors"]) == 2
    assert summary["actors"][0]["key"] == "ip:10.0.0.2"  # 12:00 最新
    assert summary["has_next"] is True
    full = render_summary(ledger, today=TODAY, window_days=30, max_actors=3)
    assert full["has_next"] is False


def test_render_summary_caps_paths_by_dwell_then_views():
    paths = [
        path_row("ip:1.1.1.1", "/a", views=9, dwell_ms=100),
        path_row("ip:1.1.1.1", "/b", views=1, dwell_ms=900),
        path_row("ip:1.1.1.1", "/c", views=5, dwell_ms=500),
    ]
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=1,
            actor_rows=[actor_row("ip:1.1.1.1", page_views=15, last_seen="2026-11-19 01:00:00")],
            path_rows=paths,
        ),
    )
    summary = render_summary(ledger, today=TODAY, window_days=30, max_paths_per_actor=2)
    assert [path["path"] for path in summary["actors"][0]["paths"]] == ["/b", "/c"]


def test_render_summary_respects_the_total_path_budget():
    ledger = merge(
        empty_ledger(),
        tail(
            max_id=1,
            actor_rows=[
                actor_row("ip:1.1.1.1", last_seen="2026-11-19 02:00:00"),
                actor_row("ip:2.2.2.2", last_seen="2026-11-19 01:00:00"),
            ],
            path_rows=[
                path_row("ip:1.1.1.1", "/a", views=1, dwell_ms=100),
                path_row("ip:1.1.1.1", "/b", views=1, dwell_ms=90),
                path_row("ip:2.2.2.2", "/a", views=1, dwell_ms=80),
            ],
        ),
    )
    summary = render_summary(ledger, today=TODAY, window_days=30, max_path_rows=2)
    assert sum(len(actor["paths"]) for actor in summary["actors"]) == 2


def test_render_summary_with_empty_ledger_is_empty_not_an_error():
    summary = render_summary(empty_ledger(), today=TODAY, window_days=30)
    assert summary == {"series": [], "actors": [], "has_next": False, "cursor": 0}


@pytest.mark.parametrize(("today", "days", "expected"), [
    ("2026-11-19", 1, "2026-11-19"),
    ("2026-11-19", 7, "2026-11-13"),
    ("2026-03-01", 30, "2026-01-31"),
])
def test_window_start_counts_the_end_day_as_the_last_day(today, days, expected):
    assert window_start(today, days) == expected
