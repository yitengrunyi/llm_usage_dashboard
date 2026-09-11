from __future__ import annotations

import uuid
from datetime import date as _Date
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class HealthResponse(BaseModel):
    ok: bool = True
    app: str
    env: str


class TenantCreateIn(BaseModel):
    name: str


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class UsageIn(BaseModel):
    tenant_id: uuid.UUID
    request_id: str | None = None
    model: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_total: int | None = None
    meta_json: str | None = None
    occurred_at: datetime | None = None


class UsageOut(BaseModel):
    id: uuid.UUID


class BillingSummaryOut(BaseModel):
    tenant_id: uuid.UUID
    period_start: datetime
    period_end: datetime
    tokens_total: int
    price_per_1k_tokens: float
    amount: float = Field(description="tokens_total/1000 * price_per_1k_tokens")


# ---------------------------------------------------------------------------
# 模型单价 CRUD
# ---------------------------------------------------------------------------

class ModelPriceIn(BaseModel):
    model_name: str = Field(description="规范模型名称，如 gpt-4.1、deepseek-r1、claude-sonnet-4-6")
    model_group: str | None = Field(
        default=None,
        description="原始 model_group（来自 LiteLLM SpendLogs，如 openai-gpt-4.1、road-claude-sonnet-4-6），用于追溯定价来源",
    )
    provider: str | None = Field(
        default=None,
        description="供应商（如 openai / road / volcengine）。空表示该模型默认单价；同模型不同供应商可配置不同单价",
    )
    model_family: str = Field(
        default="generic",
        description="模型厂商族：openai / anthropic / google / deepseek / generic",
    )
    pricing_config: dict = Field(
        description=(
            "定价配置 JSON，type 字段决定类型。推荐所有新模型使用 advanced 类型。\n\n"
            "【advanced - 高级定价（覆盖所有场景）】\n"
            "  deepseek-r1（固定单价，单位元/1M）：\n"
            "    {\"type\":\"advanced\",\"currency\":\"CNY\",\n"
            "     \"input_tiers\":[{\"up_to_k\":null,\"price_per_1m\":4}],\n"
            "     \"output_tiers\":[{\"up_to_k\":null,\"price_per_1m\":16}],\n"
            "     \"cache_read_tiers\":[{\"up_to_k\":null,\"price_per_1m\":0.8}]}\n\n"
            "【旧版类型（向后兼容）】\n"
            "  标准：     {\"type\":\"standard\",\"input_per_1m\":2.5,\"output_per_1m\":10.0}\n"
            "  含缓存：   {\"type\":\"with_cache\",\"input_per_1m\":3.0,\"output_per_1m\":15.0,"
            "\"cache_write_per_1m\":3.75,\"cache_read_per_1m\":0.30}\n"
            "  含思考：   {\"type\":\"with_thinking\",\"input_per_1m\":3.0,\"output_per_1m\":15.0,"
            "\"thinking_per_1m\":15.0}"
        ),
    )
    currency: str = Field(default="USD")
    is_active: bool = Field(default=True)
    aliases: list[str] | None = Field(
        default=None,
        description="模型名别名列表，如 [\"kimi-k2-0905\", \"ep-20250530135709-c6bk7\"]，查价时自动匹配",
    )
    note: str | None = None


class ModelPriceOut(BaseModel):
    id: uuid.UUID
    model_name: str
    model_group: str | None = None
    provider: str | None = None
    model_family: str
    pricing_config: dict
    currency: str
    is_active: bool
    aliases: list[str] | None = None
    note: str | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# 消费分析（核心业务）
# ---------------------------------------------------------------------------

class SpendQueryIn(BaseModel):
    # ── 时间范围（必填）──────────────────────────────────────────────────────
    start: datetime = Field(description="开始时间，ISO8601，如 2026-02-01T00:00:00Z")
    end: datetime = Field(description="结束时间，ISO8601，如 2026-02-28T23:59:59Z")

    # ── 过滤条件（可选，不填则不过滤）────────────────────────────────────────
    model: str | None = Field(default=None, description="按模型名称精确过滤，如 gpt-4.1")
    provider: str | None = Field(
        default=None,
        description="按供应商过滤（从 model_group/model 前缀推导），如 openai / volcengine / openrouter / moonshot",
    )
    user: str | None = Field(default=None, description="按 LiteLLM user 字段过滤")
    team_id: str | None = Field(default=None, description="按 LiteLLM team_id 字段过滤")

    # ── 分组维度（可选，默认只按模型）────────────────────────────────────────
    group_by: list[Literal["model", "provider", "date", "api_key"]] = Field(
        default=["model"],
        description=(
            "结果分组维度。默认 [\"model\"]：只按模型名聚合（带前缀的如 openai/gpt-4.1 与 gpt-4.1 合并为同一模型），不考虑供应商。\n"
            "  model    → 按规范模型名分组（去掉 model 的 '/' 前前缀）\n"
            "  provider → 按供应商分组\n"
            "  api_key  → 按 API Key 分组\n"
            "  date     → 按日期分组"
        ),
    )
    date_granularity: Literal["day", "month"] = Field(
        default="day",
        description="date 分组的时间粒度，day=按天，month=按月。group_by 不含 date 时忽略此字段",
    )

    @model_validator(mode="after")
    def check_time_range(self) -> "SpendQueryIn":
        if self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


