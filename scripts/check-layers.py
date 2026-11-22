r"""Fail the build when a lower layer imports a higher one.

The Worker has six source layers, from lowest to highest:

    core < infra < services < api
                         \ tasks
    entry (composition root)

``core`` is pure standard-library logic, ``infra`` owns D1/cache/config,
``services`` owns domain rules shared by HTTP and cron, ``api`` owns HTTP,
``tasks`` owns scheduled jobs. The dependency direction is deliberately one
way: lower layers never know about callers above them. Without a check this
erodes quietly — for example ``api/routers/admin.py`` once imported a constant
from ``tasks/activity_retention.py`` just because it was convenient.

This is a source-level import check, not an import-time test: it catches the
edge even when a module happens to import cleanly. Same-layer imports are
allowed. ``entry.py`` is the only composition root and may import ``api`` and
``tasks``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

LAYERS = ("core", "infra", "services", "api", "tasks")

# Allowed project-layer imports for each layer. A module may always import its
# own layer and lower layers. Anything absent here is a violation.
ALLOWED = {
    "core": {"core"},
    "infra": {"core", "infra"},
    "services": {"core", "infra", "services"},
    "api": {"api", "core", "infra", "services"},
    "tasks": {"core", "infra", "services", "tasks"},
    "entry": {"api", "tasks", "core", "infra", "services"},
}


def layer_of(path: Path) -> str:
    """Return the layer name for a file under ``src``."""
    rel = path.relative_to(SRC)
    if rel.name == "entry.py":
        return "entry"
    head = rel.parts[0]
    return head if head in LAYERS else "entry"


def imported_layers(tree: ast.AST) -> list[tuple[int, str]]:
    """Yield ``(lineno, top_level_package)`` for absolute project imports."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative imports stay inside the current package; the
                # absolute-path policy is enforced elsewhere.
                continue
            module = node.module or ""
            head = module.split(".", 1)[0]
            if head in LAYERS:
                found.append((node.lineno, head))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".", 1)[0]
                if head in LAYERS:
                    found.append((node.lineno, head))
    return found


def main() -> int:
    violations: list[str] = []
    checked = 0
    for path in sorted(SRC.rglob("*.py")):
        checked += 1
        layer = layer_of(path)
        allowed = ALLOWED[layer]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            print(f"{path.relative_to(ROOT)}:{exc.lineno}: cannot parse: {exc.msg}")
            return 2
        for lineno, imported in imported_layers(tree):
            if imported not in allowed:
                rel = path.relative_to(ROOT)
                violations.append(
                    f"{rel}:{lineno}: {layer} layer must not import {imported}"
                )

    if violations:
        print("Layer check failed:")
        for item in violations:
            print(f"  {item}")
        print("\nAllowed direction: core <- infra <- services <- api/tasks; entry on top.")
        return 1

    print(f"Layer check OK ({checked} files, {len(LAYERS)} layers).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
