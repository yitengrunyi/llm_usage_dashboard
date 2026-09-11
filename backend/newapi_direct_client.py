"""
new-api 平台直接 API 客户端（无需 Playwright，用账号密码登录）。
适用于不需要验证码的 new-api 平台，如 xhub.chat。

性能策略:
- session 内存缓存 30min TTL: 避免每次 fetch 都重新 login (上游对 login 限流非常严)
- 一次登录, session + user_id 在 logs / stat 两个调用间共享
- logs 翻页改并发: page 0 拿 total, 余下页 8 线程并发
- 顶层 logs + stat 2 线程并发
- 每个 GET 自带 401 retry 1 次, 兜上游 "首请求 401 怪 bug"
"""
import json
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from utils import normalize_model_name

CST = timezone(timedelta(hours=8))
PAGE_SIZE = 100  # xhub 实测上限 100 (传 10000 也只返回 100); 老注释说 10000 可能是其他实例
MAX_WORKERS = 8   # 翻页并发, 与 newapi_client.py 一致
SESSION_TTL = 1800  # 登录态本地缓存 30 min


def _parse_json(r: requests.Response, what: str) -> dict:
    """解析上游 JSON; 响应体非 JSON (空 / HTML 错误页 / 502) 时抛可读错, 不抛裸 JSONDecodeError.

    上游偶发 500/502 返空 body → r.json() 抛 'Expecting value: line 1 column 1 (char 0)',
    这个串前端看不懂也无法区分 (登录失效 / 限流 / 上游宕). 这里统一转成带 HTTP code + body 片段的
    RuntimeError, 让 run 的 error_msg 直接告诉人是上游炸了.
    """
    try:
        return r.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        body = (r.text or "").strip()
        preview = body[:120] if body else "<空响应>"
        raise RuntimeError(f"{what}: 上游返回非 JSON (HTTP {r.status_code}, body={preview!r})")


# (vendor_id) → (session, user_id, expire_ts)
_session_cache: dict[str, tuple[requests.Session, str, float]] = {}
_session_lock = threading.Lock()


def _do_login(base_url: str, username: str, password: str) -> tuple[requests.Session, str, float | None]:
    """裸登录, 不缓存。

    上游 new-api 实例登录返回有两种格式 (2026-08 起 xhub/api7 改版, 更早的实例是旧版):
    - 新版 (JWT): data = {access_token, user:{id}, token_type:"Bearer", session:{...}}
      鉴权靠 Authorization: Bearer <token>, cookie 已失效 → 必须把 token 写进 session 默认 header
    - 旧版 (cookie): data = {id, ...}, session cookie 自动带 → 不需要额外 header
    两种都兼容: 优先取新字段, 缺了再 fallback 旧的。
    """
    s = requests.Session()
    r = s.post(f"{base_url}/api/user/login", json={"username": username, "password": password}, timeout=30)
    data = _parse_json(r, "登录")
    if not data.get("success"):
        raise PermissionError(f"登录失败: {data.get('message', 'unknown error')}")
    payload = data.get("data") or {}
    # 新版 JWT: user.id + access_token (Bearer), cookie 不再生效
    user_obj = payload.get("user") or {}
    access_token = payload.get("access_token")
    token_expire_ts: float | None = None
    if "id" in user_obj and access_token:
        user_id = str(user_obj["id"])
        s.headers.update({"Authorization": f"Bearer {access_token}"})
        # JWT 过期时间 (access_expires_at, 绝对 unix ts) — 比本地 SESSION_TTL 短 (~15min),
        # 不跟着收短缓存, 缓存里的 token 会先过期 → 后续请求全 401。
        token_expire_ts = payload.get("access_expires_at")
        if token_expire_ts is not None:
            token_expire_ts = float(token_expire_ts) - 60  # 60s buffer 兜时钟漂移
    else:
        # 旧版 cookie 鉴权: id 在 data 顶层, 无 token 过期问题
        user_id = str(payload["id"])
    return s, user_id, token_expire_ts


def _get_session(vendor: dict) -> tuple[requests.Session, str]:
    """走缓存, 缺失/过期才重 login。"""
    vid = vendor["id"]
    now = time.time()
    with _session_lock:
        cached = _session_cache.get(vid)
        if cached and cached[2] > now:
            return cached[0], cached[1]

    s, uid, token_exp = _do_login(vendor["base_url"], vendor["username"], vendor["password"])
    # JWT 模式下 token 比 SESSION_TTL 短, 取两者更早到期的那个, 避免缓存里揣着过期 token
    expire = min(now + SESSION_TTL, token_exp) if token_exp else now + SESSION_TTL
    with _session_lock:
        _session_cache[vid] = (s, uid, expire)
    return s, uid


def _invalidate_session(vendor: dict) -> None:
    with _session_lock:
        _session_cache.pop(vendor["id"], None)


# 兼容老调用 (其它模块可能 import _login)
def _login(base_url: str, username: str, password: str) -> tuple[requests.Session, str]:
    s, uid, _ = _do_login(base_url, username, password)
    return s, uid


def _get_json(session: requests.Session, url: str, headers: dict, timeout: int = 30) -> dict:
    """GET + json. 兜某些 new-api 实例 'login 后每个唯一 URL 首次访问必 401' 的怪 bug:
    GET 一次, 看到 401 立刻再 GET 一次复用同一 session, 第二次几乎必 200。"""
    r = session.get(url, headers=headers, timeout=timeout)
    if r.status_code == 401:
        r = session.get(url, headers=headers, timeout=timeout)
    return _parse_json(r, url.rsplit("/", 1)[-1])


