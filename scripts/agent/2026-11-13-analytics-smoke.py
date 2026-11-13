"""Smoke test for the activity analytics queries and visitor path rules.

Runs the real SQL from ``services/analytics.py`` and ``services/visitor_paths``
against an in-memory SQLite database that has every migration applied, so a
statement that SQLite rejects (or a day-bucketing mistake) fails here rather
than in the admin page.

    python scripts/agent/2026-11-13-analytics-smoke.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from infra.db import Database
from services.analytics import (
    ANALYTICS_MAX_PATHS_PER_ACTOR,
    build_summary,
    load_summary,
)
from services.visitor_paths import is_public_path


class _Statement:
    def __init__(self, conn: sqlite3.Connection, sql: str) -> None:
        self._conn = conn
        self._sql = sql
        self._params: tuple = ()

    def bind(self, *params) -> _Statement:
        self._params = params
        return self

    async def all(self):
        cursor = self._conn.execute(self._sql, self._params)
        return _Result([dict(row) for row in cursor.fetchall()])

    async def first(self):
        cursor = self._conn.execute(self._sql, self._params)
        row = cursor.fetchone()
        return None if row is None else dict(row)

    async def run(self):
        cursor = self._conn.execute(self._sql, self._params)
        self._conn.commit()
        return _Result(cursor.rowcount)


class _Result:
    def __init__(self, value):
        self.results = value
        self.meta = type(
            "Meta", (), {"changes": value if isinstance(value, int) else 0}
        )()


class FakeD1:
    """Minimal D1 binding surface over one sqlite3 connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.queries = 0

    def prepare(self, sql: str) -> _Statement:
        self.queries += 1
        return _Statement(self._conn, sql)


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    applied = []
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        for statement in _split(path.read_text(encoding="utf-8")):
            conn.execute(statement)
        applied.append(path.name)
    conn.commit()
    return applied


def _split(sql_text: str) -> list[str]:
    """Split a migration on semicolons, dropping ``--`` comment lines."""
    lines = [
        line for line in sql_text.splitlines() if not line.strip().startswith("--")
    ]
    body = "\n".join(lines)
    return [part.strip() for part in body.split(";") if part.strip()]


def seed(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC)
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO users (id, email, password_hash, role) VALUES "
        "(1, 'a@example.com', 'x', 'admin')"
    )
    # A user created yesterday: must not count as "new today".
    conn.execute(
        "INSERT INTO users (id, email, password_hash, role, created_at) "
        "VALUES (2, 'b@example.com', 'x', 'user', ?)",
        (yesterday,),
    )
    conn.executemany(
        "INSERT INTO activity_events "
        "(user_id, type, path, ts, duration_ms, ip, country, region, city, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # Signed-in user: two views, one leave with 5s dwell.
            (
                1,
                "page_view",
                "/blog/35",
                1,
                None,
                "1.1.1.1",
                "CN",
                "Guangdong",
                "Shenzhen",
                now_s,
            ),
            (
                1,
                "page_view",
                "/wiki",
                2,
                None,
                "1.1.1.1",
                "CN",
                "Guangdong",
                "Shenzhen",
                now_s,
            ),
            (
                1,
                "page_leave",
                "/wiki",
                3,
                5000,
                "1.1.1.1",
                "CN",
                "Guangdong",
                "Shenzhen",
                now_s,
            ),
            # Anonymous visitor: no user row, geo from Cloudflare.
            (
                None,
                "page_view",
                "/blog",
                4,
                None,
                "8.8.8.8",
                "US",
                "California",
                "San Jose",
                now_s,
            ),
            (
                None,
                "page_leave",
                "/blog",
                5,
                9000,
                "8.8.8.8",
                "US",
                "California",
                "San Jose",
                now_s,
            ),
            (
                None,
                "page_view",
                "/blog",
                6,
                None,
                "8.8.8.8",
                "US",
                "California",
                "San Jose",
                now_s,
            ),
            # Anonymous, same ip, older day: same actor, not a new one.
            (
                None,
                "page_view",
                "/wiki/docs/start",
                7,
                None,
                "8.8.8.8",
                None,
                None,
                None,
                yesterday,
            ),
            # An event with neither identity (should be ignored everywhere).
            (None, "page_view", "/blog", 8, None, None, None, None, None, now_s),
        ],
    )
    conn.commit()


async def main() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    applied = apply_migrations(conn)
    print(f"migrations applied: {len(applied)} (0001..{applied[-1][:4]})")
    seed(conn)

    binding = FakeD1(conn)
    db = Database(binding)
    summary = await build_summary(db, 7)

    today = summary["today"]
    assert today["new_users"] == 1, today  # only the user created today
    assert today["active_users"] == 1, today
    assert today["anonymous_visitors"] == 1, today
    assert today["page_views"] == 5, today  # 2 signed-in + 3 anonymous (+1 ip-less)

    actors = summary["actors"]
    assert len(actors) == 2, actors
    signed, anon = actors  # ordered by last_seen DESC
    assert {signed["key"], anon["key"]} == {"u:1", "ip:8.8.8.8"}, actors

    anon = next(a for a in actors if a["key"] == "ip:8.8.8.8")
    assert anon["user_id"] is None
    assert anon["ip"] == "8.8.8.8"
    assert anon["country"] == "US", anon
    assert anon["city"] == "San Jose", anon
    assert anon["page_views"] == 3, anon
    assert anon["dwell_ms"] == 9000, anon
    # Path detail keeps the busiest pages, capped per actor.
    assert anon["paths"][0]["path"] == "/blog", anon["paths"]
    assert anon["paths"][0]["dwell_ms"] == 9000, anon["paths"]
    assert len(anon["paths"]) <= ANALYTICS_MAX_PATHS_PER_ACTOR

    signed = next(a for a in actors if a["key"] == "u:1")
    assert signed["user_email"] == "a@example.com", signed
    assert signed["dwell_ms"] == 5000, signed
    assert signed["paths"][0]["path"] == "/wiki", signed["paths"]

    # Series covers both days, keyed by the +8h bucket.
    days = [point["date"] for point in summary["series"]]
    assert len(days) == 2, summary["series"]
    assert days[0] < days[1]

    assert summary["timezone_offset_hours"] == 8
    assert summary["has_next"] is False

    # Cache: first read computes and stores, second read costs exactly one query
    # (the cache lookup) and returns the same payload.
    cached = await load_summary(db, 7)
    assert cached == summary
    before = binding.queries
    again = await load_summary(db, 7)
    assert binding.queries == before + 1, (before, binding.queries)
    assert again == summary
    await load_summary(db, 30)  # different window = different key
    assert binding.queries > before

    # Visitor path rules.
    allowed = ["/", "/blog", "/blog/35", "/blog?page=2", "/wiki/docs/start"]
    blocked = [
        "/admin",
        "/admin?tab=users",
        "/blogger",
        "/wiki2",
        "/.env",
        "/wp-login.php",
        "",
    ]
    for path in allowed:
        assert is_public_path(path), path
    for path in blocked:
        assert not is_public_path(path), path

    print(f"today: {today}")
    print(f"actors: {[(a['key'], a['page_views'], a['dwell_ms']) for a in actors]}")
    print(
        f"series: {[(d['date'], d['page_views'], d['actors']) for d in summary['series']]}"
    )
    print("analytics smoke test: OK")


if __name__ == "__main__":
    asyncio.run(main())
