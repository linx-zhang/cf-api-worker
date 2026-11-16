"""Timestamp parsing rules shared by the API models.

Kept free of pydantic/FastAPI so the rules can be tested directly with pytest
(``tests/test_due_at.py``) instead of through a request or the workers runtime.

The whole site stores UTC ``'YYYY-MM-DD HH:MM:SS'``. There is deliberately no
"date only" form for user input: a bare ``2026-11-20`` would silently mean
"somewhere in that local day", which is exactly the ambiguity that made the
older due-date handling shift by a day.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

# ``due_at`` 只接受带时刻的写法；只给日期的输入必须被拒绝
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_utc_datetime(value: str | None) -> str | None:
    """Validate a due instant; blank clears it, otherwise it must be a datetime.

    Aware input (``+08:00`` / ``Z``) is converted to UTC; naive input is taken as
    UTC already. The returned string is always ``'YYYY-MM-DD HH:MM:SS'``.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if DATE_ONLY.match(text):
        raise ValueError("due_at must include a time, e.g. 2026-11-20 15:59:59")
    try:
        # Python 3.11+ 的 fromisoformat 自己认 'Z'；带偏移的写法也一并解析
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("due_at must be a datetime like 2026-11-20 15:59:59") from None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%d %H:%M:%S")
