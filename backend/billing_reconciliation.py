"""供应商账单对账模块: 根据token用量、官方单价、折扣计算应付/实付并与实际费用对比。

数据流:
  1. 从 vendor_model_usage_daily 查询时间段内各模型的 token 用量和实际费用
  2. 从 LiteLLM pricing (upstream_model_prices.json) 获取官方单价
  3. 从 discounts.json 获取折扣配置
  4. 计算应付(计算) = Σ(tokens × 单价)
  5. 计算实付(计算) = 应付(计算) × 折扣
  6. 偏差: 应付偏差 = 应付(抓取) − 应付(计算); 实付偏差 = 实付(账单) − 实付(计算) (账单无接口, 恒 None)
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ingest.db import SessionLocal
from ingest.models import VendorModelUsageDaily, VendorApiKeyUsageDaily
from discounts import load_discounts, resolve_discount
from litellm.services.upstream_pricing import load_upstream
from exchange_rate import get_rates

logger = logging.getLogger(__name__)


def _load_db_prices() -> dict[tuple[str, str | None], dict]:
    """一次性加载本地 LiteLLM DB 的所有定价到内存 dict。

    返回 {(model_lower, provider_lower|None): pricing_config}。
    费用核算用这个 (而非 pricing_cache_service 的混合层), 因为混合层里
    upstream(GitHub) 优先会覆盖本地手动维护的定价 — kimi-k2.5 就是被
    GitHub 条目顶掉导致币种错的。直查 DB 保证用的是权威价 + 正确 currency。
    """
    from litellm.models import ModelPrice
    out: dict[tuple[str, str | None], dict] = {}
    try:
        with SessionLocal() as db:
            rows = db.execute(
                select(ModelPrice).where(ModelPrice.is_active == True)  # noqa: E712
            ).scalars().all()
        for r in rows:
            pc = r.pricing_config
            if not pc:
                continue
            # 顶层 currency 落进 pricing_config (供 _extract_prices 读)
            if "currency" not in pc and r.currency:
                pc = {**pc, "currency": r.currency}
            key = (r.model_name.lower(), r.provider.lower() if r.provider else None)
            out.setdefault(key, pc)
            # 别名也建索引
            for a in (r.aliases or []):
                out.setdefault((a.lower(), r.provider.lower() if r.provider else None), pc)
    except Exception as e:
        logger.warning(f"加载本地 DB 定价失败: {e}")
    return out


def get_model_pricing(
    model: str,
    provider: str | None = None,
    db_prices: dict[tuple[str, str | None], dict] | None = None,
) -> dict | None:
    """获取模型单价。

    查找优先级:
      1. 本地 LiteLLM DB 定价 (手动维护, 币种准确) — provider 专属 → 默认 → 别名
      2. 上游 LiteLLM GitHub 定价 (upstream_model_prices.json) — 国际模型兜底
      3. 规范化兜底 (_normalized_fallback) — 点号/日期/路径前缀等命名差异的二次匹配

    参数 db_prices: 预加载的 DB 价格 map (循环外调 _load_db_prices 批量取, 避免逐模型开 session)。
    返回: {input_per_1m, output_per_1m, cache_read_per_1m, cache_write_per_1m, currency} 或 None。
    规范化兜底命中的返回 dict 额外带 "_match": "normalized" 标记 (调用方可用于区分来源)。
    """
    model_lc = model.lower()
    prov_lc = provider.lower() if provider else None

    # 1) 本地 DB 定价优先: provider 精确 → 默认(provider=None) → 该 model 任意 provider
    if db_prices is not None:
        if prov_lc and (model_lc, prov_lc) in db_prices:
            return {**_extract_prices(db_prices[(model_lc, prov_lc)]), "_match": "db"}
        if (model_lc, None) in db_prices:
            return {**_extract_prices(db_prices[(model_lc, None)]), "_match": "db"}
        # vendor_id (如 kimi) 可能与定价 provider (如 moonshot) 不一致 → 取该 model 任意一条
        for (nm, _prov), cfg in db_prices.items():
            if nm == model_lc:
                return {**_extract_prices(cfg), "_match": "db"}

    # 2) 上游 GitHub 定价兜底 (claude/gpt 等国际模型)
    try:
        upstream = load_upstream()

        # 优先匹配带 provider 的
        if prov_lc and (model_lc, prov_lc) in upstream:
            return _extract_prices(upstream[(model_lc, prov_lc)])

        # 回退到不带 provider
        if (model_lc, None) in upstream:
            return _extract_prices(upstream[(model_lc, None)])

        # 尝试去掉模型名前缀 (如 openai- volcengine- 等)
        for prefix in ["openai-", "volcengine-", "blueshirt-", "anthropic-",
                       "vertex-", "openrouter-", "road-", "kimi-"]:
            if model_lc.startswith(prefix):
                bare = model_lc[len(prefix):]
                if (bare, None) in upstream:
                    return _extract_prices(upstream[(bare, None)])

        # 最后回退：剥离路径前缀查找
        # 例如 cloudflare/@cf/zai-org/glm-5.2 → glm-5.2
        for (name, prov), cfg in upstream.items():
            if '/' not in name:
                continue
            bare_name = name
            while '/' in bare_name:
                bare_name = bare_name.split('/', 1)[1]
            if bare_name.lower() == model_lc:
                if prov_lc and prov and prov.lower() == prov_lc:
                    return _extract_prices(cfg)
                if prov is None:
                    return _extract_prices(cfg)

        # 兜底取任意一个匹配的
        for (name, prov), cfg in upstream.items():
            if '/' not in name:
                continue
            bare_name = name
            while '/' in bare_name:
                bare_name = bare_name.split('/', 1)[1]
            if bare_name.lower() == model_lc:
                return _extract_prices(cfg)
        return _normalized_fallback(model, provider, db_prices)
    except Exception as e:
        logger.warning(f"获取模型 {model} 定价失败: {e}")
        return _normalized_fallback(model, provider, db_prices)


# ────── 规范化兜底匹配 ──────
@lru_cache(maxsize=None)
def _norm_match_name(name: str) -> str:
    """兜底匹配用的规范化名 — 只做保守归一.

    点号版本号 ↔ 短横 (doubao-seed-1.6 ↔ doubao-seed-1-6)、去日期后缀
    (-251015 / -20260215 / -2026-02-15)、去 provider 路径前缀 (volcengine/xxx)。
    刻意不做 normalize_model_name 的 preview/beta/-unlimit/-200k 剥离 —
    那些标签对应不同价格, 剥了会错配 (gemini-3-pro-preview-thinking ≠ gemini-3-pro-thinking)。
    """
    s = (name or "").strip().lower()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    s = re.sub(r"-20\d{2}-\d{2}-\d{2}$", "", s)  # -2026-02-15
    s = re.sub(r"-20\d{6}$", "", s)              # -20260215
    s = re.sub(r"-\d{6}$", "", s)                # -251015 (YYMMDD 快照)
    s = s.replace(".", "-")
    return re.sub(r"-+", "-", s).strip("-")


def _pick_from_candidates(cands: list[tuple[str, dict]]) -> dict | None:
    """同一优先级候选里选一个: 无价格的跳过; 剩余的价格不一致 → None (宁缺勿错)。"""
    priced = [(nm, cfg) for nm, cfg in cands if _has_any_price(cfg)]
    if not priced:
        return None
    first = _extract_prices(priced[0][1])

    def _sig(p: dict) -> tuple:
        return (p.get("input_per_1m"), p.get("output_per_1m"),
                p.get("cache_read_per_1m"), p.get("cache_write_per_1m"), p.get("currency"))

    for _, cfg in priced[1:]:
        if _sig(_extract_prices(cfg)) != _sig(first):
            return None
    return first


def _has_any_price(cfg: dict) -> bool:
    p = _extract_prices(cfg)
    return any(p.get(k) is not None for k in
               ("input_per_1m", "output_per_1m", "cache_read_per_1m", "cache_write_per_1m"))


def _normalized_fallback(model: str, provider: str | None,
                         db_prices: dict[tuple[str, str | None], dict] | None) -> dict | None:
    """exact 查找全部未命中后的规范化兜底.

    背景: 用量表模型名 (normalize_model_name 后, 短横线/无日期) 与定价表
    (点号 doubao-seed-1.6 / 日期快照 doubao-seed-1-6-251015 / 路径前缀
    volcengine/xxx) 命名体系不一致, exact 全 miss → 单价空缺。

    规则 (保守):
    - 双向过 _norm_match_name 后比对
    - 候选按归属分级, 不做跨 provider 借用:
        1) 本地 DB provider 专属  2) 本地 DB 默认(provider=None)
        3) upstream provider 专属  4) upstream 默认(provider=None)
    - 同级候选价格不一致 → 返回 None, 不猜
    """
    target = _norm_match_name(model)
    if not target:
        return None
    prov = (provider or "").lower()

    if db_prices is not None:
        tier_own: list[tuple[str, dict]] = []
        tier_def: list[tuple[str, dict]] = []
        for (name, p), cfg in db_prices.items():
            if _norm_match_name(name) != target:
                continue
            if p is not None and p.lower() == prov:
                tier_own.append((name, cfg))
            elif p is None:
                tier_def.append((name, cfg))
        for tier in (tier_own, tier_def):
            r = _pick_from_candidates(tier)
            if r is not None:
                return {**r, "_match": "normalized"}

    try:
        # 直接解析原始上游条目, 不用 load_upstream() 的裸名扩展键 —
        # 那层 setdefault((bare, None)) 会把任意渠道 (azure_ai/cloudflare/tensormesh...)
        # 的价收敛成同名裸键且来源不可辨, 跨厂商借用时结果依赖 JSON 迭代顺序。
        from litellm.services.upstream_pricing import _load_raw, _to_pricing_config
        raw = _load_raw()
        tier_own = []
        tier_first_party = []
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            cfg = _to_pricing_config(entry)
            if not cfg or _norm_match_name(name) != target:
                continue
            prov0 = (entry.get("litellm_provider") or "").lower()
            low = name.lower()
            if prov and (prov0 == prov or low.startswith(prov + "/")):
                # 本 vendor 专属条目
                tier_own.append((name, cfg))
            elif "/" in name and len(low.split("/")) == 2 and prov0 and low.split("/", 1)[0] == prov0:
                # 两段式自有挂牌条目 (moonshot/kimi-k2.6, dashscope/glm-5.2):
                # provider 自己的官方挂牌价, 确定性有保证; 深路径转售价
                # (cloudflare/@cf/moonshotai/kimi-k2.6) 不借。同级冲突仍拒绝。
                tier_first_party.append((name, cfg))
        for tier in (tier_own, tier_first_party):
            r = _pick_from_candidates(tier)
            if r is not None:
                return {**r, "_match": "normalized"}
    except Exception as e:
        logger.warning(f"规范化兜底 upstream 查找失败 {model}: {e}")

    return None


def _norm_currency(c: str | None) -> str:
    """统一货币代码: '元'/'cny'/￥ → 'CNY'; 'usd'/$ → 'USD'; 缺省 → 'USD'。"""
    c = (c or "").strip()
    if c in ("元", "CNY", "cny", "￥", "¥"):
        return "CNY"
    if c.upper() in ("USD", "$"):
        return "USD"
    return "USD"


def _extract_prices(cfg: dict) -> dict:
    """从 pricing config 提取单价（统一转为 per_1m 格式）。

    单价 None = 上游没给这个维度的价 (前端显示 "-");
    单价 0 = 上游明确标价为 0 (免费, 前端显示 0). 两者不能混。
    保留 currency 字段, 供调用方归一到 USD。
    """
    cfg_type = cfg.get("type", "standard")
    cur = _norm_currency(cfg.get("currency"))

    if cfg_type == "standard":
        return {
            "input_per_1m": cfg.get("input_per_1m"),
            "output_per_1m": cfg.get("output_per_1m"),
            "cache_read_per_1m": None,
            "cache_write_per_1m": None,
            "currency": cur,
        }
    elif cfg_type == "with_cache":
        return {
            "input_per_1m": cfg.get("input_per_1m"),
            "output_per_1m": cfg.get("output_per_1m"),
            "cache_read_per_1m": cfg.get("cache_read_per_1m"),
            "cache_write_per_1m": cfg.get("cache_write_per_1m"),
            "currency": cur,
        }
    elif cfg_type == "advanced":
        # advanced 有 tiers，简化处理取第一档 (≤200k 档, 多数调用落在首档)
        inp_tiers = cfg.get("input_tiers") or []
        out_tiers = cfg.get("output_tiers") or []
        cache_read_tiers = cfg.get("cache_read_tiers") or []
        # cache_write 有两种字段约定:
        #   1) 本地 DB advanced (如 claude-opus-4-6): cache_write_tiers 阶梯数组
        #   2) upstream GitHub 转换: cache_write_5min_per_1m 扁平字段
        cache_write_tiers = cfg.get("cache_write_tiers") or []

        return {
            "input_per_1m": inp_tiers[0]["price_per_1m"] if inp_tiers else None,
            "output_per_1m": out_tiers[0]["price_per_1m"] if out_tiers else None,
            "cache_read_per_1m": cache_read_tiers[0]["price_per_1m"] if cache_read_tiers else None,
            "cache_write_per_1m": (
                cache_write_tiers[0]["price_per_1m"] if cache_write_tiers
                else cfg.get("cache_write_5min_per_1m")
            ),
            "currency": cur,
        }

    return {
        "input_per_1m": None,
        "output_per_1m": None,
        "cache_read_per_1m": None,
        "cache_write_per_1m": None,
        "currency": "USD",
    }


def _to_usd(pricing: dict, usd_to_cny: float) -> dict:
    """把定价归一到 USD — 费用核算全程以 USD 计算, _cny() 再转 CNY。

    CNY 价格 ÷ 汇率 → USD; 已是 USD 或价格缺失 (None) 则原样返回。
    这样 calculate_model_cost 始终输出 USD, 与 DB 的 cost_usd 可直接比较。
    """
    if not pricing or _norm_currency(pricing.get("currency")) != "CNY":
        return pricing
    out = dict(pricing)
    for k in ("input_per_1m", "output_per_1m", "cache_read_per_1m", "cache_write_per_1m"):
        v = out.get(k)
        if v is not None:
            out[k] = round(v / usd_to_cny, 8)
    out["currency"] = "USD"
    return out


def calculate_model_cost(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    pricing: dict,
) -> dict:
    """根据 token 用量和单价计算费用明细。

    **关键**: DB 的 prompt_tokens 已经包含了 cache_read + cache_write (完整输入).
    计费时要把 cache 部分拆出来单独按 cache 单价算, 避免重复计费.

    应付 = (prompt - cache_read - cache_write) × input_price
         + cache_read × cache_read_price
         + cache_write × cache_write_price
         + completion × output_price

    返回: {
        "input_cost": float | None,       # 非缓存输入部分的费用
        "output_cost": float | None,
        "cache_read_cost": float | None,
        "cache_write_cost": float | None,
        "total_cost": float | None,
    }
    """
    def _num(v):
        """DB 返回可能是 Decimal; None 表示上游没给这个字段, 保持 None。"""
        return float(v) if v is not None else None

    prompt_tokens = _num(prompt_tokens)
    completion_tokens = _num(completion_tokens)
    cache_read_tokens = _num(cache_read_tokens)
    cache_write_tokens = _num(cache_write_tokens)

    def _cost(tokens, price):
        """用量或单价任一缺失 → None (算不出来, 前端显示 "-");
        两者都有 → 正常相乘, 结果可能是 0 (真的 0 元)。"""
        if tokens is None or price is None:
            return None
        return tokens * price / 1_000_000

    # 非缓存输入 = prompt - cache_read - cache_write (prompt 包含了全部 input)
    non_cache_input = None
    if prompt_tokens is not None:
        non_cache_input = prompt_tokens - (cache_read_tokens or 0) - (cache_write_tokens or 0)

    inp = _cost(non_cache_input, pricing.get("input_per_1m"))
    out = _cost(completion_tokens, pricing.get("output_per_1m"))
    cache_read = _cost(cache_read_tokens, pricing.get("cache_read_per_1m"))

    # 缓存写入: 没有专门的写入单价时, 退回用缓存命中的单价
    cache_write_price = pricing.get("cache_write_per_1m")
    if cache_write_price is None:
        cache_write_price = pricing.get("cache_read_per_1m")
    cache_write = _cost(cache_write_tokens, cache_write_price)

    parts = [inp, out, cache_read, cache_write]
    # 四项全算不出来 → 总额也是 None; 只要有一项算出来, 总额就按已知项求和
    total = None if all(p is None for p in parts) else sum(p for p in parts if p is not None)

    def _r(v):
        return round(v, 8) if v is not None else None

    return {
        "input_cost": _r(inp),
        "output_cost": _r(out),
        "cache_read_cost": _r(cache_read),
        "cache_write_cost": _r(cache_write),
        "total_cost": _r(total),
    }


def reconcile_vendor_billing(
    vendor_id: str,
    start_date: str,
    end_date: str,
    api_key: str | None = None,
) -> dict:
    """生成供应商账单对账报告。

    参数:
        vendor_id: 供应商ID
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
        api_key: 可选, 按 API key 过滤 (None = 不过滤走主表; 有值走 vendor_apikey_usage_daily)

    返回: {
        "vendor_id": str,
        "start_date": str,
        "end_date": str,
        "models": [
            {
                "model": str,
                "prompt_tokens": int,
                "completion_tokens": int,
                "cache_read_tokens": int,
                "cache_write_tokens": int,
                "total_tokens": int,            # prompt + completion
                "request_count": int | None,   # 调用次数; NULL = 上游无此字段
                "pricing": {
                    "input_per_1m": float,
                    "output_per_1m": float,
                    "cache_read_per_1m": float | None,
                    "cache_write_per_1m": float | None,
                    "source": "upstream" | "default",
                },
                "discount": float,
                "discount_source": "model" | "family" | "default",
                "calculated": {
                    "input_cost": float,
                    "output_cost": float,
                    "cache_read_cost": float,
                    "cache_write_cost": float,
                    "payable": float,      # 应付(计算)
                    "actual_pay": float,   # 实付(计算) = payable * discount
                },
                "fetched": {
                    "payable": float | None,     # 应付(抓取) - 供应商系统算出的扣费金额 (DB cost)
                    "actual_pay": float | None,  # 实付(账单) - 供应商线下出具的账单, 无接口 → None
                },
                "deviation": {
                    "payable": float | None,     # 应付偏差 = 应付(抓取) - 应付(计算)
                    "actual_pay": float | None,  # 实付偏差 = 实付(账单) - 实付(计算), 账单无数据 → None
                },
            }
        ],
        "summary": {
            "calculated_payable": float,       # Σ应付(计算)
            "calculated_actual_pay": float,    # Σ实付(计算)
            "fetched_payable": float | None,   # Σ应付(抓取) = DB cost 汇总
            "fetched_actual_pay": float | None,  # Σ实付(账单), 无接口 → None
            "deviation_payable": float | None,   # Σ应付(抓取) - Σ应付(计算)
            "deviation_actual_pay": float | None,  # Σ实付(账单) - Σ实付(计算), 账单无数据 → None
            "effective_discount": float | None,   # 综合计算折扣 = Σ实付(计算) / Σ应付(计算)
        }
    }
    """
    with SessionLocal() as db:
        # api_key 筛选: 走平行 key 表 (vendor_apikey_usage_daily); 否则走主表.
        table = VendorApiKeyUsageDaily if api_key is not None else VendorModelUsageDaily
        # 查询时间段内该vendor的所有模型用量
        stmt = (
            select(
                table.model,
                func.sum(table.prompt_tokens).label("prompt_tokens"),
                func.sum(table.completion_tokens).label("completion_tokens"),
                func.sum(table.cache_read_tokens).label("cache_read_tokens"),
                func.sum(table.cache_write_tokens).label("cache_write_tokens"),
                func.sum(table.total_tokens).label("total_tokens"),
                func.sum(table.request_count).label("request_count"),
                func.sum(table.cost_usd).label("actual_cost_usd"),
            )
            .where(table.vendor_id == vendor_id)
            .where(table.usage_date >= start_date)
            .where(table.usage_date <= end_date)
        )
        if api_key is not None:
            stmt = stmt.where(table.api_key == api_key)
        stmt = stmt.group_by(table.model)

        rows = db.execute(stmt).all()

    # 加载折扣配置
    discount_config = load_discounts()

    # 获取汇率 (USD -> CNY)
    rates = get_rates()
    usd_to_cny = rates.get("CNY", 7.2)

    # 批量预加载本地 DB 定价 (权威价, 币种正确); 循环里直接查内存 dict
    db_prices = _load_db_prices()

    models_data = []
    summary_calc_payable = 0.0
    summary_calc_actual = 0.0
    summary_fetched_payable = 0.0
    summary_fetched_actual = 0.0
    has_fetched_payable = False
    has_fetched_actual = False

    for row in rows:
        model = row.model

        # 获取单价 (本地 DB 优先 → upstream exact → 规范化兜底)
        pricing = get_model_pricing(model, vendor_id, db_prices=db_prices)
        if pricing is None:
            # 没找到定价, 全部 None
            pricing = {
                "input_per_1m": None,
                "output_per_1m": None,
                "cache_read_per_1m": None,
                "cache_write_per_1m": None,
                "currency": "USD",
            }
            pricing_source = "default"
        else:
            # _match 标记来源: "db"=本地价格表命中 / "normalized"=规范化兜底; 无标记=upstream
            pricing_source = pricing.pop("_match", None) or "upstream"

        # 币种归一: 把 CNY 定价折算成 USD, 这样 calculate_model_cost 始终输出 USD,
        # 与 DB 的 cost_usd 直接可比, _cny() 的 ×汇率 转换也成立。
        pricing = _to_usd(pricing, usd_to_cny)

        # 获取折扣
        discount_info = resolve_discount(vendor_id, model, discount_config)
        discount = discount_info["discount"]
        discount_source = discount_info["source"]

        # 计算费用
        calc = calculate_model_cost(
            row.prompt_tokens,
            row.completion_tokens,
            row.cache_read_tokens,
            row.cache_write_tokens,
            pricing,
        )

        payable_calc = calc["total_cost"]
        actual_pay_calc = payable_calc * discount if payable_calc is not None else None

        # 抓取值.
        # DB 的 cost_usd 是供应商系统算出的扣费金额 → 这是 **应付(抓取)**.
        # 实付(账单) = 供应商线下出具的账单, 没有接口可抓 → None (前端显示 -).
        fetched_payable = float(row.actual_cost_usd) if row.actual_cost_usd is not None else None
        fetched_actual_pay = None

        # 偏差 = 抓取 - 计算. 任一侧缺失 (账单抓不到 或 定价缺失算不出) → None, 不能做减法.
        deviation_payable = (
            fetched_payable - payable_calc
            if fetched_payable is not None and payable_calc is not None else None
        )
        deviation_actual_pay = (
            fetched_actual_pay - actual_pay_calc
            if fetched_actual_pay is not None and actual_pay_calc is not None else None
        )

        # USD → CNY. None 保持 None (算不出来就是算不出来, 不要变成 0)
        def _cny(v):
            return round(v * usd_to_cny, 6) if v is not None else None

        model_data = {
            "model": model,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_write_tokens": row.cache_write_tokens,
            "total_tokens": row.total_tokens,
            "request_count": row.request_count,
            "pricing": {
                **pricing,
                "source": pricing_source,
            },
            "discount": discount,
            "discount_source": discount_source,
            "calculated": {
                "input_cost": calc["input_cost"],
                "output_cost": calc["output_cost"],
                "cache_read_cost": calc["cache_read_cost"],
                "cache_write_cost": calc["cache_write_cost"],
                "payable": payable_calc,
                "actual_pay": actual_pay_calc,
                "payable_cny": _cny(payable_calc),
                "actual_pay_cny": _cny(actual_pay_calc),
            },
            "fetched": {
                "payable": fetched_payable,
                "actual_pay": fetched_actual_pay,
                "payable_cny": _cny(fetched_payable),
                "actual_pay_cny": _cny(fetched_actual_pay),
            },
            "deviation": {
                "payable": deviation_payable,
                "actual_pay": deviation_actual_pay,
                "payable_cny": _cny(deviation_payable),
                "actual_pay_cny": _cny(deviation_actual_pay),
            },
        }

        models_data.append(model_data)
        if payable_calc is not None:
            summary_calc_payable += payable_calc
        if actual_pay_calc is not None:
            summary_calc_actual += actual_pay_calc
        if fetched_payable is not None:
            summary_fetched_payable += fetched_payable
            has_fetched_payable = True
        if fetched_actual_pay is not None:
            summary_fetched_actual += fetched_actual_pay
            has_fetched_actual = True

    # 按计算应付倒序排列. payable 可能是 None (定价缺失) — 排最后, 不能直接参与比较
    models_data.sort(
        key=lambda x: (x["calculated"]["payable"] is not None,
                       x["calculated"]["payable"] or 0),
        reverse=True,
    )

    return {
        "vendor_id": vendor_id,
        "start_date": start_date,
        "end_date": end_date,
        "usd_to_cny": usd_to_cny,
        "models": models_data,
        "summary": {
            "calculated_payable": round(summary_calc_payable, 6),
            "calculated_actual_pay": round(summary_calc_actual, 6),
            "calculated_payable_cny": round(summary_calc_payable * usd_to_cny, 6),
            "calculated_actual_pay_cny": round(summary_calc_actual * usd_to_cny, 6),
            # 应付(抓取) = DB cost 汇总; 实付(账单) 无接口可抓 → None, 实付偏差也就无从谈起
            "fetched_payable": round(summary_fetched_payable, 6) if has_fetched_payable else None,
            "fetched_actual_pay": round(summary_fetched_actual, 6) if has_fetched_actual else None,
            "fetched_payable_cny": round(summary_fetched_payable * usd_to_cny, 6) if has_fetched_payable else None,
            "fetched_actual_pay_cny": round(summary_fetched_actual * usd_to_cny, 6) if has_fetched_actual else None,
            "deviation_payable": (
                round(summary_fetched_payable - summary_calc_payable, 6)
                if has_fetched_payable else None
            ),
            "deviation_actual_pay": (
                round(summary_fetched_actual - summary_calc_actual, 6)
                if has_fetched_actual else None
            ),
            "deviation_payable_cny": (
                round((summary_fetched_payable - summary_calc_payable) * usd_to_cny, 6)
                if has_fetched_payable else None
            ),
            "deviation_actual_pay_cny": (
                round((summary_fetched_actual - summary_calc_actual) * usd_to_cny, 6)
                if has_fetched_actual else None
            ),
            # 反推的综合折扣 = 总实付(计算) / 总应付(计算). 应付为 0 时除不了 → None.
            "effective_discount": (
                round(summary_calc_actual / summary_calc_payable, 6)
                if summary_calc_payable else None
            ),
        },
    }
