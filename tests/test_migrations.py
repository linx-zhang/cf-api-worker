"""迁移文件本身的静态契约：编号连续、命名稳定、文件非空。

真正的 SQL 语义由 ``conftest`` 在每个用例的临时库里全量执行；这里补的是
「部署时 wrangler 按文件名排序」这一层——漏号、重号、乱命名不会被业务测试
发现，却会让线上 schema 与仓库顺序不一致。
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def test_migration_files_are_sequential_and_non_empty():
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert files, "migrations/ 为空"

    numbers = []
    for path in files:
        match = _NAME.match(path.name)
        assert match, f"迁移文件名不符合 NNNN_snake_case.sql: {path.name}"
        numbers.append(int(match.group(1)))
        assert path.read_text(encoding="utf-8").strip(), f"迁移文件为空: {path.name}"

    assert numbers == list(range(1, len(files) + 1)), (
        "迁移编号必须从 0001 连续到最后一个文件，不能跳号或重号："
        f"{[path.name for path in files]}"
    )
