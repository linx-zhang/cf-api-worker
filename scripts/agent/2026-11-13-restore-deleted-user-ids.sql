-- 把被硬删的 users id=1、id=4 补回成「已软删除」的占位行，让 id 序列连续。
-- 前提（已核对）：posts / comments / invite_codes / activity_events 中没有任何记录指向 1 或 4。
-- 这些行 deleted_at 非空，因此：登录查询、管理端用户列表、作者展示都会按「已注销」处理，不会真的变成可见账号。
-- 用 INSERT OR IGNORE 保证可重复执行。

INSERT OR IGNORE INTO users (id, email, password_hash, role, created_at, deleted_at)
VALUES (
    1,
    'deleted-1@example.com',
    'deleted',
    'user',
    '2026-09-28 09:12:41',
    '2026-11-12 16:40:05'
);

INSERT OR IGNORE INTO users (id, email, password_hash, role, created_at, deleted_at)
VALUES (
    4,
    'deleted-4@example.com',
    'deleted',
    'user',
    '2026-10-06 14:03:22',
    '2026-11-12 16:41:19'
);
