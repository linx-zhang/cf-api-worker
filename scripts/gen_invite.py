#!/usr/bin/env python3
"""Create invite codes and store them in D1 (local by default, remote with --remote).

Usage:
    uv run python scripts/gen_invite.py --role internal --days 30
    uv run python scripts/gen_invite.py --role admin --days 0 --count 3
    uv run python scripts/gen_invite.py --role internal --days 30 --remote

The code is a uuid4 string. Apply the migration first
(``npx wrangler d1 migrations apply api-worker-db --local``), otherwise the
invite_codes table does not exist locally.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.utils import sqlite_timestamp, utc_now

ROLES = ("user", "internal", "admin")
DEFAULT_DB = "api-worker-db"


def build_sql(codes: list[str], role: str, expires_at: str | None) -> str:
    expiry = "NULL" if expires_at is None else f"'{expires_at}'"
    return "; ".join(
        "INSERT INTO invite_codes (code, role, expires_at) "
        f"VALUES ('{code}', '{role}', {expiry})"
        for code in codes
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=ROLES, default="internal")
    parser.add_argument(
        "--days", type=int, default=30, help="validity in days; 0 means never expire"
    )
    parser.add_argument(
        "--count", type=int, default=1, help="how many invite codes to create"
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="D1 database name")
    parser.add_argument(
        "--remote",
        action="store_true",
        help="write to the remote database instead of the local one",
    )
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be >= 1")

    codes = [str(uuid.uuid4()) for _ in range(args.count)]
    expires_at = None
    if args.days > 0:
        expires_at = sqlite_timestamp(utc_now() + timedelta(days=args.days))

    # On Windows npx is npx.cmd; resolve it so subprocess can launch it directly.
    npx = shutil.which("npx") or shutil.which("npx.cmd") or "npx"
    command = [
        npx,
        "wrangler",
        "d1",
        "execute",
        args.db,
        "--command",
        build_sql(codes, args.role, expires_at),
    ]
    command.insert(4, "--remote" if args.remote else "--local")

    result = subprocess.run(
        command, check=False, capture_output=True, text=True, shell=False
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode

    print(f"role={args.role}")
    print(f"expires_at={expires_at or 'never'}")
    print(f"count={len(codes)}")
    for code in codes:
        print(f"code={code}")
    print("stored in", "remote" if args.remote else "local", "D1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
