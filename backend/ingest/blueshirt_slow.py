"""blueshirt 慢路径核心函数 — 走 /api/log/self 拿 prompt/completion/cache 拆分字段.

为啥单独一个模块: BlueshirtAdapter 走的是快路径 /api/data/self (cost 准, 但不拆 prompt/completion/cache).
慢路径是补丁性质 — 仅 UPDATE 拆分字段, 不动 cost / total_tokens / request_count.

调用方:
- backfill_blueshirt_slow.py — 老脚本, 跑一段连续日期 (现在只能补 retention 窗口内的天)
- ingest/router.py — POST /vendors/gptmeta/slow-fill, xxl-job 每天调一次跑昨天

retention: blueshirt /api/log/self 只保留近 ~13 天, 超过的天上游返 total=0.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests
from sqlalchemy import text

from vendors import get_vendor

from ingest.apikey_slow import aggregate_by_key, write_apikey_rows
from ingest.db import SessionLocal
from newapi_client import _get_state_file, _load_session
from utils import normalize_model_name

log = logging.getLogger("ingest.blueshirt_slow")

VENDOR_ID = "blueshirt"
BASE_URL = "https://www.blueshirtmap.com"
try:
    QUOTA_PER_DOLLAR = float((get_vendor(VENDOR_ID) or {}).get("quota_per_dollar") or 500_000)
except Exception:
    QUOTA_PER_DOLLAR = 500_000   # vendors.json 读不到时的兜底
PAGE_SIZE = 10000         # 上游硬上限
PAGE_TIMEOUT = 180        # 单页 HTTP timeout — 一页慢的话 40s+, 留余量
DAY_BUDGET = 60 * 60      # 单天总超时 60min


def fetch_day_logs_serial(cookies: dict, headers: dict, day: dt.date) -> tuple[list, bool]:
    """串行翻 /api/log/self, 返回 (这一天所有 log items, complete).

    complete=False 表示翻页有缺 (单天超时 / 非 auth 失败中断 / 单页异常被跳过) —
    key 表这种"整段替换"语义的写入方必须据此跳过, 不然会把完整数据换成残缺数据.
    session 死时上游返 success:false 或 HTML 登录页, 这里识别后 raise RuntimeError
    让外层立刻 abort, 避免"0 条 = 假成功".
    """
    CST = timezone(timedelta(hours=8))
    start_ts = int(datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=CST).timestamp())
    end_ts = int(datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=CST).timestamp())

    base_params = {
        "type": 2, "page_size": PAGE_SIZE,
        "start_timestamp": start_ts, "end_timestamp": end_ts,
    }

    deadline = time.time() + DAY_BUDGET
    all_logs: list = []
    complete = True
    # 上游分页是 1-based: p=0 会被钳到第 1 页 (实测 p=0 与 p=1 返回同一页 id 集合),
    # 从 0 开始会把第一页抓两遍 → 拆分字段虚高约一页的量 (旧数据即受此害).
    p = 1
    total = None
    json_fail_count = 0
    while True:
        if time.time() > deadline:
            log.warning(f"{day} 触发单天超时 {DAY_BUDGET}s, 已拉 {len(all_logs)} 条 (total={total}), 停翻页")
            complete = False
            break
        try:
            r = requests.get(
                f"{BASE_URL}/api/log/self",
                params={**base_params, "p": p},
                cookies=cookies, headers=headers, timeout=PAGE_TIMEOUT,
            )
            try:
                j = r.json()
            except ValueError:
                json_fail_count += 1
                if json_fail_count >= 3:
                    raise RuntimeError(
                        f"session expired (response is not JSON, likely HTML login page); "
                        f"status={r.status_code} body[:200]={r.text[:200]!r}"
                    )
                log.warning(f"{day} page {p} 非 JSON 响应 (第 {json_fail_count} 次): {r.text[:120]!r}")
                complete = False  # 这页丢了
                p += 1
                continue
            if not j.get("success"):
                msg = j.get("message") or ""
                if "用户信息" in msg or "登录" in msg or "未登录" in msg or "重新登录" in msg:
                    raise RuntimeError(f"session expired: {msg}")
                log.warning(f"{day} page {p} fail: {msg}")
                complete = False
                break
            d = j.get("data") or {}
            items = d.get("items") or []
            if total is None:
                total = int(d.get("total") or 0)
                log.info(f"{day} total={total}, page_size={PAGE_SIZE}, 预计 {(total+PAGE_SIZE-1)//PAGE_SIZE} 页")
            all_logs.extend(items)
            if not items:
                break
            # 不按 len(all_logs) >= total 提前退出: 上游 total 观测按页粒度取整
            # (key 表 request_count 曾连续多天恰为 PAGE_SIZE 整倍数 = 页边界提前停,
            # 静默缺页且 complete=True 误报). 空页才是可靠的结束信号, total 只作日志.
            p += 1
            if p > 5000:
                complete = False
                break
        except RuntimeError:
            raise
        except Exception as e:
            log.warning(f"{day} page {p} 异常 (跳过): {str(e)[:120]}")
            complete = False  # 这页丢了
            p += 1
            if p > 5000:
                break
    return all_logs, complete


def aggregate_with_cache(logs: list) -> dict:
    """聚合 raw log → 按 model 出 (prompt/completion/cache_r/cache_w) 4 字段.

    口径跟 newapi_client.aggregate_logs 一致, 但只保留拆分字段 (cost/total/count 走快路径).
    """
    out: dict = defaultdict(lambda: {"prompt": 0, "completion": 0, "cache_r": 0, "cache_w": 0})
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
        # 上游 log 的 prompt_tokens 语义按渠道混合 (逐条实测 2026-08):
        #   gpt 系: prompt 已含 cache (prompt >= cache_r, 差值是非缓存输入)
        #   claude 系: prompt 是纯非缓存输入, cache 只在 other 里 (prompt << cache_r)
        # 按项目口径 prompt_tokens 必须是"完整输入含 cache", 逐条判断补齐.
        # 误判方向温和: prompt>=cache 时不补, gpt 行恒成立不会被误补.
        if prompt < cache_r + cache_w:
            prompt += cache_r + cache_w
        m = out[model]
        m["prompt"] += prompt
        m["completion"] += completion
        m["cache_r"] += cache_r
        m["cache_w"] += cache_w
    return dict(out)


def update_day(day: dt.date, agg: dict) -> int:
    """UPDATE vendor_model_usage_daily 4 个拆分字段, 再从 model 表 SUM 回写 vendor_usage_daily.
    不动 cost / total_tokens / request_count (那些走快路径写, 慢路径仅补字段).
    """
    if not agg:
        log.warning(f"{day} aggregate 空, 跳过 UPDATE")
        return 0

    with SessionLocal() as s:
        existing = s.execute(text("""
            SELECT model FROM vendor_model_usage_daily
            WHERE vendor_id=:v AND usage_date=:d
        """), {"v": VENDOR_ID, "d": day}).scalars().all()
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
                "v": VENDOR_ID, "d": day, "m": model,
                "p": m["prompt"], "c": m["completion"],
                "cr": m["cache_r"], "cw": m["cache_w"],
            })
            updated += 1

        if updated:
            # 从 model 表 SUM 回写 vendor 汇总表 — 保持两层物化一致
            # (慢路径不动 total_tokens, 那一列由快路径维护)
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
            """), {"v": VENDOR_ID, "d": day})

            # 同样把 usage_daily_total (全局表) 这天的拆分字段刷一遍.
            # 它是 SUM(vendor_usage_daily), 慢路径动了 vendor 表必须传播上去, 不然
            # Overview 顶部全局 cache_read_tokens 还停在快路径写时的旧值 (gptmeta 那时 NULL).
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


