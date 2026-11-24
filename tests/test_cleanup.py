"""日更清理：墓碑保留期、删除顺序，以及永不清理的有价值数据。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from api.app import app
from api.deps import get_db
from tasks import cron
from tasks.consumable_tombstone import purge_consumable_tombstones
from tasks.memo_tombstone import purge_memo_tombstones
from tasks.resume_share_cleanup import purge_invalid_resume_shares


def db_of():
    """复用 conftest 接好的 Database（指向内存 SQLite）。"""
    return app.dependency_overrides[get_db]()


def _scalar(api, sql: str, params: tuple = ()):
    return api.conn.execute(sql, params).fetchone()[0]


def test_consumable_tombstones_delete_cycles_before_items(api):
    user_id = api.seed_user("cleanup-consumables@example.com")

    # 两份超过 90 天的物品墓碑：一份周期也带墓碑，一份周期没标记（数据异常）
    api.execute(
        "INSERT INTO consumable_items (id, user_id, name, deleted_at) "
        "VALUES (10, ?, '旧物品', datetime('now', '-100 days'))",
        (user_id,),
    )
    api.execute(
        "INSERT INTO consumable_cycles (id, item_id, started_on, deleted_at) "
        "VALUES (100, 10, '2026-01-01', datetime('now', '-100 days'))"
    )
    api.execute(
        "INSERT INTO consumable_items (id, user_id, name, deleted_at) "
        "VALUES (11, ?, '旧物品异常子行', datetime('now', '-100 days'))",
        (user_id,),
    )
    api.execute(
        "INSERT INTO consumable_cycles (id, item_id, started_on, deleted_at) "
        "VALUES (101, 11, '2026-01-01', NULL)"
    )

    # 最近才软删的应当保留
    api.execute(
        "INSERT INTO consumable_items (id, user_id, name, deleted_at) "
        "VALUES (12, ?, '刚删的', datetime('now', '-10 days'))",
        (user_id,),
    )
    api.execute(
        "INSERT INTO consumable_cycles (id, item_id, started_on, deleted_at) "
        "VALUES (102, 12, '2026-01-01', datetime('now', '-10 days'))"
    )

    # 存活物品下的旧周期墓碑：周期应删，物品不动
    api.execute(
        "INSERT INTO consumable_items (id, user_id, name, deleted_at) "
        "VALUES (13, ?, '还在用', NULL)",
        (user_id,),
    )
    api.execute(
        "INSERT INTO consumable_cycles (id, item_id, started_on, deleted_at) "
        "VALUES (103, 13, '2025-01-01', datetime('now', '-200 days'))"
    )

    result = asyncio.run(purge_consumable_tombstones(db_of(), days=90))

    assert result == {"cycles": 3, "items": 2}
    assert _scalar(api, "SELECT COUNT(*) FROM consumable_cycles") == 1
    assert _scalar(api, "SELECT COUNT(*) FROM consumable_items") == 2
    assert _scalar(api, "SELECT id FROM consumable_items") == 12
    assert _scalar(api, "SELECT id FROM consumable_cycles") == 102


def test_memo_tombstones_keep_180_days(api):
    user_id = api.seed_user("cleanup-memos@example.com")
    api.execute(
        "INSERT INTO memos (user_id, body, deleted_at) "
        "VALUES (?, '两百天前的墓碑', datetime('now', '-200 days'))",
        (user_id,),
    )
    api.execute(
        "INSERT INTO memos (user_id, body, deleted_at) "
        "VALUES (?, '一百天前的墓碑', datetime('now', '-100 days'))",
        (user_id,),
    )
    api.execute(
        "INSERT INTO memos (user_id, body) VALUES (?, '存活备忘')",
        (user_id,),
    )

    assert asyncio.run(purge_memo_tombstones(db_of(), days=180)) == 1
    assert _scalar(api, "SELECT COUNT(*) FROM memos") == 2


def test_resume_shares_are_deleted_the_moment_they_are_dead(api):
    api.execute(
        "INSERT INTO resumes (id, title, content_html, deleted_at) "
        "VALUES (1, '存活简历', '<p>ok</p>', NULL)"
    )
    api.execute(
        "INSERT INTO resumes (id, title, content_html, deleted_at) "
        "VALUES (2, '已软删简历', '<p>gone</p>', datetime('now'))"
    )

    api.execute(
        "INSERT INTO resume_shares (code, resume_id, expires_at) "
        "VALUES ('live', 1, datetime('now', '+1 day'))"
    )
    api.execute(
        "INSERT INTO resume_shares (code, resume_id, expires_at) "
        "VALUES ('expired', 1, datetime('now', '-1 minute'))"
    )
    api.execute(
        "INSERT INTO resume_shares (code, resume_id, expires_at, revoked_at) "
        "VALUES ('revoked', 1, datetime('now', '+1 day'), datetime('now'))"
    )
    api.execute(
        "INSERT INTO resume_shares (code, resume_id, expires_at) "
        "VALUES ('orphan', 2, datetime('now', '+1 day'))"
    )

    assert asyncio.run(purge_invalid_resume_shares(db_of())) == 3
    assert _scalar(api, "SELECT code FROM resume_shares") == "live"
    # 简历本身一份都不能少
    assert _scalar(api, "SELECT COUNT(*) FROM resumes") == 2


def test_daily_cron_runs_cleanup_in_order_without_folding_the_ledger(
    monkeypatch, capsys
):
    calls: list[str] = []

    async def activity(db, days):
        calls.append("activity")
        return 1

    async def cache(db):
        calls.append("cache")
        return 2

    async def memos(db, days):
        calls.append("memos")
        return 3

    async def consumables(db, days):
        calls.append("consumables")
        return {"cycles": 4, "items": 5}

    async def shares(db):
        calls.append("shares")
        return 6

    async def ledger(db):
        calls.append("ledger")
        return {"folded": 7, "cursor": 0, "days": 0, "actors": 0, "rebuilt": False}

    monkeypatch.setattr(cron, "Database", lambda binding: object())
    monkeypatch.setattr(cron, "purge_old_activity", activity)
    monkeypatch.setattr(cron, "purge_expired_cache", cache)
    monkeypatch.setattr(cron, "purge_memo_tombstones", memos)
    monkeypatch.setattr(cron, "purge_consumable_tombstones", consumables)
    monkeypatch.setattr(cron, "purge_invalid_resume_shares", shares)
    monkeypatch.setattr(cron, "persist_analytics_ledger", ledger)

    report = asyncio.run(cron.run_cron(SimpleNamespace(DB=None), "15 18 * * *"))

    assert calls == ["activity", "cache", "memos", "consumables", "shares"]
    assert report == {
        "activity_events": 1,
        "cache_entries": 2,
        "memos": 3,
        "consumable_cycles": 4,
        "consumable_items": 5,
        "resume_shares": 6,
    }
    assert "analytics_ledger_rows" not in report
    assert "analytics ledger" not in capsys.readouterr().out

    # 小时级触发器仍然只折账本
    calls.clear()
    hourly = asyncio.run(cron.run_cron(SimpleNamespace(DB=None), "0 * * * *"))
    assert calls == ["ledger"]
    assert hourly == {"analytics_ledger_rows": 7}
