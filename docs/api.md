# 接口契约

> 这份文档是**端点表的唯一真源**：路由、鉴权、请求字段、响应形状以这里为准。
> `README.md` 只解释语义和设计理由，不再重复列端点。
> 改路由 / 改字段 / 改权限码时，同一笔提交里改这里（`scripts/check-api-doc.py` 可以核对端点表有没有漏）。
>
> 生成式文档的先例见 `docs/schema.md`：那是从线上 D1 导出的库结构，改表只能走 `migrations/`。
> 这份则是接口层——路由声明在代码里是分散的（7 个 router + `app.py` 的挂载前缀），
> 所以用一张表把它们收拢，代价是必须人工跟着改。

Base URL：`https://linx-api-78i.pages.dev`（生产）。CORS `allow_origins=["*"]`，预检返回 `204`。

## 通用约定

**信封**：所有响应都是 `{"code": string, "detail": string, "data": any}`，成功 `code = "ok"`。
请求失败时 `code` 是业务错误码（见下方错误码表），`detail` 是可以直接展示的英文原句；
前端 `src/api/errors.ts` 是唯一把它们翻成中文的地方。

**鉴权**：`Authorization: Bearer <jwt>`。JWT 只携带用户 id（`sub`）与签发时间（`iat`），
角色每次请求都从 D1 重新读取，改角色立刻生效。`users.token_invalidated_at` 一旦写入
（登出 / 管理员重置密码），早于它的 token 全部判死 → `401 unauthorized`。

**分页**：只用 `limit` + `offset`。`limit` 1–100（默认 20），`offset` ≥ 0。
列表返回 `{items, has_next}`；`page` / `page_size` / `pageSize` 一律 `422 validation_error`。
`/api/admin/users` 与 `/api/admin/invite-codes` 额外带 `total`（界面要显示「共 N 条」），
但翻页必须靠 `has_next`，不能拿 `total` 做算术——`total` 与实际页不同事务。

**排序**：`GET /api/memos` 有到期日的按 `due_at` 升序在前，没有到期日的排后面并按 `created_at` 倒序；
`done=true` 时改按 `done_at` 倒序。`GET /api/admin/users` 按 `last_active_at` 倒序，从没活跃过的按 `created_at` 倒序垫底。
其余列表按 `created_at` 倒序，所有排序都以 `id` 收尾保证稳定。

**时间**：服务端时间戳统一 `TEXT`，`datetime('now')` 写入，即 UTC；本地化只在前端做。
`/api/activity` 的 `ts` 是客户端 epoch 毫秒：允许 30 天内、未来不超过 5 分钟。

**软删除**：`users` / `posts` / `comments` / `memos` / `consumable_items` / `consumable_cycles` 都有 `deleted_at`，读取一律带 `deleted_at IS NULL`；
删帖连带软删它的评论，删评论连带软删回复子树。

## 端点表

