"""kimi_client 单测 — 覆盖"按单价反推 token"的定价口径。

背景: kimi consumes 接口只给金额不给 token, client 用 "费用 ÷ 单价" 反推。

注: 恢复自 origin/main (bd7cc65) 的可运行子集。缺价模型 NULL 化、rtoken
入口续期等用例钉在本地尚未移植的功能上, 功能移植后从 origin/main 恢复完整版。
"""
from __future__ import annotations

import pytest

import kimi_client


@pytest.fixture(autouse=True)
def _no_litellm_http(monkeypatch):
    """屏蔽 LiteLLM 价目 HTTP 调用, 默认只剩 FALLBACK_PRICING。"""
    monkeypatch.setattr(kimi_client, "_fetch_litellm_prices", lambda: {})


def _row(model: str, product_name: str, amount: int, date: str = "2026-07-25") -> dict:
    """构造一条 consumes 行. amount 单位 10^-5 CNY。"""
    return {
        "product_model_id": model,
        "product_name": product_name,
        "date": date,
        "amount": amount,
    }


class TestAggregatePricedModel:
    """有价格时的反推口径不能被改坏。"""

    def test_prompt_tokens_derived_from_input_price(self):
        # kimi-k2.6 input 6.5 ¥/M → 6.5 元 = 1M tokens
        result = kimi_client.aggregate([_row("kimi-k2.6", "prompt", 650_000)])
        m = result["models"]["kimi-k2.6"]

        assert m["prompt_tokens"] == 1_000_000
        assert m["total_cost"] == pytest.approx(6.5)

    def test_all_product_names_derived(self):
        # input 6.5 / cache_read 1.1 / output 27.0 (¥ per 1M)
        rows = [
            _row("kimi-k2.6", "prompt", 650_000),        # ¥6.5  → 1M
            _row("kimi-k2.6", "auto-caching", 110_000),  # ¥1.1  → 1M
            _row("kimi-k2.6", "completion", 2_700_000),  # ¥27.0 → 1M
        ]

        m = kimi_client.aggregate(rows)["models"]["kimi-k2.6"]

        assert m["prompt_tokens"] == 1_000_000
        assert m["cache_read_tokens"] == 1_000_000
        assert m["completion_tokens"] == 1_000_000
        assert m["total_tokens"] == 3_000_000

    def test_missing_product_name_stays_zero_when_priced(self):
        """有价格但上游没返 completion → 真的是 0 次输出, 不是 NULL。"""
        m = kimi_client.aggregate([_row("kimi-k2.6", "prompt", 650_000)])["models"]["kimi-k2.6"]

        assert m["prompt_tokens"] == 1_000_000
        assert m["completion_tokens"] == 0
        assert m["cache_read_tokens"] == 0
