-- 简历管理：后台维护 HTML 简历，`/profile` 读默认简历，分享短链按期限放行。
--
-- 为什么正文存 HTML 而不是结构化 JSON：简历本身是一份排版文档，后台编辑器
-- 直接编辑 HTML，渲染时由前端做白名单净化（见 src/utils/resumeHtml.ts）。
-- 库里的内容没有第二份真源，复制/分享都指向同一行。
--
-- is_default 用 partial unique index 保证最多一份默认；服务层先清空再设置，
-- 允许短暂「没有默认」——此时 /profile 回退到内置模板，不会 500。
CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    content_html TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_by INTEGER REFERENCES users (id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_resumes_live_created_id
    ON resumes (created_at DESC, id DESC) WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_resumes_single_default
    ON resumes (is_default) WHERE is_default = 1 AND deleted_at IS NULL;

-- 分享链接是活指针：/r/<code> 每次读当前简历内容；简历软删或链接过期/撤销即 404。
-- code 是可撤销的临时凭据，不参与任何后台列表以外的展示。
CREATE TABLE IF NOT EXISTS resume_shares (
    code TEXT PRIMARY KEY,
    resume_id INTEGER NOT NULL REFERENCES resumes (id),
    expires_at TEXT NOT NULL,
    created_by INTEGER REFERENCES users (id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_resume_shares_resume_created
    ON resume_shares (resume_id, created_at DESC);
