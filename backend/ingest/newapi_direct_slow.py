"""xhub 慢路径 — 走 /api/log/self 拿 prompt/completion/cache 拆分字段.

为啥单独一个模块: XhubAdapter 走快路径 /api/data/self (cost/total/req 准, 但不拆 prompt/comp/cache).
慢路径是补丁性质 — 仅 UPDATE 拆分字段, 不动 cost / total_tokens / request_count.

跟 blueshirt_slow.py 的区别:
- blueshirt 走 newapi_client._load_session (Playwright 扫码后存 cookies)
- xhub 走 newapi_direct_client._get_session (账号密码登录, 30 min TTL 缓存)
- vendor_id 参数化 (blueshirt_slow.py 硬编码 'blueshirt')

retention: xhub raw log 按 row 数 LRU 滚动清理 (高 throughput 账号每天几十万行,
末尾几十分钟就会顶掉前面的). cron 当天跑能拿全, backfill 老天大量丢失 — 是 4/1~4/12
DB 残缺的根因.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections import defaultdict

from sqlalchemy import text

from ingest.apikey_slow import aggregate_by_key, write_apikey_rows
from ingest.db import SessionLocal
from newapi_direct_client import _get_session, fetch_logs
from utils import normalize_model_name

log = logging.getLogger("ingest.newapi_direct_slow")

DAY_BUDGET = 60 * 60  # 单天总超时 60 min — xhub 大账号一天 raw log 可能数十万行


def aggregate_with_cache(logs: list) -> dict:
    """聚合 raw log → 按 model 出 (prompt/completion/cache_r/cache_w) 4 字段.

    口径跟 newapi_client.aggregate_logs 一致, 但只保留拆分字段
    (cost/total/count 走快路径 data/self 写, 慢路径不动那些).

    cache_w 用 None-until-seen: xhub 日志的 other 从不带 cache_creation_tokens
    (2026-08 全窗口实证, 对比 blueshirt 同款解析有非零值) → 该维度上游未暴露,
    不伪造 0 (有 cache_read 就必有写入发生)。哪天真出现了照常累计。
    """
    out: dict[str, dict] = defaultdict(lambda: {"prompt": 0, "completion": 0, "cache_r": 0, "cache_w": None})
    for entry in logs:
        if entry.get("type") != 2:
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
        m = out[model]
        m["prompt"] += prompt
        m["completion"] += completion
        m["cache_r"] += cache_r
        if cache_w:
            m["cache_w"] = (m["cache_w"] or 0) + cache_w
    return dict(out)


def update_day(vendor_id: str, day: dt.date, agg: dict) -> int:
    """UPDATE vendor_model_usage_daily 4 个拆分字段, 再从 model 表 SUM 回写 vendor_usage_daily
    和 usage_daily_total. 不动 cost / total_tokens / request_count (那些走快路径写).

    跟 blueshirt_slow.update_day 等价, 区别是 vendor_id 参数化.
    """
    if not agg:
        log.warning(f"[{vendor_id}] {day} aggregate 空, 跳过 UPDATE")
        return 0

    with SessionLocal() as s:
        existing = s.execute(text("""
            SELECT model FROM vendor_model_usage_daily
            WHERE vendor_id=:v AND usage_date=:d
        """), {"v": vendor_id, "d": day}).scalars().all()
        existing_set = set(existing)

        updated = 0
        for model, m in agg.items():
            if model not in existing_set:
                continue
            s.execute(text("""
                UPDATE vendor_model_usage_daily
                SET prompt_tokens = :p,
                    completion_tokens = :c,
                    cache_read_tokens = :cr,
                    cache_write_tokens = :cw
                WHERE vendor_id=:v AND usage_date=:d AND model=:m
            """), {
                "v": vendor_id, "d": day, "m": model,
                "p": m["prompt"], "c": m["completion"],
                "cr": m["cache_r"], "cw": m["cache_w"],
            })
            updated += 1

        if updated:
            # 从 model 表 SUM 回写 vendor 汇总表 — 保持两层物化一致
            s.execute(text("""
                UPDATE vendor_usage_daily AS v
                SET prompt_tokens = sub.p,
                    completion_tokens = sub.c,
                    cache_read_tokens = sub.cr,
                    cache_write_tokens = sub.cw
                FROM (
                    SELECT SUM(prompt_tokens) AS p,
                           SUM(completion_tokens) AS c,
                           SUM(cache_read_tokens) AS cr,
                           SUM(cache_write_tokens) AS cw
                    FROM vendor_model_usage_daily
                    WHERE vendor_id=:v AND usage_date=:d
                ) AS sub
                WHERE v.vendor_id=:v AND v.usage_date=:d
            """), {"v": vendor_id, "d": day})

            # 同样把 usage_daily_total (全局表) 这天的拆分字段刷一遍 — 慢路径动了 vendor
            # 表必须传播上去, 不然 Overview 顶部全局 cache_read_tokens 还停在旧值.
            s.execute(text("""
                UPDATE usage_daily_total AS t
                SET prompt_tokens = sub.p,
                    completion_tokens = sub.c,
                    cache_read_tokens = sub.cr,
                    cache_write_tokens = sub.cw
                FROM (
                    SELECT SUM(prompt_tokens) AS p,
                           SUM(completion_tokens) AS c,
                           SUM(cache_read_tokens) AS cr,
                           SUM(cache_write_tokens) AS cw
                    FROM vendor_usage_daily
                    WHERE usage_date=:d
                ) AS sub
                WHERE t.usage_date=:d
            """), {"d": day})

        s.commit()
        return updated


def slow_fill_day(vendor: dict, day: dt.date) -> dict:
    """跑一天: get session → fetch raw log → aggregate → update. 返回统计 dict.

    raw log 上游 LRU 清理拿不到老数据时 logs=[] / 部分缺, 静默 UPDATE 0 行不抛错
    (跟 blueshirt 一样, 慢路径补丁性质, 拿到啥补啥).
    """
    session, user_id = _get_session(vendor)
    user_header = vendor.get("user_header") or "New-Api-User"
    start_iso = f"{day.isoformat()}T00:00:00+08:00"
    end_iso = f"{day.isoformat()}T23:59:59+08:00"

    t0 = time.time()
    deadline = t0 + DAY_BUDGET
    logs, complete = fetch_logs(session, vendor["base_url"], user_id, user_header,
                                start_time=start_iso, end_time=end_iso)
    if time.time() > deadline:
        log.warning(f"[{vendor['id']}] {day} fetch_logs 超时 {DAY_BUDGET}s, 已拉 {len(logs)} 条")
        complete = False

    agg = aggregate_with_cache(logs)
    n = update_day(vendor["id"], day, agg)
    # 按 (token_name, model) 聚合写 key 表 — 同一批日志零额外请求.
    # xhub 的 row-LRU 特性: 即使今天数据也常 complete=False (len < total, 老行被挤),
    # 但拿到的就是"当前还在的全部" → 只要有日志就写 (DELETE+INSERT 幂等, cron 每天更新).
    apikeys = None
    if logs:
        apikeys = write_apikey_rows(
            vendor["id"], day, aggregate_by_key(logs),
            float(vendor.get("quota_per_dollar") or 500_000),
            vendor.get("currency") or "USD",
        )
        if not complete:
            log.info(f"[{vendor['id']}] {day} 日志不完整但已写入 {apikeys} key 行 (xhub LRU 特性, 明天 cron 会更新)")
    elapsed = time.time() - t0
    return {
        "vendor_id": vendor["id"],
        "day": day.isoformat(),
        "logs": len(logs),
        "models": len(agg),
        "updated": n,
        "apikeys": apikeys,
        "elapsed_s": round(elapsed, 1),
    }
