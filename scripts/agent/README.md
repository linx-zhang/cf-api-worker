# scripts/agent

一次性脚本（一次性 SQL 播种、数据核对、探针）放这里，不参与发布；`scripts/` 根目录只留项目工具链（`dump-schema.mjs`，被 `npm run schema` 调用）。

约定：命名 `<日期>-<用途>`；默认只读，写库时把 SQL 放进同名 `.sql` 由脚本仅做调用；不留密钥。
线上改数据优先走 `npx wrangler d1 execute api-worker-db --remote --file=<sql>`，改完用 `GET` 接口核对（不要用 HEAD）。
