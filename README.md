<!-- [LLM SYSTEM INSTRUCTION & CONTEXT]
Project: api-worker - a small blog/reporting backend running on Cloudflare Workers in Python.
Stack: Cloudflare Workers (python_workers compat flag, Pyodide runtime) + FastAPI (pure-ASGI glue) + D1 (SQLite) + PyJWT + wrangler 4. No ORM: every query is hand-written SQL through src/infra/db.py.
Layout: src/entry.py (Worker entry, Default entrypoint exported for wrangler) -> src/api/ (app.py router wiring, deps.py auth/permissions/client ip, models.py Pydantic schemas, responses.py envelope + error handlers, routers/) -> src/services/ (business rules that need SQL: invite_codes.py, visitor_paths.py, analytics.py for the read SQL, analytics_ledger.py for the ledger orchestration, analytics_merge.py for pure folding/rendering) -> src/core/ (permissions.py role->codes map, constants.py response codes/details, security.py JWT + PBKDF2, utils.py helpers) -> src/infra/ (db.py D1 wrapper, config.py env settings, passwords.py WebCrypto hashing) -> src/tasks/ (cron.py cron entry, activity_retention.py purge, cache_cleanup.py expired rows, memo_tombstone.py 90-day hard delete, analytics_ledger.py hourly fold).
Envelope: every response is {"code": str, "detail": str, "data": any}; errors are raised as ApiError(code, detail, status) and rendered by handlers in api/responses.py (422 RequestValidationError, 404 not_found, 405 method_not_allowed, 500 internal_error).
Routes: the endpoint table (method / path / auth / request / response) lives in docs/api.md and is the source of truth; `python scripts/check-api-doc.py` diffs it against the decorators. Quick list: GET /; POST /auth/register; POST /auth/login; GET /auth/me; PATCH /auth/me; POST /auth/logout; GET /posts (filters: q title fuzzy, author author fuzzy, author_id exact author); GET /posts/{post_id}; GET /posts/{post_id}/neighbors; POST /posts; POST /posts/batch-delete; PATCH /posts/{post_id}; DELETE /posts/{post_id}; GET /posts/{post_id}/comments; POST /posts/{post_id}/comments; DELETE /comments/{comment_id}; GET /memos; GET /memos/summary; POST /memos; PATCH /memos/{memo_id}; DELETE /memos/{memo_id}; GET /consumables; POST /consumables; PATCH /consumables/{item_id}; DELETE /consumables/{item_id}; GET /consumables/{item_id}/cycles; POST /consumables/{item_id}/cycles; POST /consumables/{item_id}/finish; PATCH /consumables/{item_id}/cycles/{cycle_id}; POST /clips; GET /clips/{code}; DELETE /clips/{code}; POST /activity; POST /activity/visitor; POST|GET /admin/invite-codes; DELETE /admin/invite-codes/{code}; GET /admin/users; PATCH /admin/users/{user_id}; POST /admin/users/{user_id}/reset-password; DELETE /admin/users/{user_id}; GET /admin/activity; GET /admin/analytics/summary.
Permissions (resource:action:scope): user and internal both hold account:read:self, account:update:self, post:create:any, post:update:own, post:delete:own, comment:create:any, comment:delete:own, activity:write:self. admin holds those plus post:update:any, post:delete:any, comment:delete:any, invite_code:create:any, invite_code:read:any, invite_code:delete:any, user:read:any, user:update:any, user:delete:any, user:reset_password:any, activity:read:any. PUBLIC (never returned to clients) covers post:read:any and comment:read:any. /clips adds clip:delete:own for every signed-in role and clip:delete:any for admin, while POST /clips only needs a token (same shape as /memos); GET /clips/{code} is public because the code itself is the capability, and no route lists the stored snippets — so reads carry their own budget instead: 60/min per client ip and 120/min per code, fixed one-minute windows, answering 429 rate_limited with Retry-After. /memos is self-scoped and adds no permission code: every memo route requires a token and takes the owner from it, never from the request. /consumables is the same shape — token only, owner from the token, no permission code.
Migrations (migrations/, applied in filename order via wrangler d1 migrations apply): 0001_init.sql, 0002_invite_codes_created_by.sql, 0003_user_soft_delete.sql, 0004_activity_events.sql, 0005_user_profile.sql, 0006_user_password_reset.sql, 0007_user_last_active.sql, 0008_invite_code_note.sql, 0009_token_invalidated_at.sql, 0010_list_order_indexes.sql, 0011_db_cache.sql, 0012_activity_anonymous.sql, 0013_memos.sql, 0014_memo_due_utc.sql, 0015_live_partial_indexes.sql, 0016_consumables.sql.
Schema: migrations/ is the single source of truth for the database shape. docs/schema.md is a GENERATED view of the live D1 structure (tables, columns, types, nullability, defaults, PK, indexes, foreign keys) meant for humans and LLMs to read, because there is no ORM to describe the tables in code. Never hand-edit docs/schema.md; regenerate it with `npm run schema` after every migration and commit both.
Maintenance: after any context reset, read the "Maintenance checklist" section before changing the schema or adding a route - migrations, docs/schema.md, docs/api.md and the README migration table have to move together.
D1 binding: env.DB -> database name api-worker-db (configured in wrangler.jsonc).
Commands: npx wrangler dev (local), npx wrangler d1 migrations apply api-worker-db --local|--remote, npx wrangler deploy, npx wrangler types. JWT_SECRET comes from .dev.vars locally and from a Worker secret in production.
Conventions: soft delete via deleted_at on users/posts/comments; author display name resolves as nickname -> "first_name last_name" -> email local part, masked as "已注销" for deleted accounts; POST /activity is throttled to one batch per user per 10 seconds (429 rate_limited) while batches containing page_leave always pass; POST /posts/batch-delete soft deletes 1-50 ids and reports per-id failures.
-->

