"""slow path 共享: 按 API key (token_name) 聚合 raw log → 写 vendor_apikey_usage_daily.

背景: blueshirt/nulls 的快路径 /api/data/self 不带 key 维度, 按 key 拆分
只能从慢路径 /api/log/self 行里的 token_name 字段聚合. 而 log/self 上游只保留短期
(blueshirt 实测 ~22 天, xhub 是 row 数 LRU), 所以 key 表天然只有最近几天 —
错过窗口的历史数据无法恢复.

由各 slow 模块的 slow_fill_day 调用 (blueshirt_slow / newapi_direct_slow),
翻完日志顺手聚合写入, 不额外发请求. cron 每天的 slow path 因此自动持续采集.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from exchange_rate import get_rates
from ingest.db import SessionLocal
from ingest.models import VendorApiKeyUsageDaily
from utils import normalize_model_name

log = logging.getLogger("ingest.apikey_slow")


def aggregate_by_key(logs: list) -> dict:
    """聚合 raw log → {(token_name, model): {prompt, completion, cache_r, cache_w, count, quota}}.

    口径跟 slow 模块的 aggregate_with_cache 一致 (prompt 取 log 原值, cache 单独拆,
    total = prompt + completion 不重复加 cache). 额外带 count (请求次数) 和
    quota (new-api 配额单位, / quota_per_dollar = native 金额).
    没有 token_name 的行跳过 — key 表只存有 key 的行 (api_key NOT NULL).

    cache_w 用 None-until-seen: 日志 other 不带 cache_creation_tokens 时该维度
    视为上游未暴露 (xhub 实证), 不伪造 0; blueshirt 有真实值不受影响。
    """
    out: dict = defaultdict(lambda: {
        "prompt": 0, "completion": 0, "cache_r": 0, "cache_w": None,
        "count": 0, "quota": 0,
    })
    for entry in logs:
        if entry.get("type") != 2:
            continue
        key = (entry.get("token_name") or "").strip()
        if not key:
            continue
        model = normalize_model_name(entry.get("model_name", "unknown"))
        prompt = int(entry.get("prompt_tokens") or 0)
        completion = int(entry.get("completion_tokens") or 0)
        cache_r = 0
        cache_w = 0
        other = entry.get("other")
        if isinstance(other, str) and other:
            try:
                o = json.loads(other)
                cache_r = int(o.get("cache_tokens") or 0)
                cache_w = int(o.get("cache_creation_tokens") or 0)
            except (ValueError, TypeError):
                pass
        # 口径对齐 blueshirt_slow.aggregate_with_cache: gpt 系上游 prompt 已含 cache,
        # claude 系不含 (prompt 是纯非缓存输入) — 逐条判断补齐成"完整输入含 cache".
        if prompt < cache_r + cache_w:
            prompt += cache_r + cache_w
        m = out[(key, model)]
        m["prompt"] += prompt
        m["completion"] += completion
        m["cache_r"] += cache_r
        if cache_w:
            m["cache_w"] = (m["cache_w"] or 0) + cache_w
        m["count"] += 1
        m["quota"] += int(entry.get("quota") or 0)
    return dict(out)


def write_apikey_rows(vendor_id: str, day: dt.date, by_key: dict,
                      quota_per_dollar: float, native_currency: str = "USD") -> int:
    """DELETE + INSERT 这一天 (vendor, day) 的 key 行 — 跟 job.py 主入库同幂等语义.

    cost_native = quota / quota_per_dollar (new-api 全家通用换算).
    by_key 空 → 直接返回不动已有行 (跟 job.py 的 `if key_rows:` 同策略) —
    空 fetch 分不清"这天真没数据"还是"上游把日志清了/拉取失败", 宁可留着旧行.
    调用方必须只在日志拉取完整时才调这里 (slow_fill_day 已按 complete 守卫).
    返回写入行数.
    """
    # 延迟 import: job 顶层 import 了 models/db 等, 避免环
    from ingest.job import _convert_cost

    if not by_key:
        log.info(f"[{vendor_id}] {day} 聚合无 key 行, key 表不动")
        return 0

    fx_rates = get_rates()
    fx_usd_to_cny = float(fx_rates.get("CNY") or 7.2)

    payloads = []
    for (key, model), m in by_key.items():
        cost_native = m["quota"] / quota_per_dollar
        usd, cny = _convert_cost(cost_native, native_currency, fx_usd_to_cny)
        payloads.append({
            "vendor_id": vendor_id,
            "usage_date": day,
            "api_key": key,
            "model": model,
            "prompt_tokens": m["prompt"],
            "completion_tokens": m["completion"],
            "cache_read_tokens": m["cache_r"],
            "cache_write_tokens": m["cache_w"],
            "total_tokens": m["prompt"] + m["completion"],
            "request_count": m["count"],
            "image_count": None,          # new-api 日志不带图片数
            "cost_native": cost_native,
            "cost_usd": usd,
            "cost_cny": cny,
            "fx_rate": fx_usd_to_cny,
            "last_run_id": None,          # slow path 的 run 行在 job 层管理, 这里拿不到 id
        })

    with SessionLocal() as s:
        s.execute(text(
            "DELETE FROM vendor_apikey_usage_daily "
            "WHERE vendor_id=:v AND usage_date=:d"
        ), {"v": vendor_id, "d": day})
        s.execute(pg_insert(VendorApiKeyUsageDaily).values(payloads))
        s.commit()
    return len(payloads)
