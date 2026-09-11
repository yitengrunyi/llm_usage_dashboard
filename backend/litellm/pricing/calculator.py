"""
费用计算器：根据 PricingConfig 和 ExtractedTokens 计算花费。

阶梯定价说明（AdvancedPricing）：
  - input_tiers / output_tiers 均为"按请求"的档位（每个请求独立选档，不累计）。
  - 在聚合场景（GROUP BY 之后的汇总行）中，我们用平均每请求 token 量来近似选档：
      avg_k = total_tokens / (request_count * 1000)
  - 如果 request_count 不传（默认 1），则把总量当作单请求处理。
  - 若聚合组内请求跨多个档位，此近似存在偏差，可接受（报表场景）。
"""
from __future__ import annotations

from litellm.pricing.types import (
    AdvancedPricing,
    CombinedPricing,
    ComboTierEntry,
    ExtractedTokens,
    PricingConfig,
    StandardPricing,
    TierEntry,
    TieredPricing,
    WithCachePricing,
    WithThinkingPricing,
)

CostBreakdown = dict[str, float]


def calculate(
    tokens: ExtractedTokens,
    config: PricingConfig,
    request_count: int = 1,
) -> tuple[float, CostBreakdown]:
    """
    统一计算入口，根据 config 类型分发。
    返回 (total_cost, breakdown)，货币单位由 config.currency 决定（旧类型默认 USD）。

    breakdown keys: input / output / thinking / cache_write / cache_write_5min /
                    cache_write_1h / cache_read / cache_storage
    """
    if isinstance(config, StandardPricing):
        return _calc_standard(tokens, config)
    if isinstance(config, WithCachePricing):
        return _calc_with_cache(tokens, config)
    if isinstance(config, WithThinkingPricing):
        return _calc_with_thinking(tokens, config)
    if isinstance(config, CombinedPricing):
        return _calc_combined(tokens, config)
    if isinstance(config, TieredPricing):
        return _calc_tiered(tokens, config)
    if isinstance(config, AdvancedPricing):
        return _calc_advanced(tokens, config, request_count)
    return 0.0, {}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _per_1m(tokens: int, price: float) -> float:
    """tokens 数 × 每 1M 单价"""
    return tokens / 1_000_000 * price


def _lookup_tier(total_tokens: int, request_count: int, tiers: list[TierEntry]) -> float:
    """
    按总消耗（K tokens）查找档位单价。request_count 传 1 时即按总 token 量选档。
    若 request_count == 0 则返回最后一档。
    """
    if request_count <= 0:
        return tiers[-1].price_per_1m
    total_k = (total_tokens / request_count) / 1000.0
    for tier in tiers:
        if tier.up_to_k is None or total_k <= tier.up_to_k:
            return tier.price_per_1m
    return tiers[-1].price_per_1m


def _lookup_combo_tier(
    prompt_tokens: int,
    completion_tokens: int,
    request_count: int,
    combo_tiers: list[ComboTierEntry],
) -> tuple[float, float, float | None] | None:
    """
    按 input/output 组合查找档位单价。
    返回 (input_price, output_price, cache_read_price) 或 None（未匹配）。
    """
    if request_count <= 0 or not combo_tiers:
        return None

    input_k = (prompt_tokens / request_count) / 1000.0
    output_k = (completion_tokens / request_count) / 1000.0

    for tier in combo_tiers:
        input_match = tier.input_up_to_k is None or input_k <= tier.input_up_to_k
        output_match = tier.output_up_to_k is None or output_k <= tier.output_up_to_k
        if input_match and output_match:
            return (tier.input_price_per_1m, tier.output_price_per_1m, tier.cache_read_price_per_1m)

    return None


# ---------------------------------------------------------------------------
# 各类型计算函数
# ---------------------------------------------------------------------------

def _calc_standard(tokens: ExtractedTokens, cfg: StandardPricing) -> tuple[float, CostBreakdown]:
    breakdown: CostBreakdown = {
        "input": _per_1m(tokens.prompt_tokens, cfg.input_per_1m),
        "output": _per_1m(tokens.completion_tokens, cfg.output_per_1m),
    }
    if cfg.cache_read_per_1m and tokens.cache_read_tokens:
        breakdown["cache_read"] = _per_1m(tokens.cache_read_tokens, cfg.cache_read_per_1m)
    return sum(breakdown.values()), breakdown


def _calc_with_cache(tokens: ExtractedTokens, cfg: WithCachePricing) -> tuple[float, CostBreakdown]:
    breakdown: CostBreakdown = {}

    # 普通输入（扣除 cache_read 和 cache_creation 后的非缓存 token）
    if tokens.regular_input_tokens > 0:
        breakdown["input"] = _per_1m(tokens.regular_input_tokens, cfg.input_per_1m)
    # 缓存命中：独立计费，不替代输入费
    if tokens.cache_read_tokens > 0:
        breakdown["cache_read"] = _per_1m(tokens.cache_read_tokens, cfg.cache_read_per_1m)

    breakdown["cache_write"] = _per_1m(tokens.total_cache_creation, cfg.cache_write_per_1m)
    breakdown["output"] = _per_1m(tokens.completion_tokens, cfg.output_per_1m)
    return sum(breakdown.values()), breakdown