| 方法 | 路径 | 鉴权 | 请求 | 响应 `data` |
| --- | --- | --- | --- | --- |
| GET | `/` | 公开 | — | `{service, status}` |
| POST | `/api/auth/register` | 公开 | `{email, password 8-128, invite_code?}` | 201 `TokenOut` |
| POST | `/api/auth/login` | 公开 | `{email, password}` | `TokenOut` |
| GET | `/api/auth/me` | 登录 | — | `MeOut`（含 `permissions[]`） |
| PATCH | `/api/auth/me` | `account:update:self` | 资料字段至少一项 | `MeOut` |
| POST | `/api/auth/logout` | 登录 | — | `{id}`，`detail="logout"` |
| GET | `/api/posts` | 公开 | `limit/offset`、`q≤64`（标题模糊）、`author≤64`（作者模糊）、`author_id≥1`（精确作者） | `{items: PostOut[], has_next}` |
| GET | `/api/posts/{post_id}` | 公开 | — | `PostOut` |
| GET | `/api/posts/{post_id}/neighbors` | 公开 | 同 `/api/posts` 的筛选参数 | `{prev, next}`，各为 `{id,title}` 或 `null` |
| POST | `/api/posts` | `post:create:any` | `{title 1-200, body_md}` | 201 `PostOut` |
| POST | `/api/posts/batch-delete` | `post:delete:any` | `{ids: 1-50 个，自动去重}` | `{deleted: number[], failed: [{id,code,detail}]}` |
| PATCH | `/api/posts/{post_id}` | 本人 `post:update:own` 或 `post:update:any` | `{title?, body_md?}`，至少一项 | `PostOut` |
| DELETE | `/api/posts/{post_id}` | 本人 `post:delete:own` 或 `post:delete:any` | — | `{id, deleted_comments}` |
| GET | `/api/posts/{post_id}/comments` | 公开 | — | `CommentOut[]`（裸数组，不分页） |
| POST | `/api/posts/{post_id}/comments` | `comment:create:any` | `{body 1-5000, parent_id?}` | 201 `CommentOut` |
| DELETE | `/api/comments/{comment_id}` | 本人 `comment:delete:own` 或 `comment:delete:any` | — | `{id, deleted_comments}` |
| POST | `/api/activity` | `activity:write:self` | `{events: 1-50 条}` | `{accepted}` |
| POST | `/api/activity/visitor` | 公开 | `{events: 1-20 条}` | `{accepted}` |
| POST | `/api/clips` | 登录 | `{title ≤80（默认空）, body 1-20000}` | 201 `{code, title, body, created_at, expires_at}` |
| GET | `/api/clips/{code}` | 公开 | `code` 必须是 uuid4 的 32 位十六进制 | `{code, title, body, created_at, expires_at}` |
| DELETE | `/api/clips/{code}` | 本人 `clip:delete:own` 或 `clip:delete:any` | — | `{code}` |
| GET | `/api/memos` | 登录 | `limit/offset`、`done?: bool` | `{items: MemoOut[], has_next}` |
| GET | `/api/memos/summary` | 登录 | — | `{pending, done, due}` |
| POST | `/api/memos` | 登录 | `{body 1-2000, due_at? "YYYY-MM-DD HH:MM:SS"}`（UTC） | 201 `MemoOut` |
| PATCH | `/api/memos/{memo_id}` | 登录 | `{body?, done?, due_at?}`，至少一项 | `MemoOut` |
| DELETE | `/api/memos/{memo_id}` | 登录 | — | `{id}` |
| GET | `/api/consumables` | 登录 | `today?: "YYYY-MM-DD"`（客户端本地今天，缺省用 UTC） | `{items: ConsumableOut[]}`（不分页） |
| POST | `/api/consumables` | 登录 | `{name 1-40, category?, unit? ≤8, quantity? ≥0, lead_days? 0-60, note? ≤200, started_on?, expected_days? 1-3650}`；同时开第一轮 | 201 `ConsumableOut` |
| PATCH | `/api/consumables/{item_id}` | 本人 | `{name?, category?, unit?, quantity?, lead_days?, note?}`，至少一项；三个可空字段显式传 `null` 即清空 | `ConsumableOut` |
| DELETE | `/api/consumables/{item_id}` | 本人 | — | `{id}`（连带软删所有周期） |
| GET | `/api/consumables/{item_id}/cycles` | 本人 | — | `{items: ConsumableCycleOut[]}`，新的在前，含进行中那轮 |
| POST | `/api/consumables/{item_id}/cycles` | 本人 | `{started_on, expected_days?, note?}`；已有进行中周期返回 409 | 201 `ConsumableOut` |
| POST | `/api/consumables/{item_id}/finish` | 本人 | `{ended_on?}`，缺省 UTC 今天；`ended_on < started_on` 返回 422；没有进行中周期返回 409 | `ConsumableOut` |
| PATCH | `/api/consumables/{item_id}/cycles/{cycle_id}` | 本人 | `{started_on?, ended_on?, expected_days?, note?}`；`ended_on: null` 即撤回「用完」，但若还有另一轮开着返回 409 | `ConsumableOut` |
| POST | `/api/admin/invite-codes` | `invite_code:create:any` | `{role: internal\|admin, count 1-50, expires_in_days 1-365, note?≤64}` | 201 `InviteCodeOut[]` |
| GET | `/api/admin/invite-codes` | `invite_code:read:any` | `status?: unused\|used\|expired`、`role?`、`limit/offset` | `{items, has_next, total}` |
| DELETE | `/api/admin/invite-codes/{code}` | `invite_code:delete:any` | — | `{code}` |
| GET | `/api/admin/users` | `user:read:any` | `q?≤200`（邮箱模糊）、`role?`、`limit/offset` | `{items: AdminUserOut[], has_next, total}` |
| PATCH | `/api/admin/users/{user_id}` | `user:update:any` | `{email?, role?, 资料字段?}` | `AdminUserOut` |
| POST | `/api/admin/users/{user_id}/reset-password` | `user:reset_password:any` | `{password? 8-128}` | `{id, email, password, generated, password_reset_at}` |
| DELETE | `/api/admin/users/{user_id}` | `user:delete:any` | — | `{id}` |
| GET | `/api/admin/activity` | `activity:read:any` | `user_id?`、`type?: page_view\|heartbeat\|page_leave`、`from?`、`to?`、`limit/offset` | `{items: ActivityOut[], has_next}` |
| GET | `/api/admin/analytics/summary` | `activity:read:any` | `days 1-90`（默认 7） | `AnalyticsSummaryOut` |

