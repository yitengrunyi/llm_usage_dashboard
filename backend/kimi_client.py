"""
Kimi (Moonshot) 平台客户端。

认证: 浏览器扫码登录, storage_state 保存 cookies + localStorage
拉数据: Playwright 启 context 加载 state, 拦截到 platform.kimi.com 自动带的 Authorization 头,
       然后用 context.request 重放 /api?endpoint=consumes 拿日级费用。

金额单位: 10^-5 CNY (除以 100000 得元)。无 token 数。
"""
import time
import json
from pathlib import Path
from urllib.parse import urlparse

STATE_DIR = Path(__file__).parent / "config" / "sessions"
QUOTA_DIVISOR = 100_000  # amount → CNY

# 价格通过 LiteLLM 本地 DB 查询, 不限货币:
#   - 价格 currency=元/CNY → 用 amount(¥) 直接反推
#   - 价格 currency=USD     → amount(¥) → USD 再反推
# 找不到走 FALLBACK_PRICING (内置 ¥, 来源 kimi 官网)。
import requests as _requests
from litellm.settings import settings as _litellm_settings
from exchange_rate import convert as _fx_convert

FALLBACK_PRICING = {
    # currency 默认为 CNY (¥/M tokens)
    "kimi-k2.6":              {"input_per_1m": 6.5, "cache_read_per_1m": 1.1, "output_per_1m": 27.0, "currency": "CNY"},
    "kimi-k2.5":              {"input_per_1m": 4.0, "cache_read_per_1m": 0.7, "output_per_1m": 21.0, "currency": "CNY"},
    "kimi-k2-thinking":       {"input_per_1m": 4.0, "cache_read_per_1m": 1.0, "output_per_1m": 16.0, "currency": "CNY"},
    "kimi-k2-thinking-turbo": {"input_per_1m": 16.0, "cache_read_per_1m": 4.0, "output_per_1m": 64.0, "currency": "CNY"},
    "kimi-k2-turbo-preview":  {"input_per_1m": 16.0, "cache_read_per_1m": 4.0, "output_per_1m": 64.0, "currency": "CNY"},
    "kimi-k2-0905-preview":   {"input_per_1m": 4.0, "cache_read_per_1m": 1.0, "output_per_1m": 16.0, "currency": "CNY"},
    "kimi-k2-0711-preview":   {"input_per_1m": 4.0, "cache_read_per_1m": 1.0, "output_per_1m": 16.0, "currency": "CNY"},
}

def _norm_currency(c: str | None) -> str:
    """统一货币代码: '元'/'cny' → 'CNY'; 'usd'/'$' → 'USD'。"""
    c = (c or "").strip()
    if c in ("元", "CNY", "cny", "￥", "¥"): return "CNY"
    if c.upper() in ("USD", "$"): return "USD"
    return c.upper() if c else "CNY"


def _fetch_litellm_prices() -> dict[str, dict]:
    """每次请求都拉 LiteLLM 价目, 不缓存 — 改价立即生效, 本地 HTTP 调用毫秒级。"""
    fresh: dict[str, dict] = {}
    try:
        r = _requests.get(
            "http://127.0.0.1:8000/api/litellm/model-prices",
            headers={"x-api-key": _litellm_settings.api_key},
            timeout=5,
        )
        if r.status_code == 200:
            for item in r.json():
                cfg = item.get("pricing_config") or {}
                if not cfg.get("input_per_1m"):
                    continue
                model = (item.get("model_name") or "").lower()
                provider = (item.get("provider") or "").lower()
                if provider and provider != "moonshot":
                    continue
                # provider=moonshot 优先于 provider=None
                existing = fresh.get(model)
                if existing and not provider and existing.get("_provider") == "moonshot":
                    continue
                fresh[model] = {**cfg, "_provider": provider or None}
    except Exception:
        pass
    return fresh


def _get_price(model: str, prices: dict[str, dict] | None = None) -> dict | None:
    """LiteLLM 本地价 (provider=moonshot 优先) → FALLBACK_PRICING。"""
    if prices is None:
        prices = _fetch_litellm_prices()
    cfg = prices.get(model.lower())
    if cfg:
        return cfg
    return FALLBACK_PRICING.get(model)


def _state_file(base_url: str) -> Path:
    domain = urlparse(base_url).hostname.replace(".", "_")
    return STATE_DIR / f"{domain}.json"


def _date_to_str(iso_str: str) -> str:
    """ISO → YYYY-MM-DD (兼容 daily key)。"""
    if "T" in iso_str:
        return iso_str.split("T")[0]
    return iso_str[:10]


def _iso_to_ms(iso_str: str) -> int:
    from datetime import datetime
    dt = datetime.fromisoformat(iso_str)
    return int(dt.timestamp() * 1000)


