"""bigmodel_client 单测 — 覆盖 None 安全聚合与 NULL vs 0 字段语义。

背景: 2026-08-25 起 grouped.cache_write_tokens 默认 None（上游"缓存写入"
限时免费未暴露），模型级聚合 `0 += None` 崩溃（run #189, TypeError:
unsupported operand type(s) for +=: 'int' and 'NoneType'）。
语义口径（CLAUDE.md Field Semantics）:
- 计数字段聚合 None 视为 0（上游 2026-08-24 起部分行返回 null）
- 上游整体缺失的字段（全无"缓存写入"行）结果保持 None, 不伪造 0
"""
from __future__ import annotations

import bigmodel_client


def _row(date: str, model: str, token_type: str,
         usage=None, api=None, cost=None) -> dict:
    """构造一条账单行. usage/api/cost 传 None 模拟上游 null 返回。"""
    return {
        "billingDate": date,
        "modelCode": model,
        "tokenType": token_type,
        "usageCount": usage,
        "apiUsage": api,
        "originalAmount": cost,
    }


class TestAggregateNoneSafety:
    """None 值聚合不能崩, 且 NULL vs 0 语义不能改坏。"""

    def test_no_cache_write_rows_keeps_none(self):
        """回归: 无"缓存写入"行时模型级 0 += None 崩溃 (run #189)。"""
        result = bigmodel_client.aggregate_by_day_and_model([
            _row("2026-08-24", "glm-4.5-air", "输入", 100, 10, 1.5),
            _row("2026-08-24", "glm-4.5-air", "输出", 50, 5, 0.5),
        ])
        m = result["models"]["glm-4.5-air"]

        # 上游整体缺失 → None (不是 0)
        assert m["cache_write_tokens"] is None
        assert result["grouped"][("2026-08-24", "glm-4.5-air")]["cache_write_tokens"] is None
        assert m["prompt_tokens"] == 100
        assert m["completion_tokens"] == 50
        assert m["total_cost"] == 2.0

    def test_partial_cache_write_rows_sums_none_as_zero(self):
        """部分日期有"缓存写入"行: 其余日期 None 视为 0, 总和为真数。"""
        result = bigmodel_client.aggregate_by_day_and_model([
            _row("2026-08-24", "glm-5.3", "输入", 200, 5, 2.0),   # 无缓存写入行
            _row("2026-08-25", "glm-5.3", "输入", 100, 5, 1.0),
            _row("2026-08-25", "glm-5.3", "缓存写入", 40, 0, 0.2),
        ])
        m = result["models"]["glm-5.3"]

        assert m["cache_write_tokens"] == 40  # None + 40, 不是 None 也不是 40+伪造值

    def test_null_count_fields_treated_as_zero(self):
        """上游 2026-08-24 起部分行计数字段返回 null → 聚合按 0。"""
        result = bigmodel_client.aggregate_by_day_and_model([
            _row("2026-08-24", "glm-4.5-air", "输入", None, None, None),
            _row("2026-08-24", "glm-4.5-air", "输出", 50, 10, 0.5),
        ])
        m = result["models"]["glm-4.5-air"]

        assert m["prompt_tokens"] == 0
        assert m["total_tokens"] == 50
        assert m["total_count"] == 10
        assert m["total_cost"] == 0.5

    def test_empty_rows_returns_empty_shape(self):
        """空窗口不崩, 返回空 shape。"""
        result = bigmodel_client.aggregate_by_day_and_model([])

        assert result["models"] == {}
        assert result["daily"] == []
        assert result["total_cost"] == 0