> 路由挂载：`/api/auth`、`/api/posts`、`/api/comments`、`/api/admin`、`/api/activity`、`/api/memos`、`/api/clips`、`/api/consumables`
> （见 `src/api/app.py`）。`comments.py` 同时挂在 `/api/posts` 与 `/api/comments` 两个前缀下。

## 主要对象

**TokenOut**：`{access_token, token_type: "bearer", expires_at: int(epoch 秒), user: UserOut}`

**UserOut / MeOut**：`{id, email, role, created_at, nickname?, first_name?, last_name?, avatar_url?}`；
`MeOut` 另有 `permissions: string[]`。

**PostOut**：`{id, author_id, author_name, title, body_md, created_at, updated_at}`
`author_name` 解析顺序：`nickname` → `"first_name last_name"` → 邮箱本地部分；已注销账号显示 `已注销`。

**CommentOut**：`{id, post_id, author_id, author_name, parent_id?, body, created_at}`（不含邮箱）

**MemoOut**：`{id, body, done, done_at?, due_at?, created_at, updated_at}`；
所有时间列统一是 **UTC 的 `YYYY-MM-DD HH:MM:SS`**（与 `datetime('now')` 同形，不带时区后缀）。
`done_at` 是完成时间，取消勾选时后端清空；`due_at` 是到期**时刻**——客户端把本地那一天的
23:59:59 换算成 UTC 再上送（如北京时间 11-20 到期 → `2026-11-20 15:59:59`），
只给日期的写法会被拒（422），服务端不再替客户端猜时区。

**ConsumableOut**：`{id, name, category, unit?, quantity?, lead_days, note?, created_at, updated_at, cycle?, days_used?, days_left?, due_on?, progress?, status, last_days?, suggested_days?}`
`cycle` 是进行中那一轮（没有则 `null`）；`status ∈ idle|unknown|due|soon|ok` 与所有「还剩几天/该买了」
都是服务端按 `today + started_on + expected_days + lead_days` **现算**的，库里不存派生状态。
`started_on` / `ended_on` / `due_on` 是**本地日 `YYYY-MM-DD`**（不是 memo 那种 UTC 时刻），
客户端上送 `today` 以免跨时区错一天。

**ConsumableCycleOut**：`{id, item_id, started_on, ended_on?, expected_days?, note?, created_at, updated_at, actual_days?}`；
一个物品同时只允许一条 `ended_on` 为空的周期（partial unique index 兜底）。

**ActivityOut**：`{id, user_id?, user_email?, type, path, ts, duration_ms?, ip?, country?, created_at}`；
匿名行的 `user_id` / `user_email` 为 `null`。

**AnalyticsSummaryOut**：`{days, timezone_offset_hours, today:{new_users, active_users, anonymous_visitors, page_views}, series:[{date, page_views, actors}], actors:[{key, user_id?, user_email?, ip?, country?, region?, city?, page_views, dwell_ms, last_seen, first_seen, paths:[{path, views, dwell_ms}]}], has_next}`

## 权限码矩阵

码格式 `resource:action:scope`，真源是 `src/core/permissions.py`。

| 权限码 | `user` / `internal` | `admin` |
| --- | --- | --- |
| `account:read:self` | ✓ | ✓ |
| `account:update:self` | ✓ | ✓ |
| `post:create:any` | ✓ | ✓ |
| `post:update:own` | ✓ | ✓ |
| `post:delete:own` | ✓ | ✓ |
| `comment:create:any` | ✓ | ✓ |
| `comment:delete:own` | ✓ | ✓ |
| `activity:write:self` | ✓ | ✓ |
| `clip:delete:own` | ✓ | ✓ |
| `post:update:any` | | ✓ |
| `post:delete:any` | | ✓ |
| `comment:delete:any` | | ✓ |
| `clip:delete:any` | | ✓ |
| `invite_code:create:any` | | ✓ |
| `invite_code:read:any` | | ✓ |
| `invite_code:delete:any` | | ✓ |
| `user:read:any` | | ✓ |
| `user:update:any` | | ✓ |
| `user:delete:any` | | ✓ |
| `user:reset_password:any` | | ✓ |
| `activity:read:any` | | ✓ |

`PUBLIC` 是描述未登录读权限的伪角色（`post:read:any`、`comment:read:any`），
从不通过 `/api/auth/me` 下发。`/api/memos` 与 `POST /clips` 不设权限码：登录即可，
归属由 token 决定（别人的 memo 一律回答 `404`，不区分「存在但无权」）。

