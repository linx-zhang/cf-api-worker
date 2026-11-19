"""账本编排层：冷启动、增量折叠、超长尾巴兜底、以及和参考实现的一致性。

关键断言是「一致」：``load_summary``（账本路径）的结果必须和
``build_summary``（全量扫表的参考实现）**完全相等**。这条断言是整条改动
唯一能证明"新路径没算错"的证据——比逐字段看代码可靠。

``FakeCache`` 故意把 ``get_or_set`` 实现成"每次都重算"，这样每个用例都真的
走到账本 + 尾巴合并，而不是被 60 秒缓存挡住。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from api.app import app
from api.deps import get_db
from services.analytics import ANALYTICS_WINDOW_DAYS, build_summary
from services.analytics_ledger import (
    LEDGER_KEY,
    beijing_today,
    load_summary,
    refresh_ledger,
)
from services.analytics_merge import window_start


class FakeCache:
    """只实现账本用到的三个方法；``get_or_set`` 不缓存，逼出真实计算。"""

    def __init__(self):
        self.store: dict = {}

    async def get(self, key, default=None):
        return self.store.get(key, default)

    async def set(self, key, value, timeout=300):
        self.store[key] = value

    async def get_or_set(self, key, default, timeout=300):
        value = default() if callable(default) else default
        if hasattr(value, "__await__"):
            value = await value
        self.store[key] = value
        return value


def db_of(api):
    """复用 conftest 已经接好的那个 Database（指向内存 SQLite）。"""
    return app.dependency_overrides[get_db]()


def stamp(minutes_ago: int) -> str:
    moment = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def add_event(
    api,
    *,
    kind: str = "page_view",
    user_id: int | None = None,
    ip: str | None = None,
    path: str = "/wiki",
    duration_ms: int | None = None,
    minutes_ago: int = 5,
    country: str | None = None,
    city: str | None = None,
):
    api.execute(
        "INSERT INTO activity_events "
        "(user_id, type, path, ts, duration_ms, ip, country, region, city, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (user_id, kind, path, 0, duration_ms, ip, country, city, stamp(minutes_ago)),
    )


def seed_mixed_activity(api, user_id: int | None = None) -> None:
    """今天两条 + 昨天一条 + 一条 page_leave，覆盖访客/登录/设备三种身份。"""
    who = user_id
    add_event(api, user_id=who, path="/wiki", minutes_ago=30)
    add_event(api, user_id=who, path="/blog", minutes_ago=25)
    add_event(api, ip="1.2.3.4", path="/wiki", minutes_ago=20, country="SG", city="Singapore")
    add_event(
        api,
        kind="page_leave",
        ip="1.2.3.4",
        path="/wiki",
        duration_ms=12000,
        minutes_ago=18,
    )
    add_event(api, ip="5.6.7.8", path="/space", minutes_ago=24 * 60 + 30)


def run(coro):
    """pytest 没有装异步插件，和 conftest 一样用 asyncio.run 驱动。"""
    return asyncio.run(coro)


def assert_matches_reference(api, days: int = 7) -> dict:
    """账本路径 vs 参考实现，逐字段相等。"""
    db = db_of(api)
    summary = run(load_summary(db, days, cache=FakeCache()))
    reference = run(build_summary(db, days))
    assert summary == reference
    return summary


def test_cold_start_matches_reference(api):
    user_id = api.seed_user("a@b.c", role="admin")
    seed_mixed_activity(api, user_id)
    summary = assert_matches_reference(api)
    assert summary["today"]["page_views"] >= 3
    assert len(summary["series"]) >= 1


def test_tail_fold_matches_reference(api):
    user_id = api.seed_user("a@b.c", role="admin")
    db = db_of(api)
    cache = FakeCache()
    seed_mixed_activity(api, user_id)
    run(load_summary(db, 7, cache=cache))
    # 冷启动之后新来三条：一次读取就应该把它们折进来
    add_event(api, user_id=user_id, path="/tools", minutes_ago=10)
    add_event(api, ip="1.2.3.4", path="/tools", minutes_ago=8)
    add_event(api, ip="9.9.9.9", path="/", minutes_ago=6)

    summary = run(load_summary(db, 7, cache=cache))
    reference = run(build_summary(db, 7))
    assert summary == reference


def test_persisted_ledger_then_tail_matches_reference(api):
    user_id = api.seed_user("a@b.c", role="admin")
    db = db_of(api)
    cache = FakeCache()
    seed_mixed_activity(api, user_id)
    run(refresh_ledger(db, cache=cache))

    add_event(api, ip="4.4.4.4", path="/blog", minutes_ago=4)
    summary = run(load_summary(db, 30, cache=cache))
    assert summary == run(build_summary(db, 30))


def test_read_path_never_advances_the_persisted_cursor(api):
    user_id = api.seed_user("a@b.c", role="admin")
    db = db_of(api)
    cache = FakeCache()
    seed_mixed_activity(api, user_id)
    run(load_summary(db, 7, cache=cache))
    cursor_after_cold = cache.store[LEDGER_KEY]["cursor"]

    add_event(api, ip="7.7.7.7", path="/", minutes_ago=2)
    run(load_summary(db, 7, cache=cache))
    # 折叠结果只用于本次响应，不落盘：并发读之间不会互相覆盖
    assert cache.store[LEDGER_KEY]["cursor"] == cursor_after_cold


def test_oversized_tail_triggers_rebuild(api):
    user_id = api.seed_user("a@b.c", role="admin")
    db = db_of(api)
    cache = FakeCache()
    seed_mixed_activity(api, user_id)
    run(load_summary(db, 7, cache=cache))
    for index in range(520):
        add_event(api, ip=f"10.0.{index // 250}.{index % 250}", path="/wiki", minutes_ago=3)

    summary = run(load_summary(db, 30, cache=cache))
    reference = run(build_summary(db, 30))
    assert summary == reference

    # 读路径不落盘：超长尾巴触发的重建只服务本次响应
    assert cache.store[LEDGER_KEY]["cursor"] < max_id(api)
    # 下一次定时折叠会把 cursor 追平，且不再重复折叠
    folded = run(refresh_ledger(db, cache=cache))
    assert folded["cursor"] == max_id(api)
    # 尾巴依旧超限，所以记一次「重建」而不是逐行折叠
    assert folded["rebuilt"] is True


def max_id(api) -> int:
    row = api.conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM activity_events").fetchone()
    return int(row["max_id"])


def test_missing_ledger_rebuilds_from_scratch(api):
    user_id = api.seed_user("a@b.c", role="admin")
    db = db_of(api)
    cache = FakeCache()
    seed_mixed_activity(api, user_id)
    run(load_summary(db, 7, cache=cache))
    del cache.store[LEDGER_KEY]
    summary = run(load_summary(db, 7, cache=cache))
    assert summary == run(build_summary(db, 7))
    assert LEDGER_KEY in cache.store


def test_refresh_reports_what_it_folded(api):
    user_id = api.seed_user("a@b.c", role="admin")
    db = db_of(api)
    cache = FakeCache()
    seed_mixed_activity(api, user_id)

    first = run(refresh_ledger(db, cache=cache))
    assert first["rebuilt"] is True  # 冷启动走全量
    assert first["cursor"] == max_id(api)
    assert first["days"] == 1 or first["days"] == 2

    add_event(api, ip="2.2.2.2", path="/wiki", minutes_ago=1)
    second = run(refresh_ledger(db, cache=cache))
    assert second["rebuilt"] is False
    assert second["folded"] == 1
    assert second["cursor"] == max_id(api)


def test_empty_site_reports_zeros_not_an_error(api):
    db = db_of(api)
    summary = run(load_summary(db, 7, cache=FakeCache()))
    assert summary["series"] == []
    assert summary["actors"] == []
    assert summary["has_next"] is False
    assert summary["today"] == {
        "new_users": 0,
        "active_users": 0,
        "anonymous_visitors": 0,
        "page_views": 0,
    }


def test_new_users_today_counts_only_today(api):
    api.seed_user("today@b.c")
    db = db_of(api)
    summary = run(load_summary(db, 7, cache=FakeCache()))
    assert summary["today"]["new_users"] == 1


def test_window_boundary_drops_old_days_from_the_series(api):
    user_id = api.seed_user("a@b.c", role="admin")
    add_event(api, user_id=user_id, path="/wiki", minutes_ago=1)
    # 31 天前：超出保留窗口，两个实现都不该看见
    api.execute(
        "INSERT INTO activity_events (user_id, type, path, ts, created_at) "
        "VALUES (?, 'page_view', '/wiki', 0, datetime('now', '-31 days'))",
        (user_id,),
    )
    db = db_of(api)
    summary = run(load_summary(db, 30, cache=FakeCache()))
    assert summary == run(build_summary(db, 30))
    assert len(summary["series"]) == 1


def test_event_before_the_first_bucket_is_invisible_to_both(api):
    """窗口按「展示日」算，不是滚动 30×24h：边界前一天的事件两边都不该看见。

    这是回归用例——旧的滚动截断会把 (today-29) 那天之前最多 24 小时的事件
    算进参考实现，而账本按天裁剪，两者会在边界上分叉。
    """
    api.seed_user("a@b.c", role="admin")
    first_day = window_start(beijing_today(), ANALYTICS_WINDOW_DAYS)
    day_before = (date.fromisoformat(first_day) - timedelta(days=1)).isoformat()
    # 边界前一天中午（北京）= 当天 04:00 UTC，早于 first_day 00:00 北京
    api.execute(
        "INSERT INTO activity_events (ip, type, path, ts, created_at) "
        "VALUES ('8.8.8.8', 'page_view', '/wiki', 0, ?)",
        (f"{day_before} 04:00:00",),
    )
    db = db_of(api)
    summary = run(load_summary(db, ANALYTICS_WINDOW_DAYS, cache=FakeCache()))
    assert summary == run(build_summary(db, ANALYTICS_WINDOW_DAYS))
    assert summary["series"] == []
    assert summary["actors"] == []


def test_narrow_window_reports_its_own_actor_totals(api):
    """7 天视图的访客明细必须只算 7 天，不能把 30 天账本的累计报出来。"""
    user_id = api.seed_user("a@b.c", role="admin")
    add_event(api, user_id=user_id, path="/wiki", minutes_ago=1)
    # 十天前的一次访问：在 30 天窗口内，但不该进 7 天窗口
    api.execute(
        "INSERT INTO activity_events (user_id, type, path, ts, created_at) "
        "VALUES (?, 'page_view', '/old', 0, datetime('now', '-10 days'))",
        (user_id,),
    )
    db = db_of(api)
    cache = FakeCache()
    wide = run(load_summary(db, 30, cache=cache))
    narrow = run(load_summary(db, 7, cache=cache))
    assert wide == run(build_summary(db, 30))
    assert narrow == run(build_summary(db, 7))
    wide_actor = next(a for a in wide["actors"] if a["key"] == f"u:{user_id}")
    narrow_actor = next(a for a in narrow["actors"] if a["key"] == f"u:{user_id}")
    assert wide_actor["page_views"] == narrow_actor["page_views"] + 1
    assert [p["path"] for p in narrow_actor["paths"]] == ["/wiki"]


def test_beijing_today_shifts_by_eight_hours():
    # 2026-11-19 17:30 UTC == 2026-11-20 01:30 北京：跨日了
    moment = datetime(2026, 11, 19, 17, 30, tzinfo=UTC)
    assert beijing_today(moment) == "2026-11-20"
