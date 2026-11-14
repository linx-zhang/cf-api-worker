"""D1-backed key/value cache with Django's ``django.core.cache.backends.db`` shape.

The vocabulary is deliberately Django's, so the mental model transfers:

- ``timeout=None`` -> never expires, ``timeout=0`` -> delete / do not store
- ``set`` overwrites, ``add`` only creates (or refreshes an expired row)
- reads ignore expired rows, so a missed purge is a latency problem, not a
  correctness problem

Values go through ``pickle`` and then ``base64`` so the payload is plain TEXT in
D1. Two consequences worth remembering:

1. ``pickle`` is not a wire format. Only caches we wrote ourselves may be
   unpickled; never load a payload from an untrusted path into this function.
2. The payload is opaque to SQL, so nothing can be queried by value. Keys are
   the only handle — keep them cheap and derivable (``"posts:list:v1:20:0"``).

Why a D1 table rather than Workers KV or a Durable Object: KV would be a second
binding, a second consistency model and a second quota to reason about, for data
that is already derivable from D1. A row in the same database can be joined,
inspected and dropped with one statement — and every cached value must stay
droppable, because D1 remains the only source of truth.
"""

from __future__ import annotations

import base64
import pickle
from typing import Any
from uuid import uuid4

from infra.db import Database

# Same ceiling Django documents for memcached keys; keeps a runaway key from
# turning into a row nobody can look up again.
MAX_KEY_LENGTH = 250

# Django's database backend default. Caches here are for read amplification, not
# for correctness, so a short default is the safer default.
DEFAULT_TIMEOUT = 300

# Sentinel for "never expires"; a real date, so expiry comparisons stay uniform.
NEVER_EXPIRES = "9999-12-31 00:00:00"

# Distinguishes "not cached" from "cached and the value really is None".
_MISSING = object()


def encode_value(value: Any) -> str:
    """Serialize a value into the TEXT payload stored in D1."""
    return base64.b64encode(
        pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    ).decode("ascii")


def decode_value(payload: str) -> Any:
    """Reverse :func:`encode_value`. Only safe for payloads this project wrote."""
    return pickle.loads(base64.b64decode(payload.encode("ascii")))


