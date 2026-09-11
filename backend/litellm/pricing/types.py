"""
定价配置类型定义（Pydantic），序列化后存入 model_prices.pricing_config。

支持 6 种定价类型（覆盖计费规则表中的所有场景）：
  standard      —— 固定单价（input/output/cache_read）
  with_cache    —— 带缓存读/写单价（Anthropic Claude、OpenAI cached input）
  with_thinking —— 带思考/推理 token（Claude 3.7 thinking、OpenAI o 系列、DeepSeek R1）
  combined      —— 缓存 + 思考组合（Claude 3.7 Sonnet thinking with cache）
  tiered        —— 简单阶梯（input/output 使用相同阶梯门槛，旧版兼容）
  advanced      —— 高级定价（覆盖所有复杂场景：双阶梯、思考分离、5min/1h 缓存、显/隐缓存等）

推荐新模型一律使用 advanced 类型。
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class ModelFamily(str, Enum):
    """决定如何从 LiteLLM_SpendLogs.metadata 中解析各类扩展 token"""
    OPENAI = "openai"        # prompt_tokens_details.cached_tokens / completion_tokens_details.reasoning_tokens
    ANTHROPIC = "anthropic"  # cache_creation_input_tokens / cache_read_input_tokens / thinking_tokens
    GOOGLE = "google"        # Gemini，格式与 OpenAI 相近
    DEEPSEEK = "deepseek"    # completion_tokens_details.reasoning_tokens
    GENERIC = "generic"      # 仅使用 prompt_tokens / completion_tokens


class ExtractedTokens(BaseModel):
    """从 SpendLog 提取出的各类 token 数量（用于计费计算）"""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # 缓存写入（Anthropic Claude 区分时长；其他模型用 cache_creation_tokens）
    cache_creation_tokens: int = 0      # 通用缓存写入（OpenAI 等）
    cache_creation_5m_tokens: int = 0   # Claude 5分钟缓存写入
    cache_creation_1h_tokens: int = 0   # Claude 1小时缓存写入

    # 缓存读取
    cache_read_tokens: int = 0          # OpenAI cached input / Claude cache read

    # 思考/推理 token（Claude thinking、OpenAI o系列 reasoning、Gemini reasoning）
    thinking_tokens: int = 0

    # 缓存储存时长（小时，用于 Gemini 按小时计费）；仅当 metadata 有 cache_storage_hours 时才有值
    cache_storage_hours: float = 0.0

    # 是否计收缓存储存费：仅当有缓存写入且 metadata 提供 cache_storage_hours > 0 时为 True，metadata 无则不计费
    cache_storage_applies: bool = False

    # 是否为 Batch 模式（Qwen 等模型有 batch 折扣 0.5）
    is_batch: bool = False

    # 总缓存写入（含 5m + 1h）
    @property
    def total_cache_creation(self) -> int:
        return self.cache_creation_tokens + self.cache_creation_5m_tokens + self.cache_creation_1h_tokens

    @property
    def regular_input_tokens(self) -> int:
        """纯普通输入 token = 总输入 - 所有缓存写入 - 缓存读取"""
        return max(0, self.prompt_tokens - self.total_cache_creation - self.cache_read_tokens)

    @property
    def regular_output_tokens(self) -> int:
        """纯普通输出 token = 总输出 - 思考 token"""
        return max(0, self.completion_tokens - self.thinking_tokens)


# ---------------------------------------------------------------------------
# 阶梯条目（用于 AdvancedPricing）
# ---------------------------------------------------------------------------

class TierEntry(BaseModel):
    """
    单档阶梯定价条目。
    up_to_k: 该档上限（单位 K tokens，即千 token），None 表示无上限（最后一档）。
    price_per_1m: 该档价格（每 1M tokens，单位由 AdvancedPricing.currency 决定）。

    示例（deepseek-v3.2 输入阶梯，单位元/1M）：
      [TierEntry(up_to_k=32, price_per_1m=2), TierEntry(up_to_k=128, price_per_1m=4)]
    注意：每个请求的 token 量决定使用哪一档，不是累计计算。
    """
    up_to_k: float | None = Field(description="阶梯上限（K tokens），None = 无上限")
    price_per_1m: float = Field(ge=0)


class ComboTierEntry(BaseModel):
    """
    组合阶梯定价条目：input 区间 × output 区间 = 对应价格。

    适用于「input 在某档位时，output 需要再细分」的特殊定价逻辑（如豆包 Seed 1.6）。
    计算时会优先匹配 combo_tiers，找不到匹配则回退到 input_tiers + output_tiers。

    示例（doubao-seed-1.6）：
      [
        # Input (0, 32K] + Output (0, 0.2K]
        ComboTierEntry(input_up_to_k=32, output_up_to_k=0.2, input_price_per_1m=0.8, output_price_per_1m=2, cache_read_price_per_1m=0.16),
        # Input (0, 32K] + Output (0.2K, ∞)
        ComboTierEntry(input_up_to_k=32, output_up_to_k=None, input_price_per_1m=0.8, output_price_per_1m=8),
        # Input (32K, 128K] + 任意 Output
        ComboTierEntry(input_up_to_k=128, output_up_to_k=None, input_price_per_1m=1.2, output_price_per_1m=16),
        # ...
      ]
    """
    input_up_to_k: float | None = Field(description="input 阶梯上限（K tokens），None = 无上限")
    output_up_to_k: float | None = Field(description="output 阶梯上限（K tokens），None = 无上限")
    input_price_per_1m: float = Field(ge=0, description="input 单价（每 1M tokens）")
    output_price_per_1m: float = Field(ge=0, description="output 单价（每 1M tokens）")
    cache_read_price_per_1m: float | None = Field(
        default=None,
        description="缓存读取单价（每 1M tokens），None 表示不单独计费",
    )


# ---------------------------------------------------------------------------
# 原有简单定价类型（保留向后兼容）
# ---------------------------------------------------------------------------

class StandardPricing(BaseModel):
    """固定 input/output 单价，适用于大多数模型。单位：USD/1M tokens"""
    type: Literal["standard"] = "standard"
    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    cache_read_per_1m: float | None = None
    currency: str = Field(default="USD", description="计费货币：USD（美元）或 元（人民币）")


class WithCachePricing(BaseModel):
    """带缓存读/写单价（Anthropic Claude、OpenAI cached input）"""
    type: Literal["with_cache"] = "with_cache"
    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    cache_write_per_1m: float = Field(ge=0, default=0, description="未配置时默认为 0（如 OpenAI 仅收 cache_read）")
    cache_read_per_1m: float = Field(ge=0)
    currency: str = Field(default="USD", description="计费货币：USD（美元）或 元（人民币）")


class WithThinkingPricing(BaseModel):
    """带思考 token（Claude 3.7 thinking、OpenAI o 系列、DeepSeek R1）"""
    type: Literal["with_thinking"] = "with_thinking"
    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    thinking_per_1m: float | None = None
    cache_read_per_1m: float | None = None
    currency: str = Field(default="USD", description="计费货币：USD（美元）或 元（人民币）")


class CombinedPricing(BaseModel):
    """缓存 + 思考组合（如 Claude 3.7 Sonnet thinking with cache）"""
    type: Literal["combined"] = "combined"
    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    cache_write_per_1m: float | None = None
    cache_read_per_1m: float | None = None
    thinking_per_1m: float | None = None
    currency: str = Field(default="USD", description="计费货币：USD（美元）或 元（人民币）")


class PricingTier(BaseModel):
    up_to_tokens: int | None = None
    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)


class TieredPricing(BaseModel):
    """简单阶梯（input/output 使用相同门槛，旧版兼容）"""
    type: Literal["tiered"] = "tiered"
    tiers: list[PricingTier] = Field(min_length=1)


# ---------------------------------------------------------------------------
# 高级定价类型（覆盖计费规则表中的全部场景）
# ---------------------------------------------------------------------------

class AdvancedPricing(BaseModel):
    """
    高级定价配置，覆盖所有复杂计费场景。推荐所有新模型使用此类型。

    ── 阶梯定价规则 ──
    input_tiers / output_tiers / cache_read_tiers 均为独立阶梯。
    阶梯按每个请求的 token 量（K tokens）确定档位，不累计。
    聚合查询时取平均值近似（request_count 参与计算）。

    ── 缓存写入规则 ──
    普通模型：cache_write_per_1m（单一价格）
    Claude（区分时长）：cache_write_5min_per_1m + cache_write_1h_per_1m
    qwen3（区分显/隐）：cache_write_explicit_per_1m + cache_write_implicit_per_1m
      → LiteLLM 目前未区分显/隐，默认使用 cache_write_per_1m 作为兜底

    ── 思考/非思考规则 ──
    thinking_tiers 有值 → thinking token 单独计费
    thinking_tiers 为 None → thinking token 按 output_tiers 价格计费（已含在 completion_tokens 中）

    ── 货币单位 ──
    currency: "CNY"（元/1M）或 "USD"（美元/1M）
    """
    type: Literal["advanced"] = "advanced"

    # ── 输入 token 阶梯（按 prompt_tokens/1000 选档）────────────────────────
    input_tiers: list[TierEntry] = Field(
        min_length=1,
        description="输入 token 阶梯，最后一档 up_to_k 必须为 None",
    )

    # ── 输出 token 阶梯（非思考部分，或全部输出）────────────────────────────
    output_tiers: list[TierEntry] = Field(
        min_length=1,
        description="输出 token 阶梯（non-thinking）",
    )

    # ── 组合阶梯（input × output 组合定价）──────────────────────────────────
    combo_tiers: list[ComboTierEntry] | None = Field(
        default=None,
        description="组合阶梯：input 区间 × output 区间 = 对应价格。优先匹配 combo_tiers，找不到则回退到 input_tiers + output_tiers",
    )

    # ── 思考 token 阶梯（None = 按 output_tiers 计费）──────────────────────
    thinking_tiers: list[TierEntry] | None = Field(
        default=None,
        description="思考 token 阶梯，None 表示与 output_tiers 相同价格",
    )

    # ── 缓存读取阶梯（按 cache_read_tokens/1000 选档）──────────────────────
    cache_read_tiers: list[TierEntry] | None = Field(
        default=None,
        description="缓存读取 token 阶梯，None 表示不对缓存读取计费",
    )

    # ── 缓存写入阶梯（如 Claude Opus 4.6 / Sonnet 4.6 按 200K 分档）──────
    cache_write_tiers: list[TierEntry] | None = Field(
        default=None,
        description="缓存写入 token 阶梯，None 时用 cache_write_per_1m 或 5min/1h",
    )

    # ── 缓存写入价格（三种变体，按需选用）─────────────────────────────────
    cache_write_per_1m: float | None = Field(
        default=None,
        description="通用缓存写入单价（DeepSeek/Kimi/doubao 等使用）",
    )
    cache_write_5min_per_1m: float | None = Field(
        default=None,
        description="Claude 5分钟缓存写入单价",
    )
    cache_write_1h_per_1m: float | None = Field(
        default=None,
        description="Claude 1小时缓存写入单价",
    )
    cache_write_explicit_per_1m: float | None = Field(
        default=None,
        description="qwen3-max 显式写缓存单价（LiteLLM 暂不区分，作为备用）",
    )
    cache_write_implicit_per_1m: float | None = Field(
        default=None,
        description="qwen3-max 隐式写缓存单价（LiteLLM 暂不区分，作为备用）",
    )

    # ── 缓存存储费（Gemini 特有，按小时）────────────────────────────────────
    cache_storage_per_1m: float | None = Field(
        default=None,
        description="缓存存储费（旧，按 1 小时计），向后兼容",
    )
    cache_storage_per_1m_per_hour: float | None = Field(
        default=None,
        description="缓存存储费（Gemini），每 1M tokens 每小时。有值则用 cache_storage_hours 参与计算",
    )

    # ── Batch 折扣──────────────────────────────────────────────────────────
    batch_discount: float | None = Field(
        default=None,
        description="Batch 模式折扣系数（如 0.5 = 半价），None 表示无折扣",
    )

    # ── 货币单位────────────────────────────────────────────────────────────
    currency: str = Field(
        default="USD",
        description="计费货币：USD（美元/1M）或 CNY（元/1M）",
    )


# ---------------------------------------------------------------------------
# 联合类型（discriminator = type 字段）
# ---------------------------------------------------------------------------
PricingConfig = Annotated[
    Union[
        StandardPricing,
        WithCachePricing,
        WithThinkingPricing,
        CombinedPricing,
        TieredPricing,
        AdvancedPricing,
    ],
    Field(discriminator="type"),
]
