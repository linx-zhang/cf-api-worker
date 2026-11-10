<!-- [LLM SYSTEM INSTRUCTION & CONTEXT]
Project: api-worker - a small blog/reporting backend running on Cloudflare Workers in Python.
Stack: Cloudflare Workers (python_workers compat flag, Pyodide runtime) + FastAPI (pure-ASGI glue) + D1 (SQLite) + PyJWT + wrangler 4. No ORM: every query is hand-written SQL through src/infra/db.py.
Layout: src/entry.py (Worker entry, Default entrypoint exported for wrangler) -> src/api/ (app.py router wiring, deps.py auth/permissions, models.py Pydantic schemas, responses.py envelope + error handlers, routers/) -> src/core/ (permissions.py role->codes map, constants.py response codes/details, security.py JWT + PBKDF2, utils.py helpers) -> src/infra/ (db.py D1 wrapper, config.py env settings, passwords.py WebCrypto hashing, invite_codes.py).
Envelope: every response is {"code": str, "detail": str, "data": any}; errors are raised as ApiError(code, detail, status) and rendered by handlers in api/responses.py (422 RequestValidationError, 404 not_found, 405 method_not_allowed, 500 internal_error).
Routes: GET /; POST /auth/register; POST /auth/login; GET /auth/me; PATCH /auth/me; GET /posts; GET /posts/{post_id}; POST /posts; POST /posts/batch-delete; PATCH /posts/{post_id}; DELETE /posts/{post_id}; GET /posts/{post_id}/comments; POST /posts/{post_id}/comments; DELETE /comments/{comment_id}; POST /activity; POST|GET /admin/invite-codes; DELETE /admin/invite-codes/{code}; GET /admin/users; PATCH /admin/users/{user_id}; DELETE /admin/users/{user_id}; GET /admin/activity.
Permissions (resource:action:scope): user and internal both hold account:read:self, account:update:self, post:create:any, post:update:own, post:delete:own, comment:create:any, comment:delete:own, activity:write:self. admin holds those plus post:update:any, post:delete:any, comment:delete:any, invite_code:create:any, invite_code:read:any, invite_code:delete:any, user:read:any, user:update:any, user:delete:any, activity:read:any. PUBLIC (never returned to clients) covers post:read:any and comment:read:any.
Migrations (migrations/, applied in filename order via wrangler d1 migrations apply): 0001_init.sql, 0002_invite_codes_created_by.sql, 0003_user_soft_delete.sql, 0004_activity_events.sql, 0005_user_profile.sql.
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
  core/
    permissions.py          permission codes and role -> codes mapping
    constants.py            response codes and detail strings (public contract)
    security.py             JWT issue/decode, PBKDF2 hashing primitives
    utils.py                email normalization, display name resolution, timestamps
  infra/
    db.py                   D1 wrapper: fetch_one, fetch_all, execute, insert_returning
    config.py               environment settings (JWT secret, expiry, algorithm)
    passwords.py            WebCrypto-backed password hashing
    invite_codes.py         invite code generation, listing and deletion
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
| GET | `/posts` | public | `limit` (1-100), `offset` |
| GET | `/posts/{post_id}` | public | |
| POST | `/posts` | `post:create:any` | |
| POST | `/posts/batch-delete` | `post:delete:any` | body `{ids: number[]}`, 1-50 ids, deduplicated |
| PATCH | `/posts/{post_id}` | own or `post:update:any` | |
| DELETE | `/posts/{post_id}` | own or `post:delete:any` | soft delete, also soft deletes its comments |
| GET | `/posts/{post_id}/comments` | public | |
| POST | `/posts/{post_id}/comments` | `comment:create:any` | `parent_id` must belong to the same post |
| DELETE | `/comments/{comment_id}` | own or `comment:delete:any` | soft deletes the reply subtree |
| POST | `/activity` | `activity:write:self` | 1-50 events per batch, throttled (see below) |
| POST | `/admin/invite-codes` | `invite_code:create:any` | role `internal` or `admin` |
| GET | `/admin/invite-codes` | `invite_code:read:any` | filters `status`, `role`, pagination |
| DELETE | `/admin/invite-codes/{code}` | `invite_code:delete:any` | unused codes only |
| GET | `/admin/users` | `user:read:any` | filters `q` (email search), `role`, pagination |
| PATCH | `/admin/users/{user_id}` | `user:update:any` | email, role and/or profile fields |
| DELETE | `/admin/users/{user_id}` | `user:delete:any` | soft delete; last admin and self are protected |
| GET | `/admin/activity` | `activity:read:any` | filters `user_id`, `type`, `from`, `to`, pagination |

### User profile fields

`nickname` (max 32), `first_name` (max 64), `last_name` (max 64) and `avatar_url` (max 512, must start with `http://` or `https://`) are optional. Blank strings are stored as `NULL`. They appear in `/auth/me`, the login and register `user` objects, and `/admin/users` items.

`author_name` on posts and comments resolves as: `nickname` -> `"first_name last_name"` -> the local part of the email. Accounts with `deleted_at` set render as `已注销`. This is why changing an email no longer changes how the author is displayed once a nickname or name is set.

### Activity logging

`POST /activity` accepts `{"events": [{"type": "page_view"|"heartbeat"|"page_leave", "path": ..., "ts": <epoch ms>, "duration_ms"?: ...}]}` with 1-50 events. Timestamps must be within the last 30 days and at most 5 minutes in the future.

Writes are throttled to one batch per user per 10 seconds. The check reads the newest stored batch timestamp from D1, so the window is shared by every isolate; rejected batches perform no writes and return `429` with `rate_limited` and a `Retry-After` header. A batch containing `page_leave` always passes, because it carries the only `duration_ms` value.

## Permissions

Codes use `resource:action:scope`.

| Role | Codes |
| --- | --- |
| `user`, `internal` | `account:read:self`, `account:update:self`, `post:create:any`, `post:update:own`, `post:delete:own`, `comment:create:any`, `comment:delete:own`, `activity:write:self` |
| `admin` | everything the member roles have, plus `post:update:any`, `post:delete:any`, `comment:delete:any`, `invite_code:create:any`, `invite_code:read:any`, `invite_code:delete:any`, `user:read:any`, `user:update:any`, `user:delete:any`, `activity:read:any` |

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

Soft deletion is used for users, posts and comments (`deleted_at`); reads filter it out and deleted authors render as `已注销`. Deleting a post also soft deletes its comments.

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