class SpendRecord(BaseModel):
    """单条聚合结果：仅返回输入/输出/缓存 token 与花费，时间按日期"""
    model: str | None = Field(default=None, description="模型（group_by 含 model 时有值）")
    provider: str | None = Field(default=None, description="供应商（group_by 含 provider 时有值）")
    api_key: str | None = Field(default=None, description="API Key（group_by 含 api_key 时有值）")
    date: _Date | None = Field(default=None, description="日期（group_by 含 date 时有值，按日期选择时间）")

    request_count: int = Field(default=0, description="该分组下的请求数量")
    input_tokens: int = Field(description="输入 token 总消耗")
    output_tokens: int = Field(description="输出 token 总消耗")
    cache_tokens: int = Field(description="缓存 token 总消耗（读取+写入）")
    cost: float = Field(description="该条算出的花费（原始币种）")
    currency: str = Field(default="USD", description="原始货币单位")
    cost_usd: float = Field(default=0.0, description="该条花费（美元），根据实时汇率转换")
    cost_cny: float = Field(default=0.0, description="该条花费（人民币），根据实时汇率转换")


class SpendSummaryOut(BaseModel):
    """消费汇总：仅返回输入/输出/缓存 token 合计与总花费，时间按日期"""
    period_start: datetime
    period_end: datetime
    group_by: list[str] = Field(description="分组维度")

    total_request_count: int = Field(description="本次查询命中的请求总数")
    total_input_tokens: int = Field(description="输入 token 总消耗")
    total_output_tokens: int = Field(description="输出 token 总消耗")
    total_cache_tokens: int = Field(description="缓存 token 总消耗")
    total_cost: float = Field(description="总花费（原始币种）")
    currency: str = Field(default="USD", description="原始货币单位")

    # 双币种总金额（实时汇率自动转换）
    total_cost_usd: float = Field(default=0.0, description="总花费（美元），根据实时汇率自动转换")
    total_cost_cny: float = Field(default=0.0, description="总花费（人民币），根据实时汇率自动转换")
    exchange_rate: float = Field(default=0.0, description="本次使用的汇率（1 USD = ? CNY）")

    records: list[SpendRecord]


# ---------------------------------------------------------------------------
# 单条件查询：按「供应商+模型」或「api_key」各一个接口，只返回该情况下的汇总
# ---------------------------------------------------------------------------

class SpendByModelProviderIn(BaseModel):
    """按供应商+模型查询：只返回该组合在时间范围内的汇总"""
    start: datetime = Field(description="开始时间")
    end: datetime = Field(description="结束时间")
    model: str = Field(description="模型名称，如 gpt-4.1 或 openai/gpt-4.1")
    provider: str = Field(description="供应商，如 openai / road / volcengine")

    @model_validator(mode="after")
    def check_time_range(self) -> "SpendByModelProviderIn":
        if self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


class SpendByProviderSummaryIn(BaseModel):
    """按供应商汇总：返回各供应商在时间范围内的 token 与花费汇总"""
    start: datetime = Field(description="开始时间")
    end: datetime = Field(description="结束时间")
    user: str | None = Field(default=None, description="按 LiteLLM user 字段过滤")
    team_id: str | None = Field(default=None, description="按 LiteLLM team_id 字段过滤")

    @model_validator(mode="after")
    def check_time_range(self) -> "SpendByProviderSummaryIn":
        if self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


class SpendByApiKeyIn(BaseModel):
    """按 api_key 查询：只返回该 key 在时间范围内的汇总"""
    start: datetime = Field(description="开始时间")
    end: datetime = Field(description="结束时间")
    api_key: str = Field(description="LiteLLM 记录的 api_key")

    @model_validator(mode="after")
    def check_time_range(self) -> "SpendByApiKeyIn":
        if self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


class ApiKeyOption(BaseModel):
    """API key 下拉选项 — /analytics/api-keys 返回."""
    token: str = Field(description="key 全值, 前端选中后透传给 by-api-key 查询")
    label: str = Field(description="显示用 — alias 优先, 否则 sk-...末6位")
    alias: str | None = None
    spend: float = 0
    max_budget: float | None = None
    expires: str | None = None
    blocked: bool = False


class SpendSingleOut(BaseModel):
    """单条件查询的返回：输入/输出/缓存 token、总花费与请求数"""
    period_start: datetime
    period_end: datetime
    request_count: int = Field(default=0, description="符合当前过滤条件的请求总数")
    input_tokens: int = Field(description="输入 token 总消耗")
    output_tokens: int = Field(description="输出 token 总消耗")
    cache_tokens: int = Field(description="缓存 token 总消耗")
    cost: float = Field(description="总花费（原始币种）")
    currency: str = Field(default="USD", description="原始货币单位")
    cost_usd: float = Field(default=0.0, description="总花费（美元），根据实时汇率转换")
    cost_cny: float = Field(default=0.0, description="总花费（人民币），根据实时汇率转换")
    # 以下为本次查询条件，便于前端区分
    model: str | None = Field(default=None, description="模型（按 model+provider 查询时有值）")
    provider: str | None = Field(default=None, description="供应商（按 model+provider 查询时有值）")
    api_key: str | None = Field(default=None, description="api_key（按 api_key 查询时有值，可脱敏展示）")


# ---------------------------------------------------------------------------
# 下拉选项（模型 / 供应商列表）
# ---------------------------------------------------------------------------

class SpendOptionsOut(BaseModel):
    models: list[str] = Field(description="可选的模型列表")
    providers: list[str] = Field(description="可选的供应商列表")


class UnpricedModelItem(BaseModel):
    model: str = Field(description="归一化后的模型名")
    model_group: str | None = Field(default=None, description="原始 model_group（用于创建定价条目时作为 model_name）")
    provider: str = Field(description="供应商")
    request_count: int = Field(description="请求次数")


class UnpricedModelsOut(BaseModel):
    total: int = Field(description="无定价的模型+供应商组合数")
    total_requests: int = Field(description="无定价的总请求数")
    items: list[UnpricedModelItem]
