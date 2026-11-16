"""核对 docs/api.md 的端点表是否与路由声明一致。

不导入 FastAPI：本机 pyodide/venv 差异太大，正则读装饰器就够用，
而且这里要的是「表里有没有这一行」，不是运行时行为。

用法：
    python scripts/check-api-doc.py

通过时打印统计并退出 0；发现漏写或多写时逐条列出并退出 1。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTERS = ROOT / "src" / "api" / "routers"
APP = ROOT / "src" / "api" / "app.py"
DOC = ROOT / "docs" / "api.md"

_ROUTE_RE = re.compile(
    r'@(\w+)\.(get|post|patch|delete|put)\("([^"]*)"', re.MULTILINE
)
_MOUNT_RE = re.compile(r'include_router\((\w+)\.(\w+), prefix="([^"]*)"')
# 业务路由都挂在一个带前缀的父 router 上（``app.include_router(api)``），
# 所以文档里的路径要带上这一层；正则取常量，避免脚本里再抄一遍。
_PREFIX_RE = re.compile(r'^API_PREFIX = "([^"]*)"', re.MULTILINE)


def _mounts() -> dict[str, dict[str, str]]:
    """模块名 -> {router 对象名: 前缀}，例如 comments -> {router: /posts, delete_router: /comments}。

    不能只按对象名建映射：七个模块的普通路由对象都叫 ``router``，
    只按名字取前缀会让所有模块都挂到最后一个前缀上。
    """
    text = APP.read_text(encoding="utf-8")
    mounts: dict[str, dict[str, str]] = {}
    for module, obj, prefix in _MOUNT_RE.findall(text):
        mounts.setdefault(module, {})[obj] = prefix
    return mounts


def _api_prefix() -> str:
    """业务 router 的公共前缀，例如 ``/api``；没有这一层时返回空串。"""
    text = APP.read_text(encoding="utf-8")
    match = _PREFIX_RE.search(text)
    return match.group(1) if match else ""


def _routes() -> list[tuple[str, str]]:
    mounts = _mounts()
    prefix = _api_prefix()
    result: list[tuple[str, str]] = []
    for path in sorted(ROUTERS.glob("*.py")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        module_mounts = mounts.get(path.stem, {})
        for obj, verb, sub in _ROUTE_RE.findall(text):
            mount = module_mounts.get(obj)
            if mount is None:
                continue
            full = (prefix + mount + sub) or "/"
            result.append((verb.upper(), full))
    result.append(("GET", "/"))
    return sorted(result)


def _doc_rows() -> set[tuple[str, str]]:
    rows: set[tuple[str, str]] = set()
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        verb = cells[0].replace("`", "").strip().upper()
        if verb not in {"GET", "POST", "PATCH", "DELETE", "PUT"}:
            continue
        path = cells[1].replace("`", "").strip()
        # 文档里用 {post_id} / {code} 这类占位符，统一成 {}
        path = re.sub(r"\{[^}]*\}", "{}", path)
        rows.add((verb, path))
    return rows


def main() -> int:
    routes = _routes()
    doc = _doc_rows()
    normalized = {(verb, re.sub(r"\{[^}]*\}", "{}", path)) for verb, path in routes}

    missing = sorted(normalized - doc)
    extra = sorted(doc - normalized)

    print(f"routes: {len(normalized)}  doc rows: {len(doc)}")
    for verb, path in missing:
        print(f"  漏写  {verb:6} {path}")
    for verb, path in extra:
        print(f"  多写  {verb:6} {path}")
    return 1 if missing or extra else 0


if __name__ == "__main__":
    sys.exit(main())