# api-worker

A compact backend for a blog/reporting UI: authentication with invite codes, posts with comments, soft deletion everywhere, admin user management, and an activity log fed by the frontend.

It runs on Cloudflare Workers in Python (Pyodide runtime) and uses FastAPI for routing only; there is no ORM and no async framework beyond the Workers runtime.

## Tech stack

| Layer | Choice |
| --- | --- |
| Runtime | Cloudflare Workers, `python_workers` compatibility flag |
| HTTP | FastAPI mounted as an ASGI app from `src/entry.py` |
| Database | D1 (SQLite), bound as `env.DB` |
| Auth | JWT (PyJWT) carrying only the user id; roles are re-read from D1 on every request |
| Passwords | PBKDF2-SHA256 via WebCrypto, pure-Python fallback for local scripts |
| Tooling | `wrangler` 4, `uv`, `ruff` |

## Project layout

```
src/
  entry.py                  Worker entrypoint (exports the ASGI app)
  api/
    app.py                  FastAPI app, CORS, router registration
    deps.py                 DB/settings injection, bearer auth, permission dependency
    models.py               Pydantic request/response models and validation
    responses.py            {"code","detail","data"} envelope and error handlers
    routers/
      auth.py               register, login, /auth/me (GET, PATCH)
      posts.py              post list/detail/create/update/delete, batch delete
      comments.py           comment list/create/delete (soft delete with replies)
      admin.py              users, activity log, analytics summary
      invites.py            invite code create/list/delete
      memos.py              self-scoped memos CRUD + summary
      consumables.py        consumable items, cycles, finish
      activity.py           client activity ingestion with per-user throttling
      clips.py              shareable clipboard snippets on the D1 cache (24h TTL)
  services/
    invite_codes.py         invite code generation, listing and deletion
    visitor_paths.py        public-path allow list for anonymous visitors
    analytics.py            analytics SQL + the reference implementation build_summary
    analytics_ledger.py     read path: ledger row + tail fold, rebuild, hourly persist
    analytics_merge.py      pure folding/rendering (no DB, no clock)
  core/
    permissions.py          permission codes and role -> codes mapping
    constants.py            response codes and detail strings (public contract)
    security.py             JWT issue/decode, PBKDF2 hashing primitives
    utils.py                email normalization, display name resolution, timestamps
  infra/
    db.py                   D1 wrapper: fetch_one, fetch_all, execute, insert_returning
    cache.py                D1-backed key/value cache (pickle + base64, Django-shaped API)
    config.py               environment settings (JWT secret, expiry, algorithm)
    passwords.py            WebCrypto-backed password hashing
  tasks/
    cron.py                 cron entry called by entry.py's scheduled()
    activity_retention.py   purge of activity_events older than 30 days
    cache_cleanup.py        purge of expired cache_entries rows
    memo_tombstone.py       hard delete of memos soft-deleted 90+ days ago
    analytics_ledger.py     hourly fold of the analytics ledger row
migrations/                 numbered SQL migrations applied by wrangler
docs/
  schema.md                 generated dump of the live D1 shape (`npm run schema`)
  api.md                    hand-written endpoint contract, checked by `npm run api:doc`
scripts/
  check-api-doc.py          diffs docs/api.md against the route decorators
```

