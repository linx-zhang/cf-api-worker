"""Cron job: hard delete personal-space consumables that were soft-deleted long ago.

``consumable_items`` and ``consumable_cycles`` are both user data with a
``deleted_at`` tombstone. The API never hard deletes them, so this module is the
only place where those rows go away. The retention window is deliberately shorter
than memos: a consumable cycle is a low-value structured record, and 90 days is
already far beyond the "I might restore it" window.

Ordering matters: ``consumable_cycles.item_id`` references
``consumable_items.id``. Cycles are removed first, and an item is only deleted
after it has no cycle rows left. Both tables use AUTOINCREMENT, so a hard-deleted
id is never reused.

Batches keep a large backlog from turning into one giant DELETE.
"""

from __future__ import annotations

from infra.db import Database

# Tombstone retention: long enough to cover a mistaken delete, short enough that
# dead consumables do not accumulate forever.
CONSUMABLE_TOMBSTONE_DAYS = 90

DELETE_BATCH = 5000
MAX_BATCHES = 20


async def purge_consumable_tombstones(
    db: Database, days: int = CONSUMABLE_TOMBSTONE_DAYS
) -> dict[str, int]:
    """Delete old consumable tombstones; return ``{"cycles": n, "items": n}``."""
    modifier = f"-{int(days)} days"

    # A cycle is dead when its own tombstone is old, or when its parent item is
    # an old tombstone. The second branch also catches any child rows the API
    # failed to mark, so the parent delete below cannot hit a live reference.
    cycles = 0
    for _ in range(MAX_BATCHES):
        deleted = await db.execute(
            "DELETE FROM consumable_cycles WHERE id IN ("
            "SELECT c.id FROM consumable_cycles c "
            "WHERE c.deleted_at < datetime('now', ?) "
            "OR c.item_id IN ("
            "SELECT i.id FROM consumable_items i "
            "WHERE i.deleted_at < datetime('now', ?)"
            ") LIMIT ?"
            ")",
            (modifier, modifier, DELETE_BATCH),
        )
        cycles += deleted
        if deleted < DELETE_BATCH:
            break

    # Only delete an item once every cycle under it is gone. If a single item
    # had more cycles than one run's batch budget, it waits for the next run.
    items = 0
    for _ in range(MAX_BATCHES):
        deleted = await db.execute(
            "DELETE FROM consumable_items WHERE id IN ("
            "SELECT i.id FROM consumable_items i "
            "WHERE i.deleted_at < datetime('now', ?) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM consumable_cycles c WHERE c.item_id = i.id"
            ") LIMIT ?"
            ")",
            (modifier, DELETE_BATCH),
        )
        items += deleted
        if deleted < DELETE_BATCH:
            break

    return {"cycles": cycles, "items": items}


__all__ = ["CONSUMABLE_TOMBSTONE_DAYS", "purge_consumable_tombstones"]
