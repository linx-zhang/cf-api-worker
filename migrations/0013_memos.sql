-- 个人空间（/space）的随手记：一条 memo 属于一个用户，备忘录与待办是同一张表。
--
-- done 与 done_at 是同一件事的两种表达，必须一起写：
--   勾选完成   -> done = 1, done_at = datetime('now')
--   取消勾选   -> done = 0, done_at = NULL
-- done_at 只表示"最近一次完成的时间"，不保留历史：取消再勾选会覆盖它，
-- 因为界面上只有一个位置展示这个时间，留两份只会自相矛盾。
--
-- due_at 存 'YYYY-MM-DD'，是用户眼里的到期日（本地日期语义，不存 UTC 时刻）：
-- 这一期只要求"列表里标红"，不参与任何提醒调度。真提醒需要另起一个 cron
-- 任务去扫 due_at，那是二期，届时这列不用改。
CREATE TABLE IF NOT EXISTS memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id),
    body TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    done_at TEXT,
    due_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);

-- 列表固定是"我的、未删除、按 created_at DESC, id DESC"：
-- 复合索引把 user_id 的等值过滤和排序一次走完，不做额外 sort。
CREATE INDEX IF NOT EXISTS idx_memos_user_created
    ON memos (user_id, created_at, id);

-- 「待办 / 已完成」分段筛选按 done 过滤，同一份排序键跟在后面。
CREATE INDEX IF NOT EXISTS idx_memos_user_done
    ON memos (user_id, done, created_at);
