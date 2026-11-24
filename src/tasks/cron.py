"""Cron entry point: the one place ``entry.py`` calls on a cron trigger.

Wrangler fires two triggers today (``wrangler.jsonc``):

- ``15 18 * * *`` — the nightly maintenance set: activity retention, cache purge,
  memo tombstones, consumable tombstones and dead resume share links.
- ``0 * * * *`` — hourly analytics ledger fold only.

The daily trigger deliberately does **not** fold the ledger: the hourly fold is
45 minutes away, and ``load_summary`` folds the tail in memory on read, so a
second fold here is duplicate work.

Which data is intentionally **never** purged here:

* ``users`` — accounts and audit trail; even soft-deleted rows stay.
* ``resumes`` — resume documents; soft-deleted rows stay by product decision.
* ``posts`` / ``comments`` — blog content; soft-deleted rows stay so posts and
  discussion can be restored or inspected.
* ``invite_codes`` — invite issuance/use history; small and worth keeping.

Only disposable rows are deleted: old activity events, expired cache entries,
old memo/consumable tombstones, invalid resume share tokens.
"""

from __future__ import annotations

import traceback

from core.constants import ACTIVITY_RETENTION_DAYS
from infra.db import Database
from tasks.activity_retention import purge_old_activity
from tasks.analytics_ledger import persist_analytics_ledger
from tasks.cache_cleanup import purge_expired_cache
from tasks.consumable_tombstone import (
    CONSUMABLE_TOMBSTONE_DAYS,
    purge_consumable_tombstones,
)
from tasks.memo_tombstone import MEMO_TOMBSTONE_DAYS, purge_memo_tombstones
from tasks.resume_share_cleanup import purge_invalid_resume_shares

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

    # 个人内容先清：memo 保留 180 天，比活动记录宽容得多
    tombstones = await purge_memo_tombstones(db, days=MEMO_TOMBSTONE_DAYS)
    print(
        f"memo tombstones ({cron}): removed {tombstones} rows "
        f"soft-deleted before -{MEMO_TOMBSTONE_DAYS} days"
    )

    # 消耗品是结构化低价值记录，90 天足够覆盖误删；cycles 必须先于 items 删
    consumables = await purge_consumable_tombstones(
        db, days=CONSUMABLE_TOMBSTONE_DAYS
    )
    print(
        f"consumable tombstones ({cron}): removed {consumables['cycles']} cycles "
        f"and {consumables['items']} items soft-deleted before "
        f"-{CONSUMABLE_TOMBSTONE_DAYS} days"
    )

    # 分享短链只是临时凭据：撤销、过期或指向已软删简历即删，没有缓冲期。
    # 注意这里只删 resume_shares，resumes 自身的软删墓碑永远保留。
    shares = await purge_invalid_resume_shares(db)
    print(f"resume share cleanup ({cron}): removed {shares} invalid links")

    # 不在这里折分析账本：小时触发器 45 分钟后会做，且管理页读取时会在内存补折 tail。
    # users / resumes / posts / comments / invite_codes 是有价值的历史数据，永不清理。
    return {
        "activity_events": deleted,
        "cache_entries": purged,
        "memos": tombstones,
        "consumable_cycles": consumables["cycles"],
        "consumable_items": consumables["items"],
        "resume_shares": shares,
    }