def _calc_with_thinking(tokens: ExtractedTokens, cfg: WithThinkingPricing) -> tuple[float, CostBreakdown]:
    breakdown: CostBreakdown = {}

    # 普通输入（扣除 cache_read 和 cache_creation 后的非缓存 token）
    if tokens.regular_input_tokens > 0:
        breakdown["input"] = _per_1m(tokens.regular_input_tokens, cfg.input_per_1m)
    # 缓存命中：独立计费，不替代输入费
    if tokens.cache_read_tokens > 0 and cfg.cache_read_per_1m:
        breakdown["cache_read"] = _per_1m(tokens.cache_read_tokens, cfg.cache_read_per_1m)

    if cfg.thinking_per_1m is not None:
        breakdown["thinking"] = _per_1m(tokens.thinking_tokens, cfg.thinking_per_1m)
        breakdown["output"] = _per_1m(tokens.regular_output_tokens, cfg.output_per_1m)
    else:
        breakdown["output"] = _per_1m(tokens.completion_tokens, cfg.output_per_1m)
    return sum(breakdown.values()), breakdown


def _calc_combined(tokens: ExtractedTokens, cfg: CombinedPricing) -> tuple[float, CostBreakdown]:
    breakdown: CostBreakdown = {}

    # 普通输入（扣除 cache_read 和 cache_creation 后的非缓存 token）
    if tokens.regular_input_tokens > 0:
        breakdown["input"] = _per_1m(tokens.regular_input_tokens, cfg.input_per_1m)
    # 缓存命中：独立计费，不替代输入费
    if cfg.cache_read_per_1m is not None and tokens.cache_read_tokens > 0:
        breakdown["cache_read"] = _per_1m(tokens.cache_read_tokens, cfg.cache_read_per_1m)

    if cfg.cache_write_per_1m is not None:
        breakdown["cache_write"] = _per_1m(tokens.total_cache_creation, cfg.cache_write_per_1m)
    if cfg.thinking_per_1m is not None:
        breakdown["thinking"] = _per_1m(tokens.thinking_tokens, cfg.thinking_per_1m)
        breakdown["output"] = _per_1m(tokens.regular_output_tokens, cfg.output_per_1m)
    else:
        breakdown["output"] = _per_1m(tokens.completion_tokens, cfg.output_per_1m)
    return sum(breakdown.values()), breakdown


def _calc_tiered(tokens: ExtractedTokens, cfg: TieredPricing) -> tuple[float, CostBreakdown]:
    breakdown: CostBreakdown = {
        "input": _apply_old_tiers(tokens.prompt_tokens, cfg.tiers, "input"),
        "output": _apply_old_tiers(tokens.completion_tokens, cfg.tiers, "output"),
    }
    return sum(breakdown.values()), breakdown


def _apply_old_tiers(total: int, tiers: list, token_type: str) -> float:
    """旧版 TieredPricing 的累计阶梯计算（向后兼容）"""
    cost, remaining, prev = 0.0, total, 0
    for tier in tiers:
        if remaining <= 0:
            break
        price = tier.input_per_1m if token_type == "input" else tier.output_per_1m
        if tier.up_to_tokens is not None:
            cap = tier.up_to_tokens - prev
            used = min(remaining, cap)
            cost += _per_1m(used, price)
            remaining -= used
            prev = tier.up_to_tokens
        else:
            cost += _per_1m(remaining, price)
            remaining = 0
    return cost


