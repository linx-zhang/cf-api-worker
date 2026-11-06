"""Single place where the D1 binding is used. Routes never build SQL statements
themselves beyond passing a query to one of these helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class Database:
    def __init__(self, binding) -> None:
        self._binding = binding

    def _prepare(self, sql: str, params: Iterable[Any] = ()):
        statement = self._binding.prepare(sql)
        params = tuple(params)
        if params:
            statement = statement.bind(*params)
        return statement

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        result = await self._prepare(sql, params).all()
        return list(result.results)

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = await self._prepare(sql, params).first()
        return None if row is None else dict(row)

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a mutating statement and return the number of affected rows."""
        result = await self._prepare(sql, params).run()
        return int(getattr(result.meta, "changes", 0) or 0)

    async def insert_returning(
        self, sql: str, params: Iterable[Any] = ()
    ) -> dict | None:
        """INSERT ... RETURNING <columns> -> the created row."""
        return await self.fetch_one(sql, params)