## Response envelope

Every response, success or failure, looks like:

```json
{"code": "ok", "detail": "ok", "data": {...}}
```

* Success codes: `ok`.
* Common error codes: `validation_error`, `invalid_email`, `invalid_role`, `invalid_invite_code`, `email_exists`, `invalid_credentials`, `unauthorized`, `forbidden`, `not_found`, `method_not_allowed`, `cannot_change_own_role`, `last_admin_required`, `cannot_delete_self`, `rate_limited`, `internal_error`.
* Validation failures return `422`, authorization failures `403`, missing resources `404`, throttling `429`.

## Routes

The endpoint table — method, path, auth, request body and response shape — lives in **[`docs/api.md`](docs/api.md)**. It is the single place that lists every route, because a table repeated here and there is a table that will disagree with itself the first time someone edits one copy.

That file is hand-written, so it is checked instead of trusted: `python scripts/check-api-doc.py` re-reads the decorators in `src/api/routers/` plus the `include_router` prefixes in `src/api/app.py`, and exits non-zero listing any route the table is missing or inventing. Run it before committing a route change.

The subsections below are the part a table cannot carry: why the pagination standard exists, what the neighbours endpoint returns now, how logout and password reset share one invalidation column, and what the activity limits are defending against.

### Pagination: one standard

Every paginated list returns **`{items, has_next}`** and takes **`limit` + `offset` only** (`limit` 1-100 default 20, one default and one ceiling for the whole API — `/admin/activity` is no exception). `page` / `page_size` / `pageSize` are **rejected with 422** by `reject_legacy_paging()` (`src/api/deps.py`), because they used to be silently ignored: a caller asking `?page=2` got page 1 back and could not tell.

The rule, implemented once in `split_page()` (`src/api/responses.py`):

1. the handler asks D1 for `limit + 1` rows;
2. `split_page()` drops the extra row and sets `has_next`;
3. the client sends `limit` and reads `has_next` — it never asks for `limit + 1` and never slices.

Why the server owns the probe: one statement per page (no second COUNT), the probe row is never serialized (no wasted payload, which matters because posts carry their full `body_md`), and every list — blog, admin posts, users, invite codes, activity — behaves identically, so the client has exactly one paging idiom.

Two deliberate exceptions keep a COUNT:

- `/admin/users` and `/admin/invite-codes` return `total`, because their UI prints "共 N 条" and "第 x / y 页". It is a filtered but non-transactional count: it comes from a separate statement than the page it describes, so a concurrent write can make it disagree with the visible rows by one. Page navigation must therefore be driven by `has_next`, never by arithmetic on `total`.

`/posts/{post_id}/neighbors` used to be a third one: it counted newer posts to return an `index`, so the detail page could guess which list page the reader came from. That guess was removed — the client carries the source page in router state, which is exact and free. Neighbors is now two `LIMIT 1` lookups and returns `{prev, next}` only.

Unpaginated lists (`GET /posts/{post_id}/comments`) still return a bare array on purpose — there is no page to describe, and paging a threaded comment tree is a design question, not a `limit` bolted on. It becomes worth doing when a single post carries enough comments that one request is visibly slow.

### User profile fields

`nickname` (max 32), `first_name` (max 64), `last_name` (max 64) and `avatar_url` (max 512, must start with `http://` or `https://`) are optional. Blank strings are stored as `NULL`. They appear in `/auth/me`, the login and register `user` objects, and `/admin/users` items.

`author_name` on posts and comments resolves as: `nickname` -> `"first_name last_name"` -> the local part of the email. Accounts with `deleted_at` set render as `已注销`. This is why changing an email no longer changes how the author is displayed once a nickname or name is set.

### Admin password reset