def _read_tokens(state_path: Path) -> tuple[str | None, str | None, str | None]:
    """从 Playwright storage_state JSON 直接读 localStorage 里的 token + rtoken + 缓存的 oid。"""
    if not state_path.exists():
        return None, None, None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None, None
    token = rtoken = None
    oid = data.get("_oid")  # 我们写到顶层, 不污染 Playwright 自己的字段
    for org in data.get("origins") or []:
        if "platform.kimi.com" not in (org.get("origin") or ""):
            continue
        for item in org.get("localStorage") or []:
            if item.get("name") == "token":
                token = item.get("value")
            elif item.get("name") == "rtoken":
                rtoken = item.get("value")
    return token, rtoken, oid


def _write_state(state_path: Path, token: str | None = None, rtoken: str | None = None,
                 oid: str | None = None) -> None:
    """写回 token/rtoken/oid 任一字段 (None 跳过)。"""
    if not state_path.exists():
        return
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if oid is not None:
        data["_oid"] = oid
    if token is not None or rtoken is not None:
        for org in data.get("origins") or []:
            if "platform.kimi.com" not in (org.get("origin") or ""):
                continue
            for item in (org.get("localStorage") or []):
                if token is not None and item.get("name") == "token":
                    item["value"] = token
                elif rtoken is not None and item.get("name") == "rtoken":
                    item["value"] = rtoken
    state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _refresh_token(base_url: str, rtoken: str) -> tuple[str | None, str | None]:
    """用 rtoken 续 token (Msh-Authorization 头)。返回 (new_token, new_rtoken) 或 (None, None)。"""
    try:
        r = _requests.get(
            f"{base_url}/api?endpoint=refreshToken",
            headers={"Msh-Authorization": rtoken},
            timeout=10,
        )
        if r.status_code != 200:
            return None, None
        d = r.json()
        if d.get("code") != 0:
            return None, None
        return d["data"].get("access_token"), d["data"].get("refresh_token")
    except Exception:
        return None, None


def _api_get(base_url: str, path_query: str, token: str) -> tuple[int, dict | None]:
    try:
        r = _requests.get(
            f"{base_url}/api?{path_query}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    except Exception as e:
        return -1, {"_err": str(e)}


def _fetch_raw(base_url: str, state_path: Path, start_ms: int, end_ms: int) -> list:
    """纯 HTTP 拉 kimi consumes; token 过期就用 rtoken 续。
    oid 缓存在 state 文件 (_oid 字段), 省一次 userInfo RT (~600ms)。"""
    token, rtoken, oid = _read_tokens(state_path)
    if not token:
        raise PermissionError(f"未登录 kimi, 请先点击登录: {state_path}")

    def _call(path_q: str) -> dict:
        nonlocal token, rtoken
        status, body = _api_get(base_url, path_q, token)
        if status == 401 and rtoken:
            new_t, new_rt = _refresh_token(base_url, rtoken)
            if new_t:
                token = new_t
                rtoken = new_rt or rtoken
                _write_state(state_path, token=token, rtoken=rtoken)
                status, body = _api_get(base_url, path_q, token)
        if status != 200 or not body:
            raise PermissionError(f"kimi API 失败 status={status}, 请重新登录")
        if body.get("code") != 0:
            raise RuntimeError(f"kimi API code={body.get('code')}: {body.get('message')}")
        return body

    if not oid:
        ui = _call("endpoint=userInfo")
        orgs = (ui.get("data") or {}).get("organizations") or []
        if not orgs:
            raise RuntimeError("kimi 账号没有组织")
        oid = orgs[0]["organization"]["id"]
        _write_state(state_path, oid=oid)

    try:
        data = _call(f"endpoint=consumes&start={start_ms}&end={end_ms}&date_type=daily&oid={oid}")
    except PermissionError:
        # oid 失效罕见 (换组织才会), 兜底重拉一次 userInfo
        _write_state(state_path, oid="")
        ui = _call("endpoint=userInfo")
        orgs = (ui.get("data") or {}).get("organizations") or []
        if not orgs:
            raise RuntimeError("kimi 账号没有组织")
        oid = orgs[0]["organization"]["id"]
        _write_state(state_path, oid=oid)
        data = _call(f"endpoint=consumes&start={start_ms}&end={end_ms}&date_type=daily&oid={oid}")
    return data.get("data") or []


def aggregate(rows: list) -> dict:
    """按模型聚合: 用 product_name 区分 prompt / auto-caching / completion / $web_search,
    根据 PRICING 反推 token 数 (假设 A: prompt 项不含缓存)。"""
    models = {}
    daily_map = {}
    # 一次性拉 LiteLLM 价目, 循环里复用
    litellm_prices = _fetch_litellm_prices()

    for r in rows:
        model = r.get("product_model_id") or "unknown"
        product_name = r.get("product_name") or ""
        date_str = _date_to_str(r.get("date") or "")
        amount = float(r.get("amount") or 0)
        cost_cny = amount / QUOTA_DIVISOR

        if model not in models:
            models[model] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,  # kimi 无缓存写概念
                "total_tokens": 0,
                "total_count": 0,
                "total_cost": 0.0,
            }
        m = models[model]
        m["total_cost"] += cost_cny

        # 按 product_name 反推 token
        price = _get_price(model, litellm_prices)
        if price:
            cur = _norm_currency(price.get("currency"))
            # amount 是 CNY, 价格若是 USD 先把成本换成 USD 再反推
            cost_in_price_cur = cost_cny if cur == "CNY" else _fx_convert(cost_cny, "CNY", cur)
            input_p = price.get("input_per_1m") or 0
            cache_p = price.get("cache_read_per_1m") or 0
            output_p = price.get("output_per_1m") or 0
            if product_name == "prompt" and input_p:
                m["prompt_tokens"] += round(cost_in_price_cur / input_p * 1_000_000)
            elif product_name == "auto-caching" and cache_p:
                hits = round(cost_in_price_cur / cache_p * 1_000_000)
                m["cache_tokens"] += hits
                m["cache_read_tokens"] += hits
            elif product_name == "completion" and output_p:
                m["completion_tokens"] += round(cost_in_price_cur / output_p * 1_000_000)
            # $web_search 等其它项: 只算费用, 不反推 token

        if date_str not in daily_map:
            daily_map[date_str] = {"date": date_str, "cost": 0.0}
        daily_map[date_str]["cost"] += cost_cny

    # total_tokens = 非缓存 input + cache_read + completion (OpenAI 口径下 prompt_tokens 含 cache,
    # 这里我们 prompt_tokens 字段保留为"非缓存", 详情页 total 用三者之和)
    for m in models.values():
        m["total_tokens"] = m["prompt_tokens"] + m["cache_read_tokens"] + m["completion_tokens"]
        m["total_cost"] = round(m["total_cost"], 6)
    for d in daily_map.values():
        d["cost"] = round(d["cost"], 6)

    total_cost = round(sum(m["total_cost"] for m in models.values()), 6)
    daily = sorted(daily_map.values(), key=lambda x: x["date"])

    return {"total_cost": total_cost, "models": models, "daily": daily}