## 限流

| 位置 | 窗口 | 触发后 |
| --- | --- | --- |
| `POST /activity` | 每用户 10 秒一批；含 `page_leave` 的批次永不过滤 | 429 `rate_limited` + `Retry-After` |
| `POST /activity/visitor` | 每 IP 30 秒一批；同上 | 429 `rate_limited` + `Retry-After` |
| `POST /clips` | 每用户 10 秒一条（cache 里的 `throttle:<id>`） | 429 `rate_limited` + `Retry-After: 10` |
| `GET /clips/{code}` | 每分钟每 IP 60 次、每 code 120 次（固定一分钟窗口） | 429 `rate_limited` + `Retry-After` |
| `POST /auth/login` | 15 分钟固定窗口，每 IP 10 次、每账号 5 次（含成功登录，计数在查库前） | 429 `rate_limited` + `Retry-After` |
| `GET /admin/analytics/summary` | 结果缓存 60 秒（key `analytics:summary:v1:<days>`）；缓存未命中时读账本 `analytics:ledger:v2` + 折叠 `cursor < id <= MAX(id)` 的尾巴，尾巴 > 500 行走全量重建 | 不报错，读到旧值 |

## 错误码

HTTP 状态只是传输层，业务含义看 `code`。

| `code` | HTTP | 典型场景 |
| --- | --- | --- |
| `ok` | 2xx | 成功 |
| `validation_error` | 422 | 字段不合法；用了 `page`/`page_size` 也会撞这里 |
| `invalid_email` | 422 | 邮箱格式不对 |
| `invalid_password` | 422 | 密码长度不在 8–128 |
| `invalid_invite_code` | 400 / 409 | 邀请码无效 / 并发抢用 |
| `invalid_invite_role` | 422 | 邀请码角色不是 `internal`/`admin` |
| `email_exists` | 409 | 注册或改邮箱撞已有账号 |
| `invalid_credentials` | 401 | 登录邮箱或密码错（**不是**登录态失效） |
| `unauthorized` | 401 | 缺 token / token 过期 / 早于 `token_invalidated_at` |
| `forbidden` | 403 | 没有对应权限码 |
| `not_found` | 404 | 资源不存在（含别人名下的 memo、clip、非法 code） |
| `method_not_allowed` | 405 | 路径存在但方法不对 |
| `invalid_parent_comment` | 400 | `parent_id` 不属于该文章 |
| `invite_code_in_use` | 400 | 已用掉的邀请码不能删 |
| `cannot_change_own_role` | 403 | 管理员改自己的角色 |
| `last_admin_required` | 403 | 会删/降级掉最后一个 admin |
| `cannot_delete_self` | 403 | 管理员删自己 |
| `invalid_role` | 422 | 角色不在 `user`/`internal`/`admin` |
| `invite_code_in_use` / `invalid_event_type` / `invalid_activity_path` / `invalid_client_address` / `invalid_timestamp` | 400–422 | 活动上报的类型、路径白名单、缺 IP、时间格式 |
| `rate_limited` | 429 | 见限流表 |
| `internal_error` | 500 | 插入失败等（`post`/`comment`/`memo`/`consumable` 各有自己的 detail） |
| `http_error` / `invalid_response` | 前端合成 | 非 JSON 响应、缺 `data` 字段（见 `src/api/client.ts`） |

## 前端对应模块

改端点时两边一起看，这些是前端唯一的调用出口（都经 `src/api/client.ts`）：

| 前端 | 覆盖 |
| --- | --- |
| `src/api/auth.ts` | `/api/auth/*` |
| `src/api/blog.ts` | `/api/posts`、`/api/posts/{id}/comments`、`/api/comments/{id}` |
| `src/api/memos.ts` | `/api/memos*` |
| `src/api/consumables.ts` | `/api/consumables*` |
| `src/api/clips.ts` | `/api/clips*` |
| `src/api/admin.ts` | `/api/admin/*` |
| `src/activity/` | `/api/activity`、`/api/activity/visitor` |

前端 `src/constants.ts` 里的上限（`PAGE_SIZE`、`MEMO_BODY_MAX`、`POST_TITLE_MAX`、
`COMMENT_BODY_MAX`、`CLIP_TITLE_MAX`、`CLIP_BODY_MAX`、`ACTIVITY_MAX_BATCH`、
`ACTIVITY_MAX_VISITOR_BATCH`、`ACTIVITY_VISITOR_WINDOW_MS`、`BATCH_DELETE_MAX`）
逐条对应这里的模型约束；改后端上限必须先改前端常量，否则会出现「前端放行、后端 422」。
