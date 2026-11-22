"""cron 失败必须留下可检索的一行，否则定时任务坏了只会静默。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tasks import cron


def test_cron_failure_is_logged_with_trigger(monkeypatch, capsys):
    def boom(_env):
        raise RuntimeError("d1 unavailable")

    monkeypatch.setattr(cron, "Database", boom)

    with pytest.raises(RuntimeError, match="d1 unavailable"):
        asyncio.run(cron.run_cron(SimpleNamespace(DB=None), "15 18 * * *"))

    logged = capsys.readouterr().out
    assert "cron failed cron=15 18 * * *" in logged
    assert "RuntimeError: d1 unavailable" in logged