def aggregate_by_apikey(rows: list) -> list[dict]:
    """按 (api_key_name, model) 聚合 consumes 行, 复用 aggregate 的 token 反推逻辑.

    返回 [{api_key, model, prompt_tokens, completion_tokens, cache_read_tokens,
           cache_write_tokens, total_tokens, total_cost}, ...]
    供 KimiAdapter.fetch_apikey_rows 写 vendor_apikey_usage_daily.
    与 aggregate() 同一份 rows, 同一份定价, 只是聚合维度多了 api_key.
    """
    groups: dict[tuple[str, str], dict] = {}
    litellm_prices = _fetch_litellm_prices()

    for r in rows:
        model = r.get("product_model_id") or "unknown"
        api_key = r.get("api_key_name") or ""
        if not api_key:
            continue  # 没 api_key_name 的行不进 key 表
        product_name = r.get("product_name") or ""
        amount = float(r.get("amount") or 0)
        cost_cny = amount / QUOTA_DIVISOR

        gkey = (api_key, model)
        if gkey not in groups:
            groups[gkey] = {
                "api_key": api_key, "model": model,
                "prompt_tokens": 0, "completion_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "total_cost": 0.0,
            }
        g = groups[gkey]
        g["total_cost"] += cost_cny

        price = _get_price(model, litellm_prices)
        if price:
            cur = _norm_currency(price.get("currency"))
            cost_in_price_cur = cost_cny if cur == "CNY" else _fx_convert(cost_cny, "CNY", cur)
            input_p = price.get("input_per_1m") or 0
            cache_p = price.get("cache_read_per_1m") or 0
            output_p = price.get("output_per_1m") or 0
            if product_name == "prompt" and input_p:
                g["prompt_tokens"] += round(cost_in_price_cur / input_p * 1_000_000)
            elif product_name == "auto-caching" and cache_p:
                g["cache_read_tokens"] += round(cost_in_price_cur / cache_p * 1_000_000)
            elif product_name == "completion" and output_p:
                g["completion_tokens"] += round(cost_in_price_cur / output_p * 1_000_000)

    out = []
    for g in groups.values():
        g["total_tokens"] = g["prompt_tokens"] + g["cache_read_tokens"] + g["completion_tokens"]
        g["total_cost"] = round(g["total_cost"], 6)
        out.append(g)
    return out


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    state_path = _state_file(vendor["base_url"])
    start_ms = _iso_to_ms(start_time)
    end_ms = _iso_to_ms(end_time)
    rows = _fetch_raw(vendor["base_url"], state_path, start_ms, end_ms)
    result = aggregate(rows)
    result["vendor_id"] = vendor["id"]
    result["vendor_name"] = vendor["name"]
    result["currency"] = vendor.get("currency", "CNY")
    return result
