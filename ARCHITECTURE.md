# 后端架构与维护边界

这份文档回答三个问题：**代码为什么这样分层、改动应该落在哪一层、哪些边界有自动闸门**。
部署命令和线上口径看 `AGENTS.md`，接口字段看 `docs/api.md`，表结构看 `docs/schema.md`。

## 1. 运行时形状

```
浏览器 / 前端
    │  HTTPS
    ▼
Cloudflare Worker（src/entry.py）
    │  fetch → ASGI
    ▼
FastAPI（src/api/app.py）
    │  路由 + 依赖注入
    ▼
src/api/routers/*  ──►  src/services/*  ──►  src/infra/*  ──►  D1
      │                     │                 │
      └──────────────►  src/core/*  ◄─────────┘   （纯规则 / 常量）
    ▲
    │  scheduled(controller, env, ctx)
src/tasks/cron.py  ──►  src/tasks/*  ──►  services / infra
```

- `src/entry.py` 只做运行时胶水：把 Worker 的 fetch 转成 ASGI，把 `request.cf` 里的 geo 字段补成请求头，把 cron 交给 `tasks.cron`。
- `src/api/app.py` 只做应用装配：CORS、统一响应、异常处理、路由挂载。业务逻辑不写在这里。
- 请求的「HTTP 外壳」在 `api/`，「领域规则」在 `services/`，「I/O 适配」在 `infra/`，「纯规则」在 `core/`。

## 2. 六个层各管什么

| 层 | 目录 | 负责 | 不负责 |
|---|---|---|---|
| entry | `src/entry.py` | Worker 入口、ASGI/cron 转接、geo 头 | 业务判断、SQL |
| api | `src/api/` | 路由、认证依赖、Pydantic 校验、响应封装、单资源 SQL | 跨资源的领域编排、定时任务 |
| services | `src/services/` | 被 HTTP 与 cron 共用的领域逻辑、分析账本、邀请码 | HTTP 状态码、`Request` 对象 |
| infra | `src/infra/` | D1 包装、缓存、配置、密码学适配 | 业务语义、权限判断 |
| core | `src/core/` | 权限码、契约常量、JWT/PBKDF2 原语、时间工具 | 任何网络 / D1 / 框架依赖 |
| tasks | `src/tasks/` | cron 编排与批处理清理 | 被 HTTP 直接调用 |

`src/api/routers/` 内部按资源分文件；`src/api/models.py` 是请求/响应模型与部分契约常量的集中地；`src/api/responses.py` 是唯一构造 `{code, detail, data}` 的地方。

### 为什么没有 repository 层

这个项目只有一张 D1、十来个资源，路由器里直接写单资源 SQL 比再包一层 repository 更便宜、更容易读。
只有满足下面任一条件时才把逻辑抽到 `services/`：

- HTTP 和 cron 都要用（例如 `analytics_ledger`、`invite_codes`）；
- 一次操作要跨多张表、多个步骤，且需要统一语义（例如账本折叠）；
- 规则需要脱离 FastAPI 单测（纯函数放 `services/analytics_merge.py` 这类模块）。

否则 SQL 留在对应 router，不要为了「看起来分层」增加转发层。

## 3. 依赖方向（CI 已强制）

允许的方向只有一条：

```
core ← infra ← services ← api
                         ↖ tasks
entry（组合根，可 import api / tasks）
```

同层 import 允许；低层 import 高层、`api` import `tasks` 都算违规。
`scripts/check-layers.py` 用 AST 解析 `src` 下所有 `.py` 的绝对 import，CI 的「分层依赖」步骤会跑；本地命令：

```powershell
uv run --frozen python scripts/check-layers.py
```

这条闸门不是形式主义：`api/routers/admin.py` 曾经为了一个 `ACTIVITY_RETENTION_DAYS` 直接 import `tasks/activity_retention.py`，后来该常量已移到 `core/constants.py`。这类「顺手拿一下」的依赖会让定时任务和 HTTP 层缠在一起。

## 4. 一次请求怎么走

1. `entry.py` 收到 fetch，补 geo 头，交给 FastAPI。
2. `api/deps.py` 解析 Bearer token、加载用户、暴露 `get_db()` / `require_permission()`。
3. router 用 Pydantic 模型校验输入；单资源 SQL 直接查 D1；需要复用规则时调 `services/`。
4. 成功统一走 `success(data)`；业务失败抛 `ApiError(code, detail, status)`。
5. `api/responses.py` 兜住所有异常：4xx 用统一 envelope，500 记录 request id + traceback，并回 `x-request-id`。

