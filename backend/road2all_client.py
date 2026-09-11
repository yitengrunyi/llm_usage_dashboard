"""
Road2All 平台客户端 (x.road2all.com)。
账号密码登录, cookie session, 响应封装在 retValue 中, 货币 USD。
拉数据走 POST /api/invoice/listInvoice  type=DAY 一次拿全。
"""
import threading
import time
import requests
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# 上游对 /api/user/login 有频率限制 (连续多次登录直接 "请勿频繁登录" 拒绝,
# backfill 按天 fetch 会一天一登录必炸) — 缓存 cookie session, 25 min TTL,
# 失败时 _invalidate 后重登一次。模式同 newapi_direct_client。
SESSION_TTL = 25 * 60
_session_cache: dict[str, tuple[requests.Session, float]] = {}
_session_lock = threading.Lock()


def _invalidate_session(vendor_id: str) -> None:
    with _session_lock:
        _session_cache.pop(vendor_id, None)


def _get_session(vendor: dict) -> requests.Session:
    vid = vendor["id"]
    now = time.time()
    with _session_lock:
        cached = _session_cache.get(vid)
        if cached and cached[1] > now:
            return cached[0]
    s = _login(vendor["base_url"], vendor["username"], vendor["password"])
    with _session_lock:
        _session_cache[vid] = (s, now + SESSION_TTL)
    return s


def _login(base_url: str, username: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(
        f"{base_url}/api/user/login",
        json={"username": username, "password": password},
        timeout=15,
    )
    data = r.json()
    if not data.get("success") or data.get("retCode") != 0:
        msg = data.get("retMsg") or "登录失败"
        raise PermissionError(f"登录失败: {msg}")
    return s


def _iso_to_date(iso_str: str) -> str:
    """ISO 8601 → YYYY-MM-DD (按 CST)。"""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST).strftime("%Y-%m-%d")


def fetch_logs(vendor: dict, start_time: str, end_time: str) -> list:
    """拉 [start, end] 区间所有 type=DAY 行.

    上游 2026-06 起加了 pageSize < 500 限制 (之前我们写 100000 一次性拉, 直接被拒).
    现在固定 pageSize=400 + 翻页, retValue.totalCount 给总数, 拿够就停.

    session 失效 (登录态被顶 / cookie 过期) 时重登一次再试; 仍失败才抛。
    """
    page_size = 400
    all_rows: list = []

    def _fetch_pages(s: requests.Session) -> None:
        page = 1
        while True:
            body = {
                "type": "DAY",
                "usageTimeMin": _iso_to_date(start_time),
                "usageTimeMax": _iso_to_date(end_time),
                "pageNumber": page,
                "pageSize": page_size,
            }
            r = s.post(f"{vendor['base_url']}/api/invoice/listInvoice", json=body, timeout=30)
            data = r.json()
            if not data.get("success") or data.get("retCode") != 0:
                msg = data.get("retMsg") or "拉取失败"
                raise RuntimeError(f"road2all 拉取失败: {msg}")
            ret = data.get("retValue") or {}
            rows = ret.get("list") or []
            all_rows.extend(rows)
            # 上游返 totalCount 字段就用它判停; 没有的话用 "返回 < pageSize 即最后一页" 兜底
            total = ret.get("totalCount")
            if total is not None:
                if len(all_rows) >= int(total):
                    break
            elif len(rows) < page_size:
                break
            page += 1
            if page > 100:  # 防御性: 4 万行该够任何窗口了, 真到了说明 api 行为异常
                raise RuntimeError(f"road2all 翻页超过 100 页 (累计 {len(all_rows)} 行), 怀疑 totalCount 错或 pageNumber 不生效")

    s = _get_session(vendor)
    try:
        _fetch_pages(s)
    except (RuntimeError, PermissionError):
        # session 可能中途失效 — 重登一次 (登录限流下不能多试)
        _invalidate_session(vendor["id"])
        s = _get_session(vendor)
        all_rows.clear()
        _fetch_pages(s)
    return all_rows


def _to_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _cache_fields(row: dict) -> tuple[int, int]:
    """从 usage 子对象提 (cache_read, cache_write 总量).

    cacheWrite 字段优先; 没有的话 cacheWrite5m + cacheWrite1h 相加 (Claude 系拆两档).
    上游 2026-08 起所有行都带 usage, 字段 null = 没用 cache, 按 0 算.
    """
    usage = row.get("usage") or {}
    cw = usage.get("cacheWrite")
    if cw is not None:
        cw_total = _to_int(cw)
    else:
        cw_total = _to_int(usage.get("cacheWrite5m")) + _to_int(usage.get("cacheWrite1h"))
    return _to_int(usage.get("cacheRead")), cw_total


