"""
new-api 平台数据获取客户端。
登录用 Playwright（处理验证码），查询用 requests + cookie（快速）。
"""
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone, timedelta
from utils import normalize_model_name

STATE_DIR = Path(__file__).parent / "config" / "sessions"
CST = timezone(timedelta(hours=8))
PAGE_SIZE = 100      # 服务端硬卡, Apevon 实测 >100 仍返回 100
MAX_WORKERS = 3      # 大账号 (used_quota 千亿级) 实测 >3 路触发 throttle


def _get_state_file(base_url: str) -> Path:
    from urllib.parse import urlparse
    domain = urlparse(base_url).hostname.replace(".", "_")
    return STATE_DIR / f"{domain}.json"


def _load_session(state_file: Path) -> tuple[dict, str, str]:
    """从保存的 state 文件提取 cookies、user_id、user_header。"""
    with open(state_file, "r") as f:
        data = json.load(f)

    cookies = {c["name"]: c["value"] for c in data.get("cookies", [])}

    user_id = None
    user_header = "Rix-Api-User"  # 默认
    for origin in data.get("origins", []):
        for item in origin.get("localStorage", []):
            if item["name"] == "user":
                user_id = str(json.loads(item["value"])["id"])

    if not user_id:
        raise ValueError("无法从登录态文件中获取用户 ID，请重新登录")

    return cookies, user_id, user_header


def fetch_logs(base_url: str, start_time: str = None, end_time: str = None, user_header: str | None = None) -> list:
    """
    用保存的 cookie 直接请求 API 获取日志（不启动浏览器）。
    user_header: 不同 new-api 部署使用不同的 user-id 头名 (默认 Rix-Api-User, Apevon 是 New-Api-User)。

    策略: 大账号 (used_quota 千亿+) 1 天 page 0 都 60s 不返,
    所以把整窗口先切成若干 chunk (默认 6h), 每 chunk 内再分页并发. chunk 之间也并发.
    小窗口请求 server 端 query 更快, 单次 timeout 概率大幅下降.
    """
    state_file = _get_state_file(base_url)
    if not state_file.exists():
        raise FileNotFoundError(
            f"未找到登录态文件: {state_file}\n请先点击登录按钮"
        )

    cookies, user_id, default_header = _load_session(state_file)
    header_name = user_header or default_header
    headers = {header_name: user_id}

    if not start_time or not end_time:
        # 没传时间段就退化成单次 (老路径), 走默认窗口
        return _fetch_window(base_url, cookies, headers, None, None)

    start_ts = int(datetime.fromisoformat(start_time).timestamp())
    end_ts = int(datetime.fromisoformat(end_time).timestamp())
    # 6h 一段; 总窗口 <=6h 就不切, 退化为单次
    CHUNK = 6 * 3600
    if end_ts - start_ts <= CHUNK:
        return _fetch_window(base_url, cookies, headers, start_ts, end_ts)

    chunks: list[tuple[int, int]] = []
    cur = start_ts
    while cur < end_ts:
        chunks.append((cur, min(cur + CHUNK, end_ts)))
        cur += CHUNK

    all_logs: list = []
    # 实测 blueshirt server 对并发查询有 throttle: 2 路并发也会让 3/4 chunk 60s timeout.
    # 只能 chunk 之间串行, chunk 内分页还能并发 (内部 8 路是同一 chunk 的不同页,
    # server 端缓存一次 query 拆页, 不触发限流). 1 天 = 4 chunk × ~12s = ~50s, 可接受.
    for s, e in chunks:
        try:
            all_logs.extend(_fetch_window(base_url, cookies, headers, s, e))
        except PermissionError:
            raise
        except Exception:
            pass  # 单 chunk fail 不整批跪
    return all_logs


