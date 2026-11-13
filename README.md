<!-- [LLM SYSTEM INSTRUCTION & CONTEXT]
Project: api-worker - a small blog/reporting backend running on Cloudflare Workers in Python.
Stack: Cloudflare Workers (python_workers compat flag, Pyodide runtime) + FastAPI (pure-ASGI glue) + D1 (SQLite) + PyJWT + wrangler 4. No ORM: every query is hand-written SQL through src/infra/db.py.
Layout: src/entry.py (Worker entry, Default entrypoint exported for wrangler) -> src/api/ (app.py router wiring, deps.py auth/permissions, models.py Pydantic schemas, responses.py envelope + error handlers, routers/) -> src/services/ (business rules that need SQL: invite_codes.py) -> src/core/ (permissions.py role->codes map, constants.py response codes/details, security.py JWT + PBKDF2, utils.py helpers) -> src/infra/ (db.py D1 wrapper, config.py env settings, passwords.py WebCrypto hashing) -> src/tasks/ (cron.py cron entry, activity_retention.py purge job).
Envelope: every response is {"code": str, "detail": str, "data": any}; errors are raised as ApiError(code, detail, status) and rendered by handlers in api/responses.py (422 RequestValidationError, 404 not_found, 405 method_not_allowed, 500 internal_error).
Routes: GET /; POST /auth/register; POST /auth/login; GET /auth/me; PATCH /auth/me; GET /posts; GET /posts/{post_id}; POST /posts; POST /posts/batch-delete; PATCH /posts/{post_id}; DELETE /posts/{post_id}; GET /posts/{post_id}/comments; POST /posts/{post_id}/comments; DELETE /comments/{comment_id}; POST /activity; POST|GET /admin/invite-codes; DELETE /admin/invite-codes/{code}; GET /admin/users; PATCH /admin/users/{user_id}; POST /admin/users/{user_id}/reset-password; DELETE /admin/users/{user_id}; GET /admin/activity.
Permissions (resource:action:scope): user and internal both hold account:read:self, account:update:self, post:create:any, post:update:own, post:delete:own, comment:create:any, comment:delete:own, activity:write:self. admin holds those plus post:update:any, post:delete:any, comment:delete:any, invite_code:create:any, invite_code:read:any, invite_code:delete:any, user:read:any, user:update:any, user:delete:any, user:reset_password:any, activity:read:any. PUBLIC (never returned to clients) covers post:read:any and comment:read:any.
Migrations (migrations/, applied in filename order via wrangler d1 migrations apply): 0001_init.sql, 0002_invite_codes_created_by.sql, 0003_user_soft_delete.sql, 0004_activity_events.sql, 0005_user_profile.sql, 0006_user_password_reset.sql, 0007_user_last_active.sql, 0008_invite_code_note.sql, 0009_token_invalidated_at.sql, 0010_list_order_indexes.sql.
Schema: migrations/ is the single source of truth for the database shape. docs/schema.md is a GENERATED view of the live D1 structure (tables, columns, types, nullability, defaults, PK, indexes, foreign keys) meant for humans and LLMs to read, because there is no ORM to describe the tables in code. Never hand-edit docs/schema.md; regenerate it with `npm run schema` after every migration and commit both.
Maintenance: after any context reset, read the "Maintenance checklist" section before changing the schema or adding a route - migrations, docs/schema.md and the README migration table have to move together.
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
      admin.py              invite codes, users, activity log
      activity.py           client activity ingestion with per-user throttling
  services/
    invite_codes.py         invite code generation, listing and deletion
  core/
    permissions.py          permission codes and role -> codes mapping
    constants.py            response codes and detail strings (public contract)
    security.py             JWT issue/decode, PBKDF2 hashing primitives
    utils.py                email normalization, display name resolution, timestamps
  infra/
    db.py                   D1 wrapper: fetch_one, fetch_all, execute, insert_returning
    config.py               environment settings (JWT secret, expiry, algorithm)
    passwords.py            WebCrypto-backed password hashing
  tasks/
    cron.py                 cron entry called by entry.py's scheduled()
    activity_retention.py   purge of activity_events older than 30 days
