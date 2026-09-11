"""
Token 提取器：从 LiteLLM_SpendLogs.metadata（JSONB，已是 dict）中解析各类扩展 token 信息。

LiteLLM 的真实 metadata 结构（通过数据库实测确认）：
  metadata.usage_object                    → 基础 token 汇总（和表字段一致）
  metadata.additional_usage_values         → 扩展 token（关键！）
    ├── prompt_tokens_details
    │     └── cached_tokens                → OpenAI 缓存读取 token
    ├── completion_tokens_details
    │     └── reasoning_tokens             → 思考/推理 token（Gemini reasoning、OpenAI o系列）
    ├── claude_cache_creation_1_h_tokens   → Claude 1小时缓存写入 token
    ├── claude_cache_creation_5_m_tokens   → Claude 5分钟缓存写入 token
    └── input_tokens_details               → 其他厂商扩展（Gemini 等）

  metadata.cost_breakdown                  → LiteLLM 已计算的费用（可用于对账）
    ├── input_cost
    ├── output_cost
    └── total_cost
"""
from __future__ import annotations

import logging

from litellm.pricing.types import ExtractedTokens, ModelFamily

logger = logging.getLogger(__name__)


def extract_tokens(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    metadata: dict | None,
    model_family: ModelFamily,
) -> ExtractedTokens:
    """
    从 metadata dict 中提取各类 token 数量。
    metadata 是 JSONB 字段，SQLAlchemy 读出后已是 Python dict，直接传入即可。
    """
    base = ExtractedTokens(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    if not metadata or not isinstance(metadata, dict):
        return base

    # LiteLLM 的扩展 token 数据统一存在 additional_usage_values 里
    extra: dict = metadata.get("additional_usage_values") or {}

    if model_family == ModelFamily.ANTHROPIC:
        return _extract_anthropic(base, extra)
    if model_family in (ModelFamily.OPENAI, ModelFamily.DEEPSEEK):
        return _extract_openai_style(base, extra)
    if model_family == ModelFamily.GOOGLE:
        return _extract_openai_style(base, extra)  # Gemini 格式与 OpenAI 相同
    return base


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _extract_openai_style(base: ExtractedTokens, extra: dict) -> ExtractedTokens:
    """
    OpenAI / DeepSeek / Gemini 格式（均使用 OpenAI 兼容结构）：
      additional_usage_values.prompt_tokens_details.cached_tokens    → cache_read_tokens
      additional_usage_values.completion_tokens_details.reasoning_tokens → thinking_tokens
    """
    prompt_details: dict = extra.get("prompt_tokens_details") or {}
    completion_details: dict = extra.get("completion_tokens_details") or {}

    return ExtractedTokens(
        prompt_tokens=base.prompt_tokens,
        completion_tokens=base.completion_tokens,
        cache_read_tokens=_safe_int(prompt_details.get("cached_tokens")),
        thinking_tokens=_safe_int(completion_details.get("reasoning_tokens")),
    )


def _extract_anthropic(base: ExtractedTokens, extra: dict) -> ExtractedTokens:
    """
    Anthropic Claude 格式（通过 LiteLLM 代理后的真实字段名）：
      additional_usage_values.claude_cache_creation_1_h_tokens → cache_creation_tokens（1小时缓存）
      additional_usage_values.claude_cache_creation_5_m_tokens → 也计入 cache_creation
      additional_usage_values.prompt_tokens_details.cached_tokens → cache_read_tokens
    """
    prompt_details: dict = extra.get("prompt_tokens_details") or {}

    cache_creation = (
        _safe_int(extra.get("claude_cache_creation_1_h_tokens"))
        + _safe_int(extra.get("claude_cache_creation_5_m_tokens"))
    )

    return ExtractedTokens(
        prompt_tokens=base.prompt_tokens,
        completion_tokens=base.completion_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=_safe_int(prompt_details.get("cached_tokens")),
    )


def extract_litellm_cost(metadata: dict | None) -> tuple[float, float, float] | None:
    """
    从 metadata.cost_breakdown 提取 LiteLLM 已计算的费用。
    返回 (input_cost, output_cost, total_cost)，若无数据则返回 None。
    可用于"直接采用 LiteLLM 计算结果"的模式，无需自己配单价。
    """
    if not metadata or not isinstance(metadata, dict):
        return None
    cb: dict = metadata.get("cost_breakdown") or {}
    if not cb:
        return None
    try:
        return (
            float(cb.get("input_cost") or 0),
            float(cb.get("output_cost") or 0),
            float(cb.get("total_cost") or 0),
        )
    except (TypeError, ValueError):
        return None