def _fetch_window(base_url: str, cookies: dict, headers: dict,
                  start_ts: int | None, end_ts: int | None) -> list:
    """拉一个时间窗口内全部日志: page 0 拿 total, 余下页并发抓。"""
    base_params = {"type": 2, "page_size": PAGE_SIZE}
    if start_ts is not None:
        base_params["start_timestamp"] = start_ts
    if end_ts is not None:
        base_params["end_timestamp"] = end_ts

    def _get_page(p: int) -> tuple[list, int]:
        r = requests.get(
            f"{base_url}/api/log/self",
            params={**base_params, "p": p},
            cookies=cookies, headers=headers, timeout=60,
        )
        j = r.json()
        if not j.get("success"):
            msg = j.get("message", "")
            if "未登录" in msg or "过期" in msg or "无权" in msg:
                raise PermissionError("登录态已过期，请重新登录")
            return [], 0
        d = j.get("data") or {}
        return (d.get("items") or []), int(d.get("total") or 0)

    first_items, total = _get_page(0)
    if total <= len(first_items):
        return first_items

    n_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    all_logs: list = list(first_items)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_get_page, p): p for p in range(1, n_pages)}
        for fut in as_completed(futures):
            try:
                items, _ = fut.result()
                all_logs.extend(items)
            except PermissionError:
                raise
            except Exception:
                pass
    return all_logs


def aggregate_logs(logs: list, quota_per_dollar: int = 500000) -> dict:
    """将日志聚合为统一格式：按模型汇总 + 按天汇总。"""
    models = {}
    daily_map = {}

    for log in logs:
        if log.get("type") != 2:
            continue

        model = normalize_model_name(log.get("model_name", "unknown"))
        prompt = log.get("prompt_tokens", 0)
        completion = log.get("completion_tokens", 0)
        quota = log.get("quota", 0)
        created_at = log.get("created_at", 0)

        # other 是 JSON 字符串, 缓存信息在里面: cache_tokens=读, cache_creation_tokens=写
        cache_read = 0
        cache_write = 0
        other = log.get("other")
        if isinstance(other, str) and other:
            try:
                other_obj = json.loads(other)
                cache_read = int(other_obj.get("cache_tokens") or 0)
                cache_write = int(other_obj.get("cache_creation_tokens") or 0)
            except (ValueError, TypeError):
                pass

        cost = quota / quota_per_dollar

        if model not in models:
            models[model] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
                "total_count": 0,
                "total_cost": 0,
            }
        models[model]["prompt_tokens"] += max(0, prompt - cache_read - cache_write)  # 非缓存 input
        models[model]["completion_tokens"] += completion
        models[model]["cache_tokens"] += cache_read
        models[model]["cache_read_tokens"] += cache_read
        models[model]["cache_write_tokens"] += cache_write
        models[model]["total_tokens"] += prompt + completion  # prompt 已含缓存, 不双算
        models[model]["total_count"] += 1
        models[model]["total_cost"] += cost

        date_str = datetime.fromtimestamp(created_at, CST).strftime("%Y-%m-%d") if created_at else "unknown"
        if date_str not in daily_map:
            daily_map[date_str] = {"date": date_str, "cost": 0}
        daily_map[date_str]["cost"] += cost

    for m in models.values():
        m["total_cost"] = round(m["total_cost"], 6)
    for d in daily_map.values():
        d["cost"] = round(d["cost"], 6)

    total_cost = round(sum(m["total_cost"] for m in models.values()), 6)
    daily = sorted(daily_map.values(), key=lambda x: x["date"])

    return {"total_cost": total_cost, "models": models, "daily": daily}