def _fetch_page(session: requests.Session, base_url: str, headers: dict,
                base_params: str, page: int) -> tuple[list, int]:
    """单页 GET, 返回 (items, total)。

    非 auth 失败返回 ([], -1) — 用 total=-1 标记"这页没拿到", 跟 total=0 (真没有)
    区分开, 让上层能判断完整性。
    """
    j = _get_json(session,
                  f"{base_url}/api/log/self?{base_params}&p={page}&page_size={PAGE_SIZE}",
                  headers)
    if not j.get("success"):
        msg = j.get("message", "")
        if "未登录" in msg or "过期" in msg or "无权" in msg:
            raise PermissionError("登录态已过期，请重新登录")
        return [], -1
    d = j.get("data") or {}
    items = d.get("items", []) if isinstance(d, dict) else (d or [])
    total = int(d.get("total", 0)) if isinstance(d, dict) else 0
    return items, total


def fetch_logs(session: requests.Session, base_url: str, user_id: str, user_header: str,
               start_time: str = None, end_time: str = None) -> tuple[list, bool]:
    """先 page 0 拿 total, 后续页并发抓。返回 (logs, complete)。

    complete=False 表示翻页有缺 (单页失败被跳过 / 行数少于 total) —
    key 表这种"整段替换"语义的写入方必须据此跳过, 不然会把完整数据换成残缺数据。
    """
    headers = {user_header: user_id}
    parts = ["type=2"]
    if start_time:
        parts.append(f"start_timestamp={int(datetime.fromisoformat(start_time).timestamp())}")
    if end_time:
        parts.append(f"end_timestamp={int(datetime.fromisoformat(end_time).timestamp())}")
    base_params = "&".join(parts)

    first_items, total = _fetch_page(session, base_url, headers, base_params, 0)
    if total < 0:
        return [], False
    if total <= len(first_items):
        return first_items, True

    n_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    all_logs: list = list(first_items)
    complete = True

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_fetch_page, session, base_url, headers, base_params, p): p
                   for p in range(1, n_pages)}
        for fut in as_completed(futures):
            try:
                items, _ = fut.result()
                all_logs.extend(items)
            except PermissionError:
                raise
            except Exception:
                complete = False  # 单页失败跳过, 不整批 fail, 但标记不完整
    if len(all_logs) < total:
        complete = False  # 页数齐了但行数不够 (上游 LRU 已删)
    return all_logs, complete


def aggregate_logs(logs: list, quota_per_dollar: int = 500000) -> dict:
    """将日志聚合为统一格式。"""
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


def fetch_stat_total(session: requests.Session, base_url: str, user_id: str, user_header: str,
                     start_time: str | None, end_time: str | None) -> int:
    """直接调 /api/log/self/stat 拿整窗口 quota 总额, 与平台 dashboard 一致。"""
    headers = {user_header: user_id}
    parts = []
    if start_time:
        parts.append(f"start_timestamp={int(datetime.fromisoformat(start_time).timestamp())}")
    if end_time:
        parts.append(f"end_timestamp={int(datetime.fromisoformat(end_time).timestamp())}")
    qs = ("?" + "&".join(parts)) if parts else ""
    j = _get_json(session, f"{base_url}/api/log/self/stat{qs}", headers)
    if not j.get("success"):
        return 0
    return int((j.get("data") or {}).get("quota") or 0)


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    """获取供应商用量数据（统一格式）。

    优先 /api/data/self (server 端按 model+hour 预聚合, 大账号 30 天 18s)
    旧 fallback: /api/log/self raw log 分页 (大账号几百万行, 几分钟级别)
    /api/data/self 不区分 prompt/completion 也不返 cache 拆分, 拿不到细分
    维度 — 想要 cache 明细只能走 fallback (前端可以加个开关).
    """
    quota_per_dollar = vendor.get("quota_per_dollar", 500000)
    user_header = vendor.get("user_header", "New-Api-User")

    def _do_fetch_data_self():
        session, user_id = _get_session(vendor)
        start_ts = int(datetime.fromisoformat(start_time).timestamp())
        end_ts = int(datetime.fromisoformat(end_time).timestamp())
        r = session.get(
            f"{vendor['base_url']}/api/data/self",
            params={"start_timestamp": start_ts, "end_timestamp": end_ts},
            headers={user_header: user_id},
            timeout=90,
        )
        j = _parse_json(r, "/api/data/self")
        if not j.get("success"):
            msg = j.get("message", "")
            if "未登录" in msg or "过期" in msg or "无权" in msg:
                raise PermissionError("登录态已过期")
            raise RuntimeError(f"/api/data/self 失败: {msg or j}")
        return j.get("data") or []

    try:
        rows = _do_fetch_data_self()
    except PermissionError:
        _invalidate_session(vendor)
        rows = _do_fetch_data_self()

    # 复用 newapi_client.aggregate_data_self (跟 blueshirt 同款聚合)
    from newapi_client import aggregate_data_self
    result = aggregate_data_self(rows, quota_per_dollar)

    result["vendor_id"] = vendor["id"]
    result["vendor_name"] = vendor["name"]
    result["currency"] = vendor.get("currency", "USD")
    return result

