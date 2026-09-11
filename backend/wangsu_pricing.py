"""把网宿模型名映射到 LiteLLM 定价库, 用同一套 calculator 算费用, 统一换算成 CNY。

不命中的模型 cost = 0, 同时返回 matched_alias=None 方便调试。

LiteLLM 定价库:
  - JSON: backend/litellm/data/model_prices.json (213 条)
  - 缓存: backend/litellm/services/pricing_cache_service._find_in_cache
  - 计算: backend/litellm/pricing/calculator.calculate
"""
from __future__ import annotations

import re

from pydantic import TypeAdapter

from exchange_rate import convert
from litellm.pricing.calculator import calculate
from litellm.pricing.types import ExtractedTokens, PricingConfig
from litellm.services.pricing_cache_service import _ensure_loaded, _find_in_cache

_PC_ADAPTER = TypeAdapter(PricingConfig)


def _strip_subprovider_prefix(name: str) -> str:
    """anthropic.claude-opus-4-7 → claude-opus-4-7"""
    for p in ("anthropic.", "gemini.", "openai.", "azure."):
        if name.startswith(p):
            return name[len(p):]
    return name


def _candidates(model_name: str) -> list[str]:
    """生成查找别名:
       原名 / 去 anthropic. 等子前缀 / 数字段 . ↔ - 互换
       (LiteLLM 内部已用 aliases 兼容 blueshirt- 等前缀, 我们不再枚举)。"""
    seen: list[str] = []

    def add(s: str) -> None:
        if s and s not in seen:
            seen.append(s)

    add(model_name)
    bare = _strip_subprovider_prefix(model_name)
    add(bare)
    for v in (model_name, bare):
        add(re.sub(r"(\d+)\.(\d+)", r"\1-\2", v))
        add(re.sub(r"(\d+)-(\d+)(?=\D|$)", r"\1.\2", v))
    return seen


def lookup_pricing(model_name: str) -> tuple[dict | None, str | None]:
    """返回 (config_dict, matched_alias). 没找到返回 (None, None)。"""
    _ensure_loaded()
    for cand in _candidates(model_name):
        cfg = _find_in_cache(cand, provider=None)
        if cfg:
            return cfg, cand
    return None, None


def compute_cost_cny(
    model_name: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_5m_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
    cache_creation_tokens: int = 0,
    request_count: int = 1,
) -> tuple[float, str | None, str]:
    """返回 (cost_cny, matched_alias, currency)。无定价 → (0.0, None, '')."""
    cfg_dict, matched = lookup_pricing(model_name)
    if not cfg_dict:
        return 0.0, None, ""
    try:
        cfg = _PC_ADAPTER.validate_python(cfg_dict)
    except Exception:
        return 0.0, matched, ""
    tokens = ExtractedTokens(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_creation_5m_tokens=cache_creation_5m_tokens,
        cache_creation_1h_tokens=cache_creation_1h_tokens,
    )
    cost, _ = calculate(tokens, cfg, request_count or 1)
    currency = getattr(cfg, "currency", "USD") or "USD"
    cost_cny = convert(cost, currency, "CNY") if currency != "CNY" else cost
    return float(cost_cny), matched, currency
