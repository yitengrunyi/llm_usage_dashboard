"""
SpendLog Repository：从 LiteLLM_SpendLogs 查询并聚合 token 用量。

设计原则：一条 SQL 完成所有聚合，通过 JSONB 操作符直接在 SQL 层提取扩展 token：
  - 缓存读取  : metadata -> additional_usage_values -> prompt_tokens_details -> cached_tokens
  - 思考 token: metadata -> additional_usage_values -> completion_tokens_details -> reasoning_tokens
  - Claude 缓存写入: claude_cache_creation_1_h_tokens + claude_cache_creation_5_m_tokens
  - 缓存储存计费: 仅当 cache_creation > 0 且 metadata 中有 cache_storage_hours 且 > 0 时计收，否则不计费

供应商（supplier）解析规则（优先看 group 开头）：
  - 先看 model_group 开头：若以已知供应商/渠道前缀开头（openai-、road-、volcengine- 等）→ 用该前缀
  - 若 model_group 是直接模型名开头（如 gpt-4.1、kimi-k2.5）→ 再按国内模型名推断供应商
  - 国外模型无前缀 → 默认 blueshirt
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from sqlalchemy import Float, Integer, case, cast, func, literal, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.models import LiteLLMSpendLog

# model_group 中作为前缀的供应商/渠道名（仅当 model_group 以此开头时才取首段，避免 gpt-4.1 → gpt）
_SUPPLIER_PREFIXES = (
    "openai-",
    "volcengine-",
    "blueshirt-",
    "openrouter-",
    "nulls-",
    "road-",
    "azure-",
    "moonshot-",
    "google-",
    "moonshotai-",
    "x-ai-",  # x-ai (Grok)
)


_DOMESTIC_MODEL_PROVIDER_MAP: tuple[tuple[str, str], ...] = (
    ("kimi", "moonshot"),
    ("deepseek", "deepseek"),
    ("qwen", "ali"),
    ("glm", "zhipu"),
    ("doubao", "volcengine"),
)


def _effective_supplier():
    """
    解析供应商（先判 group 开头，再判 model / provider）：
      1. model_group 以已知前缀（openai-、road-、volcengine- 等）开头 → 用该前缀
      2. model_group 以国内模型名开头（deepseek、glm、kimi 等）→ 推断供应商
      3. 按模型名推断国内供应商（kimi→moonshot, deepseek→deepseek, qwen→ali 等）
      4. 其他（国外模型无前缀）→ blueshirt
    """
    group_col = LiteLLMSpendLog.model_group
    group_cases = [
        ((group_col.isnot(None)) & (group_col.startswith(prefix)), literal(prefix.rstrip("-")))
        for prefix in _SUPPLIER_PREFIXES
    ]
    part_from_group = case(*group_cases, else_=None) if group_cases else None

    # 国内模型名推断供应商 —— 从 model_group 检查
    domestic_group_cases = [
        ((group_col.isnot(None)) & (func.lower(group_col).startswith(model_prefix)), literal(provider))
        for model_prefix, provider in _DOMESTIC_MODEL_PROVIDER_MAP
    ]
    part_from_domestic_group = case(*domestic_group_cases, else_=None) if domestic_group_cases else None

    # 国内模型名推断供应商 —— 从 model 检查
    domestic_cases = [
        (func.lower(LiteLLMSpendLog.model).startswith(model_prefix), literal(provider))
        for model_prefix, provider in _DOMESTIC_MODEL_PROVIDER_MAP
    ]
    part_from_domestic = case(*domestic_cases, else_=None) if domestic_cases else None

    # 优先 group 前缀，再 group 国内推断，再 model 国内推断，最后默认 blueshirt
    return func.coalesce(
        part_from_group,
        part_from_domestic_group,
        part_from_domestic,
        literal("blueshirt"),
    )


@dataclass
class SpendAggRow:
    """单条聚合结果，未参与分组的维度值为 None"""
    model: str | None
    provider: str | None
    date: date | None
    request_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_hit_count: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cache_creation_5m_tokens: int
    cache_creation_1h_tokens: int
    thinking_tokens: int
    litellm_spend: float = 0.0


@dataclass
class SpendRawRow:
    """单条请求行，用于阶梯定价时逐条算价再累加"""
    model: str
    model_group: str | None  # 原始 model_group 字段
    custom_llm_provider: str | None  # 原始 custom_llm_provider 字段
    api_key: str | None
    date: date | None
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cache_creation_5m_tokens: int
    cache_creation_1h_tokens: int
    thinking_tokens: int
    cache_storage_hours: float = 0.0
    # 是否计收缓存储存费：仅当有缓存写入且 metadata 提供 cache_storage_hours > 0 时为 True，metadata 无则不计费
    cache_storage_applies: bool = False
    # 是否为 Batch 模式（Qwen 等模型有 batch 折扣 0.5）
    is_batch: bool = False


def _jsonb_int(col, *keys: str):
    """
    从 JSONB 列按路径取值并转为 INTEGER，路径不存在时返回 NULL（由 COALESCE 兜底为 0）。
    等价于 SQL：(col->'key1'->'key2'->>'leafKey')::int
    """
    node = col
    for k in keys[:-1]:
        node = node[k]
    return cast(node[keys[-1]].as_string(), Integer)


def _jsonb_float(col, *keys: str):
    """
    从 JSONB 列按路径取值并转为 FLOAT，路径不存在或非数字时返回 NULL。
    用于缓存储存时长等：仅当 metadata 有该字段且 > 0 时才计收存储费。
    """
    node = col
    for k in keys[:-1]:
        node = node[k]
    return cast(node[keys[-1]].as_string(), Float)


def _jsonb_not_null(col, *keys: str):
    """
    检测 JSONB 路径是否存在且不为 NULL/JSON null（用于 batch_models 等布尔字段）。
    返回表达式：CASE WHEN col->'key1'->>'key2' IS NOT NULL THEN 1 ELSE 0 END
    注意：必须用 ->> 文本操作符（as_string()），因为 -> 返回的 JSONB null 不等于 SQL NULL，
    会导致 IS NOT NULL 对 {"batch_models": null} 这类记录错误地返回 TRUE。
    """
    node = col
    for k in keys[:-1]:
        node = node[k]
    # 使用 ->> 文本操作符：JSON null 会转为 SQL NULL，IS NOT NULL 才能正确区分「键存在且有实际值」
    return case(
        (node[keys[-1]].as_string().isnot(None), literal(1)),
        else_=literal(0),
    )


def _strip_tz(dt: datetime) -> datetime:
    """Strip timezone info for TIMESTAMP WITHOUT TIME ZONE columns."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