def fetch_stat_total(base_url: str, start_time: str | None, end_time: str | None, user_header: str | None = None) -> int:
    """直接调 /api/log/self/stat 拿整窗口 quota 总额, 与平台 dashboard 一致。"""
    state_file = _get_state_file(base_url)
    if not state_file.exists():
        raise FileNotFoundError(f"未找到登录态文件: {state_file}\n请先点击登录按钮")
    cookies, user_id, default_header = _load_session(state_file)
    headers = {(user_header or default_header): user_id}
    params = {}
    if start_time:
        params["start_timestamp"] = int(datetime.fromisoformat(start_time).timestamp())
    if end_time:
        params["end_timestamp"] = int(datetime.fromisoformat(end_time).timestamp())
    r = requests.get(f"{base_url}/api/log/self/stat", params=params, cookies=cookies, headers=headers, timeout=60)
    j = r.json()
    if not j.get("success"):
        msg = j.get("message", "")
        if "未登录" in msg or "过期" in msg or "无权" in msg:
            raise PermissionError("登录态已过期，请重新登录")
        return 0
    return int((j.get("data") or {}).get("quota") or 0)


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    """获取 new-api 供应商的用量数据（统一格式）。

    优先用 /api/data/self —— blueshirt dashboard 自己用的预聚合接口, server 端按
    (model, hour) 已聚合, 30 天 32s 返 6500 行. 比拉 raw /api/log/self 快 100x+,
    大账号 (千亿 quota) 唯一可行路径。
    /api/data/self 不返 cache_tokens 拆分 (字段是 token_used 总数), 拿不到 cache
    明细. 想要的话只能 fallback 走 fetch_logs.
    """
    base_url = vendor["base_url"]
    quota_per_dollar = vendor.get("quota_per_dollar", 500000)
    user_header = vendor.get("user_header")

    state_file = _get_state_file(base_url)
    if not state_file.exists():
        raise FileNotFoundError(f"未找到登录态文件: {state_file}\n请先点击登录按钮")
    cookies, user_id, default_header = _load_session(state_file)
    headers = {
        (user_header or default_header): user_id,
        # 部分 new-api 实例 (e.g. blueshirt) 的 WAF 会拒非浏览器 UA, 主动伪装
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    start_ts = int(datetime.fromisoformat(start_time).timestamp())
    end_ts = int(datetime.fromisoformat(end_time).timestamp())

    r = requests.get(
        f"{base_url}/api/data/self",
        params={"start_timestamp": start_ts, "end_timestamp": end_ts},
        cookies=cookies, headers=headers, timeout=90,
    )
    j = r.json()
    if not j.get("success"):
        msg = j.get("message", "")
        if "未登录" in msg or "过期" in msg or "无权" in msg or "session_expired" in str(j.get("code", "")):
            raise PermissionError("登录态已过期，请重新登录")
        raise RuntimeError(f"/api/data/self 失败: {msg or j}")

    rows = j.get("data") or []
    result = aggregate_data_self(rows, quota_per_dollar)

    result["vendor_id"] = vendor["id"]
    result["vendor_name"] = vendor["name"]
    result["currency"] = vendor.get("currency", "USD")
    return result


def aggregate_data_self(rows: list, quota_per_dollar: int = 500000) -> dict:
    """/api/data/self 返预聚合行 [(model_name, count, quota, token_used, created_at)],
    按 model + day group 成前端要的格式。"""
    models: dict = {}
    daily_map: dict = {}
    for r in rows:
        name = normalize_model_name(r.get("model_name", "unknown"))
        count = int(r.get("count") or 0)
        quota = int(r.get("quota") or 0)
        tokens = int(r.get("token_used") or 0)
        created_at = int(r.get("created_at") or 0)
        cost = quota / quota_per_dollar

        m = models.setdefault(name, {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 0,
            "total_count": 0,
            "total_cost": 0.0,
        })
        # /api/data/self 不分 prompt/completion, 全塞 total_tokens.
        # 前端列里 prompt/completion 会显示 0, 这是 trade-off (换 30 天能查).
        m["total_tokens"] += tokens
        m["total_count"] += count
        m["total_cost"] += cost

        date_str = datetime.fromtimestamp(created_at, CST).strftime("%Y-%m-%d") if created_at else "unknown"
        d = daily_map.setdefault(date_str, {"date": date_str, "cost": 0.0})
        d["cost"] += cost

    for m in models.values():
        m["total_cost"] = round(m["total_cost"], 6)
    for d in daily_map.values():
        d["cost"] = round(d["cost"], 6)

    total_cost = round(sum(m["total_cost"] for m in models.values()), 6)
    daily = sorted(daily_map.values(), key=lambda x: x["date"])
    return {"total_cost": total_cost, "models": models, "daily": daily}
