"""Smoke test for ``src/infra/cache.py`` against an in-memory SQLite database.

Not part of the build: there is no test suite in this repo, so this runs as a
one-off script. It exercises the real SQL (including ``datetime('now', ?)``
modifiers and ``ON CONFLICT``) through a binding that mimics the D1 API shape.

Run from the worker root:

    uv run python scripts/agent/2026-11-13-cache-smoke.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from infra.cache import NEVER_EXPIRES, Cache, check_key, decode_value, encode_value
from tasks.cache_cleanup import purge_expired_cache
from tasks.cron import run_cron


class _Result:
    def __init__(self, rows: list[dict] | None = None, changes: int = 0) -> None:
        self.results = rows or []
        self.meta = types.SimpleNamespace(changes=changes)


class _Statement:
    def __init__(self, conn: sqlite3.Connection, sql: str) -> None:
        self._conn = conn
        self._sql = sql
        self._params: tuple = ()

    def bind(self, *params):
        self._params = params
        return self

    async def all(self):
        cur = self._conn.execute(self._sql, self._params)
        rows = [dict(row) for row in cur.fetchall()]
        self._conn.commit()
        return _Result(rows)

    async def first(self):
        cur = self._conn.execute(self._sql, self._params)
        row = cur.fetchone()
        self._conn.commit()
        return None if row is None else dict(row)

    async def run(self):
        cur = self._conn.execute(self._sql, self._params)
        changes = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        self._conn.commit()
        return _Result(changes=changes)


class FakeD1:
    """Only the surface ``infra.db.Database`` uses."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        migrations = Path(__file__).resolve().parents[2] / "migrations"
        # 0011 是本文件的主题；0004 只为了让 run_cron 里的 activity retention 也能跑。
        for name in ("0004_activity_events.sql", "0011_db_cache.sql"):
            self._conn.executescript((migrations / name).read_text(encoding="utf-8"))

    def prepare(self, sql: str):
        return _Statement(self._conn, sql)


async def main() -> None:
    # payload round-trip: pickle must survive dict/list/tuple/None/int/bool
    sample = {"rows": [1, 2, 3], "label": "中文 · utf-8", "none": None, "flag": True}
    assert decode_value(encode_value(sample)) == sample

    cache = Cache(FakeD1())

    # miss -> default
    assert await cache.get("nope") is None
    assert await cache.get("nope", "fallback") == "fallback"

    # set + get, namespaced key stays stable across instances
    await cache.set("k1", sample)
    assert await cache.get("k1") == sample
    assert await cache.has_key("k1") is True
    other = Cache(cache._db)
    assert await other.get("k1") == sample, "同一个 namespace 才能读到同一行"

    # 缓存 None 也要能区分「没缓存」
    await cache.set("none-value", None)
    assert await cache.get("none-value", "fallback") is None

    # timeout=0 不存，只删
    await cache.set("k1", {"v": 2}, timeout=0)
    assert await cache.get("k1") is None
    assert await cache.has_key("k1") is False

    # 负 timeout 视为立刻过期：写进去但读不到
    await cache.set("stale", [1, 2, 3], timeout=-5)
    assert await cache.get("stale") is None

    # timeout=None 永不失效
    await cache.set("forever", "x", timeout=None)
    assert await cache.has_key("forever") is True
    row = await cache._db.fetch_one(
        "SELECT expires_at FROM cache_entries WHERE key = ?", ("default:forever",)
    )
    assert row["expires_at"] == NEVER_EXPIRES

    # add: 新键成功；已存在失败；过期行可以被重新占用
    assert await cache.add("fresh", 1) is True
    assert await cache.add("fresh", 2) is False
    assert await cache.get("fresh") == 1
    await cache.set("fresh", 2, timeout=-1)
    assert await cache.add("fresh", 3) is True
    assert await cache.get("fresh") == 3

    # get_many 只返回命中的键
    assert await cache.get_many(["fresh", "missing"]) == {"fresh": 3}

    # get_or_set 只计算一次
    calls = []

    async def loader():
        calls.append(1)
        return {"computed": True}

    assert await cache.get_or_set("lazy", loader) == {"computed": True}
    assert await cache.get_or_set("lazy", loader) == {"computed": True}
    assert len(calls) == 1, "第二次必须命中缓存"

    # 损坏的 payload 当 miss 处理，并清掉这一行
    await cache._db.execute(
        "INSERT INTO cache_entries (key, value, expires_at) "
        "VALUES ('default:broken', 'not-a-pickle', ?)",
        (NEVER_EXPIRES,),
    )
    assert await cache.get("broken", "recovered") == "recovered"
    assert await cache.has_key("broken") is False

    # 键长度守卫
    try:
        check_key("x" * 251)
    except ValueError:
        pass
    else:
        raise AssertionError("超长 key 必须报错")

    # purge: 只删过期行，返回值 = 删除条数
    total_before = await cache._db.fetch_one("SELECT COUNT(*) AS n FROM cache_entries")
    purged = await purge_expired_cache(cache._db)
    total_after = await cache._db.fetch_one("SELECT COUNT(*) AS n FROM cache_entries")
    print(f"purge removed {purged} rows, {total_before['n']} -> {total_after['n']}")
    assert purged >= 1
    assert total_after["n"] < total_before["n"]
    assert await cache.has_key("forever") is True, "未过期行不能被清"

    # clear 等价于删光（这里手写，避免给 Cache 增加危险 API）
    removed = await cache._db.execute("DELETE FROM cache_entries")
    assert removed == total_after["n"]

    # run_cron 的接线：两个 job 都要跑到，返回值是两个 job 的条数
    fake_env = types.SimpleNamespace(DB=FakeD1())
    report = await run_cron(fake_env, cron="smoke")
    assert set(report) == {"activity_events", "cache_entries"}, report
    assert report["cache_entries"] == 0
    print(f"run_cron returned {report}")

    print("cache smoke test: OK")


asyncio.run(main())
