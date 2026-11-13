#!/usr/bin/env node
// 往 posts 表写一篇博客：把一段 markdown 变成一个固定格式的 INSERT，再选择是否直接执行。
//
// 为什么要有这个脚本：手写 INSERT 最容易错的地方是转义（正文里的 ' 会把语句截断）
// 和 title 长度上限（后端 PostIn.title 限制 200 字符），这两件事每次都重新判断一遍
// 迟早出错。这里固定成一条路径：md 进 → SQL 出 → 可选的执行与回读。
//
// 用法：
//   node scripts/new-post.mjs --title "标题" --file drafts/xxx.md            # 只生成 SQL
//   node scripts/new-post.mjs --title "标题" --file drafts/xxx.md --execute  # 生成并写入远端 D1
//   node scripts/new-post.mjs --title "标题" --file drafts/xxx.md --author 6 --execute
//
// 约定：author 默认 6（linx@zh.com，站点内容作者）；created_at 由 D1 的默认值
// datetime('now') 写入（UTC），不要手填，除非是在补历史帖。

import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

const ROOT = path.resolve(import.meta.dirname, '..')
const DB = 'api-worker-db'
const WRANGLER = path.join(ROOT, 'node_modules', 'wrangler', 'bin', 'wrangler.js')
const TITLE_MAX = 200 // 后端 api/models.py 的 PostIn.title 约束

function arg(name, fallback = null) {
  const i = process.argv.indexOf(`--${name}`)
  if (i === -1) return fallback
  const v = process.argv[i + 1]
  if (v === undefined || v.startsWith('--')) return fallback
  return v
}

const title = arg('title')
const file = arg('file')
const author = arg('author', '6')
const out = arg('out', null)
const createdAt = arg('created-at', null)
const execute = process.argv.includes('--execute')

if (!title || !file) {
  console.error('用法: node scripts/new-post.mjs --title "…" --file <md路径> [--author 6] [--out x.sql] [--created-at "…"] [--execute]')
  process.exit(1)
}
if (!Number.isInteger(Number(author)) || Number(author) < 1) {
  console.error(`--author 必须是正整数，收到 ${author}`)
  process.exit(1)
}
// 后端按字符数校验长度，这里也按字符（码点）数，避免中英混排时判断不一致
if ([...title].length > TITLE_MAX) {
  console.error(`标题 ${[...title].length} 字，超过上限 ${TITLE_MAX}`)
  process.exit(1)
}

const abs = path.resolve(ROOT, file)
if (!fs.existsSync(abs)) {
  console.error(`找不到 md 文件: ${abs}`)
  process.exit(1)
}

const bodyRaw = fs.readFileSync(abs, 'utf8').replace(/\r\n/g, '\n').trim()
if (!bodyRaw) {
  console.error('md 文件是空的')
  process.exit(1)
}
// 详情页会单独渲染 title，正文里再出现一个 H1 就重复了，所以统一去掉首行 H1
let body = bodyRaw
if (/^#\s+/.test(body)) {
  const firstLine = body.split('\n', 1)[0]
  body = body.slice(firstLine.length).trim()
  console.log(`- 已去掉首行 H1（${firstLine.replace(/^#\s+/, '')}），它与文章标题重复`)
}

// SQL 字符串转义只有一种正确做法：把 ' 写成 ''
const q = (s) => `'${s.replace(/'/g, "''")}'`

const columns = ['author_id', 'title', 'body_md']
const values = [String(Number(author)), q(title), q(body)]
if (createdAt) {
  columns.push('created_at')
  values.push(q(createdAt))
}

const sql = [
  `-- ${title}`,
  `-- 生成于 ${new Date().toISOString()}，正文 ${[...body].length} 字，文件 ${path.relative(ROOT, abs)}`,
  `INSERT INTO posts (${columns.join(', ')}) VALUES (${values.join(', ')});`,
  '',
].join('\n')

const outPath = out ? path.resolve(ROOT, out) : path.join(ROOT, 'scripts', 'agent', `post-${Date.now()}.sql`)
fs.mkdirSync(path.dirname(outPath), { recursive: true })
fs.writeFileSync(outPath, sql, 'utf8')
console.log(`✓ SQL: ${path.relative(ROOT, outPath)}（正文 ${[...body].length} 字，标题 ${[...title].length} 字）`)

if (!execute) {
  console.log(`下一步: node scripts/new-post.mjs ... --execute   或   npx wrangler d1 execute ${DB} --remote --file="${path.relative(ROOT, outPath)}"`)
  process.exit(0)
}

const run = spawnSync(
  process.execPath,
  [WRANGLER, 'd1', 'execute', DB, '--remote', '--json', '--file', outPath],
  { cwd: ROOT, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 },
)
if (run.status !== 0) {
  console.error(run.stdout || '')
  console.error(run.stderr || '')
  process.exit(run.status ?? 1)
}
const payload = JSON.parse(run.stdout.slice(run.stdout.search(/[[{]/)))
const meta = payload[0]?.meta ?? {}
console.log(`✓ 已写入 D1：changes=${meta.changes} last_row_id=${meta.last_row_id}`)

// 立刻用列表接口回读，确认线上真的出现（D1 写入与 API 读之间不需要部署）
const res = spawnSync(
  process.execPath,
  [
    '-e',
    `fetch('https://linx-api-78i.pages.dev/posts?page=1&page_size=5')
       .then(r => r.json())
       .then(d => console.log(d.data.map(p => p.id + ' | ' + p.title).join('\\n')))`,
  ],
  { encoding: 'utf8' },
)
if (res.stdout) console.log(`--- 线上最新 5 条 ---\n${res.stdout.trim()}`)
