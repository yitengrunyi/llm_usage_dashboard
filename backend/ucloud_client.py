"""UCloud UModelVerse 计费客户端。

接口:
- GetUserBillingByKey      → 时间窗口费用 (CNY)。Items 只按 ResourceId 聚合, 无时间字段,
                              所以 daily 折线只能按天循环调用 (UCloud 自己的 console 也是这么做的)
- GetUMInferTokenUsage     → 时间窗口的 InTotal/OutTotal/RequestTotal (准确, 尊重窗口)
                              Usages[] 给每个模型的 in/out token 数 (真实, 非 sample)
                              注: 长窗口 (>~10 天) 服务端超时, 需切片

策略:
- Phase 1: 按天调 GetUserBillingByKey, 同时拿到 daily 折线 + total_cost + KeyId 列表 (Items 里有
           ResourceId)。无上游 whole-range billing — 实测 30 天/90 天的 whole-range 调用 server-side
           走慢路径要 50+ 秒, 而按天调用每次都是 0.x 秒, 并发跑总耗时反而只要 1~2s。
- Phase 2: KeyId × 10 天切片 fan-out 调 GetUMInferTokenUsage 拿 per-model token / 调用次数。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter

from ucloud.client import Client
from ucloud.core import exc
from ucloud.core.transport import _requests as _ucloud_requests_transport

CST = timezone(timedelta(hours=8))
MAX_WORKERS = 32       # 实测 token_usage 服务端并发槽 ~16-32, 再高没收益
WINDOW_DAYS = 1        # token_usage 单次窗口 — 服务端处理时间近线性放大随窗口长度,
                       # 1 天/片 总耗时最小 (62 calls / 30 day = 12s; 10 天/片 同期要 52s)


# UCloud SDK 默认每次 invoke 都 with requests.Session() —— 每次新开 TCP+TLS 握手, 拖慢并发。
# 这里 monkey-patch 成共享 session + 大连接池, 复用 keep-alive 连接。
_SHARED_SESSION = requests.Session()
_SHARED_SESSION.mount("https://", HTTPAdapter(pool_connections=64, pool_maxsize=64))
_SHARED_SESSION.mount("http://", HTTPAdapter(pool_connections=64, pool_maxsize=64))


def _patched_send(self, req, **options):
    ssl_option = options.get("ssl_option")
    kwargs = self._build_ssl_option(ssl_option) if ssl_option else {}
    req.request_time = time.time()
    session_resp = _SHARED_SESSION.request(
        method=req.method.upper(), url=req.url,
        json=req.json, data=req.data, params=req.params,
        headers=req.headers, timeout=options.get("timeout"), **kwargs,
    )
    resp = self.convert_response(session_resp)
    resp.request = req
    resp.response_time = time.time()
    if resp.status_code >= 400:
        raise exc.HTTPStatusException(resp.status_code, resp.request_uuid)
    return resp


_ucloud_requests_transport.RequestsTransport._send = _patched_send


def _client(vendor: dict) -> Client:
    return Client({
        "public_key": vendor["public_key"],
        "private_key": vendor["secret_key"],
        "project_id": vendor.get("project_id", ""),
        "base_url": "https://api.ucloud.cn",
        "timeout": 90,
    })


def _query_billing(client: Client, project_id: str, start_ts: int, end_ts: int) -> list[dict]:
    try:
        r = client.invoke("GetUserBillingByKey", {
            "ProjectId": project_id, "StartTime": start_ts, "EndTime": end_ts,
        })
        return r.get("Items") or []
    except (exc.RetCodeException, Exception):
        return []


def _query_token_usage(client: Client, project_id: str, key_id: str, start_ts: int, end_ts: int) -> dict | None:
    """单窗口 GetUMInferTokenUsage, 返回 Data dict 或 None"""
    try:
        r = client.invoke("GetUMInferTokenUsage", {
            "ProjectId": project_id, "KeyId": key_id,
            "StartTime": start_ts, "EndTime": end_ts,
        })
        return r.get("Data") or None
    except Exception:
        return None


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    client = _client(vendor)
    project_id = vendor.get("project_id", "")

    start_dt = datetime.fromisoformat(start_time).astimezone(CST)
    end_dt = datetime.fromisoformat(end_time).astimezone(CST)

    # token usage 切 10 天分片
    cur = start_dt
    slices: list[tuple[int, int]] = []
    while cur <= end_dt:
        chunk_end = min(cur + timedelta(days=WINDOW_DAYS), end_dt)
        slices.append((int(cur.timestamp()), int(chunk_end.timestamp())))
        cur = chunk_end + timedelta(seconds=1)

    # daily 按天窗口
    cur = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_windows: list[tuple[str, int, int]] = []
    while cur <= end_day:
        s = int(cur.timestamp())
        e = int((cur + timedelta(days=1) - timedelta(seconds=1)).timestamp())
        day_windows.append((cur.strftime("%Y-%m-%d"), s, e))
        cur += timedelta(days=1)

    # ── Phase 1: daily billing fan-out → cost_by_key + daily_map + total_cost
    cost_by_key: dict[str, float] = {}
    daily_map: dict[str, dict] = {}
    total_cost = 0.0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_query_billing, client, project_id, s, e): day
                for (day, s, e) in day_windows}
        for fut in as_completed(futs):
            day = futs[fut]
            items = fut.result()
            day_cost = 0.0
            for it in items:
                try:
                    cost = float(it.get("SumOrderPrice") or 0)
                except (TypeError, ValueError):
                    cost = 0.0
                if cost <= 0:
                    continue
                key = it.get("ResourceId") or "-"
                cost_by_key[key] = cost_by_key.get(key, 0.0) + cost
                day_cost += cost
                total_cost += cost
            daily_map[day] = {"date": day, "cost": round(day_cost, 4)}

    # ── Phase 2: token usage fan-out (KeyId × 10 天分片)
    by_model: dict[str, dict] = {}
    total_calls = 0
    total_in = 0
    total_out = 0

    token_tasks = [(key, s, e) for key in cost_by_key for (s, e) in slices]
    if token_tasks:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = [ex.submit(_query_token_usage, client, project_id, k, s, e)
                    for (k, s, e) in token_tasks]
            for fut in as_completed(futs):
                data = fut.result()
                if not data:
                    continue
                total_calls += int(data.get("RequestTotal") or 0)
                total_in += int(data.get("InTotal") or 0)
                total_out += int(data.get("OutTotal") or 0)
                for u in (data.get("Usages") or []):
                    t = u.get("Type")
                    m = u.get("Model") or "unknown"
                    c = int(u.get("Count") or 0)
                    if t not in ("in", "out"):
                        continue
                    entry = by_model.setdefault(m, {"in": 0, "out": 0})
                    entry[t] += c

    # 拼成 vendor 格式 (按模型聚合)
    models: dict[str, dict] = {}
    total_completion_for_split = sum(e["out"] for e in by_model.values()) or 1
    for m, e in by_model.items():
        # 把总额按 completion_tokens 占比分到模型 (近似)
        cost = total_cost * e["out"] / total_completion_for_split if total_cost > 0 else 0
        # 调用次数同样按 out 占比分摊 (vendor 级总数对, per-model 是估算)
        calls = int(total_calls * e["out"] / total_completion_for_split) if total_calls > 0 else 0
        models[m] = {
            "prompt_tokens": e["in"],
            "completion_tokens": e["out"],
            "cache_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": e["in"] + e["out"],
            "total_count": calls,
            "total_cost": round(cost, 4),
        }

    daily = sorted(daily_map.values(), key=lambda x: x["date"])

    return {
        "total_cost": round(total_cost, 4),
        "models": models,
        "daily": daily,
        "vendor_id": vendor["id"],
        "vendor_name": vendor["name"],
        "currency": "CNY",
        "_total_count": total_calls,
    }


# ────── ListPaidOrders 拉一天明细, 解析 PricingSKU 拆 cache_read / cache_write ──────
def _classify_sku(sku: str) -> str:
    """PricingSKU 文字 → prompt/completion/cache_read/cache_write/other.
    实测一天 25 种 SKU 全覆盖 (probe11 验证):
      'caching_Write' / 'Cache Writes'        → cache_write
      'cached input' / 'caching_Read'         → cache_read
      'output' / 'text output' / 'response'   → completion
      'input' (text/image/video)              → prompt
    """
    low = sku.lower()
    has_cache = "cache" in low or "caching" in low
    if has_cache:
        if "write" in low:
            return "cache_write"
        if "read" in low or "hit" in low or "cached" in low:
            return "cache_read"
        return "other"
    if "output" in low or "response" in low or "completion" in low:
        return "completion"
    if "input" in low or "prompt" in low:
        return "prompt"
    return "other"


def _fetch_day_order_items(vendor: dict, day: "datetime.date") -> list[dict]:
    """单天 ListPaidOrders 翻页, 返回原始订单 items (供按 model / 按 key 两种聚合共用)."""
    import datetime as _dt
    client = _client(vendor)
    project_id = vendor.get("project_id", "")
    start_ts = int(_dt.datetime(day.year, day.month, day.day, 0, 0, 0).timestamp())
    end_ts = int(_dt.datetime(day.year, day.month, day.day, 23, 59, 59).timestamp())

    items: list[dict] = []
    page = 1
    while True:
        try:
            r = client.invoke("ListPaidOrders", {
                "ProjectId": project_id,
                "StartTime": start_ts, "EndTime": end_ts,
                "Page": page, "PageSize": 100,
            })
        except Exception:
            break
        page_items = r.get("Orders") or []
        if not page_items:
            break
        items.extend(page_items)
        if len(page_items) < 100:
            break
        page += 1
        if page > 200:  # 安全阀
            break
    return items


def fetch_orders_for_day(vendor: dict, day: "datetime.date") -> dict:
    """单天 ListPaidOrders 翻页, 按 (model, sku_类) 求 tokens + cost.
    返回 {model: {prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens, total_cost}}
    """
    models: dict[str, dict] = {}
    for it in _fetch_day_order_items(vendor, day):
        model = it.get("ModelName") or it.get("ModelID") or "unknown"
        sku = it.get("PricingSKU") or ""
        kind = _classify_sku(sku)
        try:
            qty = float(it.get("Quantity") or 0)
            cost = float(it.get("OrderTotalPrice") or 0)
        except (TypeError, ValueError):
            qty, cost = 0.0, 0.0

        m = models.setdefault(model, {
            "prompt_tokens": 0, "completion_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "total_cost": 0.0,
        })
        if kind == "prompt":
            m["prompt_tokens"] += int(qty)
        elif kind == "completion":
            m["completion_tokens"] += int(qty)
        elif kind == "cache_read":
            m["cache_read_tokens"] += int(qty)
        elif kind == "cache_write":
            m["cache_write_tokens"] += int(qty)
        m["total_cost"] += cost
    return models


def fetch_orders_for_day_by_key(vendor: dict, day: "datetime.date") -> dict:
    """单天 ListPaidOrders 按 API key 聚合 — vendor_apikey_usage_daily (按 key 筛选) 用.

    返回 {resource_id: {"name": 显示名, "models": {model: {...同 fetch_orders_for_day 的形状}}}}.
    resource_id 是上游机器 ID (uminferapikey-xxx, 供 GetUMInferTokenUsage 查调用次数);
    name 是用户起的 key 名 (ResourceName, e.g. '魏召'), 落库时优先用它做 api_key 展示值.
    """
    out: dict[str, dict] = {}
    for it in _fetch_day_order_items(vendor, day):
        rid = it.get("ResourceID") or "-"
        key_entry = out.setdefault(rid, {"name": it.get("ResourceName") or None, "models": {}})
        model = it.get("ModelName") or it.get("ModelID") or "unknown"
        sku = it.get("PricingSKU") or ""
        kind = _classify_sku(sku)
        try:
            qty = float(it.get("Quantity") or 0)
            cost = float(it.get("OrderTotalPrice") or 0)
        except (TypeError, ValueError):
            qty, cost = 0.0, 0.0

        m = key_entry["models"].setdefault(model, {
            "prompt_tokens": 0, "completion_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "total_cost": 0.0,
        })
        if kind == "prompt":
            m["prompt_tokens"] += int(qty)
        elif kind == "completion":
            m["completion_tokens"] += int(qty)
        elif kind == "cache_read":
            m["cache_read_tokens"] += int(qty)
        elif kind == "cache_write":
            m["cache_write_tokens"] += int(qty)
        m["total_cost"] += cost
    return out
