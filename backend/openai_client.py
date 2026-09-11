"""OpenAI 平台客户端 (Admin API key, scope 含 api.usage.read)。

dashboard 对齐:
- total_cost / daily: /v1/organization/costs (USD, 服务端权威, 与 dashboard "Costs" tab 一致)
- per-model token: /v1/organization/usage/completions (group_by=model, 与 "Activity" tab 一致)
- per-model cost: dashboard 不显示 per-model cost (Costs 只到 line_item 类别: Chat/Image models),
                  我们用 LiteLLM 定价库 token×price 算, 再按 /costs 总额缩放 → sum 严格等于 dashboard 总额

未匹配定价的模型记到 unpriced_models, 前端可提示去 LiteLLM 补价。

API key 维度 (fetch_usage_by_api_key): group_by=api_key_id 一次调用拿全
(usage 双维 model+api_key_id, costs 单维) → vendor_apikey_usage_daily
(详情页按 key 筛选)。见文末探测注释。
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests
from pydantic import TypeAdapter

from exchange_rate import convert
from litellm.pricing.calculator import calculate
from litellm.pricing.types import ExtractedTokens, PricingConfig
from litellm.services.pricing_cache_service import _ensure_loaded, _find_in_cache
from utils import normalize_model_name

# OpenAI 官方账单按 US/Pacific 自然日切桌 (PT, UTC-8 / 夏令时 UTC-7).
# 用 stdlib zoneinfo 让 DST 切换自动处理.
try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except ImportError:  # Python <3.9 fallback (用不到, 我们 3.12)
    PT = timezone(timedelta(hours=-8))
# 默认走官方; 配 OPENAI_BASE_URL 指向自建透明反代即可
# (国内服务器到 api.openai.com 被 GFW 拦, 反代部署在能出海的机器上).
BASE_URL = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com").rstrip("/") + "/v1"
# bucket_width=1d 服务端硬卡 31 buckets/页; limit>31 时 /costs 实测返空 data, /usage 也截
PAGE_LIMIT = 31

# (兼容老配置) 如果只配了 OPENAI_PROXY 还能用 HTTP forward proxy
_PROXY = os.environ.get("OPENAI_PROXY") or None
_PROXIES = {"http": _PROXY, "https": _PROXY} if _PROXY else None
# 默认 30s, 反代加点延迟也够用. .env 里可配 OPENAI_TIMEOUT 覆盖.
_TIMEOUT = float(os.environ.get("OPENAI_TIMEOUT") or 30)

_PC_ADAPTER = TypeAdapter(PricingConfig)


def _bearer(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _iso_to_unix(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def _bucket_to_date(bucket: dict) -> str:
    # OpenAI 桌按 UTC 切, 但官方账单按 PT 自然日聚合; 用 PT 显示对齐 dashboard
    return datetime.fromtimestamp(bucket.get("start_time") or 0, PT).strftime("%Y-%m-%d")


def _fetch_pages(api_key: str, path: str, params: dict) -> list[dict]:
    """分页拿全部 buckets。next_page=null/has_more=false 即停。
    OpenAI Admin API 的 /costs 偶发 5xx (服务端临时错误), 退避重试 3 次."""
    all_buckets: list[dict] = []
    cursor: str | None = None
    while True:
        q = dict(params)
        if cursor:
            q["page"] = cursor
        last_err = None
        for attempt in range(3):
            r = requests.get(
                f"{BASE_URL}{path}?{urlencode(q, doseq=True)}",  # doseq: group_by 多值 → 重复参数
                headers=_bearer(api_key), timeout=_TIMEOUT,
                proxies=_PROXIES,
            )
            if r.status_code == 200:
                break
            # 5xx + 429 退避 (1s, 2s, 4s); 4xx 立刻 fail (key/scope 问题, 重试无用)
            if r.status_code >= 500 or r.status_code == 429:
                last_err = f"OpenAI {path} status={r.status_code}: {r.text[:200]}"
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"OpenAI {path} status={r.status_code}: {r.text[:200]}")
        else:
            raise RuntimeError(f"OpenAI {path} 重试 3 次仍失败: {last_err}")
        body = r.json()
        all_buckets.extend(body.get("data") or [])
        if not body.get("has_more"):
            break
        cursor = body.get("next_page")
        if not cursor:
            break
    return all_buckets


def _fetch_costs(api_key: str, start_ts: int, end_ts: int) -> dict[str, float]:
    """{date_str: usd_cost}。保留空 bucket=0, 让 daily 折线在没消耗的天也能画 0 点。

    注意 OpenAI Admin API 怪 bug: 传 end_time 时部分 bucket 的 results 会
    被服务端吃成空 (实测 only-start 能查到 5/18 $717, 一加 end_time 全部
    return []). 所以这里不传 end_time, 客户端自己按 bucket.start_time 截.

    桌左闭右开 [start_ts, end_ts), end_ts 那一秒的桌不算 (= 下一天的桌).
    """
    buckets = _fetch_pages(api_key, "/organization/costs", {
        "start_time": start_ts,
        "bucket_width": "1d", "limit": PAGE_LIMIT,
    })
    daily: dict[str, float] = {}
    for b in buckets:
        # 客户端双侧截断: bucket.start_time 不在 [start_ts, end_ts) → 跳过
        bts = b.get("start_time") or 0
        if bts < start_ts or bts >= end_ts:
            continue
        date = _bucket_to_date(b)
        daily.setdefault(date, 0.0)
        for r in (b.get("results") or []):
            amount = (r.get("amount") or {}).get("value") or 0
            try:
                daily[date] += float(amount)
            except (TypeError, ValueError):
                continue
    return daily


def _fetch_completions_usage(api_key: str, start_ts: int, end_ts: int) -> dict[str, dict]:
    """{normalized_model: {input_tokens_raw, completion_tokens, cache_read_tokens, total_count}}.

    OpenAI 返回模型名带日期 (gpt-4o-mini-2024-07-18), normalize_model_name 去版本号后汇总,
    跟定价库 (gpt-4o-mini) 对齐。

    input_tokens_raw = OpenAI 原始 input_tokens (含 cache_read), 传给定价计算用;
    对前端展示再拆 (prompt_tokens = 非缓存 input)。
    """
    buckets = _fetch_pages(api_key, "/organization/usage/completions", {
        "start_time": start_ts,
        "bucket_width": "1d", "group_by": "model", "limit": PAGE_LIMIT,
    })
    models: dict[str, dict] = {}
    for b in buckets:
        # 跟 _fetch_costs 一样, 双侧截断: bts ∈ [start_ts, end_ts)
        bts = b.get("start_time") or 0
        if bts < start_ts or bts >= end_ts:
            continue
        for r in (b.get("results") or []):
            name = normalize_model_name(r.get("model") or "unknown")
            e = models.setdefault(name, {
                "input_tokens_raw": 0, "completion_tokens": 0,
                "cache_read_tokens": 0, "total_count": 0,
            })
            e["input_tokens_raw"] += int(r.get("input_tokens") or 0)
            e["completion_tokens"] += int(r.get("output_tokens") or 0)
            e["cache_read_tokens"] += int(r.get("input_cached_tokens") or 0)
            e["total_count"] += int(r.get("num_model_requests") or 0)
    return models


def _model_cost_usd(model_name: str, tokens: dict) -> float:
    """tokens × LiteLLM 定价 → USD; 找不到价格返 0。
    优先按 provider='openai' 查, 没有再退到 provider=None (默认)。

    ExtractedTokens.regular_input_tokens = prompt_tokens - cache_read - cache_write,
    所以这里 prompt_tokens 必须传 OpenAI 原始 input_tokens (含 cache_read), 别提前减。"""
    _ensure_loaded()
    cfg_dict = _find_in_cache(model_name, provider="openai") or _find_in_cache(model_name, provider=None)
    if not cfg_dict:
        return 0.0
    try:
        cfg = _PC_ADAPTER.validate_python(cfg_dict)
    except Exception:
        return 0.0
    t = ExtractedTokens(
        prompt_tokens=tokens["input_tokens_raw"],
        completion_tokens=tokens["completion_tokens"],
        cache_read_tokens=tokens["cache_read_tokens"],
    )
    cost, _ = calculate(t, cfg, tokens["total_count"] or 1)
    cur = getattr(cfg, "currency", "USD") or "USD"
    if cur != "USD":
        cost = convert(cost, cur, "USD")
    return float(cost)


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    """统一格式 (USD)。total_cost / daily 走 /costs (权威); per-model cost
    用定价库 token×price 算后按总额缩放, 保证 sum = /costs 总额。"""
    api_key = vendor["api_key"]
    if not api_key.startswith("sk-admin-"):
        raise PermissionError(
            "OpenAI vendor 必须用 admin API key (sk-admin-...), "
            "在 https://platform.openai.com/settings/organization/admin-keys 创建"
        )

    start_ts = _iso_to_unix(start_time)
    end_ts = _iso_to_unix(end_time)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_costs = ex.submit(_fetch_costs, api_key, start_ts, end_ts)
        f_usage = ex.submit(_fetch_completions_usage, api_key, start_ts, end_ts)
        daily_cost = f_costs.result()
        models_raw = f_usage.result()

    total_cost = round(sum(daily_cost.values()), 6)

    # 定价库 raw cost, 用来 (1) 作为缩放权重 (2) 标识未匹配价格的模型
    models_raw_cost: dict[str, float] = {}
    unpriced: list[str] = []
    for name, m in models_raw.items():
        raw = _model_cost_usd(name, m)
        models_raw_cost[name] = raw
        if raw == 0 and (m["input_tokens_raw"] + m["completion_tokens"]) > 0:
            unpriced.append(name)
    raw_sum = sum(models_raw_cost.values())

    # 缩放因子让 sum(per-model) 严格等于 dashboard 总额 (吸收折扣 / 价格漂移)
    scale = (total_cost / raw_sum) if raw_sum > 0 else 0.0

    models: dict[str, dict] = {}
    for name, m in models_raw.items():
        per_cost = round(models_raw_cost[name] * scale, 6) if scale else 0.0
        # 前端 prompt_tokens 列含义 = 非缓存 input, 这里现拆
        non_cache_input = max(0, m["input_tokens_raw"] - m["cache_read_tokens"])
        models[name] = {
            "prompt_tokens": non_cache_input,
            "completion_tokens": m["completion_tokens"],
            "cache_tokens": m["cache_read_tokens"],          # 兼容老字段 (= cache_read)
            "cache_read_tokens": m["cache_read_tokens"],
            "cache_write_tokens": None,                      # usage 接口不报 cache-write (未暴露), 不伪造 0
            "total_tokens": m["input_tokens_raw"] + m["completion_tokens"],
            "total_count": m["total_count"],
            "total_cost": per_cost,
        }

    daily = sorted(
        ({"date": d, "cost": round(c, 6)} for d, c in daily_cost.items()),
        key=lambda x: x["date"],
    )

    result = {
        "total_cost": total_cost,
        "models": models,
        "daily": daily,
        "vendor_id": vendor["id"],
        "vendor_name": vendor["name"],
        "currency": vendor.get("currency", "USD"),
    }
    if unpriced:
        result["unpriced_models"] = sorted(set(unpriced))
    return result


# ---------------------------------------------------------------------------
# API key 维度 — vendor_apikey_usage_daily (vendor 详情页"按 API key 筛选")
#
# 2026-08-27 探测结论 (scripts/probe_openai_apikey.py):
# - group_by=api_key_id 在 /usage/completions 与 /costs 都是官方支持项
#   (字面量必须是 api_key_id, 写 api_key 会 400 — 首轮探测踩过这个坑误判不支持)
# - usage/completions 支持双维 group_by=["model","api_key_id"], 行直接回显
#   api_key_id + 全 token 字段 (含 input_cache_write_tokens)
# - /costs group_by=api_key_id 的分组行完整划分日总额 (实测 Σ分组 == Σ无过滤),
#   不用再单独拉无过滤基线
# → 一天 2 次调用拿全部 key 维度数据; key 名从 /organization/projects/{id}/
#   api_keys 列表解析 (org 级 admin key / 已删除 key 的 id 查不到名, 归残差合成行)
# ---------------------------------------------------------------------------

# key 清单几乎不变, 但 backend 进程常驻 — TTL 1h, 新建 key 最迟 1h 后进枚举
_KEYS_CACHE_TTL = 3600.0
_keys_cache: tuple[float, list[dict]] | None = None


def _list_project_api_keys(admin_key: str) -> list[dict]:
    """全 org 的 project API keys → [{"id": "key_...", "label": "name (project)"}].

    label 全局去重 (不同 project 同名 key 会撞 key 表 PK), 重名补 id 尾缀。
    """
    global _keys_cache
    now = time.time()
    if _keys_cache and now - _keys_cache[0] < _KEYS_CACHE_TTL:
        return _keys_cache[1]
    out: list[dict] = []
    seen: dict[str, int] = {}   # label → 撞名次数; 撞名时全部带尾缀 (稳定性见下)
    raw: list[tuple[str, str]] = []
    for p in _fetch_pages(admin_key, "/organization/projects", {"limit": 100}):
        pid, pname = p.get("id"), p.get("name") or "?"
        for k in _fetch_pages(admin_key, f"/organization/projects/{pid}/api_keys",
                              {"limit": 100}):
            kid = k.get("id")
            if not kid:
                continue
            label = f"{k.get('name') or kid} ({pname})"
            seen[label] = seen.get(label, 0) + 1
            raw.append((kid, label))
    # 撞名 → 所有同名 key 都带 id 尾缀 (不是只给后出现的补): 若只补后者, 撞名中
    # 排前的被删后, 幸存者的 label 会翻转, 14 天 lookback 外的历史行身份分裂。
    for kid, label in raw:
        suffix = f" [{kid[-6:]}]" if seen[label] > 1 else ""
        out.append({"id": kid, "label": label + suffix})
    _keys_cache = (now, out)
    return out


def _fetch_usage_by_key(api_key: str, start_ts: int, end_ts: int) -> dict[str | None, dict[str, dict]]:
    """group_by=["model","api_key_id"] 一次调用 → {api_key_id|None: {model: tokens...}}.

    api_key_id=None 的行 = org 级 admin key 等列不出清单的用量, 归残差。
    token 字段: input_tokens 已含 cache_read+write (完整输入), cache_write 单独拆。
    """
    buckets = _fetch_pages(api_key, "/organization/usage/completions", {
        "start_time": start_ts,
        "bucket_width": "1d", "group_by": ["model", "api_key_id"], "limit": PAGE_LIMIT,
    })
    out: dict[str | None, dict[str, dict]] = {}
    for b in buckets:
        bts = b.get("start_time") or 0
        if bts < start_ts or bts >= end_ts:
            continue
        for r in (b.get("results") or []):
            kid = r.get("api_key_id") or None
            name = normalize_model_name(r.get("model") or "unknown")
            e = out.setdefault(kid, {}).setdefault(name, {
                "input_tokens": 0, "completion_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "total_count": 0,
            })
            e["input_tokens"] += int(r.get("input_tokens") or 0)
            e["completion_tokens"] += int(r.get("output_tokens") or 0)
            e["cache_read_tokens"] += int(r.get("input_cached_tokens") or 0)
            e["cache_write_tokens"] += int(r.get("input_cache_write_tokens") or 0)
            e["total_count"] += int(r.get("num_model_requests") or 0)
    return out


def _fetch_costs_by_key(api_key: str, start_ts: int, end_ts: int) -> dict[str | None, float]:
    """group_by=api_key_id 的 /costs → {api_key_id|None: usd} (服务端权威).

    分组行完整划分日总额, Σ values 即该日无过滤总额 — 残差/对账都不用额外调用。
    """
    buckets = _fetch_pages(api_key, "/organization/costs", {
        "start_time": start_ts,
        "bucket_width": "1d", "group_by": "api_key_id", "limit": PAGE_LIMIT,
    })
    out: dict[str | None, float] = {}
    for b in buckets:
        bts = b.get("start_time") or 0
        if bts < start_ts or bts >= end_ts:
            continue
        for r in (b.get("results") or []):
            kid = r.get("api_key_id") or None
            out[kid] = out.get(kid, 0.0) + float((r.get("amount") or {}).get("value") or 0)
    return out


def _allocate_cost(models: dict, total_usd: float) -> dict[str, float]:
    """把一个 key 的整日权威成本按 model 分摊 — 与主表"定价估算×总额缩放"同哲学:
    总额权威, 拆分估算. 权重: 定价库 raw cost → 退化 token 量 → 退化均分;
    最大行吸收舍入差 → Σ == total_usd (对账用)."""
    if not models or total_usd == 0:
        return {name: 0.0 for name in models}
    weights = {name: max(0.0, _model_cost_usd(name, {
        "input_tokens_raw": m["input_tokens"], "completion_tokens": m["completion_tokens"],
        "cache_read_tokens": m["cache_read_tokens"], "total_count": m["total_count"],
    })) for name, m in models.items()}
    if sum(weights.values()) <= 0:
        tokens = {name: (m["input_tokens"] or 0) + (m["completion_tokens"] or 0)
                  for name, m in models.items()}
        weights = tokens if sum(tokens.values()) > 0 else {name: 1.0 for name in models}
    wsum = sum(weights.values())
    out = {name: round(total_usd * w / wsum, 6) for name, w in weights.items()}
    drift = round(total_usd - sum(out.values()), 6)
    if drift:
        top = max(out, key=lambda n: out[n])
        out[top] = round(out[top] + drift, 6)
    return out


_UNATTRIBUTED = "(未归属/已删除 key)"
_NON_COMPLETIONS = "(非 completions 用量)"


def fetch_usage_by_api_key(vendor: dict, start_time: str, end_time: str) -> list[dict]:
    """(api_key, model) 维度的一天用量+成本 → openai_adapter.fetch_apikey_rows 用.

    返回 rows: [{api_key, model, input_tokens(含 cache), completion_tokens,
    cache_read_tokens, cache_write_tokens, request_count, cost_usd}]。
    cost_usd = key 级 /costs 权威值按 model 定价权重分摊; Σ rows == 该日总额
    (分组行完整划分总额, 对账用)。

    id 查不到名字的用量 (org 级 admin key / 已删除 key / api_key_id=null 行)
    归 "_UNATTRIBUTED" 合成行; 有成本无 completions 用量的 key (纯 embeddings
    等) 补 "_NON_COMPLETIONS" 合成 model 行 — 总额永不丢。
    """
    api_key = vendor["api_key"]
    start_ts = _iso_to_unix(start_time)
    end_ts = _iso_to_unix(end_time)

    label_by_id = {k["id"]: k["label"] for k in _list_project_api_keys(api_key)}
    usage = _fetch_usage_by_key(api_key, start_ts, end_ts)
    costs = _fetch_costs_by_key(api_key, start_ts, end_ts)

    # 按 label 归桶: 已知 id → 各自 label; 未知 id / null → 残差桶 (合成 key 一行不少)
    # nc_cost 单独记: "(非 completions 用量)" 行拿自己的确切成本, 不进定价权重分摊
    # (混合残差桶里它 token 全 0, 参与分摊会恒得 0 且成本被错摊给其他 model 行)
    buckets: dict[str, dict] = {}
    for kid, models in usage.items():
        label = label_by_id.get(kid) if kid else None
        b = buckets.setdefault(label or _UNATTRIBUTED,
                               {"models": {}, "cost": 0.0, "nc_cost": 0.0})
        for m, v in models.items():
            e = b["models"].setdefault(m, {
                "input_tokens": 0, "completion_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "total_count": 0,
            })
            for f in e:
                e[f] += v[f]
        b["cost"] += costs.get(kid, 0.0)
    for kid, cost in costs.items():  # 有成本但无 completions 用量的 key
        if kid not in usage and cost:
            label = (label_by_id.get(kid) if kid else None) or _UNATTRIBUTED
            b = buckets.setdefault(label, {"models": {}, "cost": 0.0, "nc_cost": 0.0})
            b["models"].setdefault(_NON_COMPLETIONS, {
                "input_tokens": 0, "completion_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "total_count": 0,
            })
            b["cost"] += cost
            b["nc_cost"] += cost

    rows: list[dict] = []
    for label, b in buckets.items():
        nc_cost = round(b["nc_cost"], 6)
        alloc = _allocate_cost({m: v for m, v in b["models"].items()
                                if m != _NON_COMPLETIONS},
                               round(b["cost"] - b["nc_cost"], 6))
        for m, v in b["models"].items():
            rows.append({
                "api_key": label,
                "model": m,
                "input_tokens": v["input_tokens"],
                "completion_tokens": v["completion_tokens"],
                "cache_read_tokens": v["cache_read_tokens"],
                "cache_write_tokens": v["cache_write_tokens"],
                "request_count": v["total_count"],
                "cost_usd": nc_cost if m == _NON_COMPLETIONS else alloc.get(m, 0.0),
            })
    return rows
