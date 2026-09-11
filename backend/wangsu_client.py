"""网宿 sharkletAIgateway 客户端 (按用户勾选的模型 fan-out)。

清单/勾选状态在 wangsu_models.json:
  {"models": [{"code": "gpt-4.1", "enabled": true}, ...]}
通过 /api/wangsu/models CRUD + PATCH 编辑。

行为:
  - 只对 enabled=true 的模型并发查
  - 网宿 API 硬限单次 ≤ 31 天, 超过时自动拆窗口并发再 merge
  - 费用接口不返回, 暂留 0
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from wangsu_pricing import compute_cost_cny
from utils import normalize_model_name

CST = timezone(timedelta(hours=8))
HOST = "open.chinanetcenter.com"
URI = "/myview/sharkletAIgateway"
ALGO = "CNC-HMAC-SHA256"
SIGNED_HEADERS = "content-type;host"
MAX_CONCURRENT = 8           # 网宿并发限速, 实测 >10 触发 446
MAX_DAYS_PER_WINDOW = 31     # 网宿单次时间跨度上限
RETRY_STATUSES = {429, 446}  # 退避重试码
MAX_RETRIES = 3
MODELS_FILE = Path(__file__).parent / "wangsu_models.json"

log = logging.getLogger("wangsu_client")


def load_models() -> list[dict]:
    return json.loads(MODELS_FILE.read_text())["models"]


def save_models(models: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for m in models:
        code = (m.get("code") or "").strip()
        if not code:
            continue
        seen[code] = {"code": code, "enabled": bool(m.get("enabled", False))}
    out = sorted(seen.values(), key=lambda x: (not x["enabled"], x["code"]))
    MODELS_FILE.write_text(json.dumps({"models": out}, ensure_ascii=False, indent=2))
    return out


def _enabled_codes(models: list[dict]) -> list[str]:
    return [m["code"] for m in models if m.get("enabled")]


def _sign(ak: str, sk: str, body: str) -> dict:
    ts = str(int(time.time()))
    h = {
        "content-type": "application/json",
        "host": HOST,
        "x-cnc-accessKey": ak,
        "x-cnc-timestamp": ts,
        "x-cnc-auth-method": "AKSK",
    }
    canon_h = "".join(f"{k}:{h[k].lower()}\n" for k in SIGNED_HEADERS.split(";"))
    payload_hash = hashlib.sha256(body.encode()).hexdigest()
    cr = f"POST\n{URI}\n\n{canon_h}\n{SIGNED_HEADERS}\n{payload_hash}"
    sts = f"{ALGO}\n{ts}\n{hashlib.sha256(cr.encode()).hexdigest()}"
    sig = hmac.new(sk.encode(), sts.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = f"{ALGO} Credential={ak}, SignedHeaders={SIGNED_HEADERS}, Signature={sig}"
    return h


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _iso_to_date(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(CST).strftime("%Y-%m-%d")


def _model_entry(date_block: dict) -> dict:
    """注: prompt_tokens 暴露的是 *非缓存* 输入 (扣掉 cache_read 和 cache_write),
    避免前端 input+output+cache 双算。pricing 自己用 totalPromptTokens 全量。"""
    cache_read = _safe_int(date_block.get("totalPromptCacheTokens"))
    cache_write = _safe_int(date_block.get("totalPromptCreationTokens"))
    full_prompt = _safe_int(date_block.get("totalPromptTokens"))
    return {
        "prompt_tokens": max(0, full_prompt - cache_read - cache_write),
        "completion_tokens": _safe_int(date_block.get("totalCompleteTokens")),
        "cache_tokens": cache_read,            # 兼容老字段名 (= cache_read)
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": _safe_int(date_block.get("totalTokens")),
        "total_count": _safe_int(date_block.get("totalVisit")),
        "total_cost": 0.0,
        "image_count": _safe_int(date_block.get("totalInputImageNum")),
        "_full_prompt": full_prompt,           # 内部: pricing 用全量, API 序列化前去掉
    }


def _empty_entry() -> dict:
    return {k: (0 if k != "total_cost" else 0.0) for k in
            ("prompt_tokens", "completion_tokens", "cache_tokens", "cache_read_tokens",
             "cache_write_tokens", "total_tokens", "total_count", "total_cost", "image_count")} | {"priced": False}


def _add_entry(a: dict, b: dict) -> dict:
    for k in a:
        if k == "priced":
            a[k] = a[k] or bool(b.get(k))
        else:
            a[k] = a[k] + b.get(k, 0)
    return a


def _split_windows(startdate: str, enddate: str, max_days: int = MAX_DAYS_PER_WINDOW) -> list[tuple[str, str]]:
    s = datetime.fromisoformat(startdate)
    e = datetime.fromisoformat(enddate)
    out = []
    cur = s
    while cur <= e:
        win_end = min(cur + timedelta(days=max_days - 1), e)
        out.append((cur.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d")))
        cur = win_end + timedelta(days=1)
    return out


# 网宿 chartDataList series 名 → 我们关心的 token 维度
_DAILY_SERIES = {
    "promptTokens": "prompt",
    "completeTokens": "completion",
    "promptCacheTokens": "cache_read",
    "cacheCreate5mTokens": "cache_5m",
    "cacheCreate1hTokens": "cache_1h",
    "promptCreationTokens": "prompt_creation",
    "totalVisit": "count",
}


def _series_points(series: dict) -> list:
    """网宿单天查询时 data 是 dict, 多天是 list, 这里统一成 list。"""
    d = series.get("data")
    if isinstance(d, dict):
        return [d]
    return d or []


def _daily_tokens(date_block: dict) -> dict[str, dict]:
    """从单模型响应里抽出每日 token, 返回 {day: {prompt, completion, cache_read, ...}}。"""
    out: dict[str, dict] = {}
    for s in (date_block.get("chartDataList") or []):
        key = _DAILY_SERIES.get(s.get("name"))
        if not key:
            continue
        for p in _series_points(s):
            day = (p.get("time") or "")[:10]
            if not day:
                continue
            out.setdefault(day, {k: 0 for k in _DAILY_SERIES.values()})[key] = _safe_int(p.get("text"))
    return out


async def _post_one(client: httpx.AsyncClient, ak: str, sk: str, sem: asyncio.Semaphore, body: dict) -> dict:
    body_s = json.dumps(body)
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        async with sem:
            try:
                r = await client.post(
                    f"https://{HOST}{URI}",
                    headers=_sign(ak, sk, body_s),
                    content=body_s,
                    timeout=60,
                )
            except httpx.HTTPError as e:
                last_err = e
                r = None
        if r is not None and r.status_code not in RETRY_STATUSES:
            r.raise_for_status()
            return r.json()["provider"]["date"]
        # 退避: 0.5s, 1.5s, 3.5s
        await asyncio.sleep(0.5 + attempt * 1.0)
        if r is not None:
            if r.status_code == 446 or "AccountApiTooFrequence" in r.text:
                last_err = httpx.HTTPStatusError(
                    "网宿账号级 API 限流 (446 WPLUS_AccountApiTooFrequence), 请稍等 30s~1min 再查",
                    request=r.request, response=r,
                )
            else:
                last_err = httpx.HTTPStatusError(f"http {r.status_code}: {r.text[:200]}", request=r.request, response=r)
    if last_err:
        raise last_err
    raise RuntimeError("post_one: unreachable")


async def _fetch_window(client, ak, sk, sem, startdate, enddate, codes) -> tuple[dict, dict[str, dict], dict[str, float], list[str]]:
    """单 31-天窗口: 返回 (aggregate_block, {model: model_entry}, {day: cost_cny}, failed_codes)"""
    base = {
        "startdate": startdate,
        "enddate": enddate,
        "dataformat": "json",
        "returnType": "1_day",
        "datatype": "all",
    }
    agg_t = _post_one(client, ak, sk, sem, base)
    m_ts = [_post_one(client, ak, sk, sem, {**base, "model": m}) for m in codes]
    results = await asyncio.gather(agg_t, *m_ts, return_exceptions=True)
    agg, m_results = results[0], list(results[1:])
    if isinstance(agg, Exception):
        raise agg

    # 失败的 code 不静默丢弃: 串行补查一遍后回填. 并发抓取时大模型 (如 opus-4-8,
    # 响应 2MB+ / 单查 ~17s) 易撞网宿 446 限流或超时, 串行单查能救回. 补查仍失败的
    # 记进 failed 上抛, 让上层标记"部分失败"而不是绿色 success 缺数据.
    failed: list[str] = []
    for i, (code, r) in enumerate(zip(codes, m_results)):
        if not isinstance(r, Exception):
            continue
        try:
            m_results[i] = await _post_one(client, ak, sk, sem, {**base, "model": code})
        except Exception as e:
            failed.append(code)
            log.warning("[wangsu] 模型 %s 窗口 %s~%s 并发+串行补查均失败: %s",
                        code, startdate, enddate, e)
        await asyncio.sleep(1.0)

    out: dict[str, dict] = {}
    daily_cost: dict[str, float] = {}
    for code, r in zip(codes, m_results):
        if isinstance(r, Exception):
            continue
        if _safe_int(r.get("totalVisit")) == 0:
            continue
        entry = _model_entry(r)
        # 用 LiteLLM 定价库算 CNY 费用 (匹配不到的 cost=0)
        # 注: 给 pricing 的 prompt_tokens 必须是全量 (含 cache), 因为 calculator 内部会扣 cache 算 regular_input
        cost_cny, matched, _curr = compute_cost_cny(
            code,
            prompt_tokens=entry["_full_prompt"],
            completion_tokens=entry["completion_tokens"],
            cache_read_tokens=entry["cache_read_tokens"],
            cache_creation_5m_tokens=_safe_int(r.get("totalCacheCreate5mTokens")),
            cache_creation_1h_tokens=_safe_int(r.get("totalCacheCreate1hTokens")),
            cache_creation_tokens=_safe_int(r.get("totalPromptCreationTokens"))
            - _safe_int(r.get("totalCacheCreate5mTokens"))
            - _safe_int(r.get("totalCacheCreate1hTokens")),
            request_count=entry["total_count"],
        )
        entry["total_cost"] = round(cost_cny, 4)
        entry["priced"] = matched is not None
        entry.pop("_full_prompt", None)        # 清掉内部字段, 不暴露给前端
        out[code] = entry

        # 按天再算一次, 用于 daily 折线
        if matched is not None:
            for day, t in _daily_tokens(r).items():
                if t["count"] == 0:
                    continue
                d_cost, _, _ = compute_cost_cny(
                    code,
                    prompt_tokens=t["prompt"],
                    completion_tokens=t["completion"],
                    cache_read_tokens=t["cache_read"],
                    cache_creation_5m_tokens=t["cache_5m"],
                    cache_creation_1h_tokens=t["cache_1h"],
                    cache_creation_tokens=max(0, t["prompt_creation"] - t["cache_5m"] - t["cache_1h"]),
                    request_count=t["count"],
                )
                daily_cost[day] = daily_cost.get(day, 0.0) + d_cost
    return agg, out, daily_cost, failed


async def _fetch_all(ak: str, sk: str, startdate: str, enddate: str, codes: list[str]) -> tuple[list[dict], dict[str, dict], dict[str, float], list[str]]:
    """支持跨多个 31 天窗口, 自动 merge。返回 (aggs, models, daily_cost, failed_codes)。"""
    windows = _split_windows(startdate, enddate)
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with httpx.AsyncClient() as client:
        tasks = [_fetch_window(client, ak, sk, sem, ws, we, codes) for ws, we in windows]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    aggs: list[dict] = []
    merged_models: dict[str, dict] = {}
    merged_daily: dict[str, float] = {}
    failed: set[str] = set()
    for r in results:
        if isinstance(r, Exception):
            raise r
        agg, models_in_window, daily_in_window, failed_in_window = r
        aggs.append(agg)
        for code, entry in models_in_window.items():
            merged_models.setdefault(code, _empty_entry())
            _add_entry(merged_models[code], entry)
        for day, c in daily_in_window.items():
            merged_daily[day] = merged_daily.get(day, 0.0) + c
        failed.update(failed_in_window)
    # 某 code 在别的窗口成功拿到就不算失败 (跨窗口看)
    failed -= set(merged_models.keys())
    return aggs, merged_models, merged_daily, sorted(failed)


def _merged_daily(aggs: list[dict], startdate: str, enddate: str, daily_cost: dict[str, float] | None = None) -> list:
    """从 chartDataList 拿日期骨架 + 补满区间, cost 来自传入的 daily_cost 映射。"""
    daily_cost = daily_cost or {}
    daily: dict[str, dict] = {}
    for agg in aggs:
        for s in (agg.get("chartDataList") or []):
            if s.get("name") != "totalVisit":
                continue
            for p in _series_points(s):
                day = (p.get("time") or "")[:10]
                if day:
                    daily.setdefault(day, {"date": day, "cost": 0.0})
            break
    cur = datetime.fromisoformat(startdate)
    end = datetime.fromisoformat(enddate)
    while cur <= end:
        day = cur.strftime("%Y-%m-%d")
        daily.setdefault(day, {"date": day, "cost": 0.0})
        cur += timedelta(days=1)
    for day, c in daily_cost.items():
        if day in daily:
            daily[day]["cost"] = round(c, 4)
    return sorted(daily.values(), key=lambda x: x["date"])


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    startdate = _iso_to_date(start_time)
    enddate = _iso_to_date(end_time)
    codes = _enabled_codes(load_models())

    if not codes:
        # 没勾任何模型: 退化为只返回 daily/totals 0 占位, 避免空白报错
        return {
            "total_cost": 0.0,
            "currency": vendor.get("currency", "CNY"),
            "models": {},
            "daily": _merged_daily([], startdate, enddate),
            "vendor_id": vendor["id"],
            "vendor_name": vendor["name"],
            "unpriced_models": [],
            "failed_models": [],
        }

    aggs, models, daily_cost, failed_codes = asyncio.run(
        _fetch_all(vendor["access_key"], vendor["secret_key"], startdate, enddate, codes)
    )

    total_cost = round(sum(m.get("total_cost", 0) for m in models.values()), 4)
    unpriced_models = sorted(
        code for code, m in models.items()
        if not m.get("priced") and m.get("total_count", 0) > 0
    )

    # 出口处归一化模型名: 同 normalize key 的合并 (anthropic.claude-opus-4-6 +
    # claude-opus-4-6-200k → claude-opus-4-6). 内部 code 不归一化, 因为定价
    # lookup 用的就是原 code, 改了就匹配不到 LiteLLM 价目.
    normalized: dict[str, dict] = {}
    for code, m in models.items():
        key = normalize_model_name(code) or code
        if key in normalized:
            dst = normalized[key]
            for k, v in m.items():
                if isinstance(v, (int, float)) and isinstance(dst.get(k), (int, float)):
                    dst[k] += v
            dst["priced"] = dst.get("priced") or m.get("priced")
        else:
            normalized[key] = dict(m)
    unpriced_normalized = sorted({normalize_model_name(c) or c for c in unpriced_models})
    failed_normalized = sorted({normalize_model_name(c) or c for c in failed_codes})
    if failed_normalized:
        log.warning("[wangsu] %s~%s 部分模型抓取失败 (数据不完整, 未计入): %s",
                    startdate, enddate, failed_normalized)

    return {
        "total_cost": total_cost,
        "currency": vendor.get("currency", "CNY"),
        "models": normalized,
        "daily": _merged_daily(aggs, startdate, enddate, daily_cost),
        "vendor_id": vendor["id"],
        "vendor_name": vendor["name"],
        "unpriced_models": unpriced_normalized,
        "failed_models": failed_normalized,
    }
