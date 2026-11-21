-- 个人空间「日常消耗品」：以周期为主，日常只需要点一次「用完了」。
--
-- 一个物品（consumable_items）可以有多个周期（consumable_cycles）：
--   started_on  = 开封/开始使用的那一天（本地日，YYYY-MM-DD）
--   ended_on    = 用完的那一天；NULL 表示这一轮还在进行中
-- 一个物品同时最多一个进行中周期，交给下面的 partial unique index 兜底，
-- 而不是靠接口层记得先关旧周期——并发点两次也不会写出两轮。
--
-- 没有任何"消耗日志"：不记录每次用了多少，预测只依赖上一轮的实际天数。
-- 数量/单位（quantity/unit）只是展示用的辅助字段，不参与任何计算。
CREATE TABLE IF NOT EXISTS consumable_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id),
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    unit TEXT,
    quantity REAL,
    lead_days INTEGER NOT NULL DEFAULT 7 CHECK (lead_days BETWEEN 0 AND 60),
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS consumable_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES consumable_items (id),
    started_on TEXT NOT NULL,
    ended_on TEXT,
    expected_days INTEGER CHECK (expected_days IS NULL OR expected_days > 0),
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT,
    CHECK (ended_on IS NULL OR ended_on >= started_on)
);

-- 列表固定是「我的、未删除」：和 memos 的存活行索引同一形状。
CREATE INDEX IF NOT EXISTS idx_consumable_items_live_user_created
    ON consumable_items (user_id, created_at, id) WHERE deleted_at IS NULL;

-- 按物品取周期（拿进行中的那一条、算最近两轮的实际天数）。
CREATE INDEX IF NOT EXISTS idx_consumable_cycles_live_item_started
    ON consumable_cycles (item_id, started_on) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_consumable_cycles_live_item_ended
    ON consumable_cycles (item_id, ended_on) WHERE deleted_at IS NULL;

-- 一个物品只能有一个进行中周期；「先不买」时这里没有行，状态自然是待补货。
CREATE UNIQUE INDEX IF NOT EXISTS idx_consumable_cycles_open_item
    ON consumable_cycles (item_id) WHERE ended_on IS NULL AND deleted_at IS NULL;
