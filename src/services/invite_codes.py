"""Invite-code policy and D1 access shared by the admin API.

Framework-free (no FastAPI): the router imports ``generate_invite_codes`` for
the code shape and writes rows through the async helpers below, against the D1
binding.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from core.utils import sqlite_timestamp, utc_now
from infra.db import Database

INVITE_ROLES = ("internal", "admin")
MIN_COUNT, MAX_COUNT = 1, 50
MIN_DAYS, MAX_DAYS = 1, 365


# Status buckets are mutually exclusive so filters do not overlap.
def _status_conditions(alias: str = "") -> dict[str, str]:
    prefix = f"{alias}." if alias else ""
    return {
        "unused": (
            f"{prefix}used_at IS NULL AND ({prefix}expires_at IS NULL "
            f"OR {prefix}expires_at > datetime('now'))"
        ),
        "used": f"{prefix}used_at IS NOT NULL",
        "expired": (
            f"{prefix}used_at IS NULL AND {prefix}expires_at IS NOT NULL "
            f"AND {prefix}expires_at <= datetime('now')"
        ),
    }


STATUS_CONDITIONS = _status_conditions()
UNUSED_CONDITION = STATUS_CONDITIONS["unused"]
USED_CONDITION = STATUS_CONDITIONS["used"]
EXPIRED_CONDITION = STATUS_CONDITIONS["expired"]
_ALIASED_STATUS_CONDITIONS = _status_conditions("ic")

_SELECT = (
    "SELECT ic.code, ic.role, ic.expires_at, ic.created_at, ic.used_at, "
    "ic.created_by, ic.note, ic.used_by, "
    "u.email AS created_by_email, ub.email AS used_by_email "
    "FROM invite_codes ic LEFT JOIN users u ON u.id = ic.created_by "
    "LEFT JOIN users ub ON ub.id = ic.used_by"
)


def generate_invite_codes(
    role: str, count: int, expires_in_days: int | None, note: str | None = None
) -> list[dict]:
    """Build ``count`` uuid4 codes; ``expires_at`` matches D1's timestamp format."""
    expires_at = None
    if expires_in_days is not None:
        expires_at = sqlite_timestamp(utc_now() + timedelta(days=expires_in_days))
    return [
        {
            "code": str(uuid.uuid4()),
            "role": role,
            "expires_at": expires_at,
            "note": note,
        }
        for _ in range(count)
    ]


async def insert_codes(
    db: Database, codes: list[dict], created_by: int | None = None
) -> list[dict]:
    if not codes:
        return []
    values = ", ".join("(?, ?, ?, ?, ?)" for _ in codes)
    params: list[object] = []
    for item in codes:
        params.extend(
            (
                item["code"],
                item["role"],
                item["expires_at"],
                created_by,
                item.get("note"),
            )
        )
    await db.execute(
        "INSERT INTO invite_codes (code, role, expires_at, created_by, note) "
        f"VALUES {values}",
        params,
    )
    return await list_by_codes(db, [item["code"] for item in codes])


async def list_by_codes(db: Database, codes: list[str]) -> list[dict]:
    if not codes:
        return []
    placeholders = ", ".join("?" for _ in codes)
    return await db.fetch_all(
        f"{_SELECT} WHERE ic.code IN ({placeholders}) ORDER BY ic.created_at, ic.code",
        codes,
    )


async def list_codes(
    db: Database,
    *,
    status: str | None = None,
    role: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append(_ALIASED_STATUS_CONDITIONS[status])
    if role is not None:
        clauses.append("ic.role = ?")
        params.append(role)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    total_row = await db.fetch_one(
        f"SELECT COUNT(*) AS total FROM invite_codes ic{where}", params
    )
    total = int(total_row["total"]) if total_row else 0
    rows = await db.fetch_all(
        f"{_SELECT}{where} ORDER BY ic.created_at DESC, ic.code ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return rows, total


async def delete_unused(db: Database, code: str) -> str:
    """Delete an unused code; returns ``deleted``, ``used`` or ``not_found``."""
    deleted = await db.execute(
        "DELETE FROM invite_codes WHERE code = ? AND used_at IS NULL", (code,)
    )
    if deleted:
        return "deleted"
    row = await db.fetch_one("SELECT used_at FROM invite_codes WHERE code = ?", (code,))
    return "not_found" if row is None else "used"
