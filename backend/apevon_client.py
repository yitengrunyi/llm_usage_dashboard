"""Apevon (new-api 变种) 直接拉服务端聚合接口, 不再自己算每条 log。

- /api/log/self/stat          → 整个区间总 quota (与 dashboard 完全一致)
- /api/statistics/            → 按 (day_time, model_name, token_name) 分组的 daily 行, 每行带 quota / token 明细
                                抓一次拿 totalQuota + items, 不需要分页 (一个月 ~91 行, 一年 ~1100 行也撑得住 page_size=2000)
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from newapi_client import _get_state_file, _load_session
from utils import normalize_model_name

CST = timezone(timedelta(hours=8))


def _stat_total_quota(base_url: str, cookies: dict, headers: dict, start_ts: int, end_ts: int) -> int:
    """整窗口 quota 总数, 与 Apevon UI 显示完全一致。"""
    r = requests.get(
        f"{base_url}/api/log/self/stat",
        params={"start_timestamp": start_ts, "end_timestamp": end_ts},
        cookies=cookies, headers=headers, timeout=20,
    )
    j = r.json()
    if not j.get("success"):
        raise PermissionError(j.get("message", "stat 接口失败"))
    return int(j["data"]["quota"])


def _statistics_rows(base_url: str, cookies: dict, headers: dict, start_date: str, end_date: str) -> list[dict]:
    """按 (day, model, token) 聚合的 daily 行, 含 quota / 各类 token / request_count。"""
    rows: list[dict] = []
    page = 1
    page_size = 2000
    while True:
        r = requests.get(
            f"{base_url}/api/statistics/",
            params={
                "startTime": start_date, "endTime": end_date,
                "date_type": "cst", "p": page, "page_size": page_size,
                "abnormal": "false",
            },
            cookies=cookies, headers=headers, timeout=30,
        )
        j = r.json()
        if not j.get("success"):
            raise PermissionError(j.get("message", "statistics 接口失败"))
        d = j.get("data") or {}
        items = d.get("items") or []
        rows.extend(items)
        total = int(d.get("total") or 0)
        if len(rows) >= total or not items:
            break
        page += 1
    return rows


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    """统一返回格式: total_cost / models / daily / vendor_id / vendor_name / currency"""
    base_url = vendor["base_url"]
    quota_per_dollar = vendor.get("quota_per_dollar", 1_000_000)
    user_header = vendor.get("user_header") or "Rix-Api-User"

    state_file = _get_state_file(base_url)
    if not state_file.exists():
        raise FileNotFoundError(f"未找到登录态文件: {state_file}\n请先点击登录按钮")
    cookies, user_id, _ = _load_session(state_file)
    headers = {user_header: user_id}

    start_ts = int(datetime.fromisoformat(start_time).timestamp())
    end_ts = int(datetime.fromisoformat(end_time).timestamp())
    start_date = datetime.fromisoformat(start_time).astimezone(CST).strftime("%Y-%m-%d")
    end_date = datetime.fromisoformat(end_time).astimezone(CST).strftime("%Y-%m-%d")

    # stat (总额) 和 statistics (按天/模型) 互不依赖, 顶层 2 thread 并发
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_stat = ex.submit(_stat_total_quota, base_url, cookies, headers, start_ts, end_ts)
            f_rows = ex.submit(_statistics_rows, base_url, cookies, headers, start_date, end_date)
            total_quota = f_stat.result()
            rows = f_rows.result()
    except PermissionError as e:
        # session 失效 (cookie 死了): 删 storage_state.json 让前端 session 接口返 expired,
        # 用户点"登录"按钮就能重新扫码。
        msg = str(e)
        if "未登录" in msg or "过期" in msg or "无权" in msg:
            try:
                state_file.unlink()
            except OSError:
                pass
        raise PermissionError("apevon 登录态已过期，请重新登录")

    total_cost = round(total_quota / quota_per_dollar, 6)

    # 按 model 聚合 (跟 dashboard 一致). token_name 拆分由 adapter 单独走
    # _statistics_rows — 这里保持 fetch_vendor_usage 的 live "today" 路径干净,
    # 不把原始行 (含 calculate_* 比率等 70 个字段) 序列化进 JSON 响应.
    models: dict[str, dict] = {}
    daily_map: dict[str, dict] = {}
    for r in rows:
        m = normalize_model_name(r.get("model_name") or "unknown")
        q = int(r.get("quota") or 0)
        cost = q / quota_per_dollar
        if m not in models:
            models[m] = {
                "prompt_tokens": 0, "completion_tokens": 0,
                "cache_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "total_tokens": 0,
                "total_count": 0, "total_cost": 0,
            }
        # 改版前后字段名不同: 老 statistics 用 *_used 后缀, 新 coding_plan 用裸名.
        # 两套都取, 谁有值用谁 (同一接口不会同时返两套).
        models[m]["prompt_tokens"] += int(r.get("prompt_token_used") or r.get("prompt_tokens") or 0)
        models[m]["completion_tokens"] += int(r.get("complete_token_used") or r.get("completion_tokens") or 0)
        cache_read = int(r.get("cache_read_input_token_used") or r.get("cache_read_input_tokens") or 0)
        cache_write = int(r.get("cache_creation_input_token_used") or r.get("cache_creation_input_tokens") or 0)
        models[m]["cache_tokens"] += cache_read       # 兼容老字段名 (= cache_read)
        models[m]["cache_read_tokens"] += cache_read
        models[m]["cache_write_tokens"] += cache_write
        models[m]["total_tokens"] += int(r.get("token_used") or r.get("total_tokens") or 0)
        models[m]["total_count"] += int(r.get("request_count") or r.get("count") or 0)
        models[m]["total_cost"] += cost

        day = r.get("day_time") or ""
        if not day and r.get("created_at"):
            day = datetime.fromtimestamp(int(r["created_at"]), CST).strftime("%Y-%m-%d")
        if day:
            daily_map.setdefault(day, {"date": day, "cost": 0})["cost"] += cost

    for m in models.values():
        m["total_cost"] = round(m["total_cost"], 6)
    for d in daily_map.values():
        d["cost"] = round(d["cost"], 6)

    daily = sorted(daily_map.values(), key=lambda x: x["date"])

    return {
        "total_cost": total_cost,           # 来自 stat, 跟 dashboard 一致
        "models": models,
        "daily": daily,
        "vendor_id": vendor["id"],
        "vendor_name": vendor["name"],
        "currency": vendor.get("currency", "USD"),
    }