def slow_fill_day(day: dt.date) -> dict:
    """跑一天: load session → fetch → aggregate → update. 返回统计 dict.

    抛 RuntimeError('session expired ...') 调用方应该 abort 整批.
    """
    cookies, user_id, header_name = _load_session(_get_state_file(BASE_URL))
    headers = {header_name: user_id}
    t0 = time.time()
    logs, complete = fetch_day_logs_serial(cookies, headers, day)
    agg = aggregate_with_cache(logs)
    n = update_day(day, agg)
    # 按 (token_name, model) 聚合写 key 表 — 同一批日志零额外请求.
    # blueshirt 按时间保留 ~22 天: 只在日志拉取完整时才写, 避免残缺数据覆盖完整行
    # (上游过期后无法重取). 不完整时已有 key 行不动, 拆分字段照旧"拿到啥补啥".
    apikeys = None
    if complete:
        apikeys = write_apikey_rows(VENDOR_ID, day, aggregate_by_key(logs), QUOTA_PER_DOLLAR)
    else:
        log.warning(f"{day} 日志不完整 (跳页/超时), 跳过 key 表写入")
    elapsed = time.time() - t0
    return {
        "day": day.isoformat(),
        "logs": len(logs),
        "models": len(agg),
        "updated": n,
        "apikeys": apikeys,
        "elapsed_s": round(elapsed, 1),
    }
