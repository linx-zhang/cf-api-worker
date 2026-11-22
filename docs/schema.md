# 数据库表结构

> 由 `node scripts/dump-schema.mjs`（`npm run schema`）从线上 D1 `api-worker-db` 实际结构导出，**不要手改**——下一次生成会覆盖。
> 改表的唯一入口是 `migrations/`：加完迁移后重跑一次本脚本。
>
> 为什么用这种生成式文档而不是手写 ORM 模型：项目里没有 ORM，手写模型只能靠记忆维护，会和真实库悄悄对不上；这份文档每次都是从库里读出来的。

共 9 张表：`activity_events`、`cache_entries`、`comments`、`consumable_cycles`、`consumable_items`、`invite_codes`、`memos`、`posts`、`users`

## activity_events

| 列 | 类型 | 可空 | 默认 | 主键 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 |  | 1 |
| `user_id` | INTEGER | 是 |  |  |
| `type` | TEXT | 否 |  |  |
| `path` | TEXT | 否 |  |  |
| `ts` | INTEGER | 否 |  |  |
| `duration_ms` | INTEGER | 是 |  |  |
| `ip` | TEXT | 是 |  |  |
| `country` | TEXT | 是 |  |  |
| `region` | TEXT | 是 |  |  |
| `city` | TEXT | 是 |  |  |
| `created_at` | TEXT | 否 | `datetime('now')` |  |

索引：

- `idx_activity_events_created_id`（普通）：`created_at, id`
- `idx_activity_events_ip_created`（普通）：`ip, created_at`
- `idx_activity_events_user_created`（普通）：`user_id, created_at`

外键：

- `user_id` → `users.id`

## cache_entries

| 列 | 类型 | 可空 | 默认 | 主键 |
| --- | --- | --- | --- | --- |
| `key` | TEXT | 否 |  | 1 |
| `value` | TEXT | 否 |  |  |
| `expires_at` | TEXT | 否 |  |  |
| `created_at` | TEXT | 否 | `datetime('now')` |  |
| `updated_at` | TEXT | 否 | `datetime('now')` |  |

索引：

- `idx_cache_entries_expires_at`（普通）：`expires_at`

## comments

| 列 | 类型 | 可空 | 默认 | 主键 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 |  | 1 |
| `post_id` | INTEGER | 否 |  |  |
| `author_id` | INTEGER | 否 |  |  |
| `parent_id` | INTEGER | 是 |  |  |
| `body` | TEXT | 否 |  |  |
| `created_at` | TEXT | 否 | `datetime('now')` |  |
| `deleted_at` | TEXT | 是 |  |  |

索引：

- `idx_comments_live_post_created_id`（普通）：``
- `idx_comments_parent_id`（普通）：`parent_id`
- `idx_comments_post_created_id`（普通）：`post_id, created_at, id`

外键：

- `parent_id` → `comments.id`
- `author_id` → `users.id`
- `post_id` → `posts.id`

## consumable_cycles

| 列 | 类型 | 可空 | 默认 | 主键 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 |  | 1 |
| `item_id` | INTEGER | 否 |  |  |
| `started_on` | TEXT | 否 |  |  |
| `ended_on` | TEXT | 是 |  |  |
| `expected_days` | INTEGER | 是 |  |  |
| `note` | TEXT | 是 |  |  |
| `created_at` | TEXT | 否 | `datetime('now')` |  |
| `updated_at` | TEXT | 否 | `datetime('now')` |  |
| `deleted_at` | TEXT | 是 |  |  |

索引：

- `idx_consumable_cycles_live_item_ended`（普通）：``
- `idx_consumable_cycles_live_item_started`（普通）：``
- `idx_consumable_cycles_open_item`（唯一）：``

外键：

- `item_id` → `consumable_items.id`

## consumable_items

| 列 | 类型 | 可空 | 默认 | 主键 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 |  | 1 |
| `user_id` | INTEGER | 否 |  |  |
| `name` | TEXT | 否 |  |  |
| `category` | TEXT | 否 | `'other'` |  |
| `unit` | TEXT | 是 |  |  |
| `quantity` | REAL | 是 |  |  |
| `lead_days` | INTEGER | 否 | `7` |  |
| `note` | TEXT | 是 |  |  |
| `created_at` | TEXT | 否 | `datetime('now')` |  |
| `updated_at` | TEXT | 否 | `datetime('now')` |  |
| `deleted_at` | TEXT | 是 |  |  |