`POST /admin/users/{user_id}/reset-password` requires `user:reset_password:any` (admins only). The body may carry `{"password": "..."}` (8-128 characters) to set a password chosen by the administrator; when the field is omitted the server generates a 16-character temporary one from an unambiguous alphabet (no `0/O` or `1/l/I`).

Only the PBKDF2 hash is written, and the effective plaintext is returned exactly once as `data.password` with `data.generated` telling the two cases apart — nothing is stored in clear text, so a value that is lost means resetting again.

The same request stamps `users.token_invalidated_at`. `get_current_user` compares the JWT `iat` against it and answers `401 unauthorized` for tokens issued before the reset, so an admin reset also ends that account's live sessions (a token issued in the same second is still accepted). Soft-deleted accounts return `404 not_found`. Resetting your own account is allowed and ends your current session too.

### Logout

`POST /auth/logout` (signed in) stamps `users.token_invalidated_at` with `datetime('now')`. Because `get_current_user` rejects tokens whose `iat` is older than that stamp, logging out revokes every token already issued to the account — clearing the browser's copy is no longer the only thing that happens. The same caveat applies: a token issued within the same second stays valid. A password reset writes the same column, so reset and logout share one mechanism.

### Activity logging

`POST /activity` accepts `{"events": [{"type": "page_view"|"heartbeat"|"page_leave", "path": ..., "ts": <epoch ms>, "duration_ms"?: ...}]}` with 1-50 events. Timestamps must be within the last 30 days and at most 5 minutes in the future.

Writes are throttled to one batch per user per 10 seconds. The check reads the newest stored batch timestamp from D1, so the window is shared by every isolate; rejected batches perform no writes and return `429` with `rate_limited` and a `Retry-After` header. A batch containing `page_leave` always passes, because it carries the only `duration_ms` value.

### Anonymous visitors, ip and geo

The signed-in endpoint can only describe people who have an account, which for this site means its owner and a few test accounts. `POST /activity/visitor` is the missing half: no authentication, 1-20 events, `page_view`/`page_leave` only, and only for public content paths (`/`, `/blog*`, `/wiki*` — `services/visitor_paths.py`). Rejections happen before any write.

Why these limits rather than a WAF rule: the endpoint must stay open to record a real reader, so the cheapest defence that still records a visit faithfully is the batch ceiling, the public-path allow list and an ip throttle of 30 seconds (`WHERE ip = ?` on the newest row, same trick as the per-user throttle). `page_leave` always passes for the same reason it does on the signed-in path: it carries the only `duration_ms`. Anonymous heartbeats are rejected at validation, because one heartbeat per visitor per minute would outnumber the page views it is meant to explain.

Every event — signed-in or anonymous — stores the network context seen at request time: `ip`, `country`, `region`, `city`. Country comes from Cloudflare's `cf-ipcountry` header; city and region exist only inside `request.cf`, which `workers.asgi` does not forward into the ASGI scope, so `src/entry.py` re-emits the request with `x-cf-country` / `x-cf-region` / `x-cf-city` / `x-cf-colo` headers. That clone is wrapped in `try/except` and falls back to the untouched request: if it ever fails, country still arrives and city/region are simply absent. Nothing is reverse-geocoded later — the answer that matters is where the request was seen from.

`activity_events.user_id` is therefore nullable. SQLite cannot drop `NOT NULL`, so `0012` rebuilds the table (create → copy → drop → rename → recreate three indexes); historical rows keep `NULL` geo because that data never existed and inventing it would be worse than a gap.

### Activity analytics: `GET /admin/analytics/summary`

One request returns three things: today's counters (`new_users`, `active_users`, `anonymous_visitors`, `page_views`), a daily series, and per-identity detail. All SQL lives in `src/services/analytics.py`, free of FastAPI/pydantic imports, so `scripts/agent/2026-11-13-analytics-smoke.py` runs it against an in-memory SQLite database with every migration applied.

An **actor** is `u:<id>` (signed in) or `ip:<address>` (anonymous). Nothing links those two rows: a visitor who signs in legitimately appears twice rather than being guessed into one identity.

Two numbers need their meaning stated, because both are easy to misread:

- `dwell_ms` is the sum of `duration_ms` from `page_leave`. It is observed time, not wall-clock: a tab killed without `pagehide` sends no leave event and contributes nothing.
- Dates are bucketed with `+8 hours` (`ANALYTICS_TZ_OFFSET_HOURS`), because every stored timestamp is UTC and "today" here means Beijing time. Without the shift every daily number would be 8 hours off.

### The ledger: how a read stops scanning the event table

Querying the raw table was the original shape: every request scanned the 30-day window twice (once for `days=7`, once for `days=30`), about 20–30k rows per request. The admin page refreshed often enough that the free 5M rows-read/day budget would have run out on its own traffic.

So the read path is now **one ledger row plus a tail**. The ledger lives in `cache_entries` under `analytics:ledger:v2` with `timeout=None`, is written by the hourly cron (`0 * * * *`), and holds:

- `cursor` — the largest `activity_events.id` already folded in;
- `daily[day]` — page views and the distinct actor keys for that Beijing display day;
- `actors[ident].days[day]` — per-identity, per-day views, dwell, first/last seen and the path breakdown, because the page serves both a 7-day and a 30-day window and each has to report its own totals.

A request then:

1. reads the ledger row;
2. computes `MAX(id)` and folds the rows with `cursor < id <= MAX(id)` in memory (`services/analytics_merge.py`, pure functions — no DB, no clock);
3. sums only the day buckets inside the requested window and renders.

A **warm read writes nothing**, so concurrent readers never race and a refresh burst costs one cache row plus a small tail. The ledger is written only by the hourly cron, and on a cold read (no ledger), a corrupt ledger, or a tail longer than `ANALYTICS_MAX_TAIL_ROWS` (500 rows) the read falls back to `build_summary` — the reference implementation that reads the whole window — and stores the result. `tests/test_analytics_ledger.py` asserts the ledger path and the reference path agree field by field for **both** `days=7` and `days=30`.

`LEDGER_KEY` carries its shape version (`v1` was window-total-only and reported 30-day totals in the 7-day view; `v2` is per-day) — bumping the suffix is the migration, since a shape change lands under a new key and the next read simply rebuilds.

Measured on the live worker (15 consecutive samples, one minute apart):

| view | median | range | identities | series total |
| --- | --- | --- | --- | --- |
| `days=7` | 651 ms | 345–3144 ms | 25 | 1029 |
| `days=30` | 944 ms | 299–2690 ms | 44 | 1586 |

All 15 requests succeeded. The spread is Python Worker cold start, not data volume: the same queries, with unchanged data, moved between 0.3 s and 3.1 s. The ledger row itself is about **37 KB** at 45 identities / 11 day buckets. The 60-second summary cache (`analytics:summary:v1:<days>`) still sits in front of the ledger, so a page refresh usually costs neither the tail nor the render.

Rows are not kept forever: a Cron Trigger (`15 18 * * *` UTC, configured under `triggers.crons` in `wrangler.jsonc`) calls `scheduled()` in `src/entry.py` (the method name is fixed by the Workers runtime, like `fetch()`), which delegates one line to `run_cron()` in `src/tasks/cron.py`; that runs the activity purge, the cache purge, the memo tombstone hard delete and one ledger fold. Retention deletes rows whose server receive time (`created_at`) is older than 30 days, in batches of 5000 (`src/tasks/activity_retention.py`) so one statement never has to carry a full sweep, and age is measured on `created_at` rather than the client-supplied `ts`.

Because the purge deletes rows, "last active" is cached on `users.last_active_at` instead of being derived from `activity_events`: every accepted batch runs one `UPDATE ... MAX(...)` on the user row, so the value only moves forward, and `/admin/users` reads the column directly (no per-user subquery over the activity table). Deleting old events therefore never makes a dormant account look like it was never active.

## Permissions

