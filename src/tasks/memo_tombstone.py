"""Cron job: 把「软删很久」的 memo 真正删掉。

memo 是个人内容，正文上限 2,000 字符，存储成本很低；软删留着的价值是撤销、
回看和「过半年想起还有这件事」。因此保留期给到 180 天，而不是活动记录那种
30 天。硬删之后：

* 空间能回收（正文不再白占着）；
* 列表查询少扫一批死行；
* id 依然不会复用 —— 表是 AUTOINCREMENT，``sqlite_sequence`` 不回落。

这也是唯一一处从 memos 硬删的地方，和 ``activity_retention`` 同样分批，
避免一条大 DELETE 撞上 D1 的语句预算。
"""

from __future__ import annotations

from infra.db import Database

# 墓碑保留期：个人内容半年，覆盖「回头找旧记录」的常见跨度，又不会无限攒。
MEMO_TOMBSTONE_DAYS = 180

# 每批行数：和活动清理同一个量级，单次语句够便宜、正常积压一两轮就能扫完。
DELETE_BATCH = 5000

# 硬上限：一次调度不允许无限循环。
MAX_BATCHES = 20


async def purge_memo_tombstones(db: Database, days: int = MEMO_TOMBSTONE_DAYS) -> int:
    """Delete ``memos`` rows soft-deleted longer than ``days``; return rows removed."""
    modifier = f"-{int(days)} days"
    deleted_total = 0
    for _ in range(MAX_BATCHES):
        deleted = await db.execute(
            "DELETE FROM memos WHERE id IN ("
            "SELECT id FROM memos "
            "WHERE deleted_at IS NOT NULL AND deleted_at < datetime('now', ?) LIMIT ?"
            ")",
            (modifier, DELETE_BATCH),
        )
        deleted_total += deleted
        if deleted < DELETE_BATCH:
            break
    return deleted_total
