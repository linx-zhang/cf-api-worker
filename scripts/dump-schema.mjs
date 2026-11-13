// 从线上 D1 导出表结构，生成 docs/schema.md（人类和 LLM 读的「模型层」）。
//
// 为什么不用手写的 ORM 模型类：这个项目没有 ORM（每个查询都是手写 SQL），
// 手写的模型会与真实库悄悄漂移，且没有任何机制能发现。这份文档每次都由
// live D1 的实际结构生成，跟库同步，而不是跟记忆同步。
//
// 用法：node scripts/dump-schema.mjs      （或 npm run schema）
// 前提：wrangler 已登录，目标库为 wrangler.jsonc 里的 api-worker-db。
//
// 实现注意：D1 不允许 `pragma_table_info()` 这种表值函数参与 JOIN（SQLITE_AUTH），
// 只能一条条发 `PRAGMA ...` 语句，所以这里按表逐个查询。

import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

const ROOT = path.resolve(import.meta.dirname, '..')
const DB = 'api-worker-db'
const OUT = path.join(ROOT, 'docs', 'schema.md')
const WRANGLER = path.join(ROOT, 'node_modules', 'wrangler', 'bin', 'wrangler.js')

function query(sql) {
  const result = spawnSync(
    process.execPath,
    [WRANGLER, 'd1', 'execute', DB, '--remote', '--json', '--command', sql],
    { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 },
  )
  if (result.status !== 0) {
    throw new Error(`查询失败: ${sql}\n${result.stderr || result.stdout}`)
  }
  const text = result.stdout
  const start = text.search(/[[{]/)
  const payload = JSON.parse(text.slice(start))
  return payload[0]?.results ?? []
}

const quote = (name) => `"${name.replace(/"/g, '""')}"`

const objects = query(
  `SELECT type, name, tbl_name, sql FROM sqlite_master
   WHERE type IN ('table', 'index')
     AND name NOT LIKE 'sqlite_%'
     AND name NOT LIKE '_cf_%'      -- Cloudflare 内部表（KV 镜像），没有权限读
     AND name <> 'd1_migrations'    -- wrangler 自己的迁移记录表
   ORDER BY tbl_name, type DESC, name`,
)

const tables = objects.filter((o) => o.type === 'table').map((o) => o.name)

const columns = {}
const fks = {}
for (const table of tables) {
  process.stdout.write(`  读取 ${table} …\r`)
  columns[table] = query(`PRAGMA table_info(${quote(table)})`)
  fks[table] = query(`PRAGMA foreign_key_list(${quote(table)})`)
}

// 索引列从 CREATE INDEX 语句里取（D1 不允许 pragma_index_info 参与 JOIN，
// 单独再发 N 条语句又太慢，这里直接解析 DDL 文本）
function indexColumns(sql) {
  const match = /\(([^)]*)\)\s*$/.exec((sql || '').trim())
  if (!match) return ''
  return match[1]
    .split(',')
    .map((part) => part.trim().replace(/\s+(ASC|DESC)\b/i, '').toLowerCase())
    .filter(Boolean)
    .join(', ')
}

const indexes = {}
for (const obj of objects.filter((o) => o.type === 'index')) {
  ;(indexes[obj.tbl_name] ??= []).push({
    name: obj.name,
    unique: /CREATE\s+UNIQUE\s+INDEX/i.test(obj.sql || ''),
    columns: indexColumns(obj.sql),
  })
}

const lines = []
lines.push('# 数据库表结构')
lines.push('')
lines.push(
  '> 由 `node scripts/dump-schema.mjs`（`npm run schema`）从线上 D1 `api-worker-db` 实际结构导出，**不要手改**——下一次生成会覆盖。',
)
lines.push('> 改表的唯一入口是 `migrations/`：加完迁移后重跑一次本脚本。')
lines.push('>')
lines.push(
  '> 为什么用这种生成式文档而不是手写 ORM 模型：项目里没有 ORM，手写模型只能靠记忆维护，会和真实库悄悄对不上；这份文档每次都是从库里读出来的。',
)
lines.push('')
lines.push(`共 ${tables.length} 张表：${tables.map((t) => '`' + t + '`').join('、')}`)
lines.push('')

for (const table of tables) {
  lines.push(`## ${table}`)
  lines.push('')
  lines.push('| 列 | 类型 | 可空 | 默认 | 主键 |')
  lines.push('| --- | --- | --- | --- | --- |')
  for (const c of columns[table]) {
    const nullable = c.notnull === 1 || c.pk > 0 ? '否' : '是'
    const dflt = c.dflt_value === null ? '' : '`' + String(c.dflt_value).replace(/`/g, "'") + '`'
    lines.push(`| \`${c.name}\` | ${c.type || '—'} | ${nullable} | ${dflt} | ${c.pk ? c.pk : ''} |`)
  }
  lines.push('')
  const idx = indexes[table] || []
  if (idx.length) {
    lines.push('索引：')
    lines.push('')
    for (const i of idx) {
      lines.push(`- \`${i.name}\`（${i.unique ? '唯一' : '普通'}）：\`${i.columns}\``)
    }
    lines.push('')
  }
  if (fks[table].length) {
    lines.push('外键：')
    lines.push('')
    for (const f of fks[table]) {
      const onDelete =
        f.on_delete && f.on_delete !== 'NO ACTION' ? `，ON DELETE ${f.on_delete}` : ''
      lines.push(`- \`${f.from}\` → \`${f.table}.${f.to}\`${onDelete}`)
    }
    lines.push('')
  }
}

lines.push('## 不在结构里、但写查询时必须记得的约定')
lines.push('')
lines.push(
  '- 软删除：`users` / `posts` / `comments` 都有 `deleted_at`，所有读查询必须带 `deleted_at IS NULL`；删帖时同时软删它的评论。',
)
lines.push(
  '- 时间：统一 `TEXT`，由 `datetime(\'now\')` 写入，即 **UTC**；本地化只在前端做。',
)
lines.push(
  '- 稳定分页：列表按 `created_at DESC, id DESC` 排，`id` 参与排序键，避免同一秒创建的行顺序漂移。',
)
lines.push('')

fs.mkdirSync(path.dirname(OUT), { recursive: true })
fs.writeFileSync(OUT, lines.join('\n'), 'utf8')
process.stdout.write('\r')
console.log(`✓ ${path.relative(ROOT, OUT)}：${tables.length} 张表，${Object.values(columns).reduce((n, c) => n + c.length, 0)} 列`)
