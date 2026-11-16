"""``due_at`` 校验规则：全站只认 UTC datetime，不接受只给日期。

这组用例对应用户侧真的踩过的坑：日历选了某天，保存后编辑再选另一天，
日期整体偏了一天——根因就是"日期"被当成本地日语义。
"""

import pytest

from core.timestamps import normalize_utc_datetime


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("2026-11-20 15:59:59", "2026-11-20 15:59:59"),
        (" 2026-11-20 15:59:59 ", "2026-11-20 15:59:59"),
        # 客户端可能发 ISO 的 T 分隔写法
        ("2026-11-20T15:59:59", "2026-11-20 15:59:59"),
        # 小数秒不参与存储
        ("2026-11-20 15:59:59.123456", "2026-11-20 15:59:59"),
        # 只给分钟：补成整秒
        ("2026-11-20 15:59", "2026-11-20 15:59:00"),
        # Z 与偏移都换算到 UTC：北京 11-21 00:30 == UTC 11-20 16:30
        ("2026-11-20T15:59:59Z", "2026-11-20 15:59:59"),
        ("2026-11-21T00:30:00+08:00", "2026-11-20 16:30:00"),
        ("2026-11-20T08:00:00-05:00", "2026-11-20 13:00:00"),
    ],
)
def test_normalizes_to_utc_datetime(raw, expected):
    assert normalize_utc_datetime(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2026-11-20",  # 只给日期：必须拒绝，否则又会退回本地日语义
        "2026-02-30 10:00:00",  # 不存在的日子
        "not-a-date",
        "2026/11/20 10:00:00",
    ],
)
def test_rejects_non_datetime(raw):
    with pytest.raises(ValueError):
        normalize_utc_datetime(raw)


def test_date_only_message_points_at_the_fix():
    with pytest.raises(ValueError, match="must include a time"):
        normalize_utc_datetime("2026-11-20")