### 时间与软删除

- 全站存 UTC datetime 字符串 `YYYY-MM-DD HH:MM:SS`。
- 唯一例外：消耗品周期 `started_on/ended_on`、备忘录 `due_on` 是「客户端本地日」`YYYY-MM-DD`，由请求里的 `today` 对齐时区。
- 读用 `deleted_at IS NULL`，写用 `UPDATE ... SET deleted_at = datetime('now')`；不要物理删除用户可见数据。
- 分析按**北京展示日**分桶，不是滚动 24 小时。

## 5. 改动落在哪

### 新增一个接口

1. `api/models.py`：请求/响应模型与字段校验。
2. `api/routers/<resource>.py`：路由、权限依赖、SQL。
3. `docs/api.md`：补一行契约（`scripts/check-api-doc.py` 会对路由装饰器数量与文档行数）。
4. `tests/`：至少覆盖成功、参数非法、未授权三条路径。
5. `npm run api:doc` 或 `uv run --frozen python scripts/check-api-doc.py`。

### 新增表 / 字段

1. `migrations/NNNN_snake_case.sql`，编号连续，文件非空（`tests/test_migrations.py` 会查）。
2. 软删除字段、索引、默认值一起写；SQL 注释说明「为什么」而不是复述列名。
3. 更新 `docs/schema.md`（`npm run schema`）和 `docs/api.md`（如果字段出现在响应里）。
4. 生产迁移只能手工 `wrangler d1 migrations apply`，先 deploy 后迁移不会报错但行为不对。

### 新增 cron 任务

1. `tasks/<job>.py`：批处理、上限、返回删除行数。
2. `tasks/cron.py`：按 cron 表达式挂到日更或小时触发器。
3. `wrangler.jsonc`：确认触发表达式。
4. `tests/test_cron.py`：失败日志与顺序。
5. `AGENTS.md` 的「索引与清理任务现状」同步。

### 新增共享常量

先判断它属于哪一类：

- **公开契约**（分页、字段长度、活动批量上限）：真源在 `api/models.py` / `api/deps.py`，前端 `src/constants.ts` 手抄，`scripts/check-contract.mjs` 比对。
- **内部策略**（保留天数、批大小）：放 `core/constants.py` 或对应 `services/tasks` 模块，不需要前端同步。

改公开契约时**先后端 push，再前端 push**；前端 CI 会 clone 后端 main 比对。

## 6. 测试地图

| 文件 | 覆盖 |
|---|---|
| `tests/conftest.py` | 内存 SQLite、迁移全量执行、FastAPI 测试客户端 |
| `tests/test_routes.py` | 路由级鉴权、分页、软删、404/422 |
| `tests/test_limits.py` | 登录/注册/活动限流与 `page_leave` 窗口 |
| `tests/test_memos.py` | 备忘录 CRUD、月历窗口、`undated` |
| `tests/test_consumables.py` | 消耗品周期、日期边界、409 |
| `tests/test_analytics_ledger.py` | 账本与全量参考实现逐字段相等 |
| `tests/test_responses.py` | 统一 envelope、500 日志、request id |
| `tests/test_migrations.py` | 迁移命名/编号/非空 |
| `tests/test_cron.py` | cron 失败日志与顺序 |

测试不启 workerd；`pywrangler dev` 只用于手工联调。部署后的冒烟仍是手敲，见 `AGENTS.md`。

## 7. 已知的、刻意保留的债

- `api/models.py` 是单文件（约 680 行）。它让契约集中，但也意味着改多个资源时容易冲突；超过约 1000 行再按资源拆包。
- 路由器直接写 SQL，没有 repository 层；理由见上，不要为了统一而统一。
- `api/routers/consumables.py`、`services/analytics_merge.py` 仍偏大；拆之前先补行为测试。
- 部署不是 CI 自动完成：`pywrangler deploy` 和线上冒烟靠人。不要误以为 CI 绿了就已经上线。
- 契约检查只覆盖前端一侧；后端单独改 `models.py` 不会立刻红，要等下一次前端 CI。

## 8. 相关文档

- `README.md`：项目全貌、接口、数据库、缓存、部署。
- `AGENTS.md`：上线命令、坑、当前索引与 cron 状态。
- `docs/api.md`：接口契约（路由数由脚本对齐）。
- `docs/schema.md`：线上 D1 表结构快照。
- 前端仓库 `ARCHITECTURE.md`：页面/组件/API 的分层与前端数据流。
