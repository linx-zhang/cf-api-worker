"""Pytest bootstrap：``src`` 是运行时导入根，路由用例还需要一套「数据库 + 客户端」。

两件事都拿不到现成的，所以都在这里搭：

* D1 是 JS 绑定，路由用例改用**内存 SQLite + 全量迁移**。被测的 SQL 就是线上
  会执行的那一份，索引、partial index、``datetime('now')`` 语义都一致。
* ``httpx``（``TestClient`` 的依赖）不在依赖表里，所以 ``Client`` 直接讲 ASGI：
  组一个 scope、喂一条 request、把 response 收回来。

用例里只应该出现「谁、调哪个路径、期望什么」，接线留在这里。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
MIGRATIONS = ROOT / "migrations"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from api.app import app
from api.deps import get_db, get_settings
from core.security import create_access_token
from infra.config import Settings
from infra.db import Database

# 和线上同一把钥匙的形状（HS256），密钥长度按 RFC 7518 给足 32 字节，免得
# PyJWT 每次签名都发一条 InsecureKeyLengthWarning 把输出刷满。
TEST_SETTINGS = Settings(
    jwt_secret="test-secret-for-route-tests-0123456789", jwt_exp_minutes=60
)


class _Statement:
    """``infra.db.Database`` 用到的那一小片 D1 prepared statement。"""

    def __init__(self, conn: sqlite3.Connection, sql: str, params: tuple = ()):
        self._conn = conn
        self._sql = sql
        self._params = params

    def bind(self, *params):
        return _Statement(self._conn, self._sql, tuple(params))

    def _cursor(self):
        return self._conn.execute(self._sql, self._params)

    async def all(self):
        # ``UPDATE ... RETURNING`` 也走这条（posts 批量软删），所以要 commit
        rows = [dict(row) for row in self._cursor().fetchall()]
        self._conn.commit()
        return SimpleNamespace(results=rows)

    async def first(self):
        row = self._cursor().fetchone()
        self._conn.commit()
        return None if row is None else dict(row)

    async def run(self):
        cursor = self._cursor()
        changes = max(cursor.rowcount, 0)
        self._conn.commit()
        return SimpleNamespace(meta=SimpleNamespace(changes=changes))


class _Binding:
    """D1 binding 的最小替身：``prepare`` 之后交给 ``Database`` 自己去 await。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def prepare(self, sql: str):
        return _Statement(self._conn, sql)


class ApiResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    @property
    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8")) if self.body else {}

    @property
    def code(self) -> str:
        return self.json.get("code", "")

    @property
    def data(self):
        return self.json.get("data")


class Client:
    """一条用户的视角：发请求、造数据、签 token。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- 请求 ---------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body=None,
        authorization: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        request_headers = {"host": "testserver"}
        payload = b""
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers["content-type"] = "application/json"
            request_headers["content-length"] = str(len(payload))
        if authorization is not None:
            # 原样写进头里：给「scheme 不是 Bearer」这类用例用
            request_headers["authorization"] = authorization
        elif token is not None:
            request_headers["authorization"] = f"Bearer {token}"
        # 用例自定义头（如 cf-connecting-ip）最后合并，保证能覆盖默认值
        if headers:
            request_headers.update({k.lower(): v for k, v in headers.items()})

        parts = urlsplit(path)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": parts.path,
            "raw_path": parts.path.encode("utf-8"),
            "query_string": parts.query.encode("utf-8"),
            "root_path": "",
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in request_headers.items()
            ],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 54321),
            # 真实运行时这里是 Worker env；用例已覆盖 get_settings/get_db，读不到它
            "env": SimpleNamespace(),
        }

        async def receive():
            once = {"type": "http.request", "body": payload, "more_body": False}
            # 第二次调用返回 disconnect，ASGI 约定如此
            receive.sent = getattr(receive, "sent", 0) + 1
            if receive.sent > 1:
                return {"type": "http.disconnect"}
            return once

        status_holder: dict[str, int] = {}
        chunks: list[bytes] = []

        async def send(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        asyncio.run(app(scope, receive, send))
        return ApiResponse(status_holder.get("status", 0), b"".join(chunks))

    def get(
        self,
        path: str,
        token: str | None = None,
        *,
        authorization: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        return self.request(
            "GET", path, token=token, authorization=authorization, headers=headers
        )

    def post(
        self,
        path: str,
        body=None,
        token: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ):
        return self.request("POST", path, token=token, body=body, headers=headers)

    def patch(self, path: str, body=None, token: str | None = None):
        return self.request("PATCH", path, token=token, body=body)

    def delete(self, path: str, token: str | None = None):
        return self.request("DELETE", path, token=token)

    # ---- 造数据 -------------------------------------------------------------
    def seed_user(self, email: str, role: str = "user") -> int:
        cursor = self.conn.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?) "
            "RETURNING id",
            (email, "pbkdf2_sha256$10000$c2FsdA==$ZGlnaXQ=", role),
        )
        user_id = int(cursor.fetchone()["id"])
        self.conn.commit()
        return user_id

    def seed_post(self, author_id: int, title: str = "标题", body: str = "正文") -> int:
        cursor = self.conn.execute(
            "INSERT INTO posts (author_id, title, body_md) VALUES (?, ?, ?) "
            "RETURNING id",
            (author_id, title, body),
        )
        post_id = int(cursor.fetchone()["id"])
        self.conn.commit()
        return post_id

    def seed_memo(self, user_id: int, body: str = "记一条") -> int:
        cursor = self.conn.execute(
            "INSERT INTO memos (user_id, body) VALUES (?, ?) RETURNING id",
            (user_id, body),
        )
        memo_id = int(cursor.fetchone()["id"])
        self.conn.commit()
        return memo_id

    def token(self, user_id: int) -> str:
        return create_access_token(user_id, TEST_SETTINGS)[0]

    def execute(self, sql: str, params: tuple = ()):
        self.conn.execute(sql, params)
        self.conn.commit()


@pytest.fixture
def api() -> Client:
    """每个用例一份干净库：全量迁移 + 依赖覆盖，用例之间互不影响。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        conn.executescript(migration.read_text(encoding="utf-8"))

    app.dependency_overrides[get_db] = lambda: Database(_Binding(conn))
    app.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    client = Client(conn)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        conn.close()
