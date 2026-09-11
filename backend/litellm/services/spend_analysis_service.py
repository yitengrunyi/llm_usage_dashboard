"""
消费分析 Service

定价来源优先级：DB（model_prices 表，含别名匹配）→ 内置定价（builtin_prices，兜底）
流程：
  1. 确保定价缓存已加载（从 DB）
  2. 调用 query_spend_raw() 按请求拉取原始行（不聚合）
  3. 先用 model_group（原始模型名称）查定价 → 回退到规范化模型名 + 供应商查定价
  4. 在应用层按 (规范化模型名, 供应商, date) 汇总
  5. 返回输入/输出/缓存 token 与总花费，双币种（USD/CNY）根据实时汇率自动转换
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import datetime

from litellm.pricing import calculator as cost_calc
from litellm.pricing.builtin_prices import get_builtin_price
from litellm.pricing.types import AdvancedPricing, ExtractedTokens, PricingConfig
from litellm.repositories import spend_log_repository
from litellm.repositories.spend_log_repository import SpendRawRow
from litellm.schemas import SpendQueryIn, SpendRecord, SpendSummaryOut
from litellm.services.exchange_rate_service import convert_cost, get_usd_to_cny
from litellm.services.pricing_cache_service import _find_in_cache

try:
    from pydantic import TypeAdapter
    _adapter: TypeAdapter[PricingConfig] = TypeAdapter(PricingConfig)
except Exception:
    _adapter = None  # type: ignore[assignment]


def _parse_config(pricing_config: dict) -> PricingConfig | None:
    """将 pricing_config 解析为 PricingConfig。"""
    try:
        return _adapter.validate_python(pricing_config)  # type: ignore[union-attr]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 供应商识别 & 模型名归一化（实现在 app.pricing.normalizer，此处 re-export 保持兼容）
# ---------------------------------------------------------------------------
from litellm.pricing.normalizer import (
    KNOWN_PROVIDERS as _KNOWN_PROVIDERS,
    PROVIDER_PREFIXES as _PROVIDER_PREFIXES,
    DOMESTIC_MODEL_PROVIDER_MAP as _DOMESTIC_MODEL_PROVIDER_MAP,
    extract_provider as _extract_provider,
    normalized_model_name as _normalized_model_name,
)


# ---------------------------------------------------------------------------
# 日期后缀 & 版本号辅助
# ---------------------------------------------------------------------------

_DATE_SUFFIX_PATTERN = re.compile(
    r"-(\d{8}|\d{6}|\d{4}-\d{2}-\d{2}|\d{2}-\d{2}|\d{2}-\d{4})$"
)


def _strip_date_suffix(name: str) -> str | None:
    match = _DATE_SUFFIX_PATTERN.search(name)
    return name[:match.start()] if match else None


def _normalize_version_separator(name: str) -> str | None:
    """如 doubao-seed-1-6 → doubao-seed-1.6"""
    patterns = [
        (r"^(doubao-seed)-(\d+)-(\d+)(-[a-z]+)?", r"\1-\2.\3\4"),
        (r"^(deepseek-v\d+)-(\d+)", r"\1.\2"),
    ]
    for pattern, replacement in patterns:
        result, count = re.subn(pattern, replacement, name)
        if count > 0:
            return result.replace("None", "")
    return None


# ---------------------------------------------------------------------------
# 定价查找（DB 缓存优先，builtin 兜底）
# ---------------------------------------------------------------------------

def _try_find(name: str, provider: str | None = None) -> dict | None:
    """
    在 DB 缓存和 builtin 中查找定价。
    自动生成变体：去 :free 后缀、点号/连字符互换。
    支持 provider 维度：先查 (name, provider) 专属价，再回退 (name, None) 默认价。
    """
    if not name:
        return None

    variants = [name]
    base = name.split(":")[0]
    if base != name:
        variants.append(base)

    # 点号 → 连字符（如 claude-opus-4.6 → claude-opus-4-6）
    if re.search(r'\d+\.\d+', name):
        variants.append(re.sub(r'(\d+)\.(\d+)', r'\1-\2', name))
    # 连字符 → 点号（如 claude-opus-4-6 → claude-opus-4.6）
    if re.search(r'\d+-\d+(?=\D|$)', name):
        dot_v = re.sub(r'(\d+)-(\d+)(?=\D|$)', r'\1.\2', name)
        if dot_v != name:
            variants.append(dot_v)

    for v in variants:
        # 1. DB 缓存查找（含 provider 优先级：专属 → 默认）
        cfg = _find_in_cache(v, provider)
        if cfg:
            return cfg
        # 2. Builtin fallback（不区分 provider）
        cfg = get_builtin_price(v)
        if cfg:
            return cfg

    return None


def _provider_from_model_group(model_group: str | None) -> str | None:
    """从 model_group 提取供应商前缀（如 volcengine-deepseek-v3 → volcengine）。"""
    if not model_group:
        return None
    # openrouter/google/gemini-3-flash-preview 格式
    if "/" in model_group:
        first = model_group.split("/", 1)[0].lower()
        if first in _KNOWN_PROVIDERS:
            return first
    # volcengine-deepseek-v3 格式
    if "-" in model_group:
        first = model_group.lower().split("-", 1)[0]
        if first in _KNOWN_PROVIDERS:
            return first
    return None


def _lookup_price_by_model_group(model_group: str | None) -> dict | None:
    """
    优先使用 model_group（原始模型名称）查找定价。
    按 model_group 直接作为 model_name 查找，从 model_group 提取供应商。
    支持去日期后缀和版本号转换变体。
    """
    if not model_group:
        return None

    provider = _provider_from_model_group(model_group)

    # 直接用 model_group 查找
    cfg = _try_find(model_group, provider)
    if cfg:
        return cfg

    # 去日期后缀
    base = _strip_date_suffix(model_group)
    if base:
        cfg = _try_find(base, provider)
        if cfg:
            return cfg

    # 版本号转换
    base2 = base or model_group
    converted = _normalize_version_separator(base2)
    if converted:
        cfg = _try_find(converted, provider)
        if cfg:
            return cfg

    return None


def _lookup_price(model_name: str | None, provider: str | None = None) -> dict | None:
    """
    查找模型定价（同步，需在调用前确保 DB 缓存已加载）。

    查找顺序：
      1. 直接匹配（规范名 / 短名 / 原名），优先供应商专属价
      2. 去掉日期后缀后重试
      3. 版本号格式转换（如 doubao-seed-1-6 → doubao-seed-1.6）后重试
    """
    if not model_name:
        return None
    canonical = _normalized_model_name(model_name)
    short = model_name.split("/", 1)[-1]

    # Step 1: 直接匹配
    for name in (canonical, short, model_name):
        cfg = _try_find(name, provider)
        if cfg:
            return cfg

    # Step 2: 去掉日期后缀
    for name in (canonical, short, model_name):
        if not name:
            continue
        base = _strip_date_suffix(name)
        if base:
            cfg = _try_find(base, provider)
            if cfg:
                return cfg

    # Step 3: 版本号格式转换
    for name in (canonical, short, model_name):
        if not name:
            continue
        base = _strip_date_suffix(name) or name
        converted = _normalize_version_separator(base)
        if converted:
            cfg = _try_find(converted, provider)
            if cfg:
                return cfg

    return None


# ---------------------------------------------------------------------------
# Token 提取 & 单条算价
# ---------------------------------------------------------------------------

def _tokens_from_raw(row: SpendRawRow) -> ExtractedTokens:
    return ExtractedTokens(
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=row.cache_creation_5m_tokens,
        cache_creation_1h_tokens=row.cache_creation_1h_tokens,
        cache_read_tokens=row.cache_read_tokens,
        thinking_tokens=row.thinking_tokens,
        cache_storage_hours=getattr(row, "cache_storage_hours", 0.0),
        cache_storage_applies=getattr(row, "cache_storage_applies", False),
        is_batch=getattr(row, "is_batch", False),
    )


def _cost_for_row(row: SpendRawRow, pricing_config: dict | None) -> float:
    """单条请求算价。"""
    cfg = _parse_config(pricing_config) if pricing_config else None
    if not cfg:
        return 0.0
    tokens = _tokens_from_raw(row)
    if isinstance(cfg, AdvancedPricing):
        cost, _ = cost_calc.calculate(tokens, cfg, request_count=1)
    else:
        cost, _ = cost_calc.calculate(tokens, cfg)
    return cost


# ---------------------------------------------------------------------------
# CPU-bound 聚合函数（在线程池中执行）
# ---------------------------------------------------------------------------

def _normalize_for_agg(model: str | None, model_group: str | None) -> str:
    """
    聚合用规范化模型名：优先用 model_group（它是 LiteLLM 的模型组名，
    如 volcengine-deepseek-v3），因为 model 字段可能是 endpoint ID
    （如 ep-20250204184841-kxvmq）。
    """
    if model_group:
        norm = _normalized_model_name(model_group)
        if norm:
            return norm
    return _normalized_model_name(model) if model else ""


def _aggregate_rows(
    raw_rows: list[SpendRawRow],
    group_by: list[str],
    normalized_query_model: str | None,
    query_provider: str | None,
) -> dict[tuple, dict]:
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache": 0, "cost": 0.0, "currency": None, "requests": 0}
    )
    _price_cache: dict[tuple, dict | None] = {}
    for row in raw_rows:
        normalized_model = _normalize_for_agg(row.model, row.model_group)
        if normalized_query_model and normalized_model != normalized_query_model:
            continue

        provider = _extract_provider(row.model, row.model_group, row.custom_llm_provider)
        if query_provider and provider != query_provider:
            continue

        key_model = normalized_model if "model" in group_by else None
        key_provider = provider if "provider" in group_by else None
        key_date = row.date if "date" in group_by else None
        key_api_key = row.api_key if "api_key" in group_by else None
        key = (key_model, key_provider, key_date, key_api_key)

        # 定价查找（本地缓存：同 model_group+model 只查一次）
        price_key = (row.model_group, row.model)
        if price_key not in _price_cache:
            cfg = _lookup_price_by_model_group(row.model_group)
            if not cfg:
                cfg = _lookup_price(row.model, provider)
            _price_cache[price_key] = cfg
        pricing_config = _price_cache[price_key]
        cost = _cost_for_row(row, pricing_config)
        currency = "USD"
        if pricing_config:
            currency = pricing_config.get("currency", "USD")

        agg[key]["requests"] += 1
        agg[key]["input"] += row.prompt_tokens
        agg[key]["output"] += row.completion_tokens
        agg[key]["cache"] += row.cache_read_tokens + row.cache_creation_tokens
        agg[key]["cost"] += cost
        if agg[key]["currency"] is None:
            agg[key]["currency"] = currency
    return dict(agg)


def _aggregate_single_rows(
    raw_rows: list[SpendRawRow],
    normalized_query_model: str | None,
    target_provider: str | None,
) -> tuple[int, int, int, int, float, str]:
    input_tok = output_tok = cache_tok = 0
    req_count = 0
    cost_sum = 0.0
    currency = "USD"
    _price_cache: dict[tuple, dict | None] = {}
    for row in raw_rows:
        if normalized_query_model:
            normalized_model = _normalize_for_agg(row.model, row.model_group)
            if normalized_model != normalized_query_model:
                continue
        row_provider = _extract_provider(row.model, row.model_group, row.custom_llm_provider)
        if target_provider and row_provider != target_provider:
            continue

        input_tok += row.prompt_tokens
        output_tok += row.completion_tokens
        cache_tok += row.cache_read_tokens + row.cache_creation_tokens
        req_count += 1
        # 定价查找（本地缓存：同 model_group+model 只查一次）
        price_key = (row.model_group, row.model)
        if price_key not in _price_cache:
            cfg = _lookup_price_by_model_group(row.model_group)
            if not cfg:
                cfg = _lookup_price(row.model, row_provider)
            _price_cache[price_key] = cfg
        pricing_config = _price_cache[price_key]
        cost_sum += _cost_for_row(row, pricing_config)
        if pricing_config and "currency" in pricing_config:
            currency = pricing_config["currency"]

    return input_tok, output_tok, cache_tok, req_count, cost_sum, currency


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

async def get_spend_summary(db, *, query: SpendQueryIn) -> SpendSummaryOut:
    from sqlalchemy.ext.asyncio import AsyncSession
    assert isinstance(db, AsyncSession)

    group_by = list(query.group_by)
    normalized_query_model = _normalized_model_name(query.model) if query.model else None

    # 确保定价缓存已加载 & 获取汇率
    usd_to_cny = await get_usd_to_cny()

    # ── Step 1: 快速分类（无 JSONB，~0.3s）──
    from litellm.services.pricing_cache_service import get_tiered_model_names
    tiered_names = get_tiered_model_names()

    group_counts = await spend_log_repository.query_model_group_counts(
        db, start=query.start, end=query.end,
    )
    simple_groups = list({mg for _, mg, _, _ in group_counts if mg and mg.lower() not in tiered_names})
    tiered_groups = list({mg for _, mg, _, _ in group_counts if mg and mg.lower() in tiered_names})

    # ── Step 2: 并行查询（简单走聚合 + 阶梯走逐行）──
    from litellm.db import SessionLocal

    async def _query_simple():
        if not simple_groups:
            return []
        async with SessionLocal() as db2:
            return await spend_log_repository.query_spend_grouped(
                db2,
                start=query.start, end=query.end,
                model_groups=simple_groups,
                date_granularity=query.date_granularity,
                user=query.user, team_id=query.team_id,
            )

    async def _query_tiered():
        if not tiered_groups:
            return []
        async with SessionLocal() as db2:
            return await spend_log_repository.query_spend_raw(
                db2,
                start=query.start, end=query.end,
                date_granularity=query.date_granularity,
                model_groups=tiered_groups,
                user=query.user, team_id=query.team_id,
            )

    simple_agg_rows, tiered_raw_rows = await asyncio.gather(
        _query_simple(), _query_tiered(),
    )

    # ── Step 3: 聚合 ──
    agg: dict[tuple, dict] = {}

    def _add_to_agg(key: tuple, requests: int, inp: int, out: int, cache: int, cost: float, currency: str):
        if key not in agg:
            agg[key] = {"input": 0, "output": 0, "cache": 0, "cost": 0.0, "currency": None, "requests": 0}
        agg[key]["requests"] += requests
        agg[key]["input"] += inp
        agg[key]["output"] += out
        agg[key]["cache"] += cache
        agg[key]["cost"] += cost
        if agg[key]["currency"] is None:
            agg[key]["currency"] = currency

    # 简单模型聚合（SQL 已经 SUM 好了，Python 只需乘单价 + 二次归一化分组）
    for row in simple_agg_rows:
        normalized_model = _normalize_for_agg(row.model, row.model_group)
        if normalized_query_model and normalized_model != normalized_query_model:
            continue

        provider = _extract_provider(row.model, row.model_group, row.custom_llm_provider)
        if query.provider and provider != query.provider:
            continue

        # 简单定价：SUM × 单价
        pricing_config = _lookup_price_by_model_group(row.model_group)
        if not pricing_config:
            pricing_config = _lookup_price(row.model, provider)
        cfg = _parse_config(pricing_config) if pricing_config else None

        cost = 0.0
        if cfg:
            tokens = ExtractedTokens(
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                cache_creation_tokens=0,
                cache_creation_5m_tokens=row.cache_creation_5m_tokens,
                cache_creation_1h_tokens=row.cache_creation_1h_tokens,
                cache_read_tokens=row.cache_read_tokens,
                thinking_tokens=row.thinking_tokens,
            )
            cost, _ = cost_calc.calculate(tokens, cfg)

        currency = "USD"
        if pricing_config:
            currency = pricing_config.get("currency", "USD")

        key_model = normalized_model if "model" in group_by else None
        key_provider = provider if "provider" in group_by else None
        key_date = row.date if "date" in group_by else None
        key = (key_model, key_provider, key_date, None)
        _add_to_agg(key, row.request_count, row.prompt_tokens, row.completion_tokens,
                     row.cache_read_tokens + row.cache_creation_tokens, cost, currency)

    # 阶梯模型聚合（逐行算价，与原有逻辑一致）
    if tiered_raw_rows:
        tiered_agg = await asyncio.to_thread(
            _aggregate_rows, tiered_raw_rows, group_by, normalized_query_model, query.provider,
        )
        for key, data in tiered_agg.items():
            _add_to_agg(key, data["requests"], data["input"], data["output"],
                         data["cache"], data["cost"], data["currency"] or "USD")

    records = []
    # key tuple 里可能混 None (group_by 没要的字段) 和 str (要的). sorted 默认逐元素比
    # str < None 会报 TypeError, 把 None 当空串排.
    for key, data in sorted(agg.items(), key=lambda kv: tuple('' if x is None else str(x) for x in kv[0])):
        cost_val = round(data["cost"], 8)
        cur = data["currency"] or "USD"
        cost_usd, cost_cny = convert_cost(cost_val, cur, usd_to_cny)
        records.append(SpendRecord(
            model=key[0],
            provider=key[1],
            date=key[2],
            api_key=key[3],
            request_count=data["requests"],
            input_tokens=data["input"],
            output_tokens=data["output"],
            cache_tokens=data["cache"],
            cost=cost_val,
            currency=cur,
            cost_usd=cost_usd,
            cost_cny=cost_cny,
        ))

    return SpendSummaryOut(
        period_start=query.start,
        period_end=query.end,
        group_by=group_by,
        total_request_count=sum(r.request_count for r in records),
        total_input_tokens=sum(r.input_tokens for r in records),
        total_output_tokens=sum(r.output_tokens for r in records),
        total_cache_tokens=sum(r.cache_tokens for r in records),
        total_cost=round(sum(r.cost for r in records), 8),
        currency="USD",
        total_cost_usd=round(sum(r.cost_usd for r in records), 8),
        total_cost_cny=round(sum(r.cost_cny for r in records), 8),
        exchange_rate=usd_to_cny,
        records=records,
    )


async def get_options(db) -> "SpendOptionsOut":
    """获取下拉框可选的 (模型, 供应商) — 走 SpendLogs distinct, 不读 pricing 缓存.

    为啥不读 pricing 缓存: pricing 缓存是 LiteLLM 上游全集 (3700+ models, 88+ providers,
    含 fireworks/cohere/bedrock_converse/text-completion-codestral 这些公司根本没用过的),
    塞进下拉框噪声极大. 只取 SpendLogs 里实际出现过的 (model, model_group, custom_llm_provider),
    用 _extract_provider 收敛到 12+ 真实供应商, 用 _normalized_model_name 削干净版本/日期/
    context-size/lifecycle/前缀, 再 lower + 下划线/空格转 dash 进一步合并大小写/分隔差异.
    """
    from litellm.schemas import SpendOptionsOut

    # 全量 distinct — 数据量约几百行, distinct 在 SpendLogs (model, model_group, custom_llm_provider)
    # 三列上即可, 不带时间过滤 = 看历史所有用过的 vendor/model
    _, raw_rows = await spend_log_repository.query_distinct_models_and_providers(db)

    models: set[str] = set()
    providers: set[str] = set()
    for model, model_group, custom_llm_provider in raw_rows:
        # 1. 归一化 model — 优先 model_group (路由层名, 通常带 vendor 前缀完整),
        #    没 model_group 就用 model 字段 (可能是 anthropic/claude-... 这种路径名)
        raw_name = model_group or model
        norm = _normalized_model_name(raw_name)
        if norm:
            # 进一步合并: lower + 空格/下划线 → dash + 收掉多余 dash
            cleaned = norm.lower().replace(" ", "-").replace("_", "-")
            cleaned = re.sub(r"-+", "-", cleaned).strip("-")
            if cleaned:
                models.add(cleaned)

        # 2. 解析 provider — 走 normalizer.extract_provider 的优先级链
        prov = _extract_provider(model, model_group, custom_llm_provider)
        if prov:
            providers.add(prov.lower())

    return SpendOptionsOut(models=sorted(models), providers=sorted(providers))


async def get_unpriced_models(db, *, start: datetime, end: datetime) -> "UnpricedModelsOut":
    """查询时间范围内有请求但无定价的模型+供应商组合。用 SQL GROUP BY，不碰 JSONB。"""
    from litellm.schemas import UnpricedModelItem, UnpricedModelsOut
    from sqlalchemy.ext.asyncio import AsyncSession
    assert isinstance(db, AsyncSession)

    # SQL 层 GROUP BY，不碰 JSONB，返回约几百行
    group_rows = await spend_log_repository.query_model_group_counts(
        db, start=start, end=end,
    )

    # (normalized_model, provider) → request_count
    combos: dict[tuple[str, str], int] = {}
    combos_model_group: dict[tuple[str, str], str | None] = {}
    for model, model_group, custom_llm_provider, cnt in group_rows:
        if not model:
            continue
        normalized = _normalize_for_agg(model, model_group)
        provider = _extract_provider(model, model_group, custom_llm_provider)
        key = (normalized, provider)
        combos[key] = combos.get(key, 0) + cnt
        if key not in combos_model_group:
            combos_model_group[key] = model_group

    # 找出无定价的
    items = []
    for (model, provider), count in combos.items():
        cfg = _lookup_price_by_model_group(combos_model_group.get((model, provider)))
        if not cfg:
            cfg = _lookup_price(model, provider)
        if not cfg:
            items.append(UnpricedModelItem(
                model=model,
                model_group=combos_model_group.get((model, provider)),
                provider=provider,
                request_count=count,
            ))

    items.sort(key=lambda x: -x.request_count)
    return UnpricedModelsOut(
        total=len(items),
        total_requests=sum(i.request_count for i in items),
        items=items,
    )


async def get_spend_by_model_provider(
    db,
    *,
    start: datetime,
    end: datetime,
    model: str,
    provider: str,
) -> "SpendSingleOut":
    """按「供应商+模型」查询。通过反向索引将 model_group 过滤下推到 SQL 层。"""
    from litellm.schemas import SpendSingleOut
    from litellm.services.pricing_cache_service import get_model_groups_for
    from sqlalchemy.ext.asyncio import AsyncSession
    assert isinstance(db, AsyncSession)

    usd_to_cny = await get_usd_to_cny()
    normalized_query_model = _normalized_model_name(model)

    # 反向索引：规范名 + 供应商 → 原始 model_groups，直接下推到 SQL
    matched_groups = get_model_groups_for(normalized_query_model, provider)
    if not matched_groups:
        # 回退：不限供应商查（可能是 builtin 定价，不在 JSON 反向索引中）
        matched_groups = get_model_groups_for(normalized_query_model)

    if matched_groups:
        raw_rows = await spend_log_repository.query_spend_raw(
            db, start=start, end=end, model_groups=matched_groups,
        )
    else:
        # 兜底：仍用全量查询（覆盖未配置定价的模型）
        raw_rows = await spend_log_repository.query_spend_raw(db, start=start, end=end)

    input_tok, output_tok, cache_tok, req_count, cost_sum, currency = await asyncio.to_thread(
        _aggregate_single_rows, raw_rows, normalized_query_model, provider,
    )

    cost_rounded = round(cost_sum, 8)
    cost_usd, cost_cny = convert_cost(cost_rounded, currency, usd_to_cny)
    return SpendSingleOut(
        period_start=start, period_end=end,
        request_count=req_count,
        input_tokens=input_tok, output_tokens=output_tok, cache_tokens=cache_tok,
        cost=cost_rounded, currency=currency,
        cost_usd=cost_usd, cost_cny=cost_cny,
        model=model, provider=provider, api_key=None,
    )


async def get_spend_by_api_key(
    db,
    *,
    start: datetime,
    end: datetime,
    api_key: str,
) -> "SpendSingleOut":
    """按 api_key 查询。"""
    from litellm.schemas import SpendSingleOut
    from sqlalchemy.ext.asyncio import AsyncSession
    assert isinstance(db, AsyncSession)

    usd_to_cny = await get_usd_to_cny()

    raw_rows = await spend_log_repository.query_spend_raw(db, start=start, end=end, api_key=api_key)
    input_tok, output_tok, cache_tok, req_count, cost_sum, currency = await asyncio.to_thread(
        _aggregate_single_rows, raw_rows, None, None,
    )

    cost_rounded = round(cost_sum, 8)
    cost_usd, cost_cny = convert_cost(cost_rounded, currency, usd_to_cny)
    return SpendSingleOut(
        period_start=start, period_end=end,
        request_count=req_count,
        input_tokens=input_tok, output_tokens=output_tok, cache_tokens=cache_tok,
        cost=cost_rounded, currency=currency,
        cost_usd=cost_usd, cost_cny=cost_cny,
        model=None, provider=None, api_key=api_key,
    )
