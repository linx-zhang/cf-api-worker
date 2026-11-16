-- memos.due_at 从「本地日期」改成「UTC 时刻」，与全站其他时间列统一。
--
-- 旧语义：'YYYY-MM-DD'，由前端按本地日期解释，服务端用 date('now','+8 hours') 判断到期。
-- 新语义：'YYYY-MM-DD HH:MM:SS'，UTC，与 created_at / done_at / datetime('now') 同形；
-- 前端把「本地那一天的 23:59:59」换算成 UTC 再上送，服务端只做字符串比较。
--
-- 存量行的换算：原值是按北京时区（UTC+8）写的到期日，落到同一刻就是
-- 当地 23:59:59 = UTC 15:59:59，所以补上 ' 15:59:59'。
-- 只改长度正好为 10（即还没带时刻）的行，重复执行不会二次改写。
UPDATE memos
SET due_at = substr(due_at, 1, 10) || ' 15:59:59'
WHERE due_at IS NOT NULL
  AND length(due_at) = 10;