def _prompt_full(row: dict) -> int:
    """完整 input (含 cache 读+写)。

    - 顶层 inputTokens 口径不统一 (GPT_TIER/GEMINI_TIER 含 cacheRead 但不含
      cacheWrite, CLAUDE 行两者都不含), 不能直接用。
    - usage 分量在 GEMINI_TIER 行上经常严重低于计费量 (实测 8/23 一行:
      inputTokens 41.8M 而 inputUncached=0) — 上游拆分在 tier 行不可靠。
    所以取 max(顶层, 分量和): 分量含 cacheWrite 时更大、可信; 顶层更大时
    它就是计费真值 (按它拟合上游 cost 公式误差 <2%)。
    """
    top = _to_int(row.get("inputTokens"))
    usage = row.get("usage")
    if not isinstance(usage, dict):
        return top
    cr, cw = _cache_fields(row)
    comp = _to_int(usage.get("inputUncached")) + cr + cw
    return max(top, comp)


def _row_metrics(row: dict) -> tuple[int, int, int, int, int, float]:
    """一行 invoice 的 (完整 input, output, cache_read, cache_write, requests, cost).

    aggregate_logs / aggregate_logs_by_account 共用 — 两条聚合口径必须一致.
    """
    prompt = _prompt_full(row)
    cache_read, cache_write = _cache_fields(row)
    completion = _to_int(row.get("outputTokens"))
    requests_count = _to_int(row.get("requests"))
    try:
        cost = float(row.get("cost") or 0)
    except (TypeError, ValueError):
        cost = 0.0
    return prompt, completion, cache_read, cache_write, requests_count, cost


def aggregate_logs(rows: list) -> dict:
    """list 已是 per-day per-account per-model 汇总条目, 按模型再聚合.

    上游同一窗口返回多种 type: TOKEN / GPT / GPT_TIER / CLAUDE / GEMINI_TIER /
    GEMINI_IMAGE (同一模型文本行和图片行分开算, 不重复). 官网账单 = 全部 type
    之和 — 只聚合 TOKEN 会漏 ~90% 金额 (2026-08 对账时踩的坑), 所以不过滤 type.
    """
    models = {}
    daily_map = {}

    for row in rows:
        model = row.get("modelName") or "unknown"
        (prompt, completion, cache_read, cache_write,
         requests_count, cost) = _row_metrics(row)
        date_str = row.get("dateTime") or "unknown"

        if model not in models:
            models[model] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
                "total_count": 0,
                "total_cost": 0.0,
            }
        models[model]["prompt_tokens"] += prompt
        models[model]["completion_tokens"] += completion
        models[model]["cache_read_tokens"] += cache_read
        models[model]["cache_write_tokens"] += cache_write
        models[model]["cache_tokens"] += cache_read + cache_write
        models[model]["total_tokens"] += prompt + completion
        models[model]["total_count"] += requests_count
        models[model]["total_cost"] += cost

        if date_str not in daily_map:
            daily_map[date_str] = {"date": date_str, "cost": 0.0}
        daily_map[date_str]["cost"] += cost

    for m in models.values():
        m["total_cost"] = round(m["total_cost"], 6)
    for d in daily_map.values():
        d["cost"] = round(d["cost"], 6)

    total_cost = round(sum(m["total_cost"] for m in models.values()), 6)
    daily = sorted(daily_map.values(), key=lambda x: x["date"])

    return {"total_cost": total_cost, "models": models, "daily": daily}


def aggregate_logs_by_account(rows: list) -> dict:
    """按 (account, 原始 model 名) 聚合 — "按 API key 筛选" 的数据源.

    invoice 行的 account 是上游子账号名 (prod / agent-prod 之类), 即控制台里
    每个账号一把 key — 这是 invoice 接口唯一的 key 粒度 (type=TOKEN/DETAIL/KEY
    等变体均 10002 拒绝, 2026-08-27 实测). 口径与 aggregate_logs 完全一致:
    全部 type 求和, prompt 为完整 input 含 cache.

    返回 {account: {model: {prompt_tokens, completion_tokens, cache_read_tokens,
    cache_write_tokens, total_tokens, total_count, total_cost}}}.
    account 缺失/空白的行跳过 (key 表 api_key NOT NULL; 实测 2025-12 起行行都有).
    """
    out: dict = {}
    for row in rows:
        account = (row.get("account") or "").strip()
        if not account:
            continue
        model = row.get("modelName") or "unknown"
        (prompt, completion, cache_read, cache_write,
         requests_count, cost) = _row_metrics(row)
        m = out.setdefault(account, {}).setdefault(model, {
            "prompt_tokens": 0, "completion_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "total_tokens": 0, "total_count": 0, "total_cost": 0.0,
        })
        m["prompt_tokens"] += prompt
        m["completion_tokens"] += completion
        m["cache_read_tokens"] += cache_read
        m["cache_write_tokens"] += cache_write
        m["total_tokens"] += prompt + completion
        m["total_count"] += requests_count
        m["total_cost"] += cost

    for models in out.values():
        for m in models.values():
            m["total_cost"] = round(m["total_cost"], 6)
    return out


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    rows = fetch_logs(
        vendor,
        start_time, end_time,
    )
    result = aggregate_logs(rows)
    result["vendor_id"] = vendor["id"]
    result["vendor_name"] = vendor["name"]
    result["currency"] = vendor.get("currency", "USD")
    return result
