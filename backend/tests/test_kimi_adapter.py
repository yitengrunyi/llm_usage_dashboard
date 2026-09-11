"""kimi adapter 单测 — 缺价模型的 NULL 传递 + 告警上报。

注: 恢复自 origin/main (bd7cc65) 的可运行子集。warnings 告警上报用例钉在本地
尚未移植的功能上 (缺价 NULL 链路), 功能移植后从 origin/main 恢复完整版。
"""
from __future__ import annotations

import datetime as dt

import pytest

from ingest.adapters.kimi_adapter import KimiAdapter

VENDOR = {"id": "kimi", "name": "kimi", "base_url": "https://platform.kimi.com"}
DAY = dt.date(2026, 7, 25)


def _adapter(monkeypatch, result: dict) -> KimiAdapter:
    import ingest.adapters.kimi_adapter as mod
    monkeypatch.setattr(mod, "fetch_vendor_usage", lambda v, s, e: result)
    return KimiAdapter(VENDOR)


def _unpriced_model() -> dict:
    """client 对缺价模型的输出形态: token 全 None, cost 有值。"""
    return {
        "prompt_tokens": None, "completion_tokens": None,
        "cache_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None,
        "total_tokens": None, "total_count": 0, "total_cost": 224.19,
    }


def _priced_model() -> dict:
    return {
        "prompt_tokens": 1_000_000, "completion_tokens": 200_000,
        "cache_tokens": 500_000, "cache_read_tokens": 500_000, "cache_write_tokens": 0,
        "total_tokens": 1_700_000, "total_count": 0, "total_cost": 12.5,
    }


class TestUnpricedModelRow:
    def test_tokens_stay_null(self, monkeypatch):
        a = _adapter(monkeypatch, {
            "models": {"kimi-k3": _unpriced_model()},
            "unpriced_models": ["kimi-k3"],
        })

        row = a.fetch_one_day(DAY)[0]

        assert row.model == "kimi-k3"
        assert row.prompt_tokens is None
        assert row.completion_tokens is None
        assert row.cache_read_tokens is None
        assert row.total_tokens is None
        assert row.cost_native == pytest.approx(224.19)

class TestPricedModelRow:
    def test_prompt_packs_cache_into_input(self, monkeypatch):
        a = _adapter(monkeypatch, {
            "models": {"kimi-k2.6": _priced_model()}, "unpriced_models": [],
        })

        row = a.fetch_one_day(DAY)[0]

        # prompt_tokens 出库口径 = 非缓存 input + cache_read
        assert row.prompt_tokens == 1_500_000
        assert row.completion_tokens == 200_000
        assert row.cache_read_tokens == 500_000
        assert row.cache_write_tokens is None  # kimi 没这概念, 不伪造 0
        assert row.total_tokens == 1_700_000