Codes use `resource:action:scope`. The full role × code matrix is in [`docs/api.md`](docs/api.md#权限码矩阵); `src/core/permissions.py` is the code-level truth it mirrors.

| Role | Codes |
| --- | --- |
| `user`, `internal` | `account:read:self`, `account:update:self`, `post:create:any`, `post:update:own`, `post:delete:own`, `comment:create:any`, `comment:delete:own`, `activity:write:self`, `clip:delete:own` |
| `admin` | everything the member roles have, plus `post:update:any`, `post:delete:any`, `comment:delete:any`, `clip:delete:any`, `invite_code:create:any`, `invite_code:read:any`, `invite_code:delete:any`, `user:read:any`, `user:update:any`, `user:delete:any`, `user:reset_password:any`, `activity:read:any` |

`PUBLIC` is a pseudo role used only to describe unauthenticated read access (`post:read:any`, `comment:read:any`); it is never returned by `/auth/me`. `/memos`, `/consumables` and `POST /clips` carry no code at all — the token is the whole rule, and the route takes the owner from it (someone else's memo/consumable answers `404`, not `403`).

## Database

D1 binding `DB` -> database `api-worker-db` (see `wrangler.jsonc`). Migrations live in `migrations/` and are applied in filename order:

| File | Purpose |
| --- | --- |
| `0001_init.sql` | `users`, `invite_codes`, `posts`, `comments` and their indexes |
| `0002_invite_codes_created_by.sql` | tracks who created an invite code |
| `0003_user_soft_delete.sql` | `users.deleted_at` |
| `0004_activity_events.sql` | `activity_events` table and its indexes |
| `0005_user_profile.sql` | `users.nickname`, `first_name`, `last_name`, `avatar_url` |
| `0006_user_password_reset.sql` | `users.password_changed_at`, used to expire tokens after an admin reset |
| `0007_user_last_active.sql` | `users.last_active_at`, backfilled from `activity_events` so retention cannot erase it |
| `0008_invite_code_note.sql` | `invite_codes.note`, a free-form reminder of who the code was handed to |
| `0009_token_invalidated_at.sql` | renames `users.password_changed_at` to `token_invalidated_at`; reset and logout both stamp it |
| `0010_list_order_indexes.sql` | composite `(created_at, id)` indexes so `ORDER BY created_at DESC, id DESC` skips the sort step |
| `0011_db_cache.sql` | `cache_entries`, the D1-backed key/value cache that `src/infra/cache.py` reads and `run_cron` purges |
| `0012_activity_anonymous.sql` | rebuilds `activity_events`: `user_id` nullable (anonymous visitors), adds `ip`, `country`, `region`, `city`, adds the `(ip, created_at)` throttle index |
| `0013_memos.sql` | `memos`: per-user notes/todos. `due_at` is a UTC deadline instant (`YYYY-MM-DD HH:MM:SS`) used only to highlight rows (no reminders are sent); `done_at` is the most recent completion and is cleared when the box is unticked; `deleted_at` keeps deletion recoverable. Indexes `(user_id, created_at, id)` for the default list and `(user_id, done, created_at)` for the 待办/已完成 split |
| `0014_memo_due_utc.sql` | rewrites the date-only `memos.due_at` (`YYYY-MM-DD`) into the UTC datetime the rest of the schema already uses: the old value was the Beijing day, so it becomes `<day> 15:59:59` (local 23:59:59 in UTC). Idempotent — only rows still 10 characters long are touched |
| `0015_live_partial_indexes.sql` | partial indexes over live rows only (`idx_posts_live_created_id`, `idx_memos_live_user_created`, `idx_memos_live_user_done`, `idx_comments_live_post_created_id`); the old full indexes stay because filtered queries (author/q/done) still use them |
| `0016_consumables.sql` | `consumable_items` (name/category/unit/quantity/lead_days/note, per user) and `consumable_cycles` (one row per 开封→用完 周期: `started_on`, nullable `ended_on`, `expected_days`, all local days `YYYY-MM-DD`). A partial unique index on `(item_id) WHERE ended_on IS NULL AND deleted_at IS NULL` keeps at most one open cycle per item; status and ETA are derived at read time, never stored |

Soft deletion is used for users, posts, comments, memos and consumables (`deleted_at`); reads filter it out and deleted authors render as `已注销`. Deleting a post also soft deletes its comments; deleting a consumable also soft deletes its cycles.

### Cache: `cache_entries` + `src/infra/cache.py`

One D1 table doubles as the cache store, because a cache that lives in its own service (Workers KV, a Durable Object) would add a second binding, a second consistency model and a second quota to reason about for data that is already derivable from D1. Same API vocabulary as `django.core.cache.backends.db`, so the mental model transfers:

```python
from infra.cache import Cache

cache = Cache(env.DB)                       # namespace defaults to "default"
await cache.set("posts:list:v1:20:0", rows, timeout=60)
await cache.get("posts:list:v1:20:0")       # None when absent or expired
await cache.get_or_set("k", loader)         # read-through; loader may be async
await cache.add("k", v)                     # only if absent/expired -> bool
await cache.delete("k"); await cache.has_key("k"); await cache.get_many(["a", "b"])
```

Rules that keep this honest:

- `timeout=None` never expires, `timeout=0` deletes instead of storing, negative timeouts are stored already expired. Expiry is computed by SQLite (`datetime('now', ?)`), never from the Worker's own clock.
- Values are `pickle` blobs, base64-encoded into a TEXT column. Pickle is not a wire format: only unpickle payloads this project wrote, and remember the payload is opaque to SQL — keys are the only handle, so keep them derivable (`"posts:list:v1:20:0"`).
- Expired rows are invisible to reads, so a missed purge is a latency problem, not a correctness problem; `run_cron` still deletes them nightly (`tasks/cache_cleanup.py`, batched, prints `cache purge (cron): removed N expired entries`).
- Anything cached must stay droppable: `DELETE FROM cache_entries` at any moment may only cost latency, never data.
- Cache keys are namespaced (`<namespace>:<key>`), and the namespace is part of the primary key, so two features cannot overwrite each other by accident. `scripts/agent/2026-11-13-cache-smoke.py` exercises the whole surface against an in-memory SQLite database.

### Table shape for humans and LLMs: `docs/schema.md`

There is no ORM in this project, and that has a cost: nothing in the code says what a row looks like. `src/api/models.py` describes request/response bodies, not tables, and `sqlite_master` only keeps the *initial* `CREATE TABLE` — an `ALTER TABLE ... ADD COLUMN` never rewrites it, so the DDL text goes stale the first time you add a column (which already happened five times here: 0005, 0006, 0007, 0008, 0009).

So the repo keeps a generated dump instead: [`docs/schema.md`](docs/schema.md) lists every table with its columns, types, nullability, defaults, primary keys, indexes and foreign keys, plus the conventions that live only in query code (soft delete, UTC timestamps, stable pagination keys).

Rules for it:

- `migrations/` is the only source of truth. To change the shape, write a migration.
- After applying a migration, run `npm run schema` (which shells out to `node scripts/dump-schema.mjs`) against the remote D1 and commit `docs/schema.md` together with the migration.
- Never hand-edit `docs/schema.md`. It is generated; a manual edit is overwritten on the next run and would only reintroduce the drift this document exists to prevent.

Why not hand-written Django/ORM model classes: this runtime is a Pyodide Worker with no Django (and no ORM at all), so `models.Model` subclasses would be dead code that never executes, while inviting both humans and LLMs to write `Post.objects.filter(...)` calls that nothing in this project can run. Two descriptions of one table — a hand-written class and the real SQL — also drift apart silently, with no mechanism to diff them. A dump of the actual database cannot drift by construction.

### Interface contract: `docs/api.md`

`docs/schema.md` answers "what does a row look like"; [`docs/api.md`](docs/api.md) answers "what does a caller send and get back". It is hand-written, because half of a contract is not in the code: the request models do describe their own fields (and `pydantic` enforces them), but response bodies are assembled dict-literal by dict-literal in the handlers, and the semantics that matter most — which code is a `401` versus a `403`, that `page`/`page_size` are rejected, that a clip code is the capability — exist only as prose.

So the file that could drift is checked rather than trusted. `scripts/check-api-doc.py` parses the decorators and the router prefixes, then diffs them against the table in `docs/api.md`: any route missing from the doc, or documented but not declared, fails the command. What the script cannot judge is the columns — auth, request, response — which is why the checklist above names `docs/api.md` explicitly on both the "add a route" and "change a table" paths.

## Local development

```bash
npx wrangler d1 migrations apply api-worker-db --local
npx wrangler dev
```

`.dev.vars` must contain `JWT_SECRET` for local runs (it is git-ignored). The local D1 state lives under `.wrangler/` and is not committed.

Lint and format:

```bash
uv run ruff check .
uv run ruff format --check .
```

## Deploy

```bash
npx wrangler d1 migrations apply api-worker-db --remote
npx wrangler deploy
```

`JWT_SECRET` in production is a Worker secret, not a `vars` entry. After changing bindings in `wrangler.jsonc`, regenerate types with `npx wrangler types`.

## Maintenance checklist (read this after a context reset)

The steps below exist because an LLM's context is finite and gets compacted. `0010_list_order_indexes.sql` shipped without being added to the migration table above — that is exactly the failure mode this list prevents. Anything that must survive forgetting has to be written into a file, not left in a conversation.

**Adding or changing a table / column / index**

1. Write `migrations/00NN_<name>.sql`. Never edit an already-applied migration.
2. Apply it locally, then remotely: `npx wrangler d1 migrations apply api-worker-db --local` / `--remote`. **Always through `migrations apply`.** `d1 execute --file migrations/x.sql` runs the SQL but writes nothing to `d1_migrations`, so the next `migrations apply` re-runs that file — and a table rebuild like `0012` is not idempotent, so it fails and blocks every migration behind it. If it ever happens, insert the missing row: `INSERT INTO d1_migrations (name, applied_at) VALUES ('0012_activity_anonymous.sql', datetime('now'))`, then confirm with `wrangler d1 migrations list api-worker-db --remote` (it must print *No migrations to apply*).
3. Run `npm run schema` — this regenerates `docs/schema.md` from the live database.
4. Add a row to the migration table in [Database](#database), with the *reason*, not just the column name.
5. Commit the migration, `docs/schema.md` and this README in **one** commit, so the three can never be reviewed apart.
6. If the change touches a route, a permission code or the response envelope, update `docs/api.md` and run `python scripts/check-api-doc.py`.

**Adding a route**

Update all three of: `src/api/app.py` (router wiring), `docs/api.md` (the endpoint table — it is the source of truth), and the `Routes:` line at the top of this file when the one-line summary is incomplete. Then run `python scripts/check-api-doc.py`; it fails when the table and the decorators disagree. If the route is gated, add the code to `src/core/permissions.py` and both permission tables (`docs/api.md` and here).

**Where scripts go**

`scripts/` holds project tooling only, each wired to an npm script: `dump-schema.mjs` → `npm run schema`, `check-api-doc.py` → `npm run api:doc`, `new-post.mjs` → `npm run post`. One-off scripts — data seeding, probes, one-time SQL — go under `scripts/agent/` and are never referenced by the build.

**Adding a cron job**

Wire it in `src/tasks/cron.py` (`run_cron`), keep the job body in its own `src/tasks/<name>.py`, and print one line per run: the scheduled handler's return value is discarded, so Worker logs are the only trace a run leaves. There are two triggers: `15 18 * * *` runs `activity_retention` → `cache_cleanup` → `memo_tombstone` → one analytics ledger fold; `0 * * * *` runs **only** the ledger fold (`src/tasks/analytics_ledger.py:refresh_ledger`), which is what keeps the read path's tail short all day. The daily run returns counts per phase.

**Publishing a blog post**

Put the body in a markdown file, then:

```bash
npm run post -- --title "标题" --file drafts/xxx.md            # 只生成 SQL
npm run post -- --title "标题" --file drafts/xxx.md --execute   # 直接写入远端 D1 并回读列表
```

The tool enforces the two rules that keep getting broken by hand: title ≤ 200 characters (`PostIn.title`), and `'` escaping in `body_md`. It also strips a leading `# H1` from the body, because the detail page already renders the title and a second H1 shows up as a duplicate. `author_id` defaults to 6 (the site author); `created_at` comes from D1's `datetime('now')` unless you pass `--created-at` to backfill an old date. Nothing needs deploying — posts are data, and the API reads D1 directly.

**Things that keep getting re-discovered and should not be re-derived**

- `sqlite_master.sql` holds only the *initial* `CREATE TABLE`; every `ALTER TABLE ... ADD COLUMN` since 0005 is invisible there. Only `PRAGMA table_info` shows the real shape.
- D1 rejects table-valued pragma functions in a JOIN (`pragma_table_info(...)` → `not authorized: SQLITE_AUTH`). Issue one bare `PRAGMA` statement per table instead; that is why `scripts/dump-schema.mjs` loops.
- `_cf_KV` is a Cloudflare-internal table and is not readable through the API; any schema dump must filter `name NOT LIKE '_cf_%'` and skip `d1_migrations`.
- `d1 execute --json` prints its banner before the JSON, so parse from the first `[`/`{`, not the whole stdout.
