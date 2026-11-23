"""Resume storage and share-link policy.

Framework-free, like ``services/invite_codes.py``: the router owns HTTP and
permissions, this module owns SQL and the small invariants around it.

Two invariants matter:

1. At most one live resume is ``is_default = 1``. The migration has a partial
   unique index; ``set_default`` clears first, then sets, so the window between
   the two statements may have no default. That is intentional and harmless:
   ``/profile`` falls back to its built-in template when ``current`` is missing.
2. Share links are live pointers, not snapshots. Reading resolves the resume on
   every request, so editing a resume updates the link; soft-deleting the resume
   or revoking/expiring the link makes it 404 without a second cleanup pass.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from core.utils import sqlite_timestamp, utc_now
from infra.db import Database

SHARE_CODE_LENGTH = 12
_SHARE_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_SUMMARY_COLUMNS = "r.id, r.title, r.note, r.is_default, r.created_at, r.updated_at"
_DETAIL_COLUMNS = f"{_SUMMARY_COLUMNS}, r.content_html"


def _summary_out(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "note": row["note"] or "",
        "is_default": bool(row["is_default"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _detail_out(row: dict) -> dict:
    data = _summary_out(row)
    data["content_html"] = row["content_html"]
    return data


def _new_code() -> str:
    """12 chars from an unambiguous alphabet: ~59 bits, plenty for a capability."""
    return "".join(secrets.choice(_SHARE_ALPHABET) for _ in range(SHARE_CODE_LENGTH))


async def list_resumes(
    db: Database, *, limit: int = 20, offset: int = 0
) -> tuple[list[dict], int]:
    total_row = await db.fetch_one(
        "SELECT COUNT(*) AS total FROM resumes WHERE deleted_at IS NULL"
    )
    total = int(total_row["total"]) if total_row else 0
    rows = await db.fetch_all(
        f"SELECT {_SUMMARY_COLUMNS} FROM resumes r "
        "WHERE r.deleted_at IS NULL "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [_summary_out(row) for row in rows], total


async def get_resume(db: Database, resume_id: int) -> dict | None:
    row = await db.fetch_one(
        f"SELECT {_DETAIL_COLUMNS} FROM resumes r "
        "WHERE r.id = ? AND r.deleted_at IS NULL",
        (resume_id,),
    )
    return None if row is None else _detail_out(row)


async def current_resume(db: Database) -> dict | None:
    row = await db.fetch_one(
        f"SELECT {_DETAIL_COLUMNS} FROM resumes r "
        "WHERE r.is_default = 1 AND r.deleted_at IS NULL "
        "ORDER BY r.updated_at DESC, r.id DESC LIMIT 1"
    )
    return None if row is None else _detail_out(row)


async def create_resume(
    db: Database, *, title: str, note: str, content_html: str, created_by: int
) -> dict:
    row = await db.insert_returning(
        "INSERT INTO resumes (title, note, content_html, created_by) "
        "VALUES (?, ?, ?, ?) RETURNING id",
        (title, note, content_html, created_by),
    )
    assert row is not None
    resume_id = int(row["id"])
    created = await get_resume(db, resume_id)
    assert created is not None
    return created


async def update_resume(
    db: Database,
    resume_id: int,
    *,
    title: str | None = None,
    note: str | None = None,
    content_html: str | None = None,
) -> dict | None:
    fields: list[str] = []
    params: list[object] = []
    if title is not None:
        fields.append("title = ?")
        params.append(title)
    if note is not None:
        fields.append("note = ?")
        params.append(note)
    if content_html is not None:
        fields.append("content_html = ?")
        params.append(content_html)
    if not fields:
        return await get_resume(db, resume_id)
    params.append(resume_id)
    await db.execute(
        f"UPDATE resumes SET {', '.join(fields)}, updated_at = datetime('now') "
        "WHERE id = ? AND deleted_at IS NULL",
        params,
    )
    return await get_resume(db, resume_id)


async def delete_resume(db: Database, resume_id: int) -> str:
    """Soft delete; returns ``deleted``, ``default`` or ``not_found``."""
    row = await db.fetch_one(
        "SELECT is_default FROM resumes WHERE id = ? AND deleted_at IS NULL",
        (resume_id,),
    )
    if row is None:
        return "not_found"
    if row["is_default"]:
        return "default"
    await db.execute(
        "UPDATE resumes SET deleted_at = datetime('now') WHERE id = ?",
        (resume_id,),
    )
    await db.execute(
        "UPDATE resume_shares SET revoked_at = datetime('now') "
        "WHERE resume_id = ? AND revoked_at IS NULL",
        (resume_id,),
    )
    return "deleted"


async def copy_resume(
    db: Database, resume_id: int, *, created_by: int
) -> dict | None:
    source = await get_resume(db, resume_id)
    if source is None:
        return None
    title = f"{source['title'][: RESUME_COPY_TITLE_BUDGET]} 副本"
    return await create_resume(
        db,
        title=title,
        note=source["note"],
        content_html=source["content_html"],
        created_by=created_by,
    )


# Leave room for the " 副本" suffix inside RESUME_TITLE_MAX without importing
# the HTTP model into this module.
RESUME_COPY_TITLE_BUDGET = 96


async def set_default(db: Database, resume_id: int) -> dict | None:
    exists = await db.fetch_one(
        "SELECT id FROM resumes WHERE id = ? AND deleted_at IS NULL", (resume_id,)
    )
    if exists is None:
        return None
    # Clear then set: with the partial unique index this is the safe order.
    await db.execute(
        "UPDATE resumes SET is_default = 0 "
        "WHERE is_default = 1 AND deleted_at IS NULL"
    )
    await db.execute(
        "UPDATE resumes SET is_default = 1, updated_at = datetime('now') "
        "WHERE id = ? AND deleted_at IS NULL",
        (resume_id,),
    )
    row = await db.fetch_one(
        f"SELECT {_SUMMARY_COLUMNS} FROM resumes r "
        "WHERE r.id = ? AND r.deleted_at IS NULL",
        (resume_id,),
    )
    return None if row is None else _summary_out(row)


async def create_share(
    db: Database, resume_id: int, *, expires_in_minutes: int, created_by: int
) -> dict | None:
    exists = await db.fetch_one(
        "SELECT id FROM resumes WHERE id = ? AND deleted_at IS NULL", (resume_id,)
    )
    if exists is None:
        return None
    expires_at = sqlite_timestamp(utc_now() + timedelta(minutes=expires_in_minutes))
    for _ in range(5):
        code = _new_code()
        taken = await db.fetch_one(
            "SELECT 1 AS found FROM resume_shares WHERE code = ?", (code,)
        )
        if taken is not None:
            continue
        await db.execute(
            "INSERT INTO resume_shares (code, resume_id, expires_at, created_by) "
            "VALUES (?, ?, ?, ?)",
            (code, resume_id, expires_at, created_by),
        )
        created = await db.fetch_one(
            "SELECT code, expires_at, created_at FROM resume_shares WHERE code = ?",
            (code,),
        )
        if created is None:
            raise RuntimeError("resume share row disappeared after insert")
        return dict(created)
    raise RuntimeError("could not allocate a unique resume share code")


async def list_shares(db: Database, resume_id: int) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT code, expires_at, created_at FROM resume_shares "
        "WHERE resume_id = ? AND revoked_at IS NULL AND expires_at > datetime('now') "
        "ORDER BY created_at DESC, code ASC",
        (resume_id,),
    )
    return [dict(row) for row in rows]


async def revoke_share(db: Database, code: str) -> bool:
    changed = await db.execute(
        "UPDATE resume_shares SET revoked_at = datetime('now') "
        "WHERE code = ? AND revoked_at IS NULL",
        (code,),
    )
    return changed > 0


async def resolve_share(db: Database, code: str) -> dict | None:
    row = await db.fetch_one(
        "SELECT r.title, r.content_html, r.updated_at "
        "FROM resume_shares s JOIN resumes r ON r.id = s.resume_id "
        "WHERE s.code = ? AND s.revoked_at IS NULL "
        "AND s.expires_at > datetime('now') AND r.deleted_at IS NULL",
        (code,),
    )
    return None if row is None else dict(row)