migrations/                 numbered SQL migrations applied by wrangler
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

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/` | public | service status |
| POST | `/auth/register` | public | optional `invite_code` decides the role (default `user`) |
| POST | `/auth/login` | public | returns the token plus the full user object |
| GET | `/auth/me` | signed in | own user object plus `permissions` |
| PATCH | `/auth/me` | `account:update:self` | profile fields only; `email`/`role` are rejected with `422` |
| POST | `/auth/logout` | signed in | stamps `token_invalidated_at`, so every token of this account issued before now stops working |
| GET | `/posts` | public | `limit` (1-100), `offset`; optional `q` (fuzzy title match, max 64) and `author` (fuzzy match on email, nickname or "first last") |
| GET | `/posts/{post_id}` | public | |
| GET | `/posts/{post_id}/neighbors` | public | `{index, prev, next}` in list order: `prev` newer, `next` older, each `{id, title}` or `null`. Optional `q`/`author` keep the neighbours inside the filtered result set; `index` is only filled without filters |
| POST | `/posts` | `post:create:any` | |
| POST | `/posts/batch-delete` | `post:delete:any` | body `{ids: number[]}`, 1-50 ids, deduplicated |
| PATCH | `/posts/{post_id}` | own or `post:update:any` | |
| DELETE | `/posts/{post_id}` | own or `post:delete:any` | soft delete, also soft deletes its comments |
| GET | `/posts/{post_id}/comments` | public | |
| POST | `/posts/{post_id}/comments` | `comment:create:any` | `parent_id` must belong to the same post |
| DELETE | `/comments/{comment_id}` | own or `comment:delete:any` | soft deletes the reply subtree |
| POST | `/activity` | `activity:write:self` | 1-50 events per batch, throttled (see below) |
| POST | `/admin/invite-codes` | `invite_code:create:any` | role `internal` or `admin`; optional `note` (max 64) records who the code was handed to |
| GET | `/admin/invite-codes` | `invite_code:read:any` | filters `status`, `role`, pagination; each item carries `note` and `used_by_email` (`null` until someone registers with it) |
| DELETE | `/admin/invite-codes/{code}` | `invite_code:delete:any` | unused codes only |
| GET | `/admin/users` | `user:read:any` | filters `q` (email search), `role`, pagination |
| PATCH | `/admin/users/{user_id}` | `user:update:any` | email, role and/or profile fields |
| POST | `/admin/users/{user_id}/reset-password` | `user:reset_password:any` | body `{password?}`; generates one when omitted, kills older tokens |
| DELETE | `/admin/users/{user_id}` | `user:delete:any` | soft delete; last admin and self are protected |
| GET | `/admin/activity` | `activity:read:any` | filters `user_id`, `type`, `from`, `to`, pagination |

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

Rows are not kept forever: a Cron Trigger (`15 18 * * *` UTC, configured under `triggers.crons` in `wrangler.jsonc`) calls `scheduled()` in `src/entry.py` (the method name is fixed by the Workers runtime, like `fetch()`), which delegates one line to `run_cron()` in `src/tasks/cron.py`; that runs the daily `activity_events` purge and deletes rows whose server receive time (`created_at`) is older than 30 days. Deletion runs in batches of 5000 (`src/tasks/activity_retention.py`) so one statement never has to carry a full sweep, and age is measured on `created_at` rather than the client-supplied `ts`.

Because the purge deletes rows, "last active" is cached on `users.last_active_at` instead of being derived from `activity_events`: every accepted batch runs one `UPDATE ... MAX(...)` on the user row, so the value only moves forward, and `/admin/users` reads the column directly (no per-user subquery over the activity table). Deleting old events therefore never makes a dormant account look like it was never active.

## Permissions

Codes use `resource:action:scope`.

| Role | Codes |
| --- | --- |
| `user`, `internal` | `account:read:self`, `account:update:self`, `post:create:any`, `post:update:own`, `post:delete:own`, `comment:create:any`, `comment:delete:own`, `activity:write:self` |
| `admin` | everything the member roles have, plus `post:update:any`, `post:delete:any`, `comment:delete:any`, `invite_code:create:any`, `invite_code:read:any`, `invite_code:delete:any`, `user:read:any`, `user:update:any`, `user:delete:any`, `user:reset_password:any`, `activity:read:any` |

`PUBLIC` is a pseudo role used only to describe unauthenticated read access (`post:read:any`, `comment:read:any`); it is never returned by `/auth/me`.

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

Soft deletion is used for users, posts and comments (`deleted_at`); reads filter it out and deleted authors render as `已注销`. Deleting a post also soft deletes its comments.

### Table shape for humans and LLMs: `docs/schema.md`

There is no ORM in this project, and that has a cost: nothing in the code says what a row looks like. `src/api/models.py` describes request/response bodies, not tables, and `sqlite_master` only keeps the *initial* `CREATE TABLE` — an `ALTER TABLE ... ADD COLUMN` never rewrites it, so the DDL text goes stale the first time you add a column (which already happened five times here: 0005, 0006, 0007, 0008, 0009).

So the repo keeps a generated dump instead: [`docs/schema.md`](docs/schema.md) lists every table with its columns, types, nullability, defaults, primary keys, indexes and foreign keys, plus the conventions that live only in query code (soft delete, UTC timestamps, stable pagination keys).

Rules for it:

- `migrations/` is the only source of truth. To change the shape, write a migration.
- After applying a migration, run `npm run schema` (which shells out to `node scripts/dump-schema.mjs`) against the remote D1 and commit `docs/schema.md` together with the migration.
- Never hand-edit `docs/schema.md`. It is generated; a manual edit is overwritten on the next run and would only reintroduce the drift this document exists to prevent.

Why not hand-written Django/ORM model classes: this runtime is a Pyodide Worker with no Django (and no ORM at all), so `models.Model` subclasses would be dead code that never executes, while inviting both humans and LLMs to write `Post.objects.filter(...)` calls that nothing in this project can run. Two descriptions of one table — a hand-written class and the real SQL — also drift apart silently, with no mechanism to diff them. A dump of the actual database cannot drift by construction.

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
2. Apply it locally, then remotely: `npx wrangler d1 migrations apply api-worker-db --local` / `--remote`.
3. Run `npm run schema` — this regenerates `docs/schema.md` from the live database.
4. Add a row to the migration table in [Database](#database), with the *reason*, not just the column name.
5. Commit the migration, `docs/schema.md` and this README in **one** commit, so the three can never be reviewed apart.
6. If the change touches a route, a permission code or the response envelope, update the routes / permissions lines at the top of this file too.

**Adding a route**

Update all three of: the `Routes:` line at the top, the router wiring in `src/api/app.py`, and the permissions table if it is gated.

**Things that keep getting re-discovered and should not be re-derived**

- `sqlite_master.sql` holds only the *initial* `CREATE TABLE`; every `ALTER TABLE ... ADD COLUMN` since 0005 is invisible there. Only `PRAGMA table_info` shows the real shape.
- D1 rejects table-valued pragma functions in a JOIN (`pragma_table_info(...)` → `not authorized: SQLITE_AUTH`). Issue one bare `PRAGMA` statement per table instead; that is why `scripts/dump-schema.mjs` loops.
- `_cf_KV` is a Cloudflare-internal table and is not readable through the API; any schema dump must filter `name NOT LIKE '_cf_%'` and skip `d1_migrations`.
- `d1 execute --json` prints its banner before the JSON, so parse from the first `[`/`{`, not the whole stdout.
