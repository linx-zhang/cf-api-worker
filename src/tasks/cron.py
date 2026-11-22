"""Cron entry point: the one place ``entry.py`` calls on a cron trigger.

Wrangler fires two triggers today (``wrangler.jsonc``):

- ``15 18 * * *`` — the nightly maintenance set (retention, cache purge, memo
  tombstones) plus one ledger fold.
- ``0 * * * *`` — hourly ledger fold only. Everything else is daily work and
  would be pure waste twelve times a day.

So this module matches on the cron expression; ``entry.py`` stays dumb.

The ledger fold is the only job whose *read* cost matters at request time: it
runs the three tail aggregates, which scan exactly the rows that arrived since
the last fold.
"""

from __future__ import annotations

import traceback

from infra.db import Database
from tasks.activity_retention import ACTIVITY_RETENTION_DAYS, purge_old_activity
from tasks.analytics_ledger import persist_analytics_ledger
from tasks.cache_cleanup import purge_expired_cache
from tasks.memo_tombstone import MEMO_TOMBSTONE_DAYS, purge_memo_tombstones

# Keep in sync with ``wrangler.jsonc`` -> triggers.crons.
HOURLY_LEDGER_CRON = "0 * * * *"


async def run_cron(env, cron: str = "cron") -> dict[str, int]:
    """Run the job set, logging a failed trigger before re-raising.

    An exception in ``scheduled()`` already reaches the Worker log, but without
    the trigger expression or a stable prefix it is hard to find. Keep the
    runtime's failure semantics (re-raise) so the platform still marks the
    invocation failed.
    """
    try:
        return await _run_cron(env, cron)
    except Exception as exc:
        stack = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).strip()
        print(f"[api-worker] cron failed cron={cron}: {type(exc).__name__}: {exc}\n{stack}")
        raise


async def _run_cron(env, cron: str = "cron") -> dict[str, int]:
    """Run the job set that belongs to this trigger; returns per-job counts.

    ``env`` is the entrypoint's own environment: the ``env`` argument of the
    runtime's ``scheduled()`` handler is not guaranteed to be populated on
    every invocation.

    Everything is printed, not just returned: the scheduled handler's return
    value is discarded by the runtime, and Worker logs are the only place a run
    leaves a trace.
    """
    db = Database(env.DB)

    # 小时级触发器只做账本折叠：其它任务是日更，每小时跑一遍纯属浪费
    if (cron or "").strip() == HOURLY_LEDGER_CRON:
        ledger = await persist_analytics_ledger(db)
        print(
            f"analytics ledger ({cron}): folded {ledger['folded']} rows, "
            f"cursor {ledger['cursor']}, {ledger['days']} days, "
            f"{ledger['actors']} actors"
            + (" (rebuilt)" if ledger["rebuilt"] else "")
        )
        return {"analytics_ledger_rows": int(ledger["folded"] or 0)}

    deleted = await purge_old_activity(db, days=ACTIVITY_RETENTION_DAYS)
    print(
        f"activity retention ({cron}): "
        f"removed {deleted} rows older than {ACTIVITY_RETENTION_DAYS} days"
    )

    purged = await purge_expired_cache(db)
    print(f"cache purge ({cron}): removed {purged} expired entries")

    # memo 墓碑最晚清理：它只影响个人空间的历史，晚一轮没有副作用
    tombstones = await purge_memo_tombstones(db, days=MEMO_TOMBSTONE_DAYS)
    print(
        f"memo tombstones ({cron}): removed {tombstones} rows "
        f"soft-deleted before -{MEMO_TOMBSTONE_DAYS} days"
    )

    # 清理跑完再折叠：已经被删除的旧行不该再进入账本
    ledger = await persist_analytics_ledger(db)
    print(
        f"analytics ledger ({cron}): folded {ledger['folded']} rows, "
        f"cursor {ledger['cursor']}, {ledger['days']} days, "
        f"{ledger['actors']} actors"
        + (" (rebuilt)" if ledger["rebuilt"] else "")
    )

    return {
        "activity_events": deleted,
        "cache_entries": purged,
        "memos": tombstones,
        "analytics_ledger_rows": int(ledger["folded"] or 0),
    }