索引：

- `idx_consumable_items_live_user_created`（普通）：``

外键：

- `user_id` → `users.id`

## invite_codes

| 列 | 类型 | 可空 | 默认 | 主键 |
| --- | --- | --- | --- | --- |
| `code` | TEXT | 否 |  | 1 |
| `role` | TEXT | 否 | `'internal'` |  |
| `created_at` | TEXT | 否 | `datetime('now')` |  |
| `expires_at` | TEXT | 是 |  |  |
| `used_at` | TEXT | 是 |  |  |
| `used_by` | INTEGER | 是 |  |  |
| `created_by` | INTEGER | 是 |  |  |
| `note` | TEXT | 是 |  |  |

索引：

- `idx_invite_codes_created_code`（普通）：`created_at, code`
- `idx_invite_codes_used_at`（普通）：`used_at`

外键：

- `created_by` → `users.id`
- `used_by` → `users.id`

## memos

| 列 | 类型 | 可空 | 默认 | 主键 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 |  | 1 |
| `user_id` | INTEGER | 否 |  |  |
| `body` | TEXT | 否 |  |  |
| `done` | INTEGER | 否 | `0` |  |
| `done_at` | TEXT | 是 |  |  |
| `due_at` | TEXT | 是 |  |  |
| `created_at` | TEXT | 否 | `datetime('now')` |  |
| `updated_at` | TEXT | 否 | `datetime('now')` |  |
| `deleted_at` | TEXT | 是 |  |  |

索引：

- `idx_memos_live_user_created`（普通）：``
- `idx_memos_live_user_done`（普通）：``
- `idx_memos_user_created`（普通）：`user_id, created_at, id`
- `idx_memos_user_done`（普通）：`user_id, done, created_at`

外键：

- `user_id` → `users.id`

## posts

| 列 | 类型 | 可空 | 默认 | 主键 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 |  | 1 |
| `author_id` | INTEGER | 否 |  |  |
| `title` | TEXT | 否 |  |  |
| `body_md` | TEXT | 否 |  |  |
| `created_at` | TEXT | 否 | `datetime('now')` |  |
| `updated_at` | TEXT | 否 | `datetime('now')` |  |
| `deleted_at` | TEXT | 是 |  |  |

索引：

- `idx_posts_author_id`（普通）：`author_id`
- `idx_posts_created_id`（普通）：`created_at, id`
- `idx_posts_live_created_id`（普通）：``
- `idx_posts_title`（普通）：`title`

外键：

- `author_id` → `users.id`

## users

| 列 | 类型 | 可空 | 默认 | 主键 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 |  | 1 |
| `email` | TEXT | 否 |  |  |
| `password_hash` | TEXT | 否 |  |  |
| `role` | TEXT | 否 | `'user'` |  |
| `created_at` | TEXT | 否 | `datetime('now')` |  |
| `deleted_at` | TEXT | 是 |  |  |
| `nickname` | TEXT | 是 |  |  |
| `first_name` | TEXT | 是 |  |  |
| `last_name` | TEXT | 是 |  |  |
| `avatar_url` | TEXT | 是 |  |  |
| `last_active_at` | TEXT | 是 |  |  |
| `token_invalidated_at` | TEXT | 是 |  |  |

索引：

- `idx_users_created_id`（普通）：`created_at, id`
- `idx_users_email`（唯一）：`email`

## 不在结构里、但写查询时必须记得的约定

- 软删除：`users` / `posts` / `comments` 都有 `deleted_at`，所有读查询必须带 `deleted_at IS NULL`；删帖时同时软删它的评论。
- 时间：统一 `TEXT`，由 `datetime('now')` 写入，即 **UTC**；本地化只在前端做。
- 稳定分页：大多数列表按 `created_at DESC, id DESC` 排，`id` 参与排序键，避免同一秒创建的行顺序漂移。
- 业务排序例外：`memos` 按到期日升序、无到期日按创建倒序（`done=true` 按 `done_at` 倒序）；`users` 按 `last_active_at` 倒序、无活跃按注册倒序。两处仍以 `id` 收尾保证稳定。