def check_key(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError("cache key must be a non-empty string")
    if len(key) > MAX_KEY_LENGTH:
        raise ValueError(
            f"cache key longer than {MAX_KEY_LENGTH} characters: {key[:20]}..."
        )
    return key


def _expires_modifier(timeout: int | None) -> str | None:
    """SQLite ``datetime('now', ?)`` modifier for a timeout in seconds.

    ``None`` means "never": the caller stores :data:`NEVER_EXPIRES` instead, so
    this function returns ``None`` to signal that. ``0`` means "do not store".
    """
    if timeout is None:
        return None
    # No leading "+": SQLite accepts "300 seconds" but "+-5 seconds" is an
    # invalid modifier and makes datetime() return NULL (NOT NULL would then
    # reject the row), so a negative timeout must keep its own sign.
    return f"{int(timeout)} seconds"


class Cache:
    """Async, D1-backed cache. Construct one per request: ``Cache(env.DB)``."""

    def __init__(self, db: Database | Any, namespace: str = "default") -> None:
        self._db = db if isinstance(db, Database) else Database(db)
        self._prefix = f"{namespace}:"

    def _key(self, key: str) -> str:
        return check_key(f"{self._prefix}{key}")

    async def get(self, key: str, default: Any = None) -> Any:
        """Return the cached value, or ``default`` if absent / expired."""
        row = await self._db.fetch_one(
            "SELECT value FROM cache_entries "
            "WHERE key = ? AND expires_at > datetime('now')",
            (self._key(key),),
        )
        if row is None:
            return default
        try:
            return decode_value(row["value"])
        except (pickle.UnpicklingError, ValueError, EOFError):
            # A corrupt payload must never take down the caller: drop it and
            # behave like a miss, so the next writer repairs the row.
            await self.delete(key)
            return default

    async def get_many(self, keys: list[str], default: Any = None) -> dict[str, Any]:
        """Return ``{key: value}`` for the keys that are present; no prefixes."""
        if not keys:
            return {}
        prefixed = [self._key(key) for key in keys]
        placeholders = ", ".join("?" for _ in prefixed)
        rows = await self._db.fetch_all(
            "SELECT key, value FROM cache_entries "
            f"WHERE key IN ({placeholders}) AND expires_at > datetime('now')",
            prefixed,
        )
        found: dict[str, Any] = {}
        for row in rows:
            short = row["key"][len(self._prefix) :]
            try:
                found[short] = decode_value(row["value"])
            except (pickle.UnpicklingError, ValueError, EOFError):
                continue
        return found

    async def set(
        self, key: str, value: Any, timeout: int | None = DEFAULT_TIMEOUT
    ) -> None:
        """Store ``value``; ``timeout=0`` deletes the key instead."""
        stored_key = self._key(key)
        if timeout == 0:
            await self.delete(key)
            return
        payload = encode_value(value)
        modifier = _expires_modifier(timeout)
        if modifier is None:
            await self._db.execute(
                "INSERT INTO cache_entries (key, value, expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, datetime('now'), datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, expires_at = excluded.expires_at, "
                "updated_at = datetime('now')",
                (stored_key, payload, NEVER_EXPIRES),
            )
            return
        await self._db.execute(
            "INSERT INTO cache_entries (key, value, expires_at, created_at, updated_at) "
            "VALUES (?, ?, datetime('now', ?), datetime('now'), datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, expires_at = excluded.expires_at, "
            "updated_at = datetime('now')",
            (stored_key, payload, modifier),
        )

    async def add(
        self, key: str, value: Any, timeout: int | None = DEFAULT_TIMEOUT
    ) -> bool:
        """Store only if absent / expired; returns whether the value was written."""
        stored_key = self._key(key)
        payload = encode_value(value)
        modifier = _expires_modifier(timeout)
        expires_sql = "?" if modifier is None else "datetime('now', ?)"
        expires_param = NEVER_EXPIRES if modifier is None else modifier
        inserted = await self._db.execute(
            "INSERT INTO cache_entries (key, value, expires_at, created_at, updated_at) "
            f"VALUES (?, ?, {expires_sql}, datetime('now'), datetime('now')) "
            "ON CONFLICT(key) DO NOTHING",
            (stored_key, payload, expires_param),
        )
        if inserted:
            return True
        # The row exists: only a stale row may be taken over.
        refreshed = await self._db.execute(
            "UPDATE cache_entries SET value = ?, expires_at = " + expires_sql + ", "
            "updated_at = datetime('now') "
            "WHERE key = ? AND expires_at <= datetime('now')",
            (payload, expires_param, stored_key),
        )
        return bool(refreshed)

    async def delete(self, key: str) -> bool:
        """Remove one key; returns whether a row existed."""
        removed = await self._db.execute(
            "DELETE FROM cache_entries WHERE key = ?", (self._key(key),)
        )
        return bool(removed)

    async def has_key(self, key: str) -> bool:
        row = await self._db.fetch_one(
            "SELECT 1 AS present FROM cache_entries "
            "WHERE key = ? AND expires_at > datetime('now')",
            (self._key(key),),
        )
        return row is not None

    async def record_hit(self, bucket: str, timeout: int) -> int:
        """Record one hit in a window bucket and return the running count.

        A read-modify-write counter is not safe here: concurrent requests all
        read the same old value and write the same increment, so a burst walks
        straight through the limit. One row per hit makes the write atomic
        (``ON CONFLICT DO NOTHING`` never blocks another hit), and the count is
        a key-range scan over the bucket prefix.

        ``bucket`` should already identify the window (``"read:ip:1.2.3.4:293"``),
        so expired rows simply stop matching the ``expires_at`` filter and the
        window rolls over on its own. Rows are left to the existing cache purge.
        """
        hit_key = f"{bucket}:{uuid4().hex}"
        prefix = self._key(bucket) + ":"
        await self._db.execute(
            "INSERT INTO cache_entries (key, value, expires_at, created_at, updated_at) "
            "VALUES (?, ?, datetime('now', ?), datetime('now'), datetime('now')) "
            "ON CONFLICT(key) DO NOTHING",
            (self._key(hit_key), encode_value(1), f"{int(timeout)} seconds"),
        )
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS hits FROM cache_entries "
            "WHERE key >= ? AND key < ? AND expires_at > datetime('now')",
            (prefix, prefix + "~"),
        )
        return int(row["hits"]) if row else 0

    async def get_or_set(
        self, key: str, default: Any, timeout: int | None = DEFAULT_TIMEOUT
    ) -> Any:
        """Read through: return the cached value, else compute and store it.

        ``default`` may be a value or a callable; a callable is awaited, so an
        async loader can be passed straight in.
        """
        missing = _MISSING
        cached = await self.get(key, default=missing)
        if cached is not missing:
            return cached
        value = default() if callable(default) else default
        if hasattr(value, "__await__"):
            value = await value
        await self.set(key, value, timeout)
        return value
