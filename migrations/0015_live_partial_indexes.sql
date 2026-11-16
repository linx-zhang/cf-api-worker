-- 「存活行」专用索引：把墓碑从索引里剔出去。
--
-- 现状：内容表一律软删（deleted_at IS NULL 才算存活），但索引是全量的，
-- 于是列表查询虽然在 WHERE 里过滤了 deleted_at，索引扫描照样把墓碑读一遍。
-- 线上 posts 的列表查询执行计划就是 `SCAN posts USING INDEX idx_posts_created_id`，
-- memos 按 user_id 命中后也会把 48 行里的 43 行墓碑读出来。
--
-- partial index 带 `WHERE deleted_at IS NULL`，SQLite 只索引满足谓词的行：
-- 索引更小、扫描只看存活行，且查询写法完全不用改——只要 WHERE 里出现同一个
-- 谓词，规划器就会用这条索引。
--
-- 旧的全量索引保留：带 author / q / done 过滤的查询仍需要它们，
-- 而这些索引只有几十 KB，不值得为了省这点空间冒改错索引的风险。
CREATE INDEX IF NOT EXISTS idx_posts_live_created_id
    ON posts (created_at DESC, id DESC) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memos_live_user_created
    ON memos (user_id, created_at DESC, id DESC) WHERE deleted_at IS NULL;

-- 「待办 / 已完成」分段：done 是等值条件，排序键跟在后面
CREATE INDEX IF NOT EXISTS idx_memos_live_user_done
    ON memos (user_id, done, created_at DESC) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_comments_live_post_created_id
    ON comments (post_id, created_at, id) WHERE deleted_at IS NULL;