def _calc_advanced(
    tokens: ExtractedTokens,
    cfg: AdvancedPricing,
    request_count: int = 1,
) -> tuple[float, CostBreakdown]:
    """
    高级定价计算（覆盖阶梯、思考/非思考、缓存时长、缓存存储、Batch 折扣等）。

    注意：
    - thinking_tiers 为 None 时，thinking token 与普通 output 同价。
    - 如果 thinking_tiers 有值，thinking token 和 regular_output 分别计费。
    - cache_write 优先使用 5min/1h 细分；否则 fallback 到 cache_write_per_1m。
    - qwen3-max 显/隐写缓存：LiteLLM 暂不区分，fallback 到 cache_write_per_1m。
    - combo_tiers：组合阶梯优先匹配，找不到则回退到 input_tiers + output_tiers。
    """
    breakdown: CostBreakdown = {}

    # ── 组合阶梯匹配（优先）────────────────────────────────────────────────
    use_combo = cfg.combo_tiers is not None
    if use_combo:
        combo_result = _lookup_combo_tier(
            tokens.prompt_tokens,
            tokens.completion_tokens,
            request_count,
            cfg.combo_tiers,
        )
        if combo_result:
            input_price, output_price, cache_read_price = combo_result
            # 普通输入（扣除 cache_read 和 cache_creation 后的非缓存 token）
            if tokens.regular_input_tokens > 0:
                breakdown["input"] = _per_1m(tokens.regular_input_tokens, input_price)
            # 缓存命中：独立计费，不替代输入费
            if cache_read_price is not None and tokens.cache_read_tokens > 0:
                breakdown["cache_read"] = _per_1m(tokens.cache_read_tokens, cache_read_price)
            breakdown["output"] = _per_1m(tokens.completion_tokens, output_price)
        else:
            # combo_tiers 未匹配，回退到 input_tiers + output_tiers
            use_combo = False

    if not use_combo:
        # ── 输入 token ─────────────────────────────────────────────────────────
        # 缓存命中：独立计费，不替代输入费
        if cfg.cache_read_tiers and tokens.cache_read_tokens > 0:
            cache_read_price = _lookup_tier(
                tokens.cache_read_tokens, request_count, cfg.cache_read_tiers
            )
            breakdown["cache_read"] = _per_1m(tokens.cache_read_tokens, cache_read_price)
        # 普通输入（扣除 cache_read 和 cache_creation 后的非缓存 token）
        if tokens.regular_input_tokens > 0:
            input_price = _lookup_tier(tokens.prompt_tokens, request_count, cfg.input_tiers)
            breakdown["input"] = _per_1m(tokens.regular_input_tokens, input_price)

    # ── 缓存写入（阶梯 cache_write_tiers > 5min/1h > cache_write_per_1m）──
    # 缓存写入：阶梯优先（Claude Opus/Sonnet 4.6），否则 5min/1h 或 cache_write_per_1m
    if cfg.cache_write_tiers and tokens.total_cache_creation > 0:
        cache_write_price = _lookup_tier(
            tokens.total_cache_creation, request_count, cfg.cache_write_tiers
        )
        breakdown["cache_write"] = _per_1m(
            tokens.total_cache_creation, cache_write_price
        )
    else:
        if cfg.cache_write_5min_per_1m is not None and tokens.cache_creation_5m_tokens > 0:
            breakdown["cache_write_5min"] = _per_1m(
                tokens.cache_creation_5m_tokens, cfg.cache_write_5min_per_1m
            )
        if cfg.cache_write_1h_per_1m is not None and tokens.cache_creation_1h_tokens > 0:
            breakdown["cache_write_1h"] = _per_1m(
                tokens.cache_creation_1h_tokens, cfg.cache_write_1h_per_1m
            )
        if cfg.cache_write_per_1m is not None:
            general_cache_write = tokens.cache_creation_tokens
            if cfg.cache_write_5min_per_1m is None and cfg.cache_write_1h_per_1m is None:
                general_cache_write = tokens.total_cache_creation
            if general_cache_write > 0:
                breakdown["cache_write"] = _per_1m(
                    general_cache_write, cfg.cache_write_per_1m
                )

    # ── 缓存存储（Gemini：仅当有缓存写入且 metadata 表明需计费时；cost = tokens/1M * price * hours）────
    if tokens.cache_storage_applies and tokens.total_cache_creation > 0:
        if cfg.cache_storage_per_1m_per_hour is not None:
            price = cfg.cache_storage_per_1m_per_hour * tokens.cache_storage_hours
            breakdown["cache_storage"] = _per_1m(tokens.total_cache_creation, price)
        elif cfg.cache_storage_per_1m is not None:
            breakdown["cache_storage"] = _per_1m(
                tokens.total_cache_creation, cfg.cache_storage_per_1m
            )

    # ── 输出 token（区分 thinking / non-thinking）──────────────────────────
    # combo_tiers 模式下 output 已在前面计算，非 combo_tiers 模式按原有逻辑计算
    if not use_combo:
        output_price = _lookup_tier(tokens.completion_tokens, request_count, cfg.output_tiers)
        if cfg.thinking_tiers is not None and tokens.thinking_tokens > 0:
            # thinking token 按 thinking_tiers 单独计费
            thinking_price = _lookup_tier(
                tokens.thinking_tokens, request_count, cfg.thinking_tiers
            )
            breakdown["thinking"] = _per_1m(tokens.thinking_tokens, thinking_price)
            breakdown["output"] = _per_1m(tokens.regular_output_tokens, output_price)
        else:
            # thinking token 与 output token 同价（或模型根本没有 thinking）
            breakdown["output"] = _per_1m(tokens.completion_tokens, output_price)

    # ── Batch 折扣────────────────────────────────────────────────────────
    # 方式1：pricing config 中的固定折扣
    if cfg.batch_discount is not None:
        breakdown = {k: v * cfg.batch_discount for k, v in breakdown.items()}
    # 方式2：数据库中 metadata.batch_models 不为 NULL 时为 batch，折扣 0.5
    if tokens.is_batch:
        breakdown = {k: v * 0.5 for k, v in breakdown.items()}

    total = sum(breakdown.values())
    return total, breakdown
