"""Vendor adapter 基类. 每个 vendor 一个 Adapter 子类, 实现 fetch_one_day(day) 即可.

颗粒度: 一天 × 一个 model = 一条 ModelRow. job.py 主循环按天调用 adapter, 写入三层表.

字段语义 (ModelRow 落到 vendor_model_usage_daily 的口径):
- prompt_tokens   = **含 cache 的完整 input** (用户视角"输入"应该包含命中和写入)
- completion_tokens = 输出
- cache_read/write_tokens = 单独拆出 (cache 列 + 算钱用到 cache 单价)
- total_tokens    = prompt + completion (input 已含 cache, 不要再加 cache)

上游 client 内部为了"用单价反推 token"通常 prompt 是"非缓存 input" — adapter 在出库前
用 pack_input/pack_total 合并回完整口径. **client 内部算钱逻辑不动**.

为啥按天一次次调: 大部分 vendor 上游接口返"整段聚合", 把它拆到天我们要 N 次调用; 但
cron 后台跑慢点没事, 而且每天独立事务 → 中途挂能断点续 (state 按天推进).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


def pack_input(prompt: int | None, cache_read: int | None,
                cache_write: int | None) -> int | None:
    """把 client 的(非缓存 prompt, cache_r, cache_w) 合并成"完整 input" = p + cr + cw.
    全 None → None; 否则缺哪个当 0.
    """
    if prompt is None and cache_read is None and cache_write is None:
        return None
    return (prompt or 0) + (cache_read or 0) + (cache_write or 0)


def pack_total(prompt_full: int | None, completion: int | None) -> int | None:
    """total = (含 cache 的 prompt) + completion. prompt 已含 cache, 不要再加."""
    if prompt_full is None and completion is None:
        return None
    return (prompt_full or 0) + (completion or 0)


@dataclass(slots=True)
class ModelRow:
    """一个 vendor 一天一个 model 的明细行. 直接对应 vendor_model_usage_daily 一行."""
    model: str

    # tokens (NULL = 上游不给, 0 = 真 0). prompt_tokens 含 cache, 见上面字段语义说明.
    prompt_tokens: int | None
    completion_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    total_tokens: int | None

    request_count: int | None
    image_count: int | None

    # native 币种金额. job.py 会按 vendor 的 native_currency 换 usd/cny
    cost_native: float


@dataclass(slots=True)
class ApiKeyRow:
    """一个 vendor 一天一个 API key 一个 model 的明细行.

    对应 vendor_apikey_usage_daily 一行. 只有上游能按 API key (token_name)
    拆分的 vendor 才产生这种行 — 供 vendor 详情页"按 API key 筛选"用.
    adapter 在 fetch_one_day 里同时返回 ModelRow (按 model 聚合, 写主表) 和
    ApiKeyRow (按 key×model 拆分, 写平行的 key 表). 主表行为完全不变.
    """
    api_key: str          # 上游 token_name (NOT NULL — 这张表只存有 key 的行)
    model: str

    prompt_tokens: int | None
    completion_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    total_tokens: int | None

    request_count: int | None
    image_count: int | None

    cost_native: float


class VendorAdapter:
    """所有 vendor adapter 的基类. 子类只需实现 fetch_one_day."""

    # 这个 adapter 处理哪个 vendor (vendor.json 里的 id)
    vendor_id: str
    # 数据来源标识, 落到 vendor_meta.source 字段, 排查用
    source: str
    # native 币种 — 'USD' or 'CNY'
    native_currency: str
    # 这个 vendor 上游能不能拆 cache (read/write 都有就 True)
    has_cache_detail: bool = False
    # 上游 model 拆分接口跟"总额接口"分离, 总额接口稳, model 接口偶尔漏数据.
    # 走这个模式的 adapter: fetch_one_day 拿 model 拆分; fetch_upstream_total_cost 拿总额.
    # silent-empty 时只要 stat>0, 就用 stat 写 vendor_usage_daily.cost (不算 failed),
    # state 不推进, 等 model 接口补上再回来吃.
    # 慢路径补 model 拆分通过 ingest.<vendor>_slow.slow_fill_day(day) 触发.
    has_stat_fallback: bool = False

    def __init__(self, vendor: dict) -> None:
        """vendor 是 vendors.json 里那一条 dict (含 base_url / api_key / 等)."""
        self.vendor = vendor

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        """返回这一天所有 model 的明细行. 没数据返空 list 不要抛."""
        raise NotImplementedError

    def fetch_upstream_total_cost(self, day: dt.date) -> float | None:
        """[可选] 调上游"轻量 stat 接口"拿这一天的总 cost (native 币种).

        用途: silent-empty sanity check — fetch_one_day 返 0 行时, job.py 调这个 hook
        看上游真实总额. 0 → 真无调用, 不报警; >0 → 上游有钱但 statistics 返空, 真异常.

        默认 return None 表示 adapter 没实现 → job.py 走旧的"14 天 avg"启发式兜底.
        实现这个方法的 adapter 应该用比 fetch_one_day 更轻量 / 更稳定的接口
        (e.g. apevon 的 /api/log/self/stat, newapi 的 /api/data/self).
        """
        return None

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """[可选] 返回这一天的 (api_key, model) 拆分明细行, 写 vendor_apikey_usage_daily.

        只有上游能按 API key / token_name 拆分的 vendor 实现这个方法
        (e.g. apevon / wangsu / grok / openai). 没实现 → 返空 list, key 表不写,
        vendor 详情页的 api key 筛选器自动隐藏. 主表 (vendor_model_usage_daily)
        行为由 fetch_one_day 决定, 跟这个方法完全独立.
        """
        return []