async def query_spend(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    group_by: list[Literal["model", "provider", "date"]],
    date_granularity: Literal["day", "month"] = "day",
    model: str | None = None,
    provider: str | None = None,
    user: str | None = None,
    team_id: str | None = None,
) -> list[SpendAggRow]:
    """
    通用聚合查询，时间范围 [start, end)。
    group_by 决定分组维度，支持 model / provider / date 任意组合。
    """
    meta = LiteLLMSpendLog.log_metadata   # JSONB 列（数据库列名 metadata）

    # ── JSONB 扩展 token 表达式 ──────────────────────────────────────────────
    cache_read_expr = func.coalesce(
        func.sum(_jsonb_int(meta, "additional_usage_values", "prompt_tokens_details", "cached_tokens")),
        0,
    ).label("cache_read_tokens")

    thinking_expr = func.coalesce(
        func.sum(_jsonb_int(meta, "additional_usage_values", "completion_tokens_details", "reasoning_tokens")),
        0,
    ).label("thinking_tokens")

    cache_1h_expr = func.coalesce(
        func.sum(_jsonb_int(meta, "additional_usage_values", "claude_cache_creation_1_h_tokens")),
        0,
    )
    cache_5m_expr = func.coalesce(
        func.sum(_jsonb_int(meta, "additional_usage_values", "claude_cache_creation_5_m_tokens")),
        0,
    )
    # cache_creation_tokens = 5m + 1h 合计（向后兼容，也用于非 Claude 模型）
    cache_creation_expr = (cache_1h_expr + cache_5m_expr).label("cache_creation_tokens")
    cache_1h_labeled = cache_1h_expr.label("cache_creation_1h_tokens")
    cache_5m_labeled = cache_5m_expr.label("cache_creation_5m_tokens")
    litellm_spend_expr = func.coalesce(
        func.sum(cast(LiteLLMSpendLog.spend, Float)), 0.0
    ).label("litellm_spend")

    # ── 供应商表达式（用于分组与过滤）────────────────────────────────────────
    supplier_expr = _effective_supplier()

    # ── 动态 SELECT / GROUP BY 列 ────────────────────────────────────────────
    select_cols = []
    group_cols = []

    if "model" in group_by:
        select_cols.append(LiteLLMSpendLog.model.label("model"))
        group_cols.append(LiteLLMSpendLog.model)
    else:
        select_cols.append(literal_column("NULL").label("model"))

    if "provider" in group_by:
        select_cols.append(supplier_expr.label("provider"))
        group_cols.append(supplier_expr)
    else:
        select_cols.append(literal_column("NULL").label("provider"))

    if "date" in group_by:
        trunc_unit = "month" if date_granularity == "month" else "day"
        date_expr = func.date_trunc(trunc_unit, LiteLLMSpendLog.startTime).label("date")
        select_cols.append(date_expr)
        group_cols.append(func.date_trunc(trunc_unit, LiteLLMSpendLog.startTime))
    else:
        select_cols.append(literal_column("NULL").label("date"))

    # ── 完整 SELECT ──────────────────────────────────────────────────────────
    stmt = select(
        *select_cols,
        func.count(LiteLLMSpendLog.request_id).label("request_count"),
        func.coalesce(func.sum(LiteLLMSpendLog.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(LiteLLMSpendLog.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(LiteLLMSpendLog.total_tokens), 0).label("total_tokens"),
        func.count(LiteLLMSpendLog.request_id)
        .filter(LiteLLMSpendLog.cache_hit == "True")
        .label("cache_hit_count"),
        cache_read_expr,
        thinking_expr,
        cache_creation_expr,
        cache_1h_labeled,
        cache_5m_labeled,
        litellm_spend_expr,
    )

    # ── WHERE ────────────────────────────────────────────────────────────────
    stmt = stmt.where(LiteLLMSpendLog.startTime >= _strip_tz(start))
    stmt = stmt.where(LiteLLMSpendLog.startTime < _strip_tz(end))
    if model is not None:
        stmt = stmt.where(LiteLLMSpendLog.model == model)
    if provider is not None:
        stmt = stmt.where(supplier_expr == provider)
    if user is not None:
        stmt = stmt.where(LiteLLMSpendLog.user == user)
    if team_id is not None:
        stmt = stmt.where(LiteLLMSpendLog.team_id == team_id)

    if group_cols:
        stmt = stmt.group_by(*group_cols)

    def _int(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def _float(v) -> float:
        try:
            return float(v or 0.0)
        except (TypeError, ValueError):
            return 0.0

    result = []
    for row in (await db.execute(stmt)).all():
        date_val = None
        if row.date is not None:
            date_val = row.date.date() if hasattr(row.date, "date") else row.date

        result.append(SpendAggRow(
            model=row.model,
            provider=row.provider,
            date=date_val,
            request_count=_int(row.request_count),
            prompt_tokens=_int(row.prompt_tokens),
            completion_tokens=_int(row.completion_tokens),
            total_tokens=_int(row.total_tokens),
            cache_hit_count=_int(row.cache_hit_count),
            cache_read_tokens=_int(row.cache_read_tokens),
            cache_creation_tokens=_int(row.cache_creation_tokens),
            cache_creation_5m_tokens=_int(row.cache_creation_5m_tokens),
            cache_creation_1h_tokens=_int(row.cache_creation_1h_tokens),
            thinking_tokens=_int(row.thinking_tokens),
            litellm_spend=_float(row.litellm_spend),
        ))
    return result


async def query_distinct_models_and_providers(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[list[str], list[tuple[str | None, str | None, str | None]]]:
    """
    返回去重的 (model 列表, 原始字段元组列表)。
    原始字段元组: (model, model_group, custom_llm_provider)，供应用层计算供应商。
    可选 start/end 限制时间范围（不传则查全表）。
    """
    stmt = select(
        LiteLLMSpendLog.model,
        LiteLLMSpendLog.model_group,
        LiteLLMSpendLog.custom_llm_provider,
    ).distinct()
    if start is not None:
        stmt = stmt.where(LiteLLMSpendLog.startTime >= _strip_tz(start))
    if end is not None:
        stmt = stmt.where(LiteLLMSpendLog.startTime < _strip_tz(end))

    rows = (await db.execute(stmt)).all()
    models = [r.model for r in rows if r.model]
    raw_provider_data = [
        (r.model, r.model_group, r.custom_llm_provider)
        for r in rows
    ]
    return models, raw_provider_data


async def query_spend_raw(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    date_granularity: Literal["day", "month"] = "day",
    model: str | None = None,
    model_groups: list[str] | None = None,
    api_key: str | None = None,
    user: str | None = None,
    team_id: str | None = None,
) -> list[SpendRawRow]:
    """
    按请求返回原始行（不聚合），用于阶梯定价时逐条算价再累加。
    每行带 model、model_group、custom_llm_provider、date 及该请求的 token 字段。
    供应商判断在应用层完成，SQL 层只返回原始字段。
    model_groups: 仅返回 model_group IN (...) 的行（用于分流查询只拉阶梯模型）。
    """
    meta = LiteLLMSpendLog.log_metadata
    trunc_unit = "month" if date_granularity == "month" else "day"
    date_expr = func.date_trunc(trunc_unit, LiteLLMSpendLog.startTime)

    cache_read_one = func.coalesce(
        _jsonb_int(meta, "additional_usage_values", "prompt_tokens_details", "cached_tokens"), 0
    )
    thinking_one = func.coalesce(
        _jsonb_int(meta, "additional_usage_values", "completion_tokens_details", "reasoning_tokens"), 0
    )
    cache_1h_one = func.coalesce(
        _jsonb_int(meta, "additional_usage_values", "claude_cache_creation_1_h_tokens"), 0
    )
    cache_5m_one = func.coalesce(
        _jsonb_int(meta, "additional_usage_values", "claude_cache_creation_5_m_tokens"), 0
    )
    cache_creation_one = cache_1h_one + cache_5m_one
    # 缓存储存时长（小时）：仅从 metadata 读取，无则 0，不猜默认值
    cache_storage_hours_raw = _jsonb_float(meta, "additional_usage_values", "cache_storage_hours")
    cache_storage_hours_expr = func.coalesce(cache_storage_hours_raw, 0.0).label("cache_storage_hours")
    # 是否计收缓存储存费：仅当「有缓存写入」且「metadata 中有存储时长且 > 0」时为 True；metadata 没有则不计费
    cache_storage_applies_expr = case(
        (
            (cache_creation_one > 0)
            & cache_storage_hours_raw.isnot(None)
            & (cache_storage_hours_raw > 0),
            literal(1),
        ),
        else_=literal(0),
    ).label("cache_storage_applies")

    # Batch 模式：metadata.batch_models 不为 NULL 时为 batch
    batch_expr = _jsonb_not_null(meta, "batch_models").label("is_batch")

    stmt = select(
        LiteLLMSpendLog.model.label("model"),
        LiteLLMSpendLog.model_group.label("model_group"),
        LiteLLMSpendLog.custom_llm_provider.label("custom_llm_provider"),
        LiteLLMSpendLog.api_key.label("api_key"),
        date_expr.label("date"),
        LiteLLMSpendLog.prompt_tokens.label("prompt_tokens"),
        LiteLLMSpendLog.completion_tokens.label("completion_tokens"),
        cache_read_one.label("cache_read_tokens"),
        cache_creation_one.label("cache_creation_tokens"),
        cache_5m_one.label("cache_creation_5m_tokens"),
        cache_1h_one.label("cache_creation_1h_tokens"),
        thinking_one.label("thinking_tokens"),
        cache_storage_hours_expr,
        cache_storage_applies_expr,
        batch_expr,
    ).where(
        LiteLLMSpendLog.startTime >= _strip_tz(start),
        LiteLLMSpendLog.startTime < _strip_tz(end),
    )
    if model is not None:
        stmt = stmt.where(LiteLLMSpendLog.model == model)
    if model_groups is not None:
        stmt = stmt.where(LiteLLMSpendLog.model_group.in_(model_groups))
    if api_key is not None:
        stmt = stmt.where(LiteLLMSpendLog.api_key == api_key)
    if user is not None:
        stmt = stmt.where(LiteLLMSpendLog.user == user)
    if team_id is not None:
        stmt = stmt.where(LiteLLMSpendLog.team_id == team_id)

    def _int(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def _float(v, default: float = 1.0) -> float:
        try:
            f = float(v) if v is not None else default
            return f if f > 0 else default
        except (TypeError, ValueError):
            return default

    result = []
    for row in (await db.execute(stmt)).all():
        date_val = None
        if row.date is not None:
            date_val = row.date.date() if hasattr(row.date, "date") else row.date
        result.append(SpendRawRow(
            model=row.model,
            model_group=row.model_group,
            custom_llm_provider=row.custom_llm_provider,
            api_key=row.api_key,
            date=date_val,
            prompt_tokens=_int(row.prompt_tokens),
            completion_tokens=_int(row.completion_tokens),
            cache_read_tokens=_int(row.cache_read_tokens),
            cache_creation_tokens=_int(row.cache_creation_tokens),
            cache_creation_5m_tokens=_int(row.cache_creation_5m_tokens),
            cache_creation_1h_tokens=_int(row.cache_creation_1h_tokens),
            thinking_tokens=_int(row.thinking_tokens),
            cache_storage_hours=_float(getattr(row, "cache_storage_hours", None), 0.0),
            cache_storage_applies=bool(_int(getattr(row, "cache_storage_applies", 0))),
            is_batch=bool(_int(getattr(row, "is_batch", 0))),
        ))
    return result


# ---------------------------------------------------------------------------
# 聚合查询（简单定价模型）：SQL 层 GROUP BY，不返回逐行数据
# ---------------------------------------------------------------------------

@dataclass
class SpendAggGroupRow:
    """SQL 层聚合后的分组行，用于简单定价模型的费用计算。"""
    model: str
    model_group: str | None
    custom_llm_provider: str | None
    api_key: str | None
    date: date | None
    request_count: int
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cache_creation_5m_tokens: int
    cache_creation_1h_tokens: int
    thinking_tokens: int


async def query_spend_grouped(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    model_groups: list[str],
    date_granularity: Literal["day", "month"] = "day",
    user: str | None = None,
    team_id: str | None = None,
) -> list[SpendAggGroupRow]:
    """
    按 (model, model_group, custom_llm_provider, date) 聚合的 SQL 查询。
    仅查询 model_groups 列表中的 model_group，用于简单定价模型的批量聚合。
    返回 SUM 后的 token 数值，应用层直接用 SUM × 单价 算费。
    """
    if not model_groups:
        return []

    meta = LiteLLMSpendLog.log_metadata
    trunc_unit = "month" if date_granularity == "month" else "day"
    date_expr = func.date_trunc(trunc_unit, LiteLLMSpendLog.startTime)

    stmt = select(
        LiteLLMSpendLog.model.label("model"),
        LiteLLMSpendLog.model_group.label("model_group"),
        LiteLLMSpendLog.custom_llm_provider.label("custom_llm_provider"),
        date_expr.label("date"),
        func.count(LiteLLMSpendLog.request_id).label("request_count"),
        func.coalesce(func.sum(LiteLLMSpendLog.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(LiteLLMSpendLog.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(
            _jsonb_int(meta, "additional_usage_values", "prompt_tokens_details", "cached_tokens")
        ), 0).label("cache_read_tokens"),
        func.coalesce(func.sum(
            _jsonb_int(meta, "additional_usage_values", "claude_cache_creation_1_h_tokens")
        ), 0).label("cache_creation_1h_tokens"),
        func.coalesce(func.sum(
            _jsonb_int(meta, "additional_usage_values", "claude_cache_creation_5_m_tokens")
        ), 0).label("cache_creation_5m_tokens"),
        func.coalesce(func.sum(
            _jsonb_int(meta, "additional_usage_values", "completion_tokens_details", "reasoning_tokens")
        ), 0).label("thinking_tokens"),
    ).where(
        LiteLLMSpendLog.startTime >= _strip_tz(start),
        LiteLLMSpendLog.startTime < _strip_tz(end),
        LiteLLMSpendLog.model_group.in_(model_groups),
    ).group_by(
        LiteLLMSpendLog.model,
        LiteLLMSpendLog.model_group,
        LiteLLMSpendLog.custom_llm_provider,
        date_expr,
    )
    if user is not None:
        stmt = stmt.where(LiteLLMSpendLog.user == user)
    if team_id is not None:
        stmt = stmt.where(LiteLLMSpendLog.team_id == team_id)

    def _int(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    result = []
    for row in (await db.execute(stmt)).all():
        date_val = None
        if row.date is not None:
            date_val = row.date.date() if hasattr(row.date, "date") else row.date
        result.append(SpendAggGroupRow(
            model=row.model,
            model_group=row.model_group,
            custom_llm_provider=row.custom_llm_provider,
            api_key=None,
            date=date_val,
            request_count=_int(row.request_count),
            prompt_tokens=_int(row.prompt_tokens),
            completion_tokens=_int(row.completion_tokens),
            cache_read_tokens=_int(row.cache_read_tokens),
            cache_creation_tokens=_int(row.cache_creation_1h_tokens) + _int(row.cache_creation_5m_tokens),
            cache_creation_5m_tokens=_int(row.cache_creation_5m_tokens),
            cache_creation_1h_tokens=_int(row.cache_creation_1h_tokens),
            thinking_tokens=_int(row.thinking_tokens),
        ))
    return result


async def query_distinct_model_groups(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
) -> list[str]:
    """
    返回时间范围内出现过的所有 model_group 值（去重）。
    仅读普通列，不碰 JSONB，非常快。
    """
    stmt = (
        select(LiteLLMSpendLog.model_group)
        .where(
            LiteLLMSpendLog.startTime >= _strip_tz(start),
            LiteLLMSpendLog.startTime < _strip_tz(end),
        )
        .distinct()
    )
    rows = (await db.execute(stmt)).all()
    return [r.model_group for r in rows if r.model_group]


async def query_model_group_counts(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
) -> list[tuple[str | None, str | None, str | None, int]]:
    """
    返回时间范围内 (model, model_group, custom_llm_provider, count) 的去重分组统计。
    不碰 JSONB，用于 unpriced-models 等只需要统计的场景。
    """
    stmt = (
        select(
            LiteLLMSpendLog.model,
            LiteLLMSpendLog.model_group,
            LiteLLMSpendLog.custom_llm_provider,
            func.count(LiteLLMSpendLog.request_id).label("cnt"),
        )
        .where(
            LiteLLMSpendLog.startTime >= _strip_tz(start),
            LiteLLMSpendLog.startTime < _strip_tz(end),
        )
        .group_by(
            LiteLLMSpendLog.model,
            LiteLLMSpendLog.model_group,
            LiteLLMSpendLog.custom_llm_provider,
        )
    )
    rows = (await db.execute(stmt)).all()
    return [(r.model, r.model_group, r.custom_llm_provider, r.cnt) for r in rows]
