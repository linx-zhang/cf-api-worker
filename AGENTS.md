# Cloudflare Workers

STOP. Your knowledge of Cloudflare Workers APIs and limits may be outdated. Always retrieve current documentation before any Workers, KV, R2, D1, Durable Objects, Queues, Vectorize, AI, or Agents SDK task.

## Docs

- https://developers.cloudflare.com/workers/
- MCP: `https://docs.mcp.cloudflare.com/mcp`

For all limits and quotas, retrieve from the product's `/platform/limits/` page. eg. `/workers/platform/limits`

## Commands

| Command | Purpose |
|---------|---------|
| `npx wrangler dev` | Local development |
| `npx wrangler deploy` | Deploy to Cloudflare |
| `npx wrangler types` | Generate TypeScript types |

Run `wrangler types` after changing bindings in wrangler.jsonc.

## Local Explorer (Debugging & Inspection)

When running `npx wrangler dev`, a Local Explorer API is available for inspecting and debugging local Workers, bindings, and storage state. The API base URL is printed in the terminal when the dev server starts.

Key endpoints (relative to the dev server URL):

| Endpoint | Description |
|----------|-------------|
| `GET /cdn-cgi/local/explorer/api/local/workers` | List local Workers and their bindings |
| `GET /cdn-cgi/local/explorer/api/storage/kv/namespaces` | List KV namespaces |
| `GET /cdn-cgi/local/explorer/api/d1/database` | List D1 databases |
| `GET /cdn-cgi/local/explorer/api/r2/buckets` | List R2 buckets |
| `GET /cdn-cgi/local/explorer/api/workers/durable_objects/namespaces` | List Durable Object namespaces |
| `GET /cdn-cgi/local/explorer/api/workflows` | List Workflows |
| `POST /cdn-cgi/local/explorer/api/local/observability/query` | Run a read-only SQL query (SELECT/WITH only) over captured request traces and console logs. Tables: `spans`, `logs` (read attributes via `json(attributes)`). Example: `curl -X POST <base>/cdn-cgi/local/explorer/api/local/observability/query -H 'Content-Type: application/json' -d '{"sql":"SELECT service, name, outcome, duration_ms FROM spans WHERE parent_id IS NULL LIMIT 20"}'` |
| `POST /cdn-cgi/local/explorer/api/local/observability/clear` | Clear all captured traces and logs |

If the routes above don't cover what you need, fetch the full OpenAPI schema (large - use only as a last resort): `GET /cdn-cgi/local/explorer/api`

Use the Local Explorer to debug issues by inspecting storage state (KV keys, D1 rows, R2 objects, DO storage), viewing Worker bindings, and querying request traces and logs captured during the dev session.

## Node.js Compatibility

https://developers.cloudflare.com/workers/runtime-apis/nodejs/

## Errors

- **Error 1102** (CPU/Memory exceeded): Retrieve limits from `/workers/platform/limits/`
- **All errors**: https://developers.cloudflare.com/workers/observability/errors/

## Product Docs

Retrieve API references and limits from:
`/kv/` · `/r2/` · `/d1/` · `/durable-objects/` · `/queues/` · `/vectorize/` · `/workers-ai/` · `/agents/`

## Best Practices (conditional)

If the application uses Durable Objects or Workflows, refer to the relevant best practices:

- Durable Objects: https://developers.cloudflare.com/durable-objects/best-practices/rules-of-durable-objects/
- Workflows: https://developers.cloudflare.com/workflows/build/rules-of-workflows/

---

# 本项目：Python Worker（上面的 `npx wrangler deploy` 不适用，以本节为准）

个人站 `api-worker`：FastAPI + D1，入口 `src/entry.py`，D1 `api-worker-db`，cron `15 18 * * *`（UTC）。

## 上线前必跑（Windows，PowerShell，无 CI）

```powershell
$env:Path = "C:\Users\LinxZhang\.local\bin;C:\Program Files\nodejs;C:\Windows\System32;C:\Windows;" + $env:Path
$env:UV_PYTHON = "3.14"
Set-Location -LiteralPath "D:\this_code\cloudflare-workers\api-worker"

uv run --frozen pytest -q              # 45 passed
uv run --frozen ruff check src tests   # All checks passed

# 仅在新增 migrations/*.sql 时；不要加 --yes（会吐 help 并崩溃），改用 y 管道
"y" | .\node_modules\.bin\wrangler.cmd d1 migrations apply api-worker-db --remote

& ".venv\Scripts\pywrangler.exe" sync --upgrade   # 仅在改依赖时；--upgrade 才能让 pydantic-core 挑到有 wheel 的版本
& ".venv\Scripts\pywrangler.exe" deploy           # 记下输出的 Current Version ID

Invoke-WebRequest "https://api-worker.linx-zhang.workers.dev/api/posts?limit=3&offset=0"   # 200
```

## 坑

- **PATH 必须含 `C:\Windows\System32` 和 nodejs**：Pyodide 启动器要 `findstr`，缺了会在编译依赖阶段报 `'findstr' is not recognized` / `No node executable found on the path`——看起来像 Python 错误，其实是 PATH 问题。
- **解释器要 3.14**：`compatibility_date` 已越过 Pyodide 3.14 分界日（`2026-09-08`），`pywrangler` 按 3.14 编依赖，`pylock.toml` 要求 `>=3.14.2`，配 3.12 会报 "The requested interpreter resolved to Python 3.12.16, which is incompatible..."。`UV_PYTHON=3.14` + `.python-version=3.14`。
- `pylock.toml` 是 gitignore 的生成物，删了会自动重建；入库的是 `uv.lock`。
- D1 SQL **必须单行**（多行报 `incomplete input`）；`PRAGMA auto_vacuum/page_count/freelist_count` 被拒，`page_size` 可读。
- 分页参数是 `limit`/`offset`（不是 `page`/`limit`）；业务路由统一在 `/api` 下（`app.py` 的 `API_PREFIX`，前端拼 `API_ROOT`），根路径只留给 `/` 健康检查与 `/docs`、`/openapi.json`。
- 时间一律 UTC datetime 字符串 `YYYY-MM-DD HH:MM:SS`；软删写 `datetime('now')`，读用 `deleted_at IS NULL`。

## 结构约定

- `src/api/models.py` 是契约常量真源；前端 `src/constants.ts` 是手抄副本，前端 CI 的 `scripts/check-contract.mjs` 会 clone 本仓库比对——**改常量先 push 本仓库**。
- 纯规则（时间校验、权限码矩阵）抽在 `src/core/`，测试在 `tests/` 直跑 pytest，不碰 workerd。
- 定时任务在 `src/tasks/`，按 activity 保留 → cache 清理 → memos 墓碑 的顺序挂在 `cron.py`。

完整的上线清单、契约 10 项对照表、已知缺口，见前端仓库根目录 `AGENTS.md`。

